"""agyswap auto: thresholds, hysteresis, cooldown, failover, strategies, the no-return bar, --once exit codes and the
persisted state. Credential Manager is a FakeStore, agy is a fake fetcher, and every switch goes through the real switch.switch_to."""
import contextlib
import io
import json
import time
from unittest import mock

from agyswap import accounts, autoswitch, cli, paths
from agyswap.autoswitch import AutoSettings, Engine, Outcome
from agyswap.errors import AgyswapError
from agyswap.output import iso
from agyswap.usage import FetchResult, parse_usage
from agyswap.usage_store import UsageCache, backoff_seconds
from tests.helpers import SECRET, FakeStore, TempHome, token_blob

# Near real time: the CLI tests run an engine on the real clock against the same cached figures
T0 = float(int(time.time()))
DAY = 86400.0
EVENT_KEYS = {"schemaVersion", "event", "ts"}


def text(gemini: float, claude: float = 0.0, reset: float = 5 * DAY, claude_reset: float = 6 * DAY, five: float | None = None) -> str:
    """agy /usage output with these used percentages; resets are seconds after T0."""
    out = (f"Gemini Models\tWeekly Limit Remaining\t{100 - gemini}%\t{iso(T0 + reset)}\n"
           f"Claude and GPT models\tWeekly Limit Remaining\t{100 - claude}%\t{iso(T0 + claude_reset)}\n")
    if five is not None:
        out += f"Gemini Models\tFive Hour Limit Remaining\t{100 - five}%\t{iso(T0 + 3600)}\n"
    return out


class FakeAgy:
    """Answers by account folder; the live environment (no SSH variables) answers as live_folder."""

    def __init__(self, results=None, live_folder="default"):
        self.results = results or {}
        self.live_folder = live_folder
        self.calls: list[str] = []

    def __call__(self, env):
        folder = self.live_folder if "SSH_CONNECTION" not in env else env["USERPROFILE"].replace("\\", "/").rsplit("/", 1)[-1]
        self.calls.append(folder)
        result = self.results.get(folder, FetchResult(error="parse"))
        return FetchResult(usage=parse_usage(result)) if isinstance(result, str) else result


class Base(TempHome):
    FOLDERS = (("1", "me@gmail.com", "default"), ("2", "me@school.edu", "school"), ("3", "me@work.org", "work"))

    def setUp(self):
        super().setUp()
        self.now = T0
        self.data = {"activeAccountNumber": 1, "accounts": {}}
        for num, email, folder in self.FOLDERS:
            self.write_token(folder, email, secret=f"{SECRET}-{folder}")
            self.data["accounts"][num] = {"email": email, "alias": folder, "folder": folder}
        accounts.save_sequence(self.data)
        self.store = FakeStore(paths.token_path("default").read_bytes())
        self.procs = mock.patch("agyswap.procs.running", return_value={})
        self.procs.start()
        self.addCleanup(self.procs.stop)
        self.enterContext(contextlib.redirect_stderr(io.StringIO()))
        self.events: list[dict] = []
        self.agy = FakeAgy()

    def clock(self):
        return self.now

    def seed(self, folder: str, usage_text: str, interval: float = 3600.0):
        UsageCache(clock=self.clock).record_success(folder, parse_usage(usage_text), interval)

    def seed_all(self, a, b, c=None, **kw):
        self.seed("default", a)
        self.seed("school", b)
        if c is not None:
            self.seed("work", c)
        else:
            self.set_account("3", disabled=True)

    def set_account(self, num, **fields):
        data = accounts.load_sequence()
        data["accounts"][num].update(fields)
        accounts.save_sequence(data)

    def engine(self, dry_run=False, **cfg) -> Engine:
        self.events = []
        return Engine(AutoSettings(**cfg), self.events.append, dry_run=dry_run, store=self.store, fetcher=self.agy,
                      clock=self.clock)

    def tick(self, dry_run=False, **cfg) -> Outcome:
        return self.engine(dry_run=dry_run, **cfg).tick()

    def kinds(self):
        return [e["event"] for e in self.events]

    def last(self, kind):
        return [e for e in self.events if e["event"] == kind][-1]

    def active(self):
        return accounts.load_sequence()["activeAccountNumber"]

    def state(self):
        path = paths.autoswitch_state_file()
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None


