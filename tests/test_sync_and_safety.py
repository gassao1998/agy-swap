"""P2 hardening: syncing the active account's token, telling a temporary read failure from a real expiry, stdout holding only JSON under --json,
timeouts of the query subprocess, and a few edge cases cswap has hit."""
import contextlib
import io
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from agyswap import accounts, cli, commands, manage, migrate, output, paths, procs, settings, usage, usage_store
from agyswap.errors import AgyswapError, CredentialError
from agyswap.fsutil import sha256_hex
from agyswap.state import Context, collect, sync_active_login
from agyswap.usage import FetchResult, parse_usage
from agyswap.usage_store import Entry, UsageCache, backoff_seconds
from tests.helpers import SECRET, TempHome, live, token_blob
from tests.test_list_status import FakeAgy
from tests.test_usage import FREE, PRO

OLD, NEW = "2026-11-22T10:00:00.123456+08:00", "2026-11-22T11:00:00.654321+08:00"


class Base(TempHome):
    def setUp(self):
        super().setUp()
        self.write_token("default", "me@gmail.com", expiry=OLD)
        self.write_token("school", "me@school.edu")
        self.data = {"activeAccountNumber": 1, "accounts": {
            "1": {"email": "me@gmail.com", "alias": "default", "folder": "default"},
            "2": {"email": "me@school.edu", "alias": "school", "folder": "school"}}}
        accounts.save_sequence(self.data)
        p = mock.patch("agyswap.procs.running", return_value={})
        p.start()
        self.addCleanup(p.stop)

    def ctx(self, login):
        return Context(data=accounts.load_sequence(), live=login, settings=settings.load())

    def token(self, folder="default"):
        return paths.token_path(folder)


class SyncTest(Base):
    def test_newer_live_login_is_saved_with_backup(self):
        before = self.token().read_bytes()
        newer = token_blob("me@gmail.com", secret="REFRESHED", expiry=NEW)
        self.assertIsNone(sync_active_login(self.ctx(live(blob=newer))))
        self.assertEqual(self.token().read_bytes(), newer)
        self.assertEqual(self.token().with_name(self.token().name + ".bak").read_bytes(), before)

    def test_never_writes_wiped_foreign_or_older_logins(self):
        before = self.token().read_bytes()
        cases = (token_blob("me@gmail.com", secret=""),                      # tokens blanked
                 b'{"id_token_email": "me@gmail.com"}',                       # no token
                 token_blob("me@gmail.com", secret="STALE", expiry="2026-11-22T09:00:00+08:00"),  # older than the folder copy
                 token_blob("me@school.edu", secret="X", expiry=NEW))         # another account (foreign)
        for blob in cases:
            sync_active_login(self.ctx(live(blob=blob)))
            self.assertEqual(self.token().read_bytes(), before, blob)
        self.assertFalse(self.token().with_name(self.token().name + ".bak").exists())

    def test_stale_context_never_writes_into_removed_or_reused_folder(self):
        newer = token_blob("me@gmail.com", secret="REFRESHED", expiry=NEW)
        ctx = self.ctx(live(blob=newer))  # read before another agyswap removes the account
        migrate.migrate(lambda: live())
        with contextlib.redirect_stderr(io.StringIO()):
            manage.remove("1", confirm=lambda _: True)
        self.assertIn("does not exist", sync_active_login(ctx))
        self.assertFalse(paths.profile_home("default").exists())  # the removed account's login is not written back
        self.write_token("default", "new@owner.com")  # a later account reuses the folder name
        before = self.token().read_bytes()
        self.assertIn("another email", sync_active_login(ctx))
        self.assertEqual(self.token().read_bytes(), before)

    def test_restrict_failure_blocks_sync(self):
        before = self.token().read_bytes()
        with mock.patch("agyswap.fsutil.restrict_to_current_user", return_value=False):
            note = sync_active_login(self.ctx(live(blob=token_blob("me@gmail.com", secret="N", expiry=NEW))))
        self.assertIn("Could not restrict", note)
        self.assertEqual(self.token().read_bytes(), before)

    def test_busy_lock_skips_quietly(self):
        from agyswap.fsutil import file_lock
        before = self.token().read_bytes()
        with file_lock(paths.lock_file()):
            # Opening it again in the same process cannot take the lock either, which is the same as another agyswap switching
            self.assertIsNone(sync_active_login(self.ctx(live(blob=token_blob("me@gmail.com", "N", NEW)))))
        self.assertEqual(self.token().read_bytes(), before)

    def test_list_runs_sync_and_never_prints_token(self):
        newer = token_blob("me@gmail.com", secret="REFRESHED", expiry=NEW)
        out, err = io.StringIO(), io.StringIO()
        fake = FakeAgy({"school": FetchResult(usage=parse_usage(FREE))}, FetchResult(usage=parse_usage(PRO)))
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            commands.cmd_list(self.ctx(live(blob=newer)), token_status=True, fetcher=fake)
        self.assertEqual(self.token().read_bytes(), newer)
        self.assertNotIn("REFRESHED", out.getvalue() + err.getvalue())
        self.assertNotIn(SECRET, out.getvalue() + err.getvalue())


