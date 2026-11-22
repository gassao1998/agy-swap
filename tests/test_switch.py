"""Global switch: backup, save-back, write, read-back and restore, the rules for choosing a target, and the JSON shape. Credential Manager is always a FakeStore."""
import contextlib
import io
import json
import shutil
import unittest
from unittest import mock

from agyswap import accounts, cli, paths, switch
from agyswap.errors import AgyswapError, ConfigError, CredentialError
from agyswap.fsutil import file_lock, sha256_hex
from agyswap.state import load_context
from agyswap.usage import FetchResult, parse_usage
from agyswap.usage_store import UsageCache
from tests.helpers import SECRET, FakeStore, TempHome, live, token_blob

OLD, NEW = "2026-11-22T10:00:00+08:00", "2026-11-22T11:00:00+08:00"
KEYS = {"schemaVersion", "switched", "from", "to", "strategy", "reason", "message", "warnings"}


def usage_text(gemini_left: float, claude_left: float = 100) -> str:
    return (f"Gemini Models\tWeekly Limit Remaining\t{gemini_left}%\t2026-11-28T10:45:55Z\n"
            f"Claude and GPT models\tWeekly Limit Remaining\t{claude_left}%\t2026-11-29T03:50:47Z\n")


def fetcher(live_text: str, others: dict[str, str]):
    def fetch(env):
        if "SSH_CONNECTION" not in env:
            return FetchResult(usage=parse_usage(live_text))
        for folder, text in others.items():
            if env["USERPROFILE"].endswith(folder):
                return FetchResult(usage=parse_usage(text)) if text else FetchResult(error="timeout")
        return FetchResult(error="parse")
    return fetch


class Base(TempHome):
    def setUp(self):
        super().setUp()
        self.write_token("default", "me@gmail.com", expiry=OLD)
        self.write_token("school", "me@school.edu", secret="SCHOOL")
        self.data = {"activeAccountNumber": 1, "accounts": {
            "1": {"email": "me@gmail.com", "alias": "default", "folder": "default"},
            "2": {"email": "me@school.edu", "alias": "school", "folder": "school"}}}
        accounts.save_sequence(self.data)
        p = mock.patch("agyswap.procs.running", return_value={})
        p.start()
        self.addCleanup(p.stop)
        self.enterContext(contextlib.redirect_stderr(io.StringIO()))

    def token(self, folder):
        return paths.token_path(folder).read_bytes()

    def active(self):
        return accounts.load_sequence()["activeAccountNumber"]

    def unsaved(self) -> list:
        return sorted(paths.backup_dir().glob("unsaved-*.bin"))

    def assert_kept(self, blob: bytes, text: str):
        """The login was saved nowhere else: it must be in a backup no later switch overwrites, and the message must point there."""
        hits = [p for p in self.unsaved() if p.read_bytes() == blob]
        self.assertTrue(hits, "no unsaved backup holds the login")
        self.assertIn(str(hits[-1]), text)
        self.assertNotIn(str(paths.last_live_file()), text)

    def assert_nothing_written(self, store, before_active=1):
        self.assertEqual(store.writes, [])
        self.assertEqual(self.active(), before_active)
        self.assertFalse(paths.pending_switch_file().exists())