class ThresholdTest(Base):
    def test_below_threshold_does_nothing(self):
        self.seed_all(text(50), text(10), text(10))
        self.assertEqual(self.tick(), Outcome.NO_ACTION)
        self.assertEqual(self.last("no-switch")["reason"], "below-threshold")
        self.assertEqual(self.last("no-switch")["detail"], "7d 50% < 90%")
        self.assertEqual(self.store.writes, [])
        self.assertEqual(self.agy.calls, [])  # everything was cached and nothing is near a line

    def test_proactive_switch_goes_through_switch_and_records_state(self):
        self.seed_all(text(92), text(30), text(10))
        self.assertEqual(self.tick(), Outcome.SWITCHED)
        ev = self.last("switch")
        self.assertEqual((ev["trigger"], ev["from"]["number"], ev["to"]["number"], ev["dryRun"]), ("proactive", 1, 3, False))
        self.assertEqual(self.store.cred.blob, paths.token_path("work").read_bytes())
        self.assertEqual(self.active(), 3)
        st = self.state()
        self.assertEqual((st["lastSwitchFrom"], st["lastSwitchTo"], st["leftTrigger"], st["lastSwitchAt"]), (1, "3", "proactive", T0))
        self.assertEqual(st["leftHeadroom"], 8.0)
        self.assertNotIn(SECRET, json.dumps(self.events) + json.dumps(st))

    def test_weekly_threshold(self):
        self.seed_all(text(92), text(10))
        self.assertEqual(self.tick(weekly_threshold=97.0), Outcome.NO_ACTION)
        self.assertEqual(self.last("no-switch")["detail"], "7d 92% < 97%")
        self.assertEqual(self.last("poll")["weeklyThreshold"], 97.0)
        self.seed("default", text(97))
        self.assertEqual(self.tick(weekly_threshold=97.0), Outcome.SWITCHED)
        # the 5h window keeps the plain threshold
        self.now += 400  # past the cooldown
        self.seed("school", text(20, five=91))
        self.seed("default", text(10))
        self.assertEqual(self.tick(weekly_threshold=97.0), Outcome.SWITCHED)
        self.assertEqual(self.last("switch")["to"]["number"], 1)

    def test_per_account_threshold_governs_leaving_and_landing(self):
        self.set_account("1", threshold=80.0)
        self.set_account("2", threshold=60.0)
        self.seed("default", text(85))
        self.seed("school", text(70))  # most raw room, but over its own 60 line (cswap #255, #116)
        self.seed("work", text(75))
        self.assertEqual(self.tick(), Outcome.SWITCHED)
        self.assertEqual(self.last("poll")["threshold"], 80.0)
        self.assertEqual(self.last("switch")["to"]["number"], 3)

    def test_relative_hysteresis(self):
        self.seed_all(text(99), text(89))  # cswap #115: 11 points of room against 1 clears a 10-point margin
        self.assertEqual(self.tick(), Outcome.SWITCHED)
        self.store = FakeStore(paths.token_path("default").read_bytes())
        self.set_account("1")
        data = accounts.load_sequence()
        data["activeAccountNumber"] = 1
        accounts.save_sequence(data)
        paths.autoswitch_state_file().unlink()
        self.seed_all(text(99), text(89.5))
        self.assertEqual(self.tick(), Outcome.BLOCKED)
        self.assertEqual(self.last("no-switch")["reason"], "no-qualifying-candidate")


