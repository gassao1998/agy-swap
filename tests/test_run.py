"""agyswap run, run --auto, map and unmap."""
import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
from unittest import mock

from agyswap import accounts, cli, migrate, models, paths, procs, run, settings, usage
from agyswap.errors import AgyswapError, ConfigError
from agyswap.usage_store import UsageCache
from tests.helpers import DictStore, TempHome, live, token_blob


def usage_text(gemini_left=50, claude_left=50, argon_left=None):
    lines = [f"Gemini Models\tWeekly Limit Remaining\t{gemini_left}%\t2026-11-28T10:45:55Z",
             f"Claude and GPT models\tWeekly Limit Remaining\t{claude_left}%\t2026-11-29T03:50:47Z"]
    if argon_left is not None:
        lines.append(f"Gemini 4 Argon\tGemini 4 Argon Weekly Limit\t{argon_left}%\t2026-11-29T04:38:52Z")
    return "\n".join(lines) + "\n"


class Base(TempHome):
    def setUp(self):
        super().setUp()
        self.write_token("default", "me@gmail.com")
        self.write_token("school", "me@school.edu")
        accounts.save_sequence({"activeAccountNumber": 1, "accounts": {
            "1": {"email": "me@gmail.com", "alias": "default", "folder": "default"},
            "2": {"email": "me@school.edu", "alias": "school", "folder": "school"}}})
        migrate.migrate(lambda: live())
        self.store = DictStore(token_blob("me@gmail.com"))
        self.work = self.home / "work"
        (self.work / "sub").mkdir(parents=True)
        self.calls, self.fetched = [], []
        self.err = io.StringIO()
        self.enterContext(contextlib.redirect_stderr(self.err))

    def launcher(self, argv, env):
        self.calls.append((argv, env))
        return 7

    def no_fetch(self, env):
        raise AssertionError("usage must not be queried")

    def go(self, ident, args=(), *, cwd=None, fetcher=None, models_fetcher=None, **kw):
        # model lists are unknown unless a test gives them: no test may start agy models
        return run.run(ident, list(args), store=self.store, launcher=self.launcher, cwd=str(cwd or self.work),
                       fetcher=fetcher or self.no_fetch, models_fetcher=models_fetcher or (lambda env: None), **kw)

    def home_of(self, folder):
        return str(paths.profile_home(folder))

    def ran(self):
        self.assertEqual(len(self.calls), 1)
        return self.calls[0]


