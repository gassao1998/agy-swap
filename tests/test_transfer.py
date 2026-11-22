"""export, import and import-usage: the export file's permissions and content, validation before any write, and rollback."""
import base64
import contextlib
import io
import json
import os
import re
import shutil
import time
from unittest import mock

from agyswap import accounts, fsutil, migrate, output, paths, transfer
from agyswap.errors import AgyswapError, TransferError
from agyswap.state import collect, load_context
from agyswap.usage import FetchResult, parse_usage
from agyswap.usage_store import Entry, UsageCache
from tests.helpers import SECRET, TempHome, dacl_sddl, live, process_sid, token_blob
from tests.test_cli import run
from tests.test_usage import FREE

OLD, NEW = "2026-11-22T10:00:00+08:00", "2026-11-22T11:00:00+08:00"
SID = process_sid()


class Base(TempHome):
    def setUp(self):
        super().setUp()
        self.write_token("default", "me@gmail.com", expiry=OLD)
        self.write_token("school", "me@school.edu", expiry=NEW)
        accounts.save_sequence({"activeAccountNumber": 1, "accounts": {
            "1": {"email": "me@gmail.com", "alias": "default", "folder": "default", "added": "2026-11-01T00:00:00Z"},
            "2": {"email": "me@school.edu", "alias": "school", "folder": "school", "threshold": 80.0}}})
        migrate.migrate(lambda: live())
        p = mock.patch("agyswap.state.read_live_login", return_value=live("me@gmail.com", token_blob("me@gmail.com", expiry=OLD)))
        p.start()
        self.addCleanup(p.stop)
        # import must never construct a Credential Manager client
        for name in ("agyswap.manage.WinCred", "agyswap.switch.WinCred"):
            p = mock.patch(name, side_effect=AssertionError("Credential Manager touched"))
            p.start()
            self.addCleanup(p.stop)
        p = mock.patch("agyswap.fsutil.current_user_sid", return_value=SID)  # the export file is really created private, for this user
        p.start()
        self.addCleanup(p.stop)
        self.out = self.home / "out" / "accounts.json"
        self.out.parent.mkdir()

    def export(self, *argv):
        return run(["export", str(self.out), *argv])

    def envelope(self):
        return json.loads(self.out.read_text(encoding="utf-8"))