class CooldownFailoverTest(Base):
    def test_cooldown_and_at_limit_bypass(self):
        self.seed_all(text(92), text(30), text(10))
        self.assertEqual(self.tick(), Outcome.SWITCHED)
        self.now += 10
        self.seed("work", text(95))
        self.seed("default", text(10))
        self.assertEqual(self.tick(), Outcome.NO_ACTION)  # a new engine (a second --once) reads the persisted lastSwitchAt
        self.assertEqual(self.last("no-switch")["reason"], "cooldown")
        self.seed("work", text(100))
        self.assertEqual(self.tick(), Outcome.SWITCHED)
        self.assertEqual(self.last("switch")["trigger"], "at-limit")

    def test_failover_needs_three_separate_once_runs(self):
        self.seed_all(text(50), text(10))
        self.agy.results = {"default": FetchResult(error="timeout"), "school": text(10)}
        self.now += 3601  # the active account's plan is due
        reasons = []
        for n in range(1, 4):
            outcome = self.tick()
            reasons.append(self.events[-1].get("detail") or self.events[-1].get("trigger"))
            self.now += backoff_seconds(n) + 1
        self.assertEqual(reasons, ["1/3 before failover", "2/3 before failover", "failover"])
        self.assertEqual(outcome, Outcome.SWITCHED)
        self.assertEqual(self.active(), 2)
        self.assertNotIn("unhealthyTicks", self.state())  # the new active account starts over

    def test_failed_query_is_unknown_even_with_recent_cache(self):
        self.seed("default", text(70), interval=300)
        self.seed("school", text(10))
        self.set_account("3", disabled=True)
        self.agy.results = {"default": FetchResult(error="timeout")}
        self.now += 301
        self.assertEqual(self.tick(), Outcome.NO_ACTION)  # cswap #208: the 70% from five minutes ago is not today's value
        self.assertEqual(self.last("no-switch")["reason"], "active-usage-unknown")
        self.assertEqual(self.last("poll")["fetchErrors"], {"1": "timeout"})
        self.assertEqual(self.state()["unhealthyTicks"], 1)

    def test_backoff_ticks_do_not_count_again(self):
        self.seed_all(text(50), text(10))
        self.agy.results = {"default": FetchResult(error="timeout")}
        self.now += 3601
        self.tick()
        calls = list(self.agy.calls)
        self.now += 60  # still in the query backoff: no new attempt, no new count
        self.tick()
        self.assertEqual(self.last("no-switch")["detail"], "1/3 before failover")
        self.assertEqual(self.agy.calls, calls)

    def test_dry_run_counters_are_not_persisted(self):
        self.seed_all(text(50), text(10))
        self.agy.results = {"default": FetchResult(error="timeout")}
        self.now += 3601
        for n in range(1, 5):
            self.tick(dry_run=True)
            self.now += backoff_seconds(n) + 1
        self.assertEqual(self.last("no-switch")["detail"], "1/3 before failover")
        self.assertIsNone(self.state())

    def save_ticks(self, ticks, account="1", **more):
        paths.autoswitch_state_file().parent.mkdir(parents=True, exist_ok=True)
        paths.autoswitch_state_file().write_text(json.dumps(
            {"unhealthyTicks": ticks, "healthAccount": account, "healthTickAt": self.now, **more}), encoding="utf-8")

    def broken_switch_engine(self):
        self.events = []

        def switcher(target, **kw):
            raise AgyswapError("the .lock is busy")
        return Engine(AutoSettings(), self.events.append, store=self.store, fetcher=self.agy, clock=self.clock, switcher=switcher)

    def test_counters_are_saved_even_when_the_switch_fails(self):
        self.seed_all(text(50), text(10))
        self.agy.results = {"default": FetchResult(error="timeout"), "school": text(10)}
        self.now += 3601
        self.save_ticks(2)
        self.assertEqual(self.broken_switch_engine().tick(), Outcome.ERROR)  # failover was due, the switch failed
        self.assertEqual(self.state()["unhealthyTicks"], 3)
        self.save_ticks(2)
        self.agy.results["default"] = text(95)  # readable again and over the line: the count starts over
        self.now += 3601
        self.assertEqual(self.broken_switch_engine().tick(), Outcome.ERROR)
        self.assertNotIn("unhealthyTicks", self.state())

    def test_switch_clears_the_counters_in_its_own_write(self):
        self.seed_all(text(95), text(10))
        self.save_ticks(1)
        engine = self.engine()
        with mock.patch.object(engine, "_mutate_state", side_effect=AgyswapError("state lock busy")):
            self.assertEqual(engine.tick(), Outcome.SWITCHED)  # no second write is needed, so none can fail it
        self.assertNotIn("unhealthyTicks", self.state())
        self.assertEqual(self.state()["lastSwitchTo"], "2")

    def test_failed_counter_write_does_not_mask_the_outcome(self):
        self.seed_all(text(50), text(10))
        self.agy.results = {"default": FetchResult(error="timeout")}
        self.now += 3601
        engine = self.engine()
        with mock.patch.object(engine, "_mutate_state", side_effect=AgyswapError("state lock busy")):
            self.assertEqual(engine.tick(), Outcome.NO_ACTION)
        self.assertEqual(self.kinds()[-2:], ["no-switch", "error"])

    def test_tick_without_a_managed_active_account_keeps_the_counters(self):
        self.seed_all(text(50), text(10))
        self.save_ticks(2)
        self.store = FakeStore(token_blob("stranger@x.com"))
        self.assertEqual(self.tick(), Outcome.NO_ACTION)
        self.assertEqual(self.state()["unhealthyTicks"], 2)

    def test_counters_merge_with_another_process(self):
        self.seed_all(text(50), text(10))
        self.agy.results = {"default": FetchResult(error="timeout"), "school": text(10)}
        self.now += 3601
        loop = self.engine()
        loop.tick()
        self.assertEqual(self.state()["unhealthyTicks"], 1)
        self.save_ticks(2)  # a scheduled --once counted another failure meanwhile
        self.now += backoff_seconds(2) + 1
        self.assertEqual(loop.tick(), Outcome.SWITCHED)  # 2 + this one = 3, not the loop's own 1 + 1
        self.assertEqual(self.last("switch")["trigger"], "failover")

    def test_stale_counter_never_overwrites_a_newer_reset(self):
        self.seed_all(text(50), text(10))
        self.agy.results = {"default": FetchResult(error="timeout")}
        self.now += 3601
        loop = self.engine()
        loop.tick()
        self.now += backoff_seconds(1) + 1
        loop.tick()
        self.assertEqual(self.state()["unhealthyTicks"], 2)
        paths.autoswitch_state_file().write_text("{}", encoding="utf-8")  # another process saw it healthy
        self.now += 60  # in backoff: nothing new observed
        loop.tick()
        self.assertNotIn("unhealthyTicks", self.state())

    def test_another_process_query_is_not_an_unhealthy_tick(self):
        self.seed("school", text(10))
        self.set_account("3", disabled=True)
        UsageCache(clock=self.clock).claim("default")  # never measured, and another agyswap is querying it now
        self.assertEqual(self.tick(), Outcome.NO_ACTION)
        self.assertEqual(self.last("no-switch")["detail"], "0/3 before failover")
        self.assertEqual(self.agy.calls, [])
        self.assertIsNone(self.state())

    def test_no_escalation_while_failover_waits_on_a_backoff(self):
        self.seed_all(text(50), text(10), text(10))
        self.now += 500
        UsageCache(clock=self.clock).record_failure("default", "timeout")
        self.save_ticks(2)
        self.now += 100  # the active account is in its backoff, so this tick cannot reach failover
        self.assertEqual(self.tick(), Outcome.NO_ACTION)
        self.assertEqual(self.agy.calls, [])  # no agy start for every candidate

    def test_damaged_or_huge_persisted_counters(self):
        self.seed_all(text(50), text(10))
        for ticks in (10 ** 30, True, -4, "3"):
            paths.autoswitch_state_file().parent.mkdir(parents=True, exist_ok=True)
            paths.autoswitch_state_file().write_text(json.dumps(
                {"unhealthyTicks": ticks, "healthAccount": "1", "healthTickAt": T0}), encoding="utf-8")
            engine = self.engine()
            engine._load_health(engine._read_state(), "1")
            self.assertEqual(engine._unhealthy, 3 if ticks == 10 ** 30 else 0, ticks)
        self.assertEqual(backoff_seconds(10 ** 6), 1800)
        engine._sleep_until = T0 + 10 ** 12
        self.assertEqual(engine.next_delay(Outcome.BLOCKED), autoswitch.MAX_SLEEP_S)