class SwitchToTest(Base):
    def test_backup_save_back_write_and_roster(self):
        live = token_blob("me@gmail.com", secret="REFRESHED", expiry=NEW)
        store = FakeStore(live)
        seen = []
        store.on_write = lambda: seen.append(paths.pending_switch_file().exists())
        old_default = self.token("default")
        payload = switch.switch_to("school", store=store)
        self.assertEqual(store.cred.blob, self.token("school"))
        self.assertEqual(paths.last_live_file().read_bytes(), live)
        self.assertEqual(self.token("default"), live)  # the refreshed copy is saved back to default
        bak = paths.token_path("default").with_name("antigravity-oauth-token.bak")
        self.assertEqual(bak.read_bytes(), old_default)
        self.assertEqual(seen, [True])  # pending already exists at write time
        self.assertFalse(paths.pending_switch_file().exists())
        self.assertEqual(self.active(), 2)
        self.assertEqual(set(payload), KEYS)
        self.assertEqual((payload["switched"], payload["reason"], payload["strategy"]), (True, "switched", "direct"))
        self.assertEqual(payload["from"], {"number": 1, "email": "me@gmail.com"})
        self.assertEqual(payload["to"], {"number": 2, "email": "me@school.edu"})
        self.assertNotIn(SECRET, json.dumps(payload))

    def test_identifier_forms(self):
        for ident in ("2", "school", "ME@SCHOOL.EDU"):
            store = FakeStore(token_blob("me@gmail.com", expiry=OLD))
            self.data["activeAccountNumber"] = 1
            accounts.save_sequence(self.data)
            self.assertEqual(switch.switch_to(ident, store=store)["to"]["number"], 2, ident)

    def test_backup_failure_blocks_even_with_force(self):
        for patch, force in (("agyswap.switch.atomic_write_bytes", False), ("agyswap.switch.atomic_write_bytes", True)):
            store = FakeStore(token_blob("me@gmail.com", expiry=OLD))
            with mock.patch(patch, side_effect=OSError(28, "disk full")), self.assertRaises(AgyswapError) as e:
                switch.switch_to("school", force=force, store=store)
            self.assertIn("Backing up the current login failed", str(e.exception))
            self.assert_nothing_written(store)
        store = FakeStore(token_blob("me@gmail.com", expiry=OLD))
        with mock.patch("agyswap.switch.read_bytes", return_value=b"other"), self.assertRaises(AgyswapError):
            switch.switch_to("school", force=True, store=store)
        self.assert_nothing_written(store)

    def test_save_back_failure_blocks(self):
        store = FakeStore(token_blob("me@gmail.com", secret="N", expiry=NEW))
        with mock.patch("agyswap.state.replace_keep_backup", side_effect=AgyswapError("read-back mismatch")), \
                self.assertRaises(AgyswapError) as e:
            switch.switch_to("school", store=store)
        self.assertIn("Saving the current login back", str(e.exception))
        self.assert_nothing_written(store)

    def test_save_back_refused_for_foreign_or_missing_folder(self):
        store = FakeStore(token_blob("me@gmail.com", secret="N", expiry=NEW))
        self.write_token("default", "other@else.com")
        before = self.token("default")
        with self.assertRaises(AgyswapError) as e:
            switch.switch_to("school", store=store)
        self.assertIn("holds the login of another email", str(e.exception))
        self.assertEqual(self.token("default"), before)
        self.assert_nothing_written(store)
        shutil.rmtree(paths.profile_home("default"))
        with self.assertRaises(AgyswapError) as e:
            switch.switch_to("school", store=store)
        self.assertIn("does not exist", str(e.exception))
        self.assertFalse(paths.profile_home("default").exists())  # never recreated
        self.assert_nothing_written(store)

    def test_wiped_live_login_needs_force(self):
        before = self.token("default")
        wiped = token_blob("me@gmail.com", secret="")
        store = FakeStore(wiped)
        with self.assertRaises(AgyswapError):
            switch.switch_to("school", store=store)
        self.assert_nothing_written(store)
        payload = switch.switch_to("school", force=True, store=store)
        self.assertEqual(payload["reason"], "switched")
        self.assert_kept(wiped, " ".join(payload["warnings"]))
        self.assertEqual(self.token("default"), before)  # --force does not save back

    def test_unknown_identity_refused_unless_force(self):
        before = {f: self.token(f) for f in ("default", "school")}
        store = FakeStore(token_blob("stranger@else.com"))
        with self.assertRaises(AgyswapError) as e:
            switch.switch_to("school", store=store)
        self.assertIn("Cannot tell which account", str(e.exception))
        self.assert_nothing_written(store)
        stranger = store.cred.blob
        payload = switch.switch_to("school", force=True, store=store)
        self.assertEqual(payload["from"], {"number": None, "email": "stranger@else.com"})
        self.assert_kept(stranger, " ".join(payload["warnings"]))
        self.assertEqual({f: self.token(f) for f in before}, before)
        # later switches use last-live.bin and never replace the only copy of the unmanaged login
        switch.switch_to("default", store=store)
        switch.switch_to("school", store=store)
        switch.switch_to("default", store=store)
        self.assertEqual([p.read_bytes() for p in self.unsaved()], [stranger])
        self.assertNotEqual(paths.last_live_file().read_bytes(), stranger)

    def test_extra_top_level_key_aborts_unless_force(self):
        live = json.loads(token_blob("me@gmail.com", expiry=OLD))
        live["new_field"] = 1
        store = FakeStore(json.dumps(live).encode())
        with self.assertRaises(AgyswapError) as e:
            switch.switch_to("school", store=store)
        self.assertIn("new_field", str(e.exception))
        self.assert_nothing_written(store)
        self.assertEqual(switch.switch_to("school", force=True, store=store)["reason"], "switched")

    def test_readback_mismatch_restores(self):
        live = token_blob("me@gmail.com", expiry=OLD)
        store = FakeStore(live)
        store.corrupt_writes = 1
        with self.assertRaises(AgyswapError) as e:
            switch.switch_to("school", store=store)
        self.assertIn("restored from the backup", str(e.exception))
        self.assertEqual(store.cred.blob, live)
        self.assertEqual(self.active(), 1)
        self.assertFalse(paths.pending_switch_file().exists())

    def test_write_error_and_failed_restore_keep_pending(self):
        live = token_blob("me@gmail.com", expiry=OLD)
        store = FakeStore(live)
        store.fail_writes = 2
        with self.assertRaises(AgyswapError) as e:
            switch.switch_to("school", store=store)
        self.assertIn("restore failed too", str(e.exception))
        self.assert_kept(live, str(e.exception))
        self.assertTrue(paths.pending_switch_file().exists())
        self.assertEqual(self.active(), 1)
        # following the pending hint (switch to the target) settles it and clears the warning
        store.fail_writes = 0
        store._set(self.token("school"))
        payload = switch.switch_to("school", store=store)
        self.assertEqual(payload["reason"], "already-active")
        self.assertFalse(paths.pending_switch_file().exists())
        self.assertEqual(self.active(), 2)
        self.assertEqual(len(self.unsaved()), 1)

    def test_read_error_stops(self):
        store = FakeStore(token_blob("me@gmail.com"))
        load_context(read_live=lambda: live())  # finish the migration first
        store.read_error = 5
        with self.assertRaises(CredentialError):
            switch.switch_to("school", store=store)
        self.assertEqual(store.writes, [])

    def test_target_without_usable_login(self):
        paths.token_path("school").write_bytes(token_blob("me@school.edu", secret=""))
        store = FakeStore(token_blob("me@gmail.com", expiry=OLD))
        with self.assertRaises(AgyswapError):
            switch.switch_to("school", store=store)
        paths.token_path("school").write_bytes(token_blob("other@school.edu"))
        with self.assertRaises(AgyswapError) as e:
            switch.switch_to("school", store=store)
        self.assertIn("other@school.edu", str(e.exception))
        self.assert_nothing_written(store)

    def test_lock_held_refuses(self):
        store = FakeStore(token_blob("me@gmail.com", expiry=OLD))
        load_context(read_live=lambda: live())
        with file_lock(paths.lock_file()), mock.patch("agyswap.fsutil.time.sleep"), \
                self.assertRaises(AgyswapError) as e:
            switch.switch_to("school", store=store)
        self.assertIn("Another agyswap is using", str(e.exception))
        self.assertEqual(store.writes, [])


