"""Account management commands: add, login, remove, disable, enable, alias, threshold, priority, swap, move."""
import _winapi
import contextlib
import io
import tempfile
from pathlib import Path
from unittest import mock

from agyswap import accounts, cli, manage, migrate, paths
from agyswap.errors import AccountNotFoundError, AgyswapError, ConfigError
from agyswap.usage_store import UsageCache
from tests.helpers import FakeStore, TempHome, live, token_blob

OLD, NEW = "2026-11-22T10:00:00+08:00", "2026-11-22T11:00:00+08:00"


class Base(TempHome):
    def setUp(self):
        super().setUp()
        self.write_token("default", "me@gmail.com", expiry=OLD)
        self.write_token("school", "me@school.edu", expiry=NEW)
        self.data = {"activeAccountNumber": 1, "accounts": {
            "1": {"email": "me@gmail.com", "alias": "default", "folder": "default"},
            "2": {"email": "me@school.edu", "alias": "school", "folder": "school"}}}
        accounts.save_sequence(self.data)
        migrate.migrate(lambda: live())  # finish the migration first, so later commands no longer read Credential Manager
        self.enterContext(contextlib.redirect_stderr(io.StringIO()))

    def roster(self):
        return accounts.load_sequence()

    def text(self, msgs):
        return "\n".join(t for _, t in msgs)


class AddTest(Base):
    def test_new_account_gets_folder_alias_and_becomes_active(self):
        store = FakeStore(token_blob("me@work.com"))
        msgs = manage.add(store=store)
        rec = self.roster()["accounts"]["3"]
        self.assertEqual((rec["email"], rec["folder"], rec["alias"]), ("me@work.com", "slot3", "slot3"))
        self.assertEqual(self.roster()["activeAccountNumber"], 3)
        self.assertEqual(paths.token_path("slot3").read_bytes(), store.cred.blob)
        self.assertIn("Added Account 3", self.text(msgs))
        self.assertEqual(store.writes, [])  # add only reads Credential Manager

    def test_alias_names_the_folder_and_conflicts_are_refused(self):
        manage.add(alias="Work", store=FakeStore(token_blob("me@work.com")))
        self.assertEqual(self.roster()["accounts"]["3"]["folder"], "work")
        with self.assertRaises(ConfigError):
            manage.add(alias="school", store=FakeStore(token_blob("x@y.com")))

    def test_existing_account_is_refreshed_and_keeps_settings(self):
        accounts_data = self.roster()
        accounts_data["accounts"]["1"].update(threshold=85.0, priority=3)
        accounts.save_sequence(accounts_data)
        newer = token_blob("me@gmail.com", secret="NEWER", expiry=NEW)
        msgs = manage.add(store=FakeStore(newer))
        self.assertIn("Updated the login of Account 1", self.text(msgs))
        self.assertEqual(paths.token_path("default").read_bytes(), newer)
        self.assertEqual(self.roster()["accounts"]["1"]["threshold"], 85.0)

    def test_foreign_expired_login_never_lands_in_recorded_account(self):
        before = {f: paths.token_path(f).read_bytes() for f in ("default", "school")}
        msgs = manage.add(store=FakeStore(token_blob("me@school.edu", secret="EXPIRED", expiry=OLD)))
        self.assertIn("keeping the folder copy", self.text(msgs))
        self.assertEqual({f: paths.token_path(f).read_bytes() for f in before}, before)
        self.assertEqual(self.roster()["activeAccountNumber"], 2)

    def test_folder_holding_another_email_is_refused(self):
        self.write_token("default", "other@else.com")
        before = paths.token_path("default").read_bytes()
        with self.assertRaises(AgyswapError) as e:
            manage.add(store=FakeStore(token_blob("me@gmail.com", secret="N", expiry=NEW)))
        self.assertIn("another email", str(e.exception))
        self.assertEqual(paths.token_path("default").read_bytes(), before)
        self.assertEqual(self.roster()["accounts"]["1"]["email"], "me@gmail.com")

    def test_login_swapped_midway_rolls_back(self):
        store = FakeStore(token_blob("me@work.com"))
        reads = iter([store.cred, FakeStore(token_blob("me@other.com")).cred])
        store.read = lambda target: next(reads)
        with self.assertRaises(AgyswapError) as e:
            manage.add(store=store)
        self.assertIn("was replaced while it was being taken in", str(e.exception))
        self.assertFalse(paths.profile_home("slot3").exists())
        self.assertNotIn("3", self.roster()["accounts"])

    def test_slot_moves_same_account_with_its_settings(self):
        data = self.roster()
        data["accounts"]["2"].update(disabled=True, threshold=85.0, priority=5)
        accounts.save_sequence(data)
        manage.add(slot=5, store=FakeStore(token_blob("me@school.edu", expiry=NEW)))
        rec = self.roster()["accounts"]["5"]
        self.assertNotIn("2", self.roster()["accounts"])
        self.assertEqual((rec["folder"], rec["alias"], rec["disabled"], rec["threshold"], rec["priority"]),
                         ("school", "school", True, 85.0, 5))

    def test_refusals(self):
        with self.assertRaises(ConfigError):
            manage.add(slot=2, store=FakeStore(token_blob("me@work.com")))  # slot 2 belongs to someone else
        with self.assertRaises(ConfigError) as e:
            manage.add(store=FakeStore())
        self.assertIn("agyswap login", str(e.exception))
        with self.assertRaises(AgyswapError):
            manage.add(store=FakeStore(token_blob("me@work.com", secret="")))
        self.assertEqual(set(self.roster()["accounts"]), {"1", "2"})