class ExhaustionTest(Base):
    def test_all_full(self):
        self.seed_all(text(100, reset=2 * DAY), text(100, reset=DAY), text(100, reset=3 * DAY))
        self.assertEqual(self.tick(), Outcome.BLOCKED)
        self.assertEqual(self.last("all-exhausted")["earliestResetAt"], iso(T0 + DAY))
        self.assertEqual(self.store.writes, [])

    def test_all_over_line_moves_only_by_the_ratio_margin_onto_most_room(self):
        self.seed_all(text(98), text(93, reset=DAY), text(95, reset=3600))  # cswap #342, #305: most room wins, not the soonest reset
        self.assertEqual(self.tick(strategy="consume-first"), Outcome.SWITCHED)
        self.assertEqual(self.last("switch")["to"]["number"], 2)

    def test_escape_lands_below_the_lines_when_possible(self):
        self.seed_all(text(100), text(92, reset=3600), text(60, reset=4 * DAY))
        self.assertEqual(self.tick(strategy="weekly-first"), Outcome.SWITCHED)
        self.assertEqual(self.last("switch")["to"]["number"], 3)

    def test_model_fallback_to_gemini(self):
        self.seed_all(text(100, claude=100), text(20, claude=100))  # cswap #403
        self.assertEqual(self.tick(model="claude"), Outcome.SWITCHED)
        self.assertEqual((self.last("switch")["to"]["number"], self.last("switch")["trigger"]), (2, "at-limit"))
        self.assertIn("Claude and GPT models", self.last("poll")["windowsPct"]["1"])

    def test_model_fallback_to_gemini_under_priority(self):
        self.seed_all(text(100, claude=100), text(20, claude=100))  # cswap #403 must reach the priority ranking too
        self.assertEqual(self.tick(model="claude", strategy="priority-consume-first"), Outcome.SWITCHED)
        self.assertEqual((self.last("switch")["to"]["number"], self.last("switch")["trigger"]), (2, "at-limit"))