class PlaceholderTest(Base):
    def test_real_email_is_recorded_unless_another_account_has_it(self):
        self.data["accounts"]["2"]["email"] = accounts.placeholder_email(2)
        accounts.save_sequence(self.data)
        store = FakeStore(token_blob("me@gmail.com", expiry=OLD))
        payload = switch.switch_to("2", store=store)
        self.assertEqual(payload["to"], {"number": 2, "email": "me@school.edu"})
        self.assertEqual(accounts.load_sequence()["accounts"]["2"]["email"], "me@school.edu")
        self.assertEqual(load_context(read_live=lambda: live(blob=store.cred.blob)).live_owner, "2")
        self.assertEqual(switch.switch(store=store)["reason"], "switched")  # no longer seen as unmanaged

        data = accounts.load_sequence()
        data["accounts"]["2"]["email"] = accounts.placeholder_email(2)
        accounts.save_sequence(data)
        self.write_token("school", "me@gmail.com", secret="COPY")  # Account-1's login in Account-2's folder
        store = FakeStore(token_blob("me@gmail.com", expiry=OLD))
        writes = len(store.writes)
        with self.assertRaises(AgyswapError) as e:
            switch.switch_to("2", store=store)
        self.assertIn("which is already Account-1", str(e.exception))
        self.assertEqual(len(store.writes), writes)
        self.assertTrue(accounts.is_placeholder(accounts.load_sequence()["accounts"]["2"]["email"]))