class RunTest(Base):
    def test_named_account_runs_isolated_and_mirrors_exit_code(self):
        self.assertEqual(self.go("school", ["-p", "hi"]), 7)
        argv, env = self.ran()
        self.assertEqual(argv, [str(paths.agy_bin()), "-p", "hi"])
        self.assertEqual((env["USERPROFILE"], env["HOME"]), (self.home_of("school"),) * 2)
        self.assertEqual({k: env[k] for k in paths.SSH_ENV}, paths.SSH_ENV)
        self.assertNotIn("GEMINI_API_KEY", env)
        self.assertEqual(self.store.writes, [])

    def test_active_account_is_isolated_too(self):
        # cswap #239, #396: the same account always uses its own folder, active or not
        self.go("1")
        _, env = self.ran()
        self.assertEqual(env["USERPROFILE"], self.home_of("default"))
        self.assertEqual(env["SSH_CONNECTION"], paths.SSH_ENV["SSH_CONNECTION"])

    def test_nested_run_ignores_the_terminal_environment(self):
        # cswap #381, #515, #206: inside a run terminal USERPROFILE, the SSH variables and GEMINI_API_KEY belong to another account
        with mock.patch.dict(os.environ, {"USERPROFILE": self.home_of("school"), "HOME": self.home_of("school"),
                                          "GEMINI_API_KEY": "leak", **paths.SSH_ENV}):
            self.go(None)
        _, env = self.ran()
        self.assertEqual(env["USERPROFILE"], self.home_of("default"))
        self.assertNotIn("GEMINI_API_KEY", env)

    def test_bare_run_uses_nearest_mapping_then_active(self):
        accounts.set_mapping(self.work, "school", "me@school.edu")
        self.go(None, cwd=self.work / "sub")
        self.assertEqual(self.calls[-1][1]["USERPROFILE"], self.home_of("school"))
        self.go(None, cwd=self.home)
        self.assertEqual(self.calls[-1][1]["USERPROFILE"], self.home_of("default"))
        self.assertIn("no account mapped", self.err.getvalue())
        accounts.set_mapping(self.work, "gone", "x@y.z")
        self.go(None, cwd=self.work)
        self.assertEqual(self.calls[-1][1]["USERPROFILE"], self.home_of("default"))
        self.assertIn("no longer exists", self.err.getvalue())

    def test_bare_run_without_active_account_fails(self):
        self.store = DictStore()
        with self.assertRaises(ConfigError):
            self.go(None)
        self.assertEqual(self.calls, [])

    def test_require_session(self):
        with self.assertRaises(ConfigError):
            self.go("default", require_session=True)
        self.go("school", require_session=True)
        self.ran()

    def test_bad_login_files_are_refused(self):
        paths.token_path("school").write_bytes(b"")
        with self.assertRaises(AgyswapError):
            self.go("school")
        paths.token_path("school").write_bytes(token_blob("other@x.com"))
        with self.assertRaisesRegex(AgyswapError, "belongs to other@x.com"):
            self.go("school")
        paths.token_path("school").unlink()
        with self.assertRaisesRegex(AgyswapError, "login --slot 2"):
            self.go("school")
        self.assertEqual(self.calls, [])

    def test_api_key_account(self):
        data = accounts.load_sequence()
        data["accounts"]["3"] = {"email": "api-key-3@agyswap.local", "folder": "key3", "kind": "apikey"}
        accounts.save_sequence(data)
        with self.assertRaisesRegex(ConfigError, "add-token --slot 3"):
            self.go("3")
        self.store.write(paths.apikey_target("key3"), b"AIzaTEST", username=paths.APIKEY_USERNAME)
        self.go("3")
        _, env = self.ran()
        self.assertEqual(env["GEMINI_API_KEY"], "AIzaTEST")
        self.assertEqual(env["USERPROFILE"], self.home_of("key3"))
        self.assertEqual(env["SSH_CLIENT"], paths.SSH_ENV["SSH_CLIENT"])
        self.assertTrue(paths.profile_home("key3").is_dir())

    def test_share_flags_are_accepted_but_share_nothing(self):
        self.go("school", share_history=True)
        self.assertIn("not implemented yet", self.err.getvalue())
        self.assertEqual({p.name for p in paths.profile_home("school").iterdir()}, {".gemini"})

    def test_active_login_is_synced_to_its_folder_first(self):
        newer = token_blob("me@gmail.com", secret="NEWER", expiry="2026-11-22T12:00:00+00:00")
        self.store = DictStore(newer)
        self.go("1")
        self.assertEqual(paths.token_path("default").read_bytes(), newer)
        self.assertEqual(self.store.writes, [])


class PidFileTest(Base):
    def pid_file(self):
        return paths.run_pid_file("school")

    def test_pid_file_exists_during_the_run_and_is_removed_after(self):
        seen = []
        self.launcher = lambda argv, env: seen.append(json.loads(self.pid_file().read_text(encoding="utf-8"))) or 0
        self.go("school")
        self.assertEqual(seen[0]["pid"], os.getpid())
        self.assertEqual(seen[0]["started"], procs.process_started(os.getpid()))
        self.assertFalse(self.pid_file().exists())

    def test_live_run_of_the_same_account_warns_and_keeps_its_file(self):
        sleeper = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
        self.addCleanup(sleeper.wait)
        self.addCleanup(sleeper.kill)
        record = {"pid": sleeper.pid, "started": procs.process_started(sleeper.pid)}
        self.pid_file().write_text(json.dumps(record), encoding="utf-8")
        self.go("school")
        self.ran()
        self.assertIn(f"PID {sleeper.pid}", self.err.getvalue())
        self.assertEqual(json.loads(self.pid_file().read_text(encoding="utf-8"))["pid"], sleeper.pid)

    def test_reused_pid_counts_as_gone(self):
        # cswap #508: same PID, different creation time means Windows gave the PID to another process
        self.pid_file().write_text(json.dumps({"pid": os.getpid(), "started": 1}), encoding="utf-8")
        self.go("school")
        self.assertNotIn("in progress", self.err.getvalue())
        self.assertFalse(self.pid_file().exists())

    def test_process_started(self):
        self.assertIsInstance(procs.process_started(os.getpid()), int)
        done = subprocess.run([sys.executable, "-c", "pass"])
        self.assertEqual(done.returncode, 0)
        for pid in (0, -1, True, "1", 2 ** 40):
            self.assertIsNone(procs.process_started(pid))