class ExportTest(Base):
    def test_file_is_created_private_and_holds_logins(self):
        made = []
        real = fsutil.create_private_file

        def spy(folder, prefix, data):
            made.append(real(folder, prefix, data))
            return made[-1]

        with mock.patch("agyswap.fsutil.create_private_file", spy):
            code, out, err = self.export()
        self.assertEqual(code, 0, err)
        self.assertEqual(made[0].parent, self.out.parent)
        sddl = dacl_sddl(self.out)  # the rename kept the DACL the file was created with
        self.assertTrue(sddl.startswith("D:P"), sddl)
        self.assertEqual(re.findall(r"\([^)]*\)", sddl), [f"(A;;FA;;;{SID})"])
        self.assertEqual(out, "")
        self.assertNotIn(SECRET, err)
        self.assertIn("plain text", err)
        env = self.envelope()
        self.assertEqual((env["formatVersion"], env["encrypted"], env["activeAccountNumber"]), (1, False, 1))
        one, two = env["accounts"]
        self.assertEqual(base64.b64decode(one["login"]), paths.token_path("default").read_bytes())
        self.assertEqual((one["alias"], two["alias"], two["threshold"]), ("default", "school", 80.0))
        self.assertNotIn("settings", one)
        self.assertEqual([p.name for p in self.out.parent.iterdir()], ["accounts.json"])  # no temp file left behind

    def test_full_adds_agy_settings(self):
        transfer.settings_path("school").write_text('{"theme": "dark"}', encoding="utf-8")
        self.assertEqual(self.export("--full")[0], 0)
        two = self.envelope()["accounts"][1]
        self.assertEqual(json.loads(base64.b64decode(two["settings"])), {"theme": "dark"})

    def test_refuses_directories_existing_files_and_failed_restriction(self):
        code, _, err = run(["export", str(self.out.parent)])
        self.assertEqual(code, 1)
        self.assertIn("not a directory", err)
        self.out.write_text("keep", encoding="utf-8")
        code, _, err = self.export()
        self.assertEqual((code, self.out.read_text(encoding="utf-8")), (1, "keep"))
        self.assertIn("--force", err)
        self.assertEqual(self.export("--force")[0], 0)
        self.assertEqual(self.envelope()["formatVersion"], 1)
        self.out.unlink()
        with mock.patch("agyswap.fsutil.current_user_sid", return_value=None):
            code, _, err = self.export()
        self.assertEqual(code, 1)
        self.assertEqual(list(self.out.parent.iterdir()), [])

    def test_a_temp_file_that_cannot_be_deleted_is_named(self):
        with mock.patch("agyswap.transfer.os.rename", side_effect=PermissionError(13, "denied")), \
                mock.patch("pathlib.Path.unlink", side_effect=PermissionError(13, "denied")):
            code, _, err = self.export()
        self.assertEqual(code, 1)
        left = [p for p in self.out.parent.iterdir()]
        self.assertEqual(len(left), 1)
        self.assertIn(f"could not delete {left[0]}", err)
        self.assertIn("delete it by hand", err)

    def test_stdout_export_warns_on_stderr_and_refuses_a_terminal(self):
        code, out, err = run(["export", "-"])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["formatVersion"], 1)
        self.assertIn("plain text", err)
        tty = io.StringIO()
        tty.isatty = lambda: True
        with contextlib.redirect_stdout(tty), contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(TransferError):
                transfer.export_accounts("-")
        self.assertEqual(tty.getvalue(), "")

    def test_mintty_pty_pipe_counts_as_a_terminal(self):
        names = {r"\msys-1888ae32e00d56aa-pty0-to-master": True, r"\cygwin-1888ae32e00d56aa-pty3-from-master": True,
                 r"\msys-1888ae32e00d56aa-pipe-nt-0x1a2b": False, None: False}
        for name, refused in names.items():
            with mock.patch("agyswap.transfer._pipe_name", return_value=name):
                code, out, err = run(["export", "-"])
            self.assertEqual((code, out == ""), (1, True) if refused else (0, False), name)
        read, write = os.pipe()
        with open(write, "wb") as w, open(read, "rb"):
            self.assertFalse(transfer._PTY_PIPE_RE.search(transfer._pipe_name(w) or ""))  # an anonymous pipe is not a pty

    def test_skips_unusable_accounts_and_api_keys(self):
        data = accounts.load_sequence()
        data["accounts"]["3"] = {"email": "api-key-3@agyswap.local", "folder": "slot3", "kind": "apikey"}
        accounts.save_sequence(data)
        paths.token_path("school").write_bytes(token_blob("someone@else.com"))
        code, _, err = self.export()
        self.assertEqual(code, 0)
        self.assertEqual([a["number"] for a in self.envelope()["accounts"]], [1])
        self.assertIn("belongs to someone@else.com", err)
        self.assertIn("API key accounts are not exported", err)
        self.out.unlink()
        code, _, err = self.export("--account", "school")
        self.assertEqual(code, 1)
        self.assertFalse(self.out.exists())

    def test_active_login_is_synced_before_export(self):
        newer = token_blob("me@gmail.com", secret="FRESH", expiry=NEW)
        with mock.patch("agyswap.state.read_live_login", return_value=live("me@gmail.com", newer)):
            self.assertEqual(self.export("--account", "1")[0], 0)
        self.assertEqual(base64.b64decode(self.envelope()["accounts"][0]["login"]), newer)


