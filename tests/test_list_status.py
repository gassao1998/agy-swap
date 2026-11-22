"""The JSON shape and text output of list and status, using a fake agy query and a fake Credential Manager."""
import contextlib
import io
import json
import time
import unittest
from unittest import mock

from agyswap import accounts, commands, output, pace, paths, settings, usage_store
from agyswap.login import login_email
from agyswap.state import Context
from agyswap.usage import FetchResult, parse_usage
from agyswap.usage_store import Entry, UsageCache, poll_interval
from tests.helpers import SECRET, TempHome, live
from tests.test_usage import FREE, PRO

ROW_KEYS = {"number", "email", "organizationName", "organizationUuid", "isOrganization", "active",
            "usageStatus", "usage"}


class FakeAgy:
    def __init__(self, by_home=None, live_result=None):
        self.calls = []
        self.by_home = by_home or {}
        self.live_result = live_result

    def __call__(self, env):
        home = env.get("USERPROFILE", "") if "SSH_CONNECTION" in env else None
        self.calls.append(home)
        if home is None:
            return self.live_result
        for folder, result in self.by_home.items():
            if home.endswith(folder):
                return result
        return FetchResult(error="parse")


class ListStatusTest(TempHome):
    def setUp(self):
        super().setUp()
        self.write_token("default", "me@gmail.com")
        self.write_token("school", "me@school.edu")
        self.data = {"activeAccountNumber": 1, "accounts": {
            "1": {"email": "me@gmail.com", "alias": "default", "folder": "default"},
            "2": {"email": "me@school.edu", "alias": "school", "folder": "school"}}}
        accounts.save_sequence(self.data)
        self.fake = FakeAgy({"school": FetchResult(usage=parse_usage(FREE))}, FetchResult(usage=parse_usage(PRO)))
        p = mock.patch("agyswap.procs.running", return_value={})
        p.start()
        self.addCleanup(p.stop)
        self.enterContext(contextlib.redirect_stderr(io.StringIO()))

    def ctx(self, login):
        return Context(data=accounts.load_sequence(), live=login, settings=settings.load())

    def test_list_json_shape_and_cache(self):
        payload = commands.cmd_list(self.ctx(live("me@gmail.com")), json_output=True, fetcher=self.fake)
        self.assertEqual(payload["schemaVersion"], 1)
        self.assertEqual(payload["activeAccountNumber"], 1)
        one, two = payload["accounts"]
        self.assertTrue(ROW_KEYS <= set(one))
        self.assertEqual((one["active"], one["usageStatus"], one["alias"]), (True, "ok", "default"))
        self.assertEqual(set(one["usage"]), {"fiveHour", "sevenDay", "scoped"})
        self.assertIn("usageFetchedAt", one)
        self.assertNotIn("lastGoodUsage", one)
        self.assertEqual((two["active"], two["usage"]["sevenDay"]["pct"]), (False, 7.0))
        self.assertNotIn("fiveHour", two["usage"])
        self.assertNotIn("duplicateAccountWarnings", payload)
        self.assertNotIn(SECRET, json.dumps(payload))
        self.assertEqual(self.fake.calls[0], None)  # the active account is queried with the normal environment
        commands.cmd_list(self.ctx(live("me@gmail.com")), json_output=True, fetcher=self.fake)
        self.assertEqual(len(self.fake.calls), 2)  # no further queries within the serve period

    def test_school_in_keyring_and_default_without_login(self):
        paths_default = self.home / "profiles" / "default"
        for p in sorted(paths_default.rglob("*"), reverse=True):
            p.unlink() if p.is_file() else p.rmdir()
        self.data["activeAccountNumber"] = 2
        accounts.save_sequence(self.data)
        payload = commands.cmd_list(self.ctx(live("me@school.edu")), json_output=True,
                                    fetcher=FakeAgy(live_result=FetchResult(usage=parse_usage(FREE))))
        self.assertEqual(payload["activeAccountNumber"], 2)
        self.assertEqual(payload["accounts"][0]["usageStatus"], "no_credentials")
        self.assertIsNone(payload["accounts"][0]["usage"])
        self.assertEqual(payload["accounts"][1]["usageStatus"], "ok")

    def test_status_three_shapes_and_keychain_error(self):
        self.assertEqual(commands.cmd_status(self.ctx(live()), json_output=True, fetcher=self.fake),
                         {"schemaVersion": 1, "active": None})
        self.assertEqual(commands.cmd_status(self.ctx(live("other@x.com")), json_output=True, fetcher=self.fake),
                         {"schemaVersion": 1, "active": {"email": "other@x.com", "managed": False}})
        broken = live(blob=b"not json")
        self.assertEqual(commands.cmd_status(self.ctx(broken), json_output=True, fetcher=self.fake)["active"],
                         {"email": "unknown@agyswap.local", "managed": False})
        managed = commands.cmd_status(self.ctx(live("me@gmail.com")), json_output=True, fetcher=self.fake)
        self.assertEqual(managed["totalManagedAccounts"], 2)
        self.assertEqual({k for k in managed["active"]} - {"usageFetchedAt", "usageAgeSeconds"},
                         {"number", "email", "organizationName", "organizationUuid", "isOrganization",
                          "managed", "usageStatus", "usage", "alias"})
        paths.token_path("default").write_bytes(b"")
        err = commands.cmd_status(self.ctx(live(error="error code 5")), json_output=True, fetcher=self.fake)
        self.assertEqual((err["active"]["number"], err["active"]["usageStatus"]), (1, "keychain_unavailable"))

    def test_unreadable_keychain_reads_the_active_folder_copy(self):
        """Over SSH Credential Manager cannot be read: the active account is queried through its folder like run, nothing is written, and the row says so."""
        before = paths.token_path("default").read_bytes()
        fake = FakeAgy({"default": FetchResult(usage=parse_usage(PRO)), "school": FetchResult(usage=parse_usage(FREE))})
        out, err = io.StringIO(), io.StringIO()
        ssh = live(error="CredReadW failed for gemini:antigravity, error code 1312", code=1312)
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            payload = commands.cmd_list(self.ctx(ssh), json_output=True, fetcher=fake)
            status = commands.cmd_status(self.ctx(ssh), json_output=True, fetcher=fake)
            commands.cmd_list(self.ctx(ssh), fetcher=fake)
        self.assertIn("not available in this logon session", err.getvalue())
        self.assertNotIn("error code 1312", err.getvalue())  # the plain explanation replaces the raw API error
        other = io.StringIO()
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(other):
            commands.cmd_list(self.ctx(live(error="CredReadW failed for gemini:antigravity, error code 5", code=5)), fetcher=fake)
        self.assertIn("Cannot read Credential Manager (CredReadW failed", other.getvalue())
        one, two = payload["accounts"]
        self.assertEqual((one["active"], one["usageStatus"], one["globalLoginUnchecked"]), (True, "ok", True))
        self.assertEqual(one["usage"]["sevenDay"]["pct"], parse_usage(PRO)["seven_day"]["pct"])
        self.assertNotIn("globalLoginUnchecked", two)
        self.assertEqual((status["active"]["usageStatus"], status["active"]["globalLoginUnchecked"]), ("ok", True))
        self.assertTrue(fake.calls[0].endswith("default"))  # isolated: SSH variables and the account folder as home
        self.assertNotIn(None, fake.calls)
        self.assertIn("global login was not checked", out.getvalue())
        self.assertEqual(paths.token_path("default").read_bytes(), before)

    def test_relogin_then_backoff(self):
        fake = FakeAgy({"school": FetchResult(error="auth", relogin=True)}, FetchResult(usage=parse_usage(PRO)))
        payload = commands.cmd_list(self.ctx(live("me@gmail.com")), json_output=True, fetcher=fake)
        self.assertEqual(payload["accounts"][1]["usageStatus"], "relogin_required")
        commands.cmd_list(self.ctx(live("me@gmail.com")), json_output=True, fetcher=fake)
        self.assertEqual(fake.calls.count(None), 1)
        self.assertEqual(len(fake.calls), 2)  # school is in backoff, so agy is not started again

    def test_stale_failure_projects_last_good(self):
        cache = UsageCache()
        cache.record_success("school", parse_usage(FREE), 600)
        cache.clock = lambda: time.time() + 2000
        cache.record_failure("school", "timeout")
        entry = cache.entry("school")
        row = output.account_row(2, self.data["accounts"]["2"], False, entry, time.time() + 2000)
        self.assertEqual(row["usageStatus"], "unavailable")
        self.assertIsNone(row["usage"])
        self.assertEqual(row["usageError"], "timeout")
        self.assertIn("usageRetryAt", row)
        self.assertEqual(row["lastGoodUsage"]["sevenDay"]["pct"], 7.0)
        self.assertGreater(row["lastGoodAgeSeconds"], 900)

    def test_duplicate_and_lockstep_warnings(self):
        self.write_token("school", "me@gmail.com")
        (self.home / "profiles" / "school" / ".gemini" / "antigravity-cli" / "antigravity-oauth-token").write_bytes(
            (self.home / "profiles" / "default" / ".gemini" / "antigravity-cli" / "antigravity-oauth-token").read_bytes())
        same = FakeAgy({"school": FetchResult(usage=parse_usage(PRO))}, FetchResult(usage=parse_usage(PRO)))
        payload = commands.cmd_list(self.ctx(live("me@gmail.com")), json_output=True, fetcher=same)
        dup = payload["duplicateAccountWarnings"]
        self.assertEqual(len(dup), 2)
        self.assertIn("hold the same login file", dup[0])
        self.assertIn("its login file belongs to me@gmail.com", dup[1])
        self.assertEqual(len(payload["lockstepUsageWarnings"]), 1)

    def test_refreshed_copy_in_wrong_slot_is_caught_without_same_hash(self):
        # A's refreshed token was saved into B: the hashes differ and so do the roster emails; only the email inside the login file gives it away
        self.write_token("school", "me@gmail.com", secret="ROTATED")
        warnings = commands.duplicate_warnings(accounts.load_sequence())
        self.assertTrue(any("both belong to me@gmail.com" in w for w in warnings))
        self.assertTrue(any("Account-2 (school) is recorded as me@school.edu" in w for w in warnings))
        self.assertFalse(any("hold the same login file" in w for w in warnings))

    def test_idle_accounts_are_not_lockstep(self):
        def idle():
            t = time.time()
            text = (f"Gemini Models\tWeekly Limit Remaining\t100%\t{output.iso(t + 7 * 86400 + 1)}\n"
                    f"Claude and GPT models\tWeekly Limit Remaining\t100%\t{output.iso(t + 7 * 86400 + 1)}\n")
            return FetchResult(usage=parse_usage(text))
        fake = FakeAgy({"school": idle()}, idle())
        payload = commands.cmd_list(self.ctx(live("me@gmail.com")), json_output=True, fetcher=fake)
        self.assertNotIn("lockstepUsageWarnings", payload)

    def test_text_list_and_token_status_never_print_secrets(self):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            commands.cmd_list(self.ctx(live("me@gmail.com")), token_status=True, fetcher=self.fake)
            commands.cmd_status(self.ctx(live("me@gmail.com")), fetcher=self.fake)
        text = out.getvalue() + err.getvalue()
        self.assertIn("(active)", text)
        self.assertIn("Gemini Models\n", text)
        self.assertIn("5h: ", text)
        self.assertIn("Claude and GPT models\n", text)
        self.assertIn("7d: ", text)
        self.assertIn("Credential Manager: present", text)
        self.assertIn("sha256 ", text)
        self.assertIn("belongs to me@school.edu", text)
        self.assertNotIn(SECRET, text)

    def test_foreign_credential_is_reported_not_rewritten(self):
        """The roster says Account-1 and Credential Manager holds Account-2's login: as in cswap, Account-1 is reported as foreign_credential and the roster is left alone."""
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            payload = commands.cmd_list(self.ctx(live("me@school.edu")), json_output=True, fetcher=self.fake)
            status = commands.cmd_status(self.ctx(live("me@school.edu")), json_output=True, fetcher=self.fake)
        self.assertEqual(payload["activeAccountNumber"], 1)
        one, two = payload["accounts"]
        self.assertEqual((one["active"], one["usageStatus"], one["usage"]), (True, "foreign_credential", None))
        self.assertEqual((two["active"], two["usageStatus"]), (False, "ok"))
        self.assertEqual((status["active"]["number"], status["active"]["usageStatus"]), (1, "foreign_credential"))
        self.assertIn("foreign_credential", err.getvalue())
        self.assertIn("agyswap switch 2", err.getvalue())
        self.assertEqual(accounts.load_sequence()["activeAccountNumber"], 1)
        # Account-1's folder was not overwritten by someone else's login
        self.assertEqual(login_email(paths.token_path("default").read_bytes()), "me@gmail.com")

    def test_unrecorded_active_is_inferred_from_email(self):
        self.data["activeAccountNumber"] = None
        accounts.save_sequence(self.data)
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            payload = commands.cmd_list(self.ctx(live("me@school.edu")), json_output=True, fetcher=self.fake)
        self.assertEqual(payload["activeAccountNumber"], 2)
        self.assertIn("The roster records no account as active", err.getvalue())

    def test_cli_json_error_envelope_on_corrupt_roster(self):
        from agyswap import cli
        (self.home / "sequence.json").write_text("{torn", encoding="utf-8")
        out = io.StringIO()
        with mock.patch("agyswap.state.read_live_login", return_value=live("me@gmail.com")), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            code = cli.dispatch(["list", "--json"])
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(out.getvalue())["error"]["type"], "ConfigError")

    def test_empty_roster(self):
        (self.home / "sequence.json").unlink()
        ctx = Context(data=None, live=live(), settings=settings.load())
        self.assertEqual(commands.cmd_list(ctx, json_output=True),
                         {"schemaVersion": 1, "activeAccountNumber": None, "accounts": []})