class ActiveTest(Base):
    def test_api_key_account_with_same_email_is_not_active(self):
        old = self.data["accounts"]
        self.data["accounts"] = {"1": {"email": "me@gmail.com", "folder": "key1", "kind": "apikey"},
                                 "2": old["1"], "3": old["2"]}
        self.data["activeAccountNumber"] = None
        accounts.save_sequence(self.data)
        self.assertEqual(self.ctx(live("me@gmail.com")).active_number, "2")

    def test_duplicate_email_prefers_identical_folder(self):
        self.data["accounts"]["2"]["email"] = "me@gmail.com"
        self.data["activeAccountNumber"] = None
        accounts.save_sequence(self.data)
        blob = token_blob("me@gmail.com", secret="ONLY-IN-2")
        self.token("school").write_bytes(blob)
        self.assertEqual(self.ctx(live(blob=blob)).live_owner, "2")

    def test_status_without_keychain_or_roster_is_an_error(self):
        (self.home / "sequence.json").unlink()
        ctx = Context(data=None, live=live(error="CredReadW failed, error code 5"), settings=settings.load())
        with self.assertRaises(CredentialError):
            commands.cmd_status(ctx, json_output=True)
        out = io.StringIO()
        with mock.patch("agyswap.state.read_live_login", return_value=live(error="error code 5")), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            code = cli.dispatch(["status", "--json"])
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(out.getvalue())["error"]["type"], "CredentialError")


