"""Retries of atomic writes, ReplaceFileW, the order of permission lockdown, and the encoding and platform-detection rules for all source code."""
import os
import re
import unittest
from pathlib import Path
from unittest import mock

from agyswap import fsutil, paths, procs, usage
from agyswap.errors import AgyswapError, ConfigError
from tests.helpers import TempHome, dacl_sddl, process_sid, set_dacl_sddl

SRC = Path(__file__).resolve().parents[1] / "src" / "agyswap"
ACE_RE = re.compile(r"\([^)]*\)")


def flaky(real, failures, winerror=32):
    """The first `failures` calls raise the kind of temporary lock error antivirus or an indexer causes; later calls work normally."""
    calls = []

    def fn(*args, **kwargs):
        calls.append(1)
        if len(calls) <= failures:
            raise PermissionError(13, "sharing violation", None, winerror)
        return real(*args, **kwargs)
    return fn, calls


class AtomicWriteTest(TempHome):
    def test_replace_retries_transient_lock(self):
        target = self.home / "x.json"
        fn, calls = flaky(os.replace, 3)
        with mock.patch("agyswap.fsutil.os.replace", fn):
            fsutil.atomic_write_bytes(target, b"new")
        self.assertEqual(target.read_bytes(), b"new")
        self.assertEqual(len(calls), 4)
        self.assertEqual([p.name for p in self.home.iterdir()], ["x.json"])  # no temp file is left behind

    def test_persistent_or_other_errors_surface(self):
        target = self.home / "x.json"
        target.write_bytes(b"old")
        fn, calls = flaky(os.replace, 99)
        with mock.patch("agyswap.fsutil.os.replace", fn), self.assertRaises(PermissionError):
            fsutil.atomic_write_bytes(target, b"new")
        self.assertEqual(len(calls), fsutil._ATTEMPTS)
        self.assertEqual(target.read_bytes(), b"old")
        fn, calls = flaky(os.replace, 1, winerror=2)
        with mock.patch("agyswap.fsutil.os.replace", fn), self.assertRaises(PermissionError):
            fsutil.atomic_write_bytes(target, b"new")
        self.assertEqual(len(calls), 1)
        self.assertEqual([p.name for p in self.home.iterdir()], ["x.json"])

    def test_overwrite_is_a_new_file_not_in_place(self):
        target = self.home / "x.json"
        fsutil.atomic_write_bytes(target, b"one")
        before = target.stat().st_ino
        fsutil.atomic_write_bytes(target, b"two")
        self.assertNotEqual(target.stat().st_ino, before)

    def test_read_retries_transient_lock(self):
        target = self.home / "x.json"
        target.write_text('{"a": 1}', encoding="utf-8")
        fn, calls = flaky(Path.read_bytes, 2, winerror=5)
        with mock.patch.object(Path, "read_bytes", fn):
            self.assertEqual(fsutil.read_json(target), {"a": 1})
        self.assertEqual(len(calls), 3)

    def test_replace_keep_backup(self):
        target = self.home / "p" / "token"
        fsutil.replace_keep_backup(target, b"first")  # the target does not exist yet: a plain atomic write
        fsutil.replace_keep_backup(target, b"second")
        self.assertEqual(target.read_bytes(), b"second")
        self.assertEqual((self.home / "p" / "token.bak").read_bytes(), b"first")
        fsutil.replace_keep_backup(target, b"third")
        self.assertEqual((self.home / "p" / "token.bak").read_bytes(), b"second")
        self.assertEqual(sorted(p.name for p in target.parent.iterdir()), ["token", "token.bak"])

    def test_replace_keep_backup_verifies_read_back(self):
        target = self.home / "token"
        target.write_bytes(b"old")
        with mock.patch("agyswap.fsutil.read_bytes", return_value=b"garbled"), self.assertRaises(AgyswapError):
            fsutil.replace_keep_backup(target, b"new")


FAKE_SID = "S-1-5-21-1-2-3-1001"


def recorder(calls: list, sid: str = FAKE_SID):
    def fake_run(argv, **kwargs):
        calls.append(argv)
        return mock.Mock(returncode=0, stdout=f'"pc\\user","{sid}"\r\n')
    return fake_run