class AutoTest(Base):
    def fetcher(self, default, school):
        texts = {str(self.real_home): default, self.home_of("school"): school}

        def fetch(env):
            self.fetched.append(env["USERPROFILE"])
            return usage.classify(texts[env["USERPROFILE"]])
        return fetch

    def target(self):
        return self.ran()[1]["USERPROFILE"]

    def test_gemini_exhausted_moves_to_next_account(self):
        self.go(None, ["-p", "q"], auto=True, fetcher=self.fetcher(usage_text(0), usage_text(40)))
        self.assertEqual(self.target(), self.home_of("school"))
        self.assertIn("this run uses Account-2", self.err.getvalue())
        self.assertNotIn("no account mapped", self.err.getvalue())
        self.assertEqual(self.store.writes, [])  # never a global switch
        self.assertEqual(accounts.load_sequence()["activeAccountNumber"], 1)

    def test_headroom_stays_and_queries_only_the_start(self):
        self.go(None, auto=True, fetcher=self.fetcher(usage_text(10), usage_text(0)))
        self.assertEqual(self.target(), self.home_of("default"))
        self.assertEqual(self.fetched, [str(self.real_home)])

    def test_model_class_comes_from_agy_arguments(self):
        f = self.fetcher(usage_text(gemini_left=0, claude_left=30), usage_text())
        self.go(None, ["--model", "claude-sonnet-4-6"], auto=True, fetcher=f)
        self.assertEqual(self.target(), self.home_of("default"))
        self.calls.clear()
        UsageCache().forget("default")  # otherwise the cached answer above is served
        f = self.fetcher(usage_text(gemini_left=30, claude_left=0), usage_text())
        self.go(None, ["--model=gpt-oss-120b"], auto=True, fetcher=f)
        self.assertEqual(self.target(), self.home_of("school"))

    def test_argon_limit_counts_for_argon_only(self):
        f = self.fetcher(usage_text(gemini_left=50, argon_left=0), usage_text())
        self.go(None, ["--model", "gemini-4-argon-high"], auto=True, fetcher=f)
        self.assertEqual(self.target(), self.home_of("school"))
        self.calls.clear()
        self.go(None, ["--model", "gemini-3.8-flash"], auto=True, fetcher=f)
        self.assertEqual(self.target(), self.home_of("default"))

    def test_named_start_and_wraparound(self):
        self.go("school", auto=True, fetcher=self.fetcher(usage_text(30), usage_text(0)))
        self.assertEqual(self.target(), self.home_of("default"))

    def test_all_exhausted_runs_the_start_anyway(self):
        self.go(None, auto=True, fetcher=self.fetcher(usage_text(0), usage_text(0)))
        self.assertEqual(self.target(), self.home_of("default"))
        self.assertIn("no account has Gemini quota left", self.err.getvalue())

    def test_unknown_usage_is_not_ruled_out(self):
        self.go(None, auto=True, fetcher=lambda env: usage.FetchResult(error="timeout"))
        self.assertEqual(self.target(), self.home_of("default"))

    def test_skipped_candidates(self):
        data = accounts.load_sequence()
        data["accounts"]["2"]["disabled"] = True
        data["accounts"]["3"] = {"email": "api-key-3@agyswap.local", "folder": "key3", "kind": "apikey"}
        accounts.save_sequence(data)
        self.store.write(paths.apikey_target("key3"), b"AIzaTEST", username=paths.APIKEY_USERNAME)
        f = self.fetcher(usage_text(0), usage_text(40))
        self.go(None, auto=True, fetcher=f)
        self.assertEqual(self.target(), self.home_of("default"))  # school disabled, API keys not included
        self.calls.clear()
        settings.set_value("autoswitch.includeApiKeyAccounts", "true")
        self.go(None, auto=True, fetcher=f)
        self.assertEqual(self.target(), self.home_of("key3"))

    def test_relogin_required_candidate_is_skipped(self):
        data = accounts.load_sequence()
        data["accounts"]["3"] = {"email": "me@work.com", "folder": "work"}
        accounts.save_sequence(data)
        self.write_token("work", "me@work.com")
        texts = {str(self.real_home): usage_text(0), self.home_of("school"): "Authentication required. Visit https://x\n",
                 self.home_of("work"): usage_text(20)}
        self.go(None, auto=True, fetcher=lambda env: usage.classify(texts[env["USERPROFILE"]]))
        self.assertEqual(self.target(), self.home_of("work"))


