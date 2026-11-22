"""add-token: Gemini API keys in Credential Manager entries of their own, and remove cleaning them up."""
import contextlib
import io
from unittest import mock

from agyswap import accounts, manage, migrate, paths
from agyswap.errors import AgyswapError, ConfigError
from tests.helpers import DictStore, TempHome, live


class AddTokenTest(TempHome):
    def setUp(self):
        super().setUp()
        self.write_token("default", "me@gmail.com")
        accounts.save_sequence({"activeAccountNumber": 1, "accounts": {
            "1": {"email": "me@gmail.com", "alias": "default", "folder": "default"}}})
        migrate.migrate(lambda: live())
        self.store = DictStore()
        self.enterContext(contextlib.redirect_stderr(io.StringIO()))

    def add(self, token="AIzaKEY1", **kw):
        return manage.add_token(token, store=self.store, **kw)

    def record(self, num):
        return accounts.load_sequence()["accounts"][num]

    def test_new_key_account(self):
        msgs = self.add()
        rec = self.record("2")
        self.assertEqual((rec["email"], rec["folder"], rec["kind"], rec["alias"]),
                         ("api-key-2@agyswap.local", "slot2", "apikey", "slot2"))
        self.assertEqual(self.store.items["agyswap:apikey:slot2"].blob, b"AIzaKEY1")
        self.assertNotIn(paths.KEYRING_TARGET, self.store.writes)  # the global login is never touched
        self.assertTrue(paths.profile_home("slot2").is_dir())
        self.assertEqual(accounts.load_sequence()["activeAccountNumber"], 1)
        self.assertIn("Experimental", "\n".join(t for _, t in msgs))

    def test_same_slot_or_email_replaces_the_key_and_keeps_settings(self):
        self.add(email="ci@example.com")
        data = accounts.load_sequence()
        data["accounts"]["2"].update(alias="ci", priority=4)
        accounts.save_sequence(data)
        msgs = self.add("AIzaKEY2", email="CI@example.com")
        self.assertIn("Updated API key for Account 2", msgs[0][1])
        self.assertEqual(self.store.items["agyswap:apikey:slot2"].blob, b"AIzaKEY2")
        self.assertEqual((self.record("2")["alias"], self.record("2")["priority"]), ("ci", 4))
        self.add("AIzaKEY3", slot=2)
        self.assertEqual(self.store.items["agyswap:apikey:slot2"].blob, b"AIzaKEY3")
        self.assertEqual(self.record("2")["email"], "CI@example.com")

    def test_refusals(self):
        with self.assertRaisesRegex(ConfigError, "login account"):
            self.add(slot=1)
        with self.assertRaisesRegex(ConfigError, "login account"):
            self.add(email="me@gmail.com")
        for bad in ("   ", "two words", "AIza\x07"):
            with self.assertRaises(ConfigError):
                self.add(bad)
        with self.assertRaises(ConfigError):
            self.add(email="not-an-email")
        self.add(email="a@b.c")
        with self.assertRaisesRegex(ConfigError, "already Account-2"):
            self.add(slot=5, email="a@b.c")
        self.assertEqual(sorted(accounts.load_sequence()["accounts"]), ["1", "2"])

    def test_key_from_stdin_and_no_prompt_without_terminal(self):
        with mock.patch("sys.stdin", io.StringIO("AIzaFROMSTDIN\n")):
            self.add("-")
        self.assertEqual(self.store.items["agyswap:apikey:slot2"].blob, b"AIzaFROMSTDIN")
        with mock.patch("sys.stdin", io.StringIO("")), mock.patch("getpass.getpass") as prompt:
            with self.assertRaisesRegex(ConfigError, "not running in a terminal"):
                self.add("")
        prompt.assert_not_called()

    def test_failed_write_leaves_nothing_behind(self):
        self.store.fail_writes = 1
        with self.assertRaises(AgyswapError):
            self.add()
        self.assertNotIn("2", accounts.load_sequence()["accounts"])
        self.assertFalse(paths.profile_home("slot2").exists())
        self.assertEqual(self.store.items, {})

    def test_failed_roster_write_restores_the_previous_key(self):
        self.add()
        with mock.patch("agyswap.accounts.save_sequence", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                self.add("AIzaNEW", slot=2)
        self.assertEqual(self.store.items["agyswap:apikey:slot2"].blob, b"AIzaKEY1")
        self.assertTrue(paths.profile_home("slot2").is_dir())

    def test_remove_deletes_the_stored_key(self):
        self.add()
        manage.remove("2", confirm=lambda prompt: True, store=self.store)
        self.assertEqual(self.store.items, {})
        self.assertFalse(paths.profile_home("slot2").exists())