class ImportTest(Base):
    def exported(self, **changes):
        self.assertEqual(self.export()[0], 0)
        env = self.envelope()
        env.update(changes)
        self.out.write_text(json.dumps(env), encoding="utf-8")
        return env

    def test_roundtrip_after_remove_keeps_active_untouched(self):
        self.exported()
        data = accounts.load_sequence()
        del data["accounts"]["2"]
        accounts.save_sequence(data)
        before = paths.token_path("school").read_bytes()
        shutil.rmtree(paths.profile_home("school"))
        code, _, err = run(["import", str(self.out)])
        self.assertEqual(code, 0, err)
        self.assertIn("1 imported, 0 overwritten, 1 skipped", err)
        rec = accounts.load_sequence()["accounts"]["2"]
        self.assertEqual((rec["email"], rec["alias"], rec["threshold"]), ("me@school.edu", "school", 80.0))
        self.assertEqual(paths.token_path(rec["folder"]).read_bytes(), before)
        self.assertEqual(accounts.load_sequence()["activeAccountNumber"], 1)

    def test_into_a_fresh_home_does_not_activate(self):
        self.exported()
        shutil.rmtree(paths.profiles_dir())
        paths.sequence_file().unlink()
        with mock.patch("agyswap.manage.read_live_login", return_value=live()):  # the migration of a fresh home reads it once
            code, _, err = run(["import", str(self.out)])
        self.assertEqual(code, 0, err)
        data = accounts.load_sequence()
        self.assertEqual(accounts.numbers(data), ["1", "2"])
        self.assertIsNone(data["activeAccountNumber"])

    def test_heals_missing_and_relogin_accounts_skips_healthy(self):
        self.exported()
        paths.token_path("default").unlink()
        UsageCache().record_failure("school", "auth", relogin=True)
        UsageCache().record_failure("default", "timeout")
        code, _, err = run(["import", str(self.out)])
        self.assertEqual(code, 0, err)
        self.assertIn("2 replaced", err)
        self.assertIn("switch 1 --force", err)  # the active account's folder was rewritten
        self.assertTrue(paths.token_path("default").exists())
        self.assertFalse(UsageCache().entry("school").relogin)
        self.assertIsNone(UsageCache().entry("default").last_error)
        code, _, err = run(["import", str(self.out)])
        self.assertIn("0 imported, 0 overwritten, 2 skipped", err)
        code, _, err = run(["import", str(self.out), "--force"])
        self.assertIn("2 overwritten", err)

    def test_force_keeps_a_newer_local_login(self):
        self.exported()
        newest = token_blob("me@school.edu", secret="NEWEST", expiry="2026-11-23T11:00:00+08:00")
        paths.token_path("school").write_bytes(newest)
        code, _, err = run(["import", str(self.out), "--force"])
        self.assertEqual(code, 0)
        self.assertIn("1 kept", err)
        self.assertEqual(paths.token_path("school").read_bytes(), newest)

    def test_agy_settings_are_written_only_with_include_settings(self):
        transfer.settings_path("school").write_text('{"mcpServers": {"x": {"command": "calc.exe"}}, "theme": "dark"}', encoding="utf-8")
        self.assertEqual(self.export("--full")[0], 0)
        for flags, written in (([], False), (["--include-settings"], True)):
            data = accounts.load_sequence()
            data["accounts"].pop("2", None)
            accounts.save_sequence(data)
            shutil.rmtree(paths.profiles_dir() / "school", ignore_errors=True)
            code, _, err = run(["import", str(self.out), *flags])
            self.assertEqual(code, 0, err)
            folder = accounts.load_sequence()["accounts"]["2"]["folder"]
            self.assertEqual(transfer.settings_path(folder).exists(), written, flags)
            if written:
                self.assertIn('me@school.edu: "mcpServers", "theme"', err)
            else:
                self.assertIn("pass --include-settings", err)
        self.assertEqual(run(["export", str(self.out), "--include-settings"])[0], 2)

    def test_a_kept_newer_login_leaves_the_record_and_folder_alone(self):
        # every roster save stamps lastUpdated to the second, so freeze the stamp or the byte comparison below flakes on a second boundary
        self.enterContext(mock.patch("agyswap.accounts.timestamp", return_value="2026-11-22T00:00:00Z"))
        accounts.save_sequence(accounts.load_sequence())
        transfer.settings_path("school").write_text('{"theme": "dark"}', encoding="utf-8")
        env = self.exported()
        transfer.settings_path("school").unlink()
        env["accounts"][1].update(disabled=True, priority=5, settings=base64.b64encode(b'{"hooks": {}}').decode())
        self.out.write_text(json.dumps(env), encoding="utf-8")
        newest = token_blob("me@school.edu", secret="NEWEST", expiry="2026-11-23T11:00:00+08:00")
        paths.token_path("school").write_bytes(newest)
        before = paths.sequence_file().read_bytes()
        code, _, err = run(["import", str(self.out), "--force", "--include-settings"])
        self.assertEqual(code, 0, err)
        self.assertIn("1 kept", err)
        self.assertEqual(paths.sequence_file().read_bytes(), before)  # no disabled or priority from the file
        self.assertFalse(transfer.settings_path("school").exists())

    def test_any_invalid_account_means_nothing_is_written(self):
        env = self.exported()
        env["accounts"].append(dict(env["accounts"][0], number=7, email="x@y.z", alias=None))
        self.out.write_text(json.dumps(env), encoding="utf-8")
        before = paths.sequence_file().read_bytes()
        for broken in (dict(env["accounts"][1], login="not base64!"),
                       dict(env["accounts"][1], email="other@school.edu"),
                       dict(env["accounts"][1], login=base64.b64encode(token_blob("me@school.edu", secret="")).decode()),
                       dict(env["accounts"][1], threshold=5)):
            env2 = dict(env, accounts=[{**env["accounts"][0], "email": "new@a.b",
                                        "login": base64.b64encode(token_blob("new@a.b")).decode(), "alias": "new"}, broken])
            self.out.write_text(json.dumps(env2), encoding="utf-8")
            code, _, err = run(["import", str(self.out)])
            self.assertEqual(code, 1, broken)
            self.assertEqual(paths.sequence_file().read_bytes(), before)
            self.assertFalse(paths.profile_home("new").exists())
        for bad in ({"formatVersion": 2, "accounts": []}, {"version": 1, "accounts": [{}]},
                    {"formatVersion": 1, "encrypted": True, "accounts": [{}]}):
            self.out.write_text(json.dumps(bad), encoding="utf-8")
            self.assertEqual(run(["import", str(self.out)])[0], 1)
        self.assertIn("not a directory", run(["import", str(self.out.parent)])[2])

    def test_hostile_files_fail_cleanly(self):
        env = self.exported()
        good = env["accounts"][1]
        before = paths.sequence_file().read_bytes()
        cases = {"nested": "[" * 100000 + "]" * 100000,
                 "slot": json.dumps(dict(env, accounts=[dict(good, number=10 ** 50)])),
                 "threshold": json.dumps(dict(env, accounts=[dict(good, threshold=10 ** 400)])),
                 "email": json.dumps(dict(env, accounts=[dict(good, email="me@school.edu\x1b]0;x\x07")])),
                 "added": json.dumps(dict(env, accounts=[dict(good, added="2026\r\nDone")])),
                 "alias": json.dumps(dict(env, accounts=[dict(good, alias="a" * 300)])),
                 "settings": json.dumps(dict(env, accounts=[dict(good, settings=base64.b64encode(b"[" * 100000).decode())])),
                 "rows": json.dumps(dict(env, accounts=[good] * (transfer.MAX_ROWS + 1)))}
        for name, text in cases.items():
            self.out.write_text(text, encoding="utf-8")
            code, _, err = run(["import", str(self.out)])
            self.assertEqual(code, 1, name)
            self.assertNotIn("Traceback", err, name)
            self.assertNotIn("\x1b", err, name)
        self.assertEqual(paths.sequence_file().read_bytes(), before)
        with mock.patch("agyswap.transfer.MAX_INPUT_BYTES", 100):
            self.assertIn("larger than", run(["import", str(self.out)])[2])
        self.out.unlink()
        self.exported()
        paths.token_path("school").unlink()
        with mock.patch("agyswap.transfer._write_account", side_effect=OSError(22, "The filename is too long", "x")):
            code, _, err = run(["import", str(self.out)])
        self.assertEqual(code, 1)
        self.assertIn("too long", err)

    def test_failure_midway_rolls_back(self):
        env = self.exported()
        env["accounts"] = [{**env["accounts"][0], "number": 3, "email": "a@a.a", "alias": "a",
                            "login": base64.b64encode(token_blob("a@a.a")).decode()},
                           {**env["accounts"][0], "number": 4, "email": "b@b.b", "alias": "b",
                            "login": base64.b64encode(token_blob("b@b.b")).decode()}]
        self.out.write_text(json.dumps(env), encoding="utf-8")
        real = transfer.store_login
        calls = []

        def flaky(folder, blob):
            calls.append(folder)
            if len(calls) == 2:
                raise AgyswapError("disk full")
            return real(folder, blob)

        before = paths.sequence_file().read_bytes()
        with mock.patch("agyswap.transfer.store_login", flaky):
            self.assertEqual(run(["import", str(self.out)])[0], 1)
        self.assertEqual(paths.sequence_file().read_bytes(), before)
        self.assertFalse(paths.profile_home("a").exists())
        self.assertFalse(paths.profile_home("b").exists())

    def test_alias_clash_is_dropped_and_stdin_works(self):
        env = self.exported()
        env["accounts"] = [{**env["accounts"][0], "number": 3, "email": "a@a.a", "alias": "school",
                            "login": base64.b64encode(token_blob("a@a.a")).decode()}]
        stdin = io.TextIOWrapper(io.BytesIO(json.dumps(env).encode("utf-8")), encoding="utf-8")
        with mock.patch("sys.stdin", stdin):
            code, _, err = run(["import", "-"])
        self.assertEqual(code, 0, err)
        self.assertIn("dropping the imported alias", err)
        rec = accounts.load_sequence()["accounts"]["3"]
        self.assertNotIn("alias", rec)
        self.assertEqual(rec["folder"], "slot3")