PERSONAL = ["gemini-4-argon-high", "claude-opus-5-5-high", "claude-sonnet-4-6", "gemini-3.8-flash-high"]
SCHOOL = ["gemini-3.8-flash-high", "claude-sonnet-4-6", "gpt-oss-120b-medium"]
OPUS = ["--model", "claude-opus-5-5-high"]


class ModelAutoTest(Base):
    """run --auto with a model named: an account whose model list lacks it is skipped. Lists come from fakes, never from agy."""
    fetcher = AutoTest.fetcher
    target = AutoTest.target

    def lists(self, default, school):
        texts = {self.home_of("default"): default, self.home_of("school"): school}  # always the account folder, even for the active account

        def fetch(env):
            self.listed.append(env["USERPROFILE"])
            return texts[env["USERPROFILE"]]
        return fetch

    def setUp(self):
        super().setUp()
        self.listed = []

    def auto(self, args, default_left, school_left, default_models=PERSONAL, school_models=SCHOOL, **kw):
        return self.go(None, args, auto=True, fetcher=self.fetcher(usage_text(claude_left=default_left, gemini_left=default_left),
                                                                  usage_text(claude_left=school_left, gemini_left=school_left)),
                       models_fetcher=self.lists(default_models, school_models), **kw)

    def test_candidate_without_the_model_is_skipped_and_start_runs_anyway(self):
        self.auto(OPUS, 0, 50)  # the personal quota is gone, but school has no Opus 5.5
        self.assertEqual(self.target(), self.home_of("default"))
        err = self.err.getvalue()
        self.assertIn("Account-2 (school) does not offer claude-opus-5-5-high; skipping it", err)
        self.assertIn("no account that offers claude-opus-5-5-high has quota left; running Account-1 (default) anyway", err)
        self.assertNotIn("this run uses", err)

    def test_candidate_with_the_model_is_still_used(self):
        self.auto(["--model", "claude-sonnet-4-6"], 0, 50)
        self.assertEqual(self.target(), self.home_of("school"))
        self.assertNotIn("does not offer", self.err.getvalue())

    def test_start_without_the_model_is_skipped(self):
        self.auto(["--model", "gpt-oss-120b-medium"], 50, 50, default_models=PERSONAL)
        self.assertEqual(self.target(), self.home_of("school"))
        err = self.err.getvalue()
        self.assertIn("Account-1 (default) does not offer gpt-oss-120b-medium; skipping it", err)
        self.assertIn("Account-1 (default) does not offer it; this run uses Account-2 (school)", err)
        self.assertNotIn("quota left", err)

    def test_model_match_is_exact_but_not_case_sensitive(self):
        self.auto(["--model=CLAUDE-OPUS-5-5-HIGH"], 50, 50)
        self.assertEqual(self.target(), self.home_of("default"))
        self.calls.clear()
        self.auto(["--model", "claude-opus-5-5"], 50, 50)  # a prefix of an id is not an id
        self.assertEqual(self.target(), self.home_of("default"))
        self.assertIn("Account-1 (default) does not offer claude-opus-5-5; skipping it", self.err.getvalue())

    def test_unknown_list_never_rules_an_account_out(self):
        self.go(None, OPUS, auto=True, fetcher=self.fetcher(usage_text(claude_left=0), usage_text(claude_left=40)),
                models_fetcher=lambda env: None)
        self.assertEqual(self.target(), self.home_of("school"))
        self.assertNotIn("does not offer", self.err.getvalue())
        self.assertEqual(models.ModelCache().get("school"), None)  # a failed fetch is not remembered as "offers nothing"
        self.calls.clear()
        self.go(None, OPUS, auto=True, fetcher=self.fetcher(usage_text(claude_left=0), usage_text(claude_left=40)),
                models_fetcher=lambda env: [])
        self.assertEqual(self.target(), self.home_of("school"))
        self.assertIsNone(models.ModelCache().get("default"))

    def test_no_explicit_model_asks_for_no_list(self):
        def boom(env):
            raise AssertionError("model lists must not be fetched")
        for args in ([], ["-p", "q"]):
            self.calls.clear()
            self.go(None, args, auto=True, fetcher=self.fetcher(usage_text(40), usage_text(40)), models_fetcher=boom)
            self.assertEqual(self.target(), self.home_of("default"))

    def test_lists_are_fetched_only_for_accounts_considered_and_cached(self):
        self.auto(OPUS, 50, 50)
        self.assertEqual(self.listed, [self.home_of("default")])  # the start account fit, so school was never looked at
        self.calls.clear()
        self.auto(OPUS, 50, 50)
        self.assertEqual(self.listed, [self.home_of("default")])  # served from the cache the second time
        self.assertEqual(models.ModelCache().get("default"), PERSONAL)
        self.assertIsNone(models.ModelCache().get("school"))

    def test_skipped_accounts_are_not_asked_either(self):
        data = accounts.load_sequence()
        data["accounts"]["2"]["disabled"] = True
        accounts.save_sequence(data)
        self.auto(OPUS, 0, 50)
        self.assertEqual(self.listed, [self.home_of("default")])