class LoginTest(Base):
    def runner(self, email, **kwargs):
        def run(home):
            path = Path(home) / paths.TOKEN_REL
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(token_blob(email, **kwargs))
            return 0
        return run

    def test_new_account_never_touches_credential_store(self):
        with mock.patch("agyswap.credman._real_api", side_effect=AssertionError("touched Credential Manager")):
            msgs = manage.login(alias="work", runner=self.runner("me@work.com"))
        rec = self.roster()["accounts"]["3"]
        self.assertEqual((rec["email"], rec["folder"], rec["alias"]), ("me@work.com", "work", "work"))
        self.assertEqual(self.roster()["activeAccountNumber"], 1)
        self.assertIn("Added Account 3", self.text(msgs))

    def test_failed_or_duplicate_login_removes_new_folder(self):
        with self.assertRaises(AgyswapError):
            manage.login(runner=lambda home: 0)
        with self.assertRaises(ConfigError) as e:
            manage.login(runner=self.runner("me@school.edu"))
        self.assertIn("login --slot 2", str(e.exception))
        self.assertFalse(paths.profile_home("slot3").exists())
        self.assertNotIn("3", self.roster()["accounts"])

    def test_relogin_wrong_account_restores_previous(self):
        before = paths.token_path("school").read_bytes()
        with self.assertRaises(AgyswapError) as e:
            manage.login(slot=2, runner=self.runner("me@gmail.com"))
        self.assertIn("was restored", str(e.exception))
        self.assertEqual(paths.token_path("school").read_bytes(), before)
        msgs = manage.login(slot=2, runner=self.runner("me@school.edu", secret="FRESH"))
        self.assertIn("Logged in again as Account 2", self.text(msgs))
        self.assertEqual(manage.login(slot=2, runner=lambda home: 0)[0][1], "The login file of Account-2 did not change")


class RemoveTest(Base):
    def test_remove_deletes_folder_record_mapping_and_cache(self):
        accounts.set_mapping(self.home, "school", "me@school.edu")
        UsageCache().record_failure("school", "timeout")
        msgs = manage.remove("school", confirm=lambda prompt: True)
        self.assertIn("Removed Account-2", self.text(msgs))
        self.assertNotIn("2", self.roster()["accounts"])
        self.assertFalse(paths.profile_home("school").exists())
        self.assertEqual(accounts.load_mappings(), {})
        self.assertIsNone(UsageCache().entry("school").last_error)
        self.assertEqual([p.name for p in paths.profiles_dir().iterdir()], ["default"])

    def test_junction_target_survives(self):
        with tempfile.TemporaryDirectory() as outside:
            keep = Path(outside) / "keep.txt"
            keep.write_text("a file in a real home folder", encoding="utf-8")
            _winapi.CreateJunction(outside, str(paths.profile_home("school") / "shared"))
            manage.remove("2", confirm=lambda prompt: True)
            self.assertTrue(keep.exists())
            self.assertFalse(paths.profile_home("school").exists())

    def test_cancel_and_non_tty(self):
        with mock.patch("sys.stdin") as stdin:
            stdin.isatty.return_value = False
            self.assertEqual(manage.remove("2"), [("dim", "Cancelled")])
        self.assertIn("2", self.roster()["accounts"])
        self.assertTrue(paths.token_path("school").exists())

    def test_roster_write_failure_puts_folder_back(self):
        with mock.patch("agyswap.accounts.atomic_write_json", side_effect=OSError("disk full")), \
                self.assertRaises(OSError):
            manage.remove("2", confirm=lambda prompt: True)
        self.assertTrue(paths.token_path("school").exists())
        self.assertIn("2", self.roster()["accounts"])

    def test_pre_switch_backup_of_the_removed_account_is_deleted(self):
        last = paths.last_live_file()
        last.parent.mkdir(parents=True, exist_ok=True)
        last.write_bytes(token_blob("ME@gmail.com"))
        manage.remove("2", confirm=lambda prompt: True)
        self.assertTrue(last.exists())  # it belongs to another account
        manage.remove("1", confirm=lambda prompt: True)
        self.assertFalse(last.exists())

    def test_active_account_warns(self):
        msgs = manage.remove("1", confirm=lambda prompt: True)
        self.assertIn("the Credential Manager login is untouched", self.text(msgs))
        self.assertIsNone(self.roster()["activeAccountNumber"])