class StrategyTest(Base):
    def test_best_and_next_available(self):
        self.seed_all(text(92), text(40), text(10))
        self.assertEqual(self.tick(dry_run=True), Outcome.SWITCHED)
        self.assertEqual(self.last("switch")["to"]["number"], 3)
        self.assertEqual(self.tick(dry_run=True, strategy="next-available"), Outcome.SWITCHED)
        self.assertEqual(self.last("switch")["to"]["number"], 2)

    def test_weekly_first(self):
        self.seed_all(text(92), text(10, reset=4 * DAY), text(40, reset=DAY))
        self.assertEqual(self.tick(dry_run=True, strategy="weekly-first"), Outcome.SWITCHED)
        self.assertEqual(self.last("switch")["to"]["number"], 3)
        self.seed("default", text(50))
        self.assertEqual(self.tick(dry_run=True, strategy="weekly-first"), Outcome.NO_ACTION)

    def test_consume_first(self):
        self.seed_all(text(50, reset=3 * DAY), text(30, reset=4 * DAY), text(60, reset=DAY))
        self.assertEqual(self.tick(dry_run=True, strategy="consume-first"), Outcome.SWITCHED)
        self.assertEqual((self.last("switch")["to"]["number"], self.last("switch")["trigger"]), (3, "consume-first"))
        self.seed("work", text(95, reset=DAY))  # the sooner one has no room under its line
        self.assertEqual(self.tick(dry_run=True, strategy="consume-first"), Outcome.NO_ACTION)
        self.assertEqual(self.last("no-switch")["reason"], "already-consuming-soonest")

    def consume_first_after_refresh(self, default, work="", claim_default=False):
        """consume-first picks work (sooner reset) on cached figures; the refresh before acting returns these answers."""
        self.now -= 500
        self.seed("default", text(50, reset=3 * DAY))
        self.seed("school", text(30, reset=4 * DAY))
        self.seed("work", text(60, reset=DAY))
        self.now += 500
        if claim_default:
            UsageCache(clock=self.clock).claim("default")
        self.agy.results = {"default": default, "school": text(30, reset=4 * DAY), "work": work or text(60, reset=DAY)}
        return self.tick(strategy="consume-first")

    def test_refresh_reclassifies_the_trigger(self):
        # the refreshed active account is exhausted: an escape, not an idle consume-first hold
        self.assertEqual(self.consume_first_after_refresh(text(100, reset=3 * DAY), text(100, reset=DAY)), Outcome.SWITCHED)
        self.assertEqual((self.last("switch")["to"]["number"], self.last("switch")["trigger"]), (2, "at-limit"))

    def test_refresh_failure_counts_as_unhealthy(self):
        self.assertEqual(self.consume_first_after_refresh(FetchResult(error="timeout")), Outcome.NO_ACTION)
        self.assertEqual(self.last("no-switch")["detail"], "1/3 before failover")
        self.assertEqual(self.state()["unhealthyTicks"], 1)

    def test_refresh_quarantines_a_relogin_candidate(self):
        self.consume_first_after_refresh(text(50, reset=3 * DAY), FetchResult(error="auth", relogin=True))
        self.assertEqual(self.last("account-quarantined")["number"], "3")
        self.assertNotIn("switch", self.kinds())

    def test_stale_active_reading_holds_consume_first(self):
        self.assertEqual(self.consume_first_after_refresh(text(50, reset=3 * DAY), claim_default=True), Outcome.NO_ACTION)
        self.assertEqual(self.last("no-switch")["reason"], "stale-usage")
        self.assertIn("account 1", self.last("no-switch")["detail"])

    def test_priority_tiers(self):
        self.set_account("2", priority=5)
        self.set_account("3", priority=5)
        self.seed_all(text(10, reset=DAY), text(50, reset=4 * DAY), text(60, reset=2 * DAY))
        self.assertEqual(self.tick(dry_run=True, strategy="priority-consume-first"), Outcome.SWITCHED)
        self.assertEqual((self.last("switch")["to"]["number"], self.last("switch")["trigger"]), (3, "priority"))
        self.seed("work", text(95, reset=2 * DAY))  # over its line: a higher tier never lands there
        self.assertEqual(self.tick(dry_run=True, strategy="priority-consume-first"), Outcome.SWITCHED)
        self.assertEqual(self.last("switch")["to"]["number"], 2)
        self.seed("school", text(95, reset=4 * DAY))
        self.assertEqual(self.tick(dry_run=True, strategy="priority-consume-first"), Outcome.NO_ACTION)
        self.assertEqual(self.last("no-switch")["reason"], "already-consuming-priority")

    def test_manual_switch_is_not_undone(self):
        self.set_account("3", priority=9)
        self.seed_all(text(10), text(10), text(10))
        paths.autoswitch_state_file().parent.mkdir(parents=True, exist_ok=True)
        paths.autoswitch_state_file().write_text(json.dumps({"lastSwitchTo": "3", "lastSwitchFrom": 1, "lastSwitchAt": T0 - DAY}),
                                                 encoding="utf-8")
        self.assertEqual(self.tick(strategy="priority-consume-first"), Outcome.NO_ACTION)  # cswap #598: the user moved back to 1 by hand
        self.assertEqual(self.last("no-switch")["reason"], "manual-switch")
        self.seed("default", text(95))
        self.assertEqual(self.tick(strategy="priority-consume-first"), Outcome.SWITCHED)
        self.assertEqual(self.last("switch")["to"]["number"], 3)

    def test_no_switching_back_and_forth(self):
        self.seed_all(text(92), text(50))
        self.assertEqual(self.tick(), Outcome.SWITCHED)
        self.now += 400
        self.seed("school", text(95))
        self.seed("default", text(92))  # the account we left has not recovered: only the new active burned
        self.assertEqual(self.tick(), Outcome.BLOCKED)
        self.assertEqual(self.active(), 2)
        self.seed("default", text(80))  # it recovered: going back is a move on its own merits
        self.assertEqual(self.tick(), Outcome.SWITCHED)
        self.assertEqual(self.active(), 1)