class ModelListTest(Base):
    OUT = "claude-opus-5-5-high\tClaude Opus 5.5 (High)\ngpt-oss-120b-medium\tGPT-OSS 120B (Medium)\n"

    def test_parse_takes_ids_from_tab_separated_lines_only(self):
        text = "Fetching available models...\n" + self.OUT + "Authentication required\nbad id\twith space\n\t\tempty\n"
        self.assertEqual(models.parse(text), ["claude-opus-5-5-high", "gpt-oss-120b-medium"])
        self.assertEqual(models.parse(""), [])
        self.assertEqual(models.parse("Fetching available models...\n"), [])

    def test_fetch_runs_agy_models_and_treats_failures_as_unknown(self):
        with mock.patch.object(usage, "capture", return_value=("Fetching available models...\n" + self.OUT, False)) as cap:
            self.assertEqual(models.fetch({"A": "1"}), ["claude-opus-5-5-high", "gpt-oss-120b-medium"])
        self.assertEqual(cap.call_args.args[:2], ({"A": "1"}, ["models"]))
        for captured in (None, (self.OUT, True), ("Authentication required\n", False), ("", False)):
            with mock.patch.object(usage, "capture", return_value=captured):
                self.assertIsNone(models.fetch({}), captured)

    def test_cache_expires_after_a_day(self):
        now = [1000.0]
        cache = models.ModelCache(clock=lambda: now[0])
        cache.put("default", ["a"])
        cache.put("school", ["b"])
        self.assertEqual((cache.get("default"), cache.get("school")), (["a"], ["b"]))
        now[0] += models.TTL_S - 1
        self.assertEqual(cache.get("default"), ["a"])
        now[0] += 2
        self.assertIsNone(cache.get("default"))
        paths.models_cache_file().write_text('{"accounts": {"x": {"fetchedAt": 1, "models": 5}, "y": 3}}', encoding="utf-8")
        self.assertEqual((cache.get("x"), cache.get("y"), cache.get("z")), (None, None, None))

    def test_known_fetches_with_the_account_folder_environment(self):
        seen = []
        ids = models.known("school", fetcher=lambda env: seen.append(env) or ["m"])
        self.assertEqual(ids, ["m"])
        self.assertEqual((seen[0]["USERPROFILE"], seen[0]["SSH_CLIENT"]), (self.home_of("school"), paths.SSH_ENV["SSH_CLIENT"]))
        self.assertEqual(models.known("school", fetcher=lambda env: self.fail("cached")), ["m"])