class SelfSwitchTest(Base):
    def test_active_is_a_noop_unless_force(self):
        live = token_blob("me@gmail.com", secret="BROKEN-GLOBAL", expiry=OLD)
        store = FakeStore(live)
        before = self.token("default")
        payload = switch.switch_to("default", store=store)
        self.assertEqual((payload["switched"], payload["reason"]), (False, "already-active"))
        self.assertEqual(payload["from"], payload["to"])
        self.assertEqual(store.writes, [])
        self.assertFalse(paths.last_live_file().exists())
        payload = switch.switch_to("default", force=True, store=store)
        self.assertEqual((payload["switched"], payload["reason"]), (False, "activated"))
        self.assertEqual(store.cred.blob, before)
        self.assertEqual(self.token("default"), before)

    def test_foreign_expired_login_never_written_to_recorded_account(self):
        # The roster says Account-1, Credential Manager holds Account-2 and its token is older than the one in Account-2's folder
        self.write_token("school", "me@school.edu", secret="FOLDER", expiry=NEW)
        expired = token_blob("me@school.edu", secret="EXPIRED", expiry=OLD)
        before = {f: self.token(f) for f in ("default", "school")}
        store = FakeStore(expired)
        payload = switch.switch_to("school", store=store)
        self.assertEqual(payload["reason"], "already-active")
        self.assertEqual(self.active(), 2)  # the roster is settled
        self.assertEqual(store.writes, [])
        store = FakeStore(expired)
        payload = switch.switch_to("default", store=store)
        self.assertEqual(payload["from"]["number"], 2)
        self.assertIn("holds a newer login", " ".join(payload["warnings"]))
        self.assertEqual({f: self.token(f) for f in before}, before)


class RotationTest(Base):
    def add_work(self):
        self.write_token("work", "me@work.com")
        self.data["accounts"]["3"] = {"email": "me@work.com", "alias": "work", "folder": "work"}
        accounts.save_sequence(self.data)

    def test_skips_missing_relogin_and_disabled(self):
        self.add_work()
        paths.token_path("school").unlink()
        payload = switch.switch(store=FakeStore(token_blob("me@gmail.com", expiry=OLD)))
        self.assertEqual((payload["reason"], payload["to"]["number"], payload["strategy"]), ("switched", 3, "rotation"))
        self.assertIn("no login file", payload["warnings"][0])

        self.write_token("school", "me@school.edu")
        UsageCache().record_failure("school", "auth", relogin=True,
                                    cred_sha=sha256_hex(paths.token_path("school").read_bytes()))
        self.data["activeAccountNumber"] = 1
        accounts.save_sequence(self.data)
        payload = switch.switch(store=FakeStore(token_blob("me@gmail.com", expiry=OLD)))
        self.assertEqual(payload["to"]["number"], 3)
        self.assertIn("needs a new login", payload["warnings"][0])

        self.data["accounts"]["3"]["disabled"] = True
        self.data["activeAccountNumber"] = 1
        accounts.save_sequence(self.data)
        payload = switch.switch(store=FakeStore(token_blob("me@gmail.com", expiry=OLD)))
        self.assertEqual(payload["reason"], "no-valid-target")
        self.assertEqual(set(payload), KEYS)

    def test_wraps_around_and_no_live_login(self):
        self.data["activeAccountNumber"] = 2
        accounts.save_sequence(self.data)
        payload = switch.switch(store=FakeStore(token_blob("me@school.edu", secret="SCHOOL")))
        self.assertEqual(payload["to"]["number"], 1)
        store = FakeStore()
        payload = switch.switch(store=store)  # no login: activate the roster's account
        self.assertEqual((payload["switched"], payload["from"], payload["to"]["number"]), (True, None, 1))
        self.assertEqual(store.cred.blob, self.token("default"))

    def test_no_live_login_failed_write_deletes_again(self):
        store = FakeStore()
        store.corrupt_writes = 1
        with self.assertRaises(AgyswapError):
            switch.switch(store=store)
        self.assertIsNone(store.cred)

    def test_unmanaged_and_only_one(self):
        payload = switch.switch(store=FakeStore(token_blob("stranger@else.com")))
        self.assertEqual((payload["reason"], payload["from"]), ("unmanaged-account",
                                                                {"number": None, "email": "stranger@else.com"}))
        del self.data["accounts"]["2"]
        accounts.save_sequence(self.data)
        payload = switch.switch(store=FakeStore(token_blob("me@gmail.com")))
        self.assertEqual((payload["reason"], payload["to"]["number"], payload["switched"]),
                         ("only-one-account", 1, False))

    def test_no_accounts(self):
        accounts.save_sequence(accounts.new_sequence())
        with self.assertRaises(ConfigError):
            switch.switch(store=FakeStore(token_blob("me@gmail.com")))