class TransientTest(Base):
    def test_unreadable_token_file_is_not_no_credentials(self):
        fake = FakeAgy({"school": FetchResult(usage=parse_usage(FREE))}, FetchResult(usage=parse_usage(PRO)))
        with mock.patch("agyswap.state.read_token_file", return_value=None):
            entries = collect(self.ctx(live("me@gmail.com")), ["1", "2"], fetcher=fake)
        self.assertIsNone(entries["2"].sentinel)
        self.assertEqual(entries["2"].decision_value()["seven_day"]["pct"], 7.0)

    def test_timeout_is_not_relogin_and_relogin_clears_after_new_login(self):
        fake = FakeAgy({"school": FetchResult(error="timeout")}, FetchResult(usage=parse_usage(PRO)))
        entries = collect(self.ctx(live("me@gmail.com")), ["2"], fetcher=fake)
        self.assertIsNone(entries["2"].sentinel)
        cache = UsageCache()
        cache.record_failure("school", "auth", relogin=True, cred_sha=sha256_hex(self.token("school").read_bytes()))
        fake = FakeAgy({"school": FetchResult(usage=parse_usage(FREE))}, None)
        entries = collect(self.ctx(live("me@gmail.com")), ["2"], fetcher=fake)
        self.assertEqual(entries["2"].sentinel, "relogin_required")  # same login: no re-query while in backoff
        self.assertEqual(fake.calls, [])
        self.write_token("school", "me@school.edu", secret="RELOGGED")  # logged in again
        entries = collect(self.ctx(live("me@gmail.com")), ["2"], fetcher=fake)
        self.assertIsNone(entries["2"].sentinel)
        self.assertEqual(len(fake.calls), 1)

    def test_slot_identical_to_live_is_queried_through_live(self):
        self.token("school").write_bytes(token_blob("me@school.edu"))
        fake = FakeAgy({}, FetchResult(usage=parse_usage(FREE)))
        collect(self.ctx(live(blob=token_blob("me@school.edu"))), ["2"], fetcher=fake)
        self.assertEqual(fake.calls, [None])  # queried through the Credential Manager copy, not refreshed again from the folder

    def test_corrupt_cache_fields_and_huge_failure_count(self):
        paths.usage_cache_file().parent.mkdir(parents=True, exist_ok=True)
        paths.usage_cache_file().write_text(json.dumps({"accounts": {"school": {
            "consecutiveFailures": "abc", "relogin": "yes", "fetchedAt": "x"}}}), encoding="utf-8")
        entry = UsageCache().entry("school")
        self.assertEqual((entry.consecutive_failures, entry.relogin), (0, False))
        UsageCache().record_failure("school", "timeout")
        self.assertEqual(backoff_seconds(2000), 1800)


class ClaimAndLogTest(Base):
    """One agy start per account at a time, and a late result never overwrites a newer one (cswap #157, #85); each start is logged without token content (cswap #434, #590)."""

    def test_live_claim_by_another_process_skips_the_query(self):
        UsageCache().claim("school")  # another agyswap is querying school right now
        fake = FakeAgy({"school": FetchResult(usage=parse_usage(FREE))}, None)
        collect(self.ctx(live("me@gmail.com")), ["2"], fetcher=fake)
        self.assertEqual(fake.calls, [])

    def test_expired_claim_is_taken_over(self):
        UsageCache(clock=lambda: time.time() - 1000).claim("school")
        fake = FakeAgy({"school": FetchResult(usage=parse_usage(FREE))}, None)
        entries = collect(self.ctx(live("me@gmail.com")), ["2"], fetcher=fake)
        self.assertEqual(len(fake.calls), 1)
        self.assertEqual(entries["2"].decision_value()["seven_day"]["pct"], 7.0)

    def test_late_result_does_not_overwrite_newer(self):
        cache = UsageCache()
        late = UsageCache(clock=lambda: time.time() - 1000).claim("school")  # a query that hung past its claim
        cache.record_success("school", parse_usage(PRO), 600)  # unclaimed writes (import-usage style) still land
        newer = cache.claim("school")  # the expired claim is taken over
        self.assertIsNotNone(newer)
        cache.record_success("school", parse_usage(FREE), 600, claim=late)
        self.assertEqual(cache.entry("school").last_good["seven_day"]["pct"], 0.0)
        cache.record_failure("school", "timeout", claim=late)
        self.assertEqual(cache.entry("school").consecutive_failures, 0)
        cache.record_success("school", parse_usage(FREE), 600, claim=newer)
        self.assertEqual(cache.entry("school").last_good["seven_day"]["pct"], 7.0)
        self.assertIsNotNone(cache.claim("school"))  # a finished query releases its claim

    def test_claim_is_released_when_the_query_raises(self):
        for exc in (RuntimeError("boom"), KeyboardInterrupt()):
            def broken(env, exc=exc):
                raise exc
            with self.assertRaises(type(exc)):
                collect(self.ctx(live("me@gmail.com")), ["2"], fetcher=broken)
            self.assertIsNone(UsageCache().entry("school").claim_until)
            self.assertIsNotNone(UsageCache().claim("school"))  # not locked out for CLAIM_S
            UsageCache().release("school", "nobody")  # someone else's claim id leaves it alone
            self.assertIsNotNone(UsageCache().entry("school").claim_until)
            paths.usage_cache_file().unlink()

    def test_far_future_claim_does_not_lock_out(self):
        UsageCache(clock=lambda: time.time() + 10 ** 7).claim("school")  # a clock that ran ahead, or a damaged file
        self.assertFalse(UsageCache().entry("school").claimed(time.time()))
        self.assertIsNotNone(UsageCache().claim("school"))

    def test_claim_rechecks_the_row_under_the_lock(self):
        cache = UsageCache()
        seen = cache.entry("school")  # this process decided to query on this reading...
        cache.record_success("school", parse_usage(FREE), 600)  # ...and another one finished a query meanwhile
        self.assertIsNone(cache.claim("school", seen))
        self.assertIsNotNone(cache.claim("school", cache.entry("school")))

    def test_clock_is_read_inside_the_cache_lock(self):
        moment, real = [1_800_000_000.0], usage_store.file_lock

        @contextlib.contextmanager
        def slow_lock(path, **kw):
            moment[0] += 50  # waiting for another process
            with real(path, **kw):
                yield

        cache = UsageCache(clock=lambda: moment[0])
        with mock.patch("agyswap.usage_store.file_lock", slow_lock):
            claim = cache.claim("school")
            cache.record_success("school", parse_usage(FREE), 600, claim=claim)
        self.assertEqual(cache.entry("school").fetched_at, 1_800_000_100.0)

    def test_each_query_is_logged_without_token(self):
        fake = FakeAgy({"school": FetchResult(error="timeout")}, FetchResult(usage=parse_usage(PRO)))
        collect(self.ctx(live("me@gmail.com")), ["1", "2"], fetcher=fake)
        text = paths.log_file().read_text(encoding="utf-8")
        self.assertIn("usage query start: folder school, login ", text)
        self.assertIn("usage query end: folder school, result timeout", text)
        self.assertIn("usage query end: folder default, result ok", text)
        self.assertNotIn(SECRET, text)