class PermissionTest(TempHome):
    """The real restrict_to_current_user on real temp folders: it changes only their DACLs, always to this process's own user."""

    def setUp(self):
        super().setUp()
        self.sid = process_sid()
        p = mock.patch("agyswap.fsutil.current_user_sid", lambda: self.sid)
        p.start()
        self.addCleanup(p.stop)
        self.folder = self.home / "profiles" / "school"
        self.folder.mkdir(parents=True)
        self.child = self.folder / "old-token"
        self.child.write_bytes(b"x")
        # another user's explicit entries, which /grant:r and /inheritance:r used to leave in place
        set_dacl_sddl(self.folder, f"D:(A;OICI;FA;;;{self.sid})(A;OICI;FR;;;WD)")
        set_dacl_sddl(self.child, f"D:(A;;FA;;;{self.sid})(A;;FR;;;S-1-5-32-545)")

    def test_restrict_leaves_only_the_current_user(self):
        self.assertTrue(REAL_RESTRICT(self.folder))
        self.assertTrue(dacl_sddl(self.folder).startswith("D:P"))
        self.assertEqual(ACE_RE.findall(dacl_sddl(self.folder)), [f"(A;OICI;FA;;;{self.sid})"])
        self.assertEqual(ACE_RE.findall(dacl_sddl(self.child)), [f"(A;ID;FA;;;{self.sid})"])  # inherits only from the folder
        self.assertEqual(self.child.read_bytes(), b"x")

    def test_without_children_and_for_a_single_file(self):
        self.assertTrue(REAL_RESTRICT(self.folder, children=False))
        self.assertEqual(ACE_RE.findall(dacl_sddl(self.folder)), [f"(A;OICI;FA;;;{self.sid})"])
        self.assertIn(";BU)", dacl_sddl(self.child))  # what is inside keeps its own entries (BU: Users)
        self.assertTrue(REAL_RESTRICT(self.child))
        self.assertTrue(dacl_sddl(self.child).startswith("D:P"))
        self.assertEqual(ACE_RE.findall(dacl_sddl(self.child)), [f"(A;;FA;;;{self.sid})"])

    def test_restrict_fails_closed(self):
        with mock.patch("agyswap.fsutil.current_user_sid", return_value=None):
            self.assertFalse(REAL_RESTRICT(self.folder))  # no SID available
        with mock.patch("agyswap.fsutil.owner_sid", return_value=None):
            self.assertFalse(REAL_RESTRICT(self.folder))  # the owner cannot be read
        with mock.patch("agyswap.fsutil.current_user_sid", return_value="not-a-sid"), \
                mock.patch("agyswap.fsutil.owner_sid", return_value="not-a-sid"):
            self.assertFalse(REAL_RESTRICT(self.folder))
        self.assertIn("WD", dacl_sddl(self.folder))  # nothing was changed

    def test_links_and_folders_owned_by_another_account_are_refused(self):
        import _winapi

        with mock.patch("agyswap.fsutil.owner_sid", return_value="S-1-5-21-7-7-7-1002"), self.assertRaises(AgyswapError) as e:
            REAL_RESTRICT(self.folder)
        self.assertIn("owned by another Windows account", str(e.exception))
        self.assertIn("WD", dacl_sddl(self.folder))
        link = self.home / "link"
        _winapi.CreateJunction(str(self.folder), str(link))
        with self.assertRaises(AgyswapError) as e:
            REAL_RESTRICT(link)
        self.assertIn("junction", str(e.exception))
        self.assertIn("WD", dacl_sddl(self.folder))  # the target was not changed through the link
        for trusted in ("S-1-5-32-544", "S-1-5-18"):
            with mock.patch("agyswap.fsutil.owner_sid", return_value=trusted):
                self.assertTrue(REAL_RESTRICT(self.folder, children=False))

    def test_current_user_sid_reads_whoami_by_its_full_path(self):
        with mock.patch("agyswap.fsutil.subprocess.run", recorder(calls := [])):
            self.assertEqual(REAL_CURRENT_USER_SID(), FAKE_SID)
        self.assertEqual(calls[0][0], paths.system_exe("whoami"))
        with mock.patch("agyswap.fsutil.subprocess.run", return_value=mock.Mock(returncode=0, stdout="")):
            self.assertIsNone(REAL_CURRENT_USER_SID())

    def test_owner_sid_reads_the_real_owner(self):
        self.assertRegex(REAL_OWNER_SID(self.home), r"^S-1-\d+(-\d+)+$")
        self.assertIsNone(REAL_OWNER_SID(self.home / "missing"))

    def test_data_root_is_locked_down_as_it_is_created(self):
        root = self.home / "data"
        seen = []
        with mock.patch.dict(os.environ, {"AGYSWAP_HOME": str(root)}), \
                mock.patch("agyswap.fsutil.restrict_to_current_user", lambda p, **k: seen.append(p) or True):
            fsutil.atomic_write_json(paths.sequence_file(), {})
            fsutil.ensure_private_dir(paths.profile_home("x"))
        self.assertEqual(seen, [root, root / "profiles" / "x"])
        root2 = self.home / "data2"
        with mock.patch.dict(os.environ, {"AGYSWAP_HOME": str(root2)}), \
                mock.patch("agyswap.fsutil.restrict_to_current_user", return_value=False):
            with self.assertRaises(AgyswapError):
                fsutil.atomic_write_json(paths.sequence_file(), {})
            with self.assertRaises(AgyswapError):
                with fsutil.file_lock(paths.lock_file()):
                    pass
        self.assertFalse(root2.exists())  # not left behind unprotected

    def test_ensure_private_dir_raises_when_restrict_fails(self):
        with mock.patch("agyswap.fsutil.restrict_to_current_user", return_value=False):
            with self.assertRaises(AgyswapError):
                fsutil.ensure_private_dir(self.home / "profiles" / "x")


REAL_RESTRICT = fsutil.restrict_to_current_user  # keep it before TempHome replaces it
REAL_OWNER_SID = fsutil.owner_sid
REAL_CURRENT_USER_SID = fsutil.current_user_sid