class StrategyTest(Base):
    def run_best(self, live_left, school_left, models=()):
        UsageCache().path.unlink(missing_ok=True)  # query again every time, ignoring the previous round's cache
        store = FakeStore(token_blob("me@gmail.com", expiry=OLD))
        return switch.switch(strategy="best", models=models, store=store,
                             fetcher=fetcher(usage_text(live_left), {"school": school_left}))

    def test_best(self):
        self.assertEqual(self.run_best(10, None)["reason"], "usage-unavailable")
        self.assertEqual(self.run_best(80, usage_text(60))["reason"], "already-best")
        self.assertEqual(self.run_best(0, usage_text(0))["reason"], "candidates-exhausted")
        payload = self.run_best(10, usage_text(60))
        self.assertEqual((payload["reason"], payload["to"]["number"], payload["strategy"]), ("switched", 2, "best"))

    def test_best_with_model_limits(self):
        store = FakeStore(token_blob("me@gmail.com", expiry=OLD))
        payload = switch.switch(strategy="best", models=("claude",), store=store,
                                fetcher=fetcher(usage_text(90, claude_left=5), {"school": usage_text(50, 80)}))
        self.assertEqual(payload["to"]["number"], 2)
        self.assertEqual(switch.inert_models({"1": parse_usage(usage_text(5))}, ("glaude",)), ["glaude"])
        self.assertTrue(switch.model_matches("Claude and GPT models", "gpt"))
        self.assertFalse(switch.model_matches("Gemini Models", "claude"))

    def test_next_available_skips_exhausted(self):
        store = FakeStore(token_blob("me@gmail.com", expiry=OLD))
        payload = switch.switch(strategy="next-available", store=store,
                                fetcher=fetcher(usage_text(50), {"school": usage_text(0)}))
        self.assertEqual(payload["reason"], "candidates-exhausted")
        self.assertIn("Skipped", payload["warnings"][0])
        self.assertEqual(store.writes, [])

    def test_live_changed_while_choosing(self):
        store = FakeStore(token_blob("me@gmail.com", expiry=OLD))

        def fetch(env):
            store._set(token_blob("me@gmail.com", secret="CHANGED"))
            return FetchResult(usage=parse_usage(usage_text(10 if "SSH_CONNECTION" not in env else 90)))
        with self.assertRaises(AgyswapError) as e:
            switch.switch(strategy="best", store=store, fetcher=fetch)
        self.assertIn("changed while the target was being chosen", str(e.exception))
        self.assertEqual(store.writes, [])


class CliSwitchTest(Base):
    def run_cli(self, argv, store):
        out, err = io.StringIO(), io.StringIO()
        with mock.patch("agyswap.switch.WinCred", return_value=store), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.dispatch(argv)
        return code, out.getvalue(), err.getvalue()

    def test_json_stdout_is_one_object_and_models_keys(self):
        store = FakeStore(token_blob("me@gmail.com", expiry=OLD))
        code, out, err = self.run_cli(["switch", "school", "--json"], store)
        self.assertEqual(code, 0, err)
        self.assertEqual(set(json.loads(out)), KEYS)
        # agy does not exist (AGY_BIN points at a temp location): usage cannot be read, yet models and modelSource must still be included
        code, out, err = self.run_cli(["switch", "--strategy", "best", "--model", "gemini", "--json"],
                                      FakeStore(token_blob("me@school.edu", secret="SCHOOL")))
        payload = json.loads(out)
        self.assertEqual((payload["models"], payload["modelSource"]), (["gemini"], "cli"))

    def test_error_envelope_and_api_key_note(self):
        store = FakeStore(token_blob("stranger@else.com"))
        with mock.patch.dict("os.environ", {"GEMINI_API_KEY": "k"}):
            code, out, err = self.run_cli(["switch", "school", "--json"], store)
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(out)["error"]["type"], "AgyswapError")
        self.assertIn("GEMINI_API_KEY", err)

    def test_state_is_complete_before_printing(self):
        store = FakeStore(token_blob("me@gmail.com", expiry=OLD))
        with mock.patch("agyswap.cli._print_switch", side_effect=OSError("stdout closed")), \
                self.assertRaises(OSError):
            self.run_cli(["switch", "school"], store)
        self.assertEqual(self.active(), 2)
        self.assertFalse(paths.pending_switch_file().exists())

    def test_text_output(self):
        code, out, _ = self.run_cli(["switch", "2"], FakeStore(token_blob("me@gmail.com", expiry=OLD)))
        self.assertEqual(code, 0)
        self.assertIn("Switched to Account-2", out)
        self.assertNotIn(SECRET, out)


if __name__ == "__main__":
    unittest.main()