class SettingsTest(Base):
    def test_disable_enable_with_every_identifier_form(self):
        for ident in ("2", "school", "ME@SCHOOL.EDU"):
            self.assertIn("Disabled Account-2", self.text(manage.set_disabled(ident, True)))
            self.assertIn("is already disabled", self.text(manage.set_disabled(ident, True)))
            self.assertIn("Enabled", self.text(manage.set_disabled(ident, False)))
        manage.set_disabled("2", True)
        self.assertIn("No accounts remain in rotation", self.text(manage.set_disabled("1", True)))
        with self.assertRaises(AccountNotFoundError):
            manage.set_disabled("nobody", True)

    def test_alias(self):
        manage.set_alias("2", "uni")
        self.assertEqual(self.roster()["accounts"]["2"]["alias"], "uni")
        self.assertEqual(self.roster()["accounts"]["2"]["folder"], "school")
        with self.assertRaises(ConfigError):
            manage.set_alias("uni", "default")
        with self.assertRaises(ConfigError):
            manage.set_alias("uni", "123")
        manage.set_alias("me@school.edu", None)
        self.assertNotIn("alias", self.roster()["accounts"]["2"])
        self.assertIn("1: default", self.text(manage.list_aliases()))

    def test_threshold_and_priority(self):
        self.assertIn("85%", self.text(manage.set_threshold("school", "85")))
        self.assertIn("is already at", self.text(manage.set_threshold("2", "85")))
        with self.assertRaises(ConfigError):
            manage.set_threshold("2", "30")
        self.assertIn("Account-2  85%", self.text(manage.list_thresholds()))
        manage.set_threshold("2", None)
        self.assertNotIn("threshold", self.roster()["accounts"]["2"])
        manage.set_priority("2", "7")
        self.assertEqual(self.roster()["accounts"]["2"]["priority"], 7)
        manage.set_priority("2", None)
        self.assertNotIn("priority", self.roster()["accounts"]["2"])
        with self.assertRaises(ConfigError):
            manage.set_priority("2", "1001")

    def test_swap_and_move(self):
        manage.swap("default", "school")
        data = self.roster()
        self.assertEqual((data["accounts"]["1"]["folder"], data["activeAccountNumber"]), ("school", 2))
        with self.assertRaises(ConfigError):
            manage.swap("1", "school")
        self.assertIn("to slot 7", self.text(manage.move("default", "7")))
        data = self.roster()
        self.assertEqual((sorted(data["accounts"]), data["activeAccountNumber"]), (["1", "7"], 7))
        self.assertIn("Swapped", self.text(manage.move("7", "1")))
        self.assertIn("Already in slot", self.text(manage.move("1", "1")))
        for bad in ("0", "abc", "100"):
            with self.assertRaises(ConfigError):
                manage.move("1", bad)


class CliManageTest(Base):
    def run_cli(self, argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                code = cli.dispatch(argv)
            except SystemExit as e:
                code = e.code
        return code, out.getvalue(), err.getvalue()

    def test_argument_checks(self):
        for argv in (["alias", "2", "x", "--unset"], ["alias", "--unset"], ["threshold", "2"], ["priority", "2"]):
            self.assertEqual(self.run_cli(argv)[0], 2, argv)

    def test_commands_print_after_success(self):
        code, out, _ = self.run_cli(["disable", "school"])
        self.assertEqual((code, "Disabled Account-2" in out), (0, True))
        code, out, _ = self.run_cli(["threshold"])
        self.assertIn("global default  90%", out)
        code, out, _ = self.run_cli(["swap", "1", "2"])
        self.assertIn("Swapped Account 1 and Account 2", out)
        code, _, err = self.run_cli(["alias", "9", "x"])
        self.assertEqual(code, 1)
        self.assertIn("Account-9 does not exist", err)

    def test_remove_from_non_tty_is_cancelled(self):
        with mock.patch("sys.stdin") as stdin:
            stdin.isatty.return_value = False
            code, out, _ = self.run_cli(["remove", "2"])
        self.assertEqual((code, "Cancelled" in out), (0, True))
        self.assertIn("2", self.roster()["accounts"])

    def test_switch_text_mode_adds_unmanaged_login(self):
        store = FakeStore(token_blob("me@work.com"))
        with mock.patch("agyswap.switch.WinCred", return_value=store), \
                mock.patch("agyswap.manage.WinCred", return_value=store), \
                mock.patch("agyswap.procs.running", return_value={}):
            code, out, _ = self.run_cli(["switch"])
        self.assertEqual(code, 0)
        self.assertIn("automatically added", out)
        self.assertEqual(self.roster()["accounts"]["3"]["email"], "me@work.com")