class CliRunTest(Base):
    def test_stdout_carries_only_agy_output(self):
        # cswap #337: a fake agy prints JSON; agyswap's own notices must stay on stderr
        payload = '{"answer": "ok"}'
        with tempfile.TemporaryFile() as captured, \
                mock.patch.dict(os.environ, {"AGY_BIN": sys.executable}), \
                mock.patch("agyswap.run.WinCred", return_value=self.store), \
                mock.patch("os.getcwd", return_value=str(self.work)):
            out = io.StringIO()
            sys.stdout.flush()
            saved = os.dup(1)
            os.dup2(captured.fileno(), 1)
            try:
                with contextlib.redirect_stdout(out):
                    code = cli.dispatch(["run", "school", "--", "-c", f"print({payload!r})"])
            finally:
                os.dup2(saved, 1)
                os.close(saved)
            captured.seek(0)
            child = captured.read().decode("utf-8")
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(child), {"answer": "ok"})
        self.assertEqual(out.getvalue(), "")
        self.assertIn("running Account-2", self.err.getvalue())

    def test_missing_agy_is_a_clean_error(self):
        with mock.patch("agyswap.run.WinCred", return_value=self.store), \
                mock.patch("os.getcwd", return_value=str(self.work)):
            self.assertEqual(cli.dispatch(["run", "school"]), 1)
        self.assertIn("Could not start agy", self.err.getvalue())

    def test_options_and_retired_profile(self):
        with mock.patch("agyswap.run.run", return_value=5) as fake:
            self.assertEqual(cli.dispatch(["run", "--auto", "--no-share", "--", "-p", "x"]), 5)
        args, kwargs = fake.call_args
        self.assertEqual(args, (None, ["-p", "x"]))
        self.assertTrue(kwargs["auto"])
        self.assertEqual(cli.dispatch(["run", "--profile", "school", "--", "-p", "x"]), 1)


class MapTest(Base):
    def cmd(self, argv):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = cli.dispatch(argv)
        return code, out.getvalue()

    def test_map_list_unmap(self):
        code, out = self.cmd(["map", "school", str(self.work)])
        self.assertEqual(code, 0)
        shown = accounts.normalize_path(self.work)
        self.assertIn(f"Mapped {shown} → Account-2 (me@school.edu)", out)
        _, out = self.cmd(["map", "1", str(self.work)])
        self.assertIn("(was me@school.edu)", out)
        _, out = self.cmd(["map"])
        self.assertIn(f"{shown} → 1: me@gmail.com", out)
        _, out = self.cmd(["unmap", str(self.work)])
        self.assertIn(f"Unmapped {shown}", out)
        _, out = self.cmd(["unmap", str(self.work)])
        self.assertIn("No mapping for", out)
        _, out = self.cmd(["map"])
        self.assertIn("No directory mappings yet", out)

    def test_map_current_directory_missing_path_and_removed_account(self):
        with mock.patch("os.getcwd", return_value=str(self.work)):
            self.cmd(["map", "me@school.edu"])
        self.assertEqual(accounts.resolve_mapping(self.work)[1]["folder"], "school")
        _, out = self.cmd(["map", "school", str(self.home / "nope")])
        self.assertIn("not an existing directory", out)
        accounts.set_mapping(self.home / "old", "gone", "x@y.z")
        _, out = self.cmd(["map"])
        self.assertIn("x@y.z (account removed)", out)
        self.assertEqual(self.cmd(["map", "9", str(self.work)])[0], 1)
