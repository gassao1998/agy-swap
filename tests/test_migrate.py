import json
from unittest import mock

from agyswap import accounts, migrate, paths
from agyswap.errors import AgyswapError
from tests.helpers import TempHome, live, token_blob


class MigrateTest(TempHome):
    def old_layout(self):
        self.write_token("school", "me@school.edu")
        (self.home / "usage.json").write_text('{"school": {"t": 1}}', encoding="utf-8")
        (self.home / "active").write_text("school", encoding="utf-8")

    def run_migrate(self, login):
        calls = []

        def reader():
            calls.append(1)
            return login
        return migrate.migrate(reader), calls

    def test_fresh_install_does_nothing(self):
        notes, calls = self.run_migrate(live("me@gmail.com"))
        self.assertEqual(notes, [])
        self.assertFalse(paths.sequence_file().exists())
        self.assertEqual(calls, [])

    def test_school_in_keyring_writes_no_login(self):
        self.old_layout()
        notes, _ = self.run_migrate(live("me@school.edu"))
        data = accounts.load_sequence()
        self.assertEqual(data["accounts"]["1"]["folder"], "default")
        self.assertEqual(data["accounts"]["1"]["email"], "slot-1@agyswap.local")
        self.assertEqual(data["accounts"]["2"], {**data["accounts"]["2"], "alias": "school", "folder": "school",
                                                 "email": "me@school.edu"})
        self.assertEqual(data["activeAccountNumber"], 2)
        self.assertFalse(paths.token_path("default").exists())
        self.assertFalse((self.home / "usage.json").exists())
        self.assertFalse((self.home / "active").exists())
        self.assertTrue(any("will be filled in once the personal account is logged back in" in n for n in notes))
        done = json.loads(paths.migrations_file().read_text(encoding="utf-8"))
        self.assertNotIn("v1-default-login", done)
        # Run again: same result, and the note is not repeated
        notes2, _ = self.run_migrate(live("me@school.edu"))
        self.assertEqual(notes2, [])
        self.assertEqual(accounts.load_sequence()["accounts"], data["accounts"])

    def test_personal_login_is_adopted_once(self):
        self.old_layout()
        blob = token_blob("me@gmail.com")
        notes, _ = self.run_migrate(live(blob=blob))
        data = accounts.load_sequence()
        self.assertEqual(paths.token_path("default").read_bytes(), blob)
        self.assertEqual(data["accounts"]["1"]["email"], "me@gmail.com")
        self.assertEqual(data["activeAccountNumber"], 1)
        self.assertTrue(any("as the default login file" in n for n in notes))
        snapshot = (paths.sequence_file().read_text(encoding="utf-8"), paths.token_path("default").read_bytes())
        notes2, calls = self.run_migrate(live(blob=token_blob("me@gmail.com", secret="NEWER")))
        self.assertEqual(notes2, [])
        self.assertEqual(calls, [])  # once everything is done Credential Manager is no longer read
        self.assertEqual(paths.token_path("default").read_bytes(), snapshot[1])

    def test_existing_default_login_is_not_overwritten(self):
        self.old_layout()
        path = self.write_token("default", "me@gmail.com")
        before = path.read_bytes()
        self.run_migrate(live(blob=token_blob("me@gmail.com", secret="OTHER")))
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(accounts.load_sequence()["accounts"]["1"]["email"], "me@gmail.com")

    def test_folder_is_restricted_before_login_is_written(self):
        self.old_layout()
        order = []
        real_write = migrate.atomic_write_bytes

        def restrict(path):
            order.append(("restrict", path.name, paths.token_path("default").exists()))
            return True

        def write(path, data):
            order.append(("write", path.name))
            real_write(path, data)

        with mock.patch("agyswap.fsutil.restrict_to_current_user", restrict), \
                mock.patch("agyswap.migrate.atomic_write_bytes", write):
            self.run_migrate(live("me@gmail.com"))
        self.assertEqual(order[:2], [("restrict", "default", False), ("write", "antigravity-oauth-token")])

    def test_restrict_failure_writes_nothing(self):
        self.old_layout()
        with mock.patch("agyswap.fsutil.restrict_to_current_user", return_value=False):
            notes, _ = self.run_migrate(live("me@gmail.com"))
        self.assertFalse(paths.token_path("default").exists())
        self.assertTrue(any("Could not restrict" in n for n in notes))
        self.assertNotIn("v1-default-login", json.loads(paths.migrations_file().read_text(encoding="utf-8")))

    def test_wiped_or_partial_login_is_never_written(self):
        self.old_layout()
        for blob in (b"", b"{}", b'{"id_token_email": "me@gmail.com"}',
                     token_blob("me@gmail.com", secret=""),
                     b'{"id_token_email": "me@gmail.com", "token": {"access_token": "a", "refresh_token": " "}}'):
            self.run_migrate(live(blob=blob) if blob else live(blob=b"x"))
            self.assertFalse(paths.token_path("default").exists(), blob)

    def test_unknown_other_account_blocks_adoption(self):
        (self.home / "profiles" / "school").mkdir(parents=True)  # school's login file cannot be read, so its email is a placeholder
        notes, _ = self.run_migrate(live("me@gmail.com"))
        self.assertFalse(paths.token_path("default").exists())
        self.assertTrue(any("cannot be ruled out" in n for n in notes))

    def test_recorded_other_active_blocks_adoption(self):
        self.old_layout()
        self.run_migrate(live(error="error code 5"))
        data = accounts.load_sequence()
        data["activeAccountNumber"] = 2
        accounts.save_sequence(data)
        self.run_migrate(live("me@gmail.com"))
        self.assertFalse(paths.token_path("default").exists())

    def test_uppercase_default_folder_is_the_same_folder(self):
        self.write_token("Default", "me@gmail.com")
        self.write_token("school", "me@school.edu")
        self.run_migrate(live("me@gmail.com"))
        folders = [r["folder"] for r in accounts.load_sequence()["accounts"].values()]
        self.assertEqual(folders, ["default", "school"])

    def test_lock_only_when_work_remains(self):
        self.old_layout()
        with mock.patch("agyswap.migrate.file_lock", wraps=migrate.file_lock) as lock:
            self.run_migrate(live("me@gmail.com"))
            self.assertEqual(lock.call_count, 1)
            self.run_migrate(live("me@gmail.com"))
            self.assertEqual(lock.call_count, 1)

    def test_existing_data_folder_is_locked_down_once(self):
        accounts.save_sequence(accounts.new_sequence())
        paths.migrations_file().write_text(json.dumps({s: "x" for s in migrate.STEPS if s != "v1-root-acl"}),
                                           encoding="utf-8")
        seen = []
        with mock.patch("agyswap.migrate.restrict_to_current_user", lambda p, **k: seen.append((p, k)) or True):
            self.run_migrate(live("me@gmail.com"))
            self.run_migrate(live("me@gmail.com"))
        self.assertEqual(seen, [(self.home, {"children": False})])
        paths.migrations_file().unlink()
        with mock.patch("agyswap.migrate.restrict_to_current_user", side_effect=AgyswapError("owned by another")), \
                self.assertRaises(AgyswapError):
            self.run_migrate(live("me@gmail.com"))

    def test_keyring_unreadable_or_empty_leaves_step_pending(self):
        self.old_layout()
        for login in (live(error="CredReadW failed, error code 5"), live()):
            self.run_migrate(login)
            self.assertFalse(paths.token_path("default").exists())
        self.assertIsNone(accounts.load_sequence()["activeAccountNumber"])
        self.run_migrate(live("me@gmail.com"))
        self.assertTrue(paths.token_path("default").exists())