class JsonStdoutTest(Base):
    def test_stdout_is_only_json_even_with_notes(self):
        (self.home / "pending_switch.json").write_text('{"from": 1, "to": 2}', encoding="utf-8")
        newer = token_blob("me@gmail.com", secret="N", expiry=NEW)
        # agy cannot be found (AGY_BIN points at a file that does not exist): no account can be queried, plus the foreign, unmanaged and sync cases that print warnings
        for argv, login in ((["list", "--json"], live("me@school.edu")), (["status", "--json"], live(blob=newer)),
                            (["list", "--json"], live("someone@else.com"))):
            out, err = io.StringIO(), io.StringIO()
            with mock.patch("agyswap.state.read_live_login", return_value=login), \
                    contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                code = cli.dispatch(argv)
            self.assertEqual(code, 0, err.getvalue())
            self.assertEqual(json.loads(out.getvalue())["schemaVersion"], 1)
            self.assertIn("the last switch did not finish", err.getvalue())


class FetchTest(unittest.TestCase):
    def test_timeout_with_partial_output_kills_tree(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, {"AGYSWAP_HOME": tmp}):
            fake = Path(tmp) / "agy.bat"
            # Print one valid line and then hang; ping is a child process of cmd and holds the stdout pipe
            fake.write_text("@echo off\r\necho Gemini Models\tWeekly Limit Remaining\t40%%\t2026-11-28T10:45:55Z\r\n"
                            "ping -n 60 127.0.0.1 >nul\r\n", encoding="ascii")
            start = time.monotonic()
            result = usage.fetch(dict(os.environ), agy=fake, timeout=2)
            self.assertEqual(result.error, "timeout")
            self.assertIsNone(result.usage)
            self.assertLess(time.monotonic() - start, 20)

    def test_classify_partial_and_model_boundary(self):
        self.assertEqual(usage.classify(FREE, timed_out=True).error, "timeout")
        u = parse_usage("Gemini 4\tGemini 4 Weekly Limit\t50%\t2026-11-29T04:38:52Z\n")
        self.assertEqual(len(usage.limits_for_model("gemini-4-pro", u)), 1)
        self.assertEqual(usage.limits_for_model("gemini-40-pro", u), [])

    def test_envs_use_real_home_and_drop_api_key(self):
        with mock.patch.dict(os.environ, {"USERPROFILE": r"C:\x\agyswap\profiles\school", "HOME": "h",
                                          "SSH_CLIENT": "1", "GEMINI_API_KEY": "k"}), \
                mock.patch("agyswap.usage.real_home", return_value=r"C:\Users\me"):
            env = usage.live_env()
            self.assertEqual((env["USERPROFILE"], env["HOME"]), (r"C:\Users\me", r"C:\Users\me"))
            self.assertNotIn("GEMINI_API_KEY", env)
            self.assertNotIn("SSH_CLIENT", env)
            self.assertNotIn("GEMINI_API_KEY", usage.profile_env(Path("C:/p")))
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.dict(os.environ, {"AGYSWAP_HOME": tmp, "USERPROFILE": str(Path(tmp) / "profiles" / "x")}), \
                mock.patch("agyswap.usage.real_home", return_value=None), self.assertRaises(AgyswapError):
            usage.live_env()

    def test_real_home_reads_the_token(self):
        home = usage.real_home()
        self.assertTrue(home and Path(home).is_dir())