class OnceAndSafetyTest(Base):
    def run_cli(self, argv):
        out = io.StringIO()
        with mock.patch("agyswap.autoswitch.WinCred", return_value=self.store), mock.patch("agyswap.usage.fetch", self.agy), \
                contextlib.redirect_stdout(out):
            code = cli.dispatch(argv)
        return code, [json.loads(line) for line in out.getvalue().splitlines()]

    def test_once_exit_codes_and_json_lines(self):
        roster = paths.sequence_file().read_bytes()
        tokens = {f: paths.token_path(f).read_bytes() for f in ("default", "school", "work")}
        for usage_texts, code in (((text(50), text(10), text(10)), 2), ((text(95), text(10), text(10)), 0),
                                  ((text(100), text(100), text(100)), 3)):
            for folder, t in zip(("default", "school", "work"), usage_texts):
                self.seed(folder, t)
            got, events = self.run_cli(["auto", "--once", "--dry-run", "--json"])
            self.assertEqual(got, code, events)
            self.assertTrue(all(EVENT_KEYS <= set(e) for e in events))
            self.assertEqual(events[0]["event"], "poll")
            self.assertEqual(set(events[0]) - EVENT_KEYS, {"active", "headroomPct", "threshold", "windowsPct"})
        self.assertEqual(self.store.writes, [])  # dry-run never writes
        self.assertIsNone(self.state())
        self.assertEqual(paths.sequence_file().read_bytes(), roster)
        self.assertEqual({f: paths.token_path(f).read_bytes() for f in tokens}, tokens)
        self.assertEqual(list(paths.backup_dir().glob("*")) if paths.backup_dir().exists() else [], [])

    def test_cli_flags_override_settings(self):
        self.seed_all(text(85), text(10), text(10))
        code, events = self.run_cli(["auto", "--once", "--dry-run", "--json", "--threshold", "80", "--strategy", "next-available"])
        self.assertEqual(code, 0)
        self.assertEqual((events[0]["threshold"], events[-1]["to"]["number"]), (80.0, 2))

    def test_dry_run_reports_running_processes(self):
        self.procs.stop()
        with mock.patch("agyswap.procs.running", return_value={"antigravity.exe": 1}):
            self.seed_all(text(95), text(10))
            self.assertEqual(self.tick(dry_run=True), Outcome.SWITCHED)
        self.assertIn("agy or the App is running", self.last("switch")["warnings"][0])
        self.procs.start()

    def test_live_run_is_rechecked_under_the_lock(self):
        self.seed_all(text(95), text(10), text(30))
        alive = lambda path: 4242 if "school" in str(path) else None  # noqa: E731
        with mock.patch("agyswap.run._runner_alive", side_effect=alive):
            self.assertEqual(self.tick(dry_run=True), Outcome.SWITCHED)  # dry-run predicts the same target
            self.assertEqual(self.last("switch")["to"]["number"], 3)
            self.assertEqual(self.tick(), Outcome.SWITCHED)  # cswap #508: school is best but has a live run, so work
        self.assertEqual(self.last("switch")["to"]["number"], 3)

    def test_output_errors_never_stop_the_loop(self):
        self.seed_all(text(50), text(10))
        for exc in (BrokenPipeError(32, "Broken pipe"), UnicodeEncodeError("cp950", "·", 0, 1, "unmappable")):
            def broken_output(ev, exc=exc):
                raise exc
            engine = Engine(AutoSettings(), broken_output, store=self.store, fetcher=self.agy, clock=self.clock)
            self.assertEqual(engine.tick(), Outcome.NO_ACTION)
            waits = []

            def wait(delay, engine=engine):
                waits.append(delay)
                engine._stop.set()
            with mock.patch.object(engine._stop, "wait", side_effect=wait), mock.patch.object(engine, "next_delay", return_value=10_000.0):
                self.assertEqual(engine._loop(), 0)  # the sleep event failed too
            self.assertEqual(waits, [10_000.0])

    def test_next_delay_survives_a_damaged_roster(self):
        engine = self.engine()
        for damaged in ({"activeAccountNumber": 1, "accounts": None}, {"activeAccountNumber": 1, "accounts": {"1": "x"}}, ["x"]):
            with mock.patch("agyswap.accounts.load_sequence", return_value=damaged):
                self.assertGreater(engine.next_delay(Outcome.NO_ACTION), 0)

    def test_cooldown_ignores_a_far_future_switch_time(self):
        engine = self.engine()
        self.assertFalse(engine._in_cooldown({"lastSwitchAt": self.now + 10 ** 6}))  # a clock that ran ahead cannot freeze proactive moves
        self.assertTrue(engine._in_cooldown({"lastSwitchAt": self.now + 30}))  # a small step back still counts as just switched
        self.assertTrue(engine._in_cooldown({"lastSwitchAt": self.now - 30}))
        self.assertFalse(engine._in_cooldown({"lastSwitchAt": self.now - 400}))

    def test_stale_decision_never_switches(self):
        """The account changed hands, or the target was disabled, while agy was being queried: the switch is called off under .lock."""
        def manual_switch_to_3():
            self.store._set(paths.token_path("work").read_bytes())

        def disable_2():
            self.set_account("2", disabled=True)

        for meddle in (manual_switch_to_3, disable_2):
            self.store = FakeStore(paths.token_path("default").read_bytes())
            self.seed_all(text(95), text(10), text(30))
            self.events = []

            def switcher(target, **kw):
                meddle()
                return autoswitch.switch.switch_to(target, **kw)

            engine = Engine(AutoSettings(), self.events.append, store=self.store, fetcher=self.agy, clock=self.clock, switcher=switcher)
            self.assertEqual(engine.tick(), Outcome.NO_ACTION, meddle.__name__)
            self.assertEqual(self.last("no-switch")["reason"], "state-changed")
            self.assertEqual(self.store.writes, [])
            self.assertIsNone(self.state())  # no anti-flap state mixing two accounts
            self.set_account("2", disabled=False)

    def test_a_stopped_engine_never_switches(self):
        """Stopped while its tick was deciding (the dashboard went back to dry-run): the switch is called off under .lock."""
        self.seed_all(text(95), text(10), text(30))
        self.events = []

        def switcher(target, **kw):
            engine.stop()
            return autoswitch.switch.switch_to(target, **kw)

        engine = Engine(AutoSettings(), self.events.append, store=self.store, fetcher=self.agy, clock=self.clock, switcher=switcher)
        self.assertEqual(engine.tick(), Outcome.NO_ACTION)
        self.assertTrue(engine.stopped)
        self.assertEqual(self.store.writes, [])
        self.assertNotIn("switch", self.kinds())

    def test_quarantine_and_release(self):
        self.seed_all(text(50), text(10))
        self.now += 3601
        self.agy.results = {"default": text(50), "school": FetchResult(error="auth", relogin=True)}
        self.tick()
        self.assertEqual(self.last("account-quarantined")["number"], "2")
        self.assertIn("2", self.state()["quarantine"])
        self.write_token("school", "me@school.edu", secret="RELOGGED")
        self.now += 60
        self.tick()
        self.assertEqual(self.events[0]["event"], "account-unquarantined")
        self.assertEqual(self.events[0]["reason"], "credentials-replaced")

    def test_no_or_unmanaged_active_account(self):
        self.store = FakeStore()
        self.assertEqual(self.tick(), Outcome.NO_ACTION)
        self.assertEqual(self.last("no-switch")["reason"], "no-active-account")
        self.store = FakeStore(token_blob("stranger@x.com"))
        self.assertEqual(self.tick(), Outcome.NO_ACTION)
        self.assertEqual(self.last("no-switch")["reason"], "unmanaged-active-account")
        self.store.read_error = 5
        self.assertEqual(self.tick(), Outcome.ERROR)
        self.assertEqual(self.last("error")["transient"], True)

    def test_config_warnings(self):
        paths.settings_file().write_text(json.dumps({"autoswitch": {"treshold": 80}}), encoding="utf-8")
        self.seed_all(text(10), text(10))
        self.tick(model="gemni")
        warnings = [e["message"] for e in self.events if e["event"] == "config-warning"]
        self.assertTrue(any("autoswitch.treshold" in w and "autoswitch.threshold" in w for w in warnings), warnings)
        self.assertTrue(any("gemni" in w for w in warnings), warnings)


class PaceTest(Base):
    def test_one_candidate_per_tick_and_escalation_near_a_line(self):
        self.agy.results = {"default": text(50), "school": text(10), "work": text(10)}
        self.tick()
        self.assertEqual(self.agy.calls, ["default", "school"])  # the active account and the never-queried candidate
        self.now += 61
        self.tick()
        self.assertEqual(self.agy.calls, ["default", "school", "work"])  # the active plan is not due yet
        self.agy.calls.clear()
        self.now += 700
        self.agy.results["default"] = text(80)  # within 15 points of the line: every stale candidate is queried
        self.tick()
        self.assertEqual(self.agy.calls, ["default", "school", "work"])

    def test_human_lines(self):
        self.seed_all(text(92), text(30), text(10))
        self.engine(dry_run=True).tick()
        lines = [autoswitch.human(e) for e in self.events]
        self.assertTrue(lines[0].startswith("Account-1 (me@gmail.com): 7d 92% used (switch at 90%) | others: #2: 7d 30%"), lines[0])
        self.assertEqual(lines[-1], "[dry-run] would switch Account-1 -> Account-3 (me@work.org) (proactive)")