class DataFolderTest(TempHome):
    """The data folder must be inside the real profile folder: elsewhere other Windows users may be able to create or change files in it."""

    def setUp(self):
        super().setUp()
        self.profile = self.home / "me"
        self.profile.mkdir()
        p = mock.patch("agyswap.paths.real_home", return_value=str(self.profile))
        p.start()
        self.addCleanup(p.stop)

    def base_with(self, **env) -> Path:
        with mock.patch.dict(os.environ, env):
            return paths.base()

    def test_outside_the_profile_is_refused(self):
        import _winapi

        outside = self.home / "shared"
        outside.mkdir()
        junction = self.profile / "link"
        _winapi.CreateJunction(str(outside), str(junction))
        for where in (outside, self.home / "me2", self.profile, junction / "agyswap"):
            with self.assertRaises(ConfigError, msg=str(where)):
                self.base_with(AGYSWAP_HOME=str(where))
        inside = self.profile / "AppData" / "Local" / "agyswap"
        self.assertEqual(self.base_with(AGYSWAP_HOME=str(inside)), inside)
        with mock.patch.dict(os.environ, {"LOCALAPPDATA": str(outside)}):
            os.environ.pop("AGYSWAP_HOME")
            with self.assertRaises(ConfigError):
                paths.base()  # the default location is checked too
        with mock.patch("agyswap.paths.real_home", return_value=None), self.assertRaises(ConfigError):
            self.base_with(AGYSWAP_HOME=str(inside))

    def test_cli_reports_a_refused_folder(self):
        from agyswap import cli
        import contextlib
        import io
        import json

        for argv in (["list"], ["list", "--json"]):
            out, err = io.StringIO(), io.StringIO()
            with mock.patch.dict(os.environ, {"AGYSWAP_HOME": str(self.home / "shared")}), \
                    contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                self.assertEqual(cli.dispatch(argv), 1)
            if "--json" in argv:
                self.assertEqual(json.loads(out.getvalue())["error"]["type"], "ConfigError")
            else:
                self.assertIn("not inside your user profile folder", err.getvalue())
        self.assertFalse((self.home / "shared").exists())


class SystemToolTest(TempHome):
    """Windows tools are started by their full System32 path: with a bare name CreateProcess looks in the current folder first."""

    def run_all(self) -> list:
        calls = []

        def fake_run(argv, **kwargs):
            calls.append(argv)
            return mock.Mock(returncode=0, stdout='"pc\\user","S-1-5-21-1-2-3-1001"\r\n')

        def fake_popen(argv, **kwargs):
            calls.append(argv)
            raise OSError("not started in tests")

        with mock.patch("subprocess.run", fake_run), mock.patch("subprocess.Popen", fake_popen), \
                mock.patch("agyswap.fsutil.owner_sid", return_value=FAKE_SID):
            procs.running()
            fsutil.current_user_sid()
            usage._kill_tree(mock.Mock(pid=1234))
        return calls

    def test_every_system_tool_is_an_absolute_system32_path(self):
        for root in (r"D:\Win", None):
            env = {"SystemRoot": root} if root else {}
            with mock.patch.dict(os.environ, env):
                if root is None:
                    os.environ.pop("SystemRoot", None)
                calls = self.run_all()
            system32 = os.path.normcase(os.path.join(root or r"C:\Windows", "System32"))
            self.assertEqual(len(calls), 3)
            for argv in calls:
                self.assertTrue(os.path.isabs(argv[0]), argv)
                self.assertEqual(os.path.normcase(os.path.dirname(argv[0])), system32, argv)

    def test_no_bare_tool_names_in_source(self):
        for path in SRC.glob("*.py"):
            text = path.read_text(encoding="utf-8")
            self.assertNotRegex(text, r"subprocess\.(run|Popen)\(\s*\[\s*[\"']", path.name)


class SourceRulesTest(unittest.TestCase):
    """Pitfalls cswap has hit, guarded by scanning the source: platform.system() can hang on WMI on some Windows machines (cswap PR #55),
    and text reads and writes without an explicit encoding decode UTF-8 as cp950 (cswap #12)."""

    def sources(self):
        return [(p, p.read_text(encoding="utf-8")) for p in SRC.glob("*.py")]

    def test_no_platform_system(self):
        for path, text in self.sources():
            self.assertNotRegex(text, r"platform\.(system|win32_ver|uname)\(", path.name)

    def test_text_io_names_encoding(self):
        for path, text in self.sources():
            for n, line in enumerate(text.splitlines(), 1):
                where = f"{path.name}:{n}"
                if re.search(r"\.(read_text|write_text)\(", line):
                    self.assertIn("encoding=", line, where)
                m = re.search(r"\bopen\(([^)]*)\)", line)
                if m and "fdopen" not in line:
                    self.assertRegex(m.group(1), r"\"[a-z+]*b[a-z+]*\"|encoding=", where)
                if "text=True" in line:
                    self.assertIn("encoding=", line, where)


if __name__ == "__main__":
    unittest.main()