class SmallFixesTest(Base):
    def test_threshold_only_when_valid(self):
        for value, shown in ((None, False), ("abc", False), (300, False), (85, True)):
            row = output.account_row(1, {"email": "a@b.c", "threshold": value}, False, Entry(), time.time())
            self.assertEqual("threshold" in row, shown, value)

    def test_extreme_reset_time_does_not_crash(self):
        self.assertIsNone(output.reset_strings("0001-01-01T00:00:00Z", 1_800_000_000.0))
        self.assertEqual(commands._local(-1e18), "?")

    def test_age_uses_the_same_now(self):
        cache = UsageCache(clock=lambda: 1_800_000_000.0)
        cache.record_success("school", parse_usage(FREE), 600)
        entry = UsageCache(clock=lambda: 1_800_000_100.0).entry("school")
        row = output.account_row(2, self.data["accounts"]["2"], False, entry, 1_800_000_100.0)
        self.assertEqual(row["usageAgeSeconds"], 100.0)

    def test_stale_text_marks_first_line_and_past_reset(self):
        fetched = 1_800_000_000.0
        text = f"Gemini Models\tWeekly Limit Remaining\t60%\t{output.iso(fetched + 3600)}\n" \
               f"Claude and GPT models\tWeekly Limit Remaining\t90%\t{output.iso(fetched + 86400)}\n"
        UsageCache(clock=lambda: fetched).record_success("school", parse_usage(text), 600)
        now = fetched + 7200
        lines = output.entry_lines(UsageCache(clock=lambda: now).entry("school"), now)
        self.assertIn("figures below were fetched 2h ago", lines[0])
        self.assertIn("passed", lines[1])
        self.assertNotIn("in 0m", "".join(lines))
        self.assertIn("in 22h", lines[2])

    def test_token_status_reports_unreadable_file(self):
        with mock.patch("agyswap.commands.read_token_file_stat", return_value=None):
            lines = commands.token_status_lines(self.ctx(live("me@gmail.com")), "2", self.data["accounts"]["2"], False)
        self.assertEqual(lines, ["account folder login file: unreadable"])

    def test_tasklist_names_are_case_insensitive(self):
        self.assertEqual(procs.parse_tasklist('"agy.exe","1"\n"AGY.EXE","2"\n"Antigravity.exe","3"\n'),
                         {"agy.exe": 2, "antigravity.exe": 1})


if __name__ == "__main__":
    unittest.main()