class ImportUsageTest(Base):
    def doc(self, age=10.0, status="ok"):
        now = time.time()
        entry = Entry(last_good=parse_usage(FREE), fetched_at=now, age_s=0.0)
        row = output.account_row(2, accounts.load_sequence()["accounts"]["2"], False, entry, now)
        row.update(usageStatus=status, usageAgeSeconds=age)
        other = dict(row, email="stranger@x.y")
        self.out.write_text(json.dumps({"schemaVersion": 1, "accounts": [row, other]}), encoding="utf-8")

    def test_adopts_newer_keeps_own_and_holds(self):
        self.doc()
        code, _, err = run(["import-usage", str(self.out), "--hold", "600"])
        self.assertEqual(code, 0, err)
        self.assertIn("1 adopted, 0 kept, 1 skipped", err)
        entry = UsageCache().entry("school")
        self.assertEqual(entry.last_good["seven_day"]["pct"], 7.0)
        self.assertTrue(entry.in_backoff(entry.fetched_at + 500))
        ctx = load_context(quiet=True)
        calls = []
        later = entry.fetched_at + 400  # past the serve period, inside the hold
        collect(ctx, ["2"], fetcher=lambda env: calls.append(env) or FetchResult(error="parse"), now=later)
        self.assertEqual(calls, [])
        self.doc(age=5000)
        code, _, err = run(["import-usage", str(self.out), "--hold", "0"])
        self.assertIn("0 adopted, 1 kept", err)
        self.assertIn("holds lifted", err)
        self.assertIsNone(UsageCache().entry("school").hold_until)

    def test_rejects_bad_documents(self):
        for bad in ({"schemaVersion": 2, "accounts": []}, {"schemaVersion": 1},
                    {"schemaVersion": 1, "accounts": [{"email": "me@school.edu", "usageStatus": "ok", "usageAgeSeconds": -1,
                                                       "usage": {}}]},
                    {"schemaVersion": 1, "accounts": [{"email": "me@school.edu", "usageStatus": "ok", "usageAgeSeconds": 1,
                                                       "usage": {"scoped": [{"name": "x", "window": "9d", "pct": 1}]}}]}):
            self.out.write_text(json.dumps(bad), encoding="utf-8")
            self.assertEqual(run(["import-usage", str(self.out)])[0], 1, bad)
        self.assertEqual(UsageCache().entry("school").last_good, None)

    def test_rejects_hostile_values_without_a_traceback(self):
        self.doc()
        doc = json.loads(self.out.read_text(encoding="utf-8"))
        row = doc["accounts"][0]
        window = row["usage"]["scoped"][0]
        for change in ({"usageAgeSeconds": 10 ** 400}, {"usageAgeSeconds": 1e20}, {"email": "me@school.edu\x1b[2J"},
                       {"usage": dict(row["usage"], scoped=[dict(window, pct=10 ** 400)])},
                       {"usage": dict(row["usage"], scoped=[dict(window, resetsAt="soon")])},
                       {"usage": dict(row["usage"], scoped=[window] * (transfer.MAX_WINDOWS + 1))}):
            self.out.write_text(json.dumps(dict(doc, accounts=[dict(row, **change)])), encoding="utf-8")
            code, _, err = run(["import-usage", str(self.out)])
            self.assertEqual(code, 1, change)
            self.assertNotIn("Traceback", err)
            self.assertNotIn("\x1b", err)
        self.out.write_text('{"schemaVersion": 1, "accounts": ' + "[" * 100000 + "]" * 100000 + "}", encoding="utf-8")
        self.assertIn("nested too deeply", run(["import-usage", str(self.out)])[2])
        self.assertEqual(UsageCache().entry("school").last_good, None)


if __name__ == "__main__":
    import unittest
    unittest.main()