class PaceAndTimeTest(unittest.TestCase):
    def test_pace(self):
        fetched = 1_800_000_000.0
        reset = output.iso(fetched + 3.5 * 86400)
        p = pace.compute({"pct": 80, "resets_at": reset}, fetched)
        self.assertAlmostEqual(p.expected_pct, 50.0)
        self.assertTrue(p.ahead)
        self.assertFalse(pace.will_last(p))
        self.assertIsNotNone(pace.projected_exhaustion(p, fetched))
        self.assertIsNone(pace.compute({"pct": 80, "resets_at": output.iso(fetched + 6.5 * 86400)}, fetched))
        # The current period can still be worked out when the reset time is several periods in the past
        old = pace.compute({"pct": 10, "resets_at": output.iso(fetched - 14 * 86400 + 3.5 * 86400)}, fetched)
        self.assertAlmostEqual(old.expected_pct, 50.0)
        self.assertTrue(pace.will_last(pace.Pace(50, 0, 100, 600, False)))
        # For an unused window agy answers "now + 7 days + 1 second": it must not be counted as almost a full week
        self.assertIsNone(pace.compute({"pct": 0, "resets_at": output.iso(fetched + 7 * 86400 + 1)}, fetched))
        self.assertTrue(pace.unstarted({"pct": 0, "window": "7d", "resets_at": output.iso(fetched + 7 * 86400 + 1)},
                                       fetched))
        self.assertTrue(pace.unstarted({"pct": 0, "window": "5h", "resets_at": output.iso(fetched + 5 * 3600)},
                                       fetched))
        self.assertFalse(pace.unstarted({"pct": 3, "window": "7d", "resets_at": output.iso(fetched + 7 * 86400)},
                                        fetched))

    def test_window_json_pace_only_on_weekly(self):
        fetched = time.time()
        u = parse_usage(PRO)
        j = output.usage_to_json(u, fetched)
        self.assertEqual(set(j["fiveHour"]) - {"countdown", "clock"}, {"pct", "resetsAt"})
        five = [w for w in j["scoped"] if w["window"] == "5h"]
        self.assertTrue(all("expectedPct" not in w for w in five))

    def test_reset_strings(self):
        now = 1_800_000_000.0
        self.assertEqual(output.reset_strings(output.iso(now + 2 * 86400 + 3 * 3600), now)[0], "2d 3h")
        self.assertEqual(output.reset_strings(output.iso(now + 3600 + 120), now)[0], "1h 2m")
        self.assertEqual(output.reset_strings(output.iso(now - 60), now)[0], "0m")
        self.assertIsNone(output.reset_strings("not-a-time", now))

    def test_usage_fields(self):
        self.assertEqual(output.usage_fields(None, None), ("unavailable", None))
        self.assertEqual(output.usage_fields("api_key", None), ("api_key", None))
        self.assertEqual(output.usage_fields(parse_usage(FREE), None)[0], "ok")
        self.assertEqual(Entry(last_good={"x": 1}, age_s=901).decision_value(), None)
        self.assertEqual(Entry(last_good={"x": 1}, age_s=899).decision_value(), {"x": 1})


class PollPlanTest(unittest.TestCase):
    def test_intervals(self):
        free, busy = parse_usage(FREE), parse_usage(FREE.replace("93%", "20%"))
        dead = parse_usage(FREE.replace("93%", "0%"))
        self.assertEqual(poll_interval(free, active=True, threshold=90), usage_store.ACTIVE_INTERVAL_S)
        self.assertEqual(poll_interval(busy, active=True, threshold=90, previous=free), usage_store.URGENT_INTERVAL_S)
        self.assertEqual(poll_interval(busy, active=True, threshold=90, previous=busy), usage_store.ACTIVE_INTERVAL_S)
        self.assertEqual(poll_interval(free, active=False, threshold=90), usage_store.CANDIDATE_INTERVAL_S)
        self.assertEqual(poll_interval(dead, active=True, threshold=90), usage_store.EXHAUSTED_INTERVAL_S)
        self.assertEqual([usage_store.backoff_seconds(n) for n in (1, 2, 3, 4, 9)], [300, 600, 1200, 1800, 1800])
        self.assertGreater(usage_store.STALE_OK_S, usage_store.CANDIDATE_INTERVAL_S)


if __name__ == "__main__":
    unittest.main()
