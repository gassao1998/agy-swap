"""purge, upgrade and the daily update check, with git replaced by a recorder."""
import contextlib
import io
import os
import shutil
from pathlib import Path
from unittest import mock

from agyswap import accounts, cli, manage, paths, settings, update_check
from agyswap.errors import AgyswapError
from tests.helpers import DictStore, TempHome, live, process_sid, token_blob
from tests.test_cli import run

LOCAL, REMOTE = "a" * 40, "b" * 40
SID = process_sid()


class FakeGit:
    """Answers the git commands agyswap runs; records each argv and keyword arguments."""

    def __init__(self, remote=REMOTE, local=LOCAL, ancestor=False, pull_rc=0, after=None):
        self.calls, self.remote, self.local, self.ancestor, self.pull_rc, self.after = [], remote, local, ancestor, pull_rc, after

    def __call__(self, argv, **kwargs):
        self.calls.append((argv, kwargs))
        verb = argv[3]
        out = {"rev-parse": self.local, "ls-remote": f"{self.remote}\tHEAD\n" if self.remote else ""}.get(verb, "")
        rc = 0
        if verb == "ls-remote" and not self.remote:
            rc = 128
        if verb == "merge-base":
            rc = 0 if self.ancestor else 1
        if verb == "pull":
            rc = self.pull_rc
            if self.after and not rc:
                self.local = self.after
        return mock.Mock(returncode=rc, stdout=out, stderr="")


class UpdateCheckTest(TempHome):
    def setUp(self):
        super().setUp()
        self.git = str(self.home / "Git" / "cmd" / "git.exe")
        for name, value in (("find_git", self.git), ("checkout", self.home)):
            p = mock.patch(f"agyswap.update_check.{name}", return_value=value)
            p.start()
            self.addCleanup(p.stop)

    def check(self, fake):
        with mock.patch("agyswap.update_check.subprocess.run", fake):
            return update_check.check_for_update()

    def test_notice_once_a_day_and_quiet_when_current(self):
        fake = FakeGit()
        msg = self.check(fake)
        self.assertIn("Run `agyswap upgrade`", msg)
        self.assertIn(REMOTE[:7], msg)
        argv, kwargs = fake.calls[0]
        self.assertEqual(argv, [self.git, "-C", str(self.home), "ls-remote", "origin", "HEAD"])
        self.assertNotIn("shell", kwargs)
        self.assertEqual((kwargs["cwd"], kwargs["env"]["GIT_TERMINAL_PROMPT"]), (str(self.home), "0"))
        self.assertEqual(kwargs["env"]["GIT_CEILING_DIRECTORIES"], str(self.home.parent))
        self.check(fake)
        self.assertEqual([c[0][3] for c in fake.calls].count("ls-remote"), 1)  # cached for a day
        self.assertIsNone(self.check(FakeGit(local=REMOTE)))
        self.assertIsNone(self.check(FakeGit(ancestor=True)))  # origin's commit is already in this checkout

    def test_inherited_git_variables_are_dropped(self):
        planted = {"GIT_DIR": str(self.home / "other" / ".git"), "GIT_CONFIG_PARAMETERS": "'core.fsmonitor=calc.exe'",
                   "GIT_SSH_COMMAND": "calc.exe", "SSH_ASKPASS": "calc.exe", "GCM_INTERACTIVE": "always"}
        fake = FakeGit()
        with mock.patch.dict(os.environ, planted):
            self.check(fake)
        env = fake.calls[0][1]["env"]
        self.assertFalse([k for k in env if k.upper().startswith("GIT_") and k not in ("GIT_TERMINAL_PROMPT", "GIT_CEILING_DIRECTORIES")])
        self.assertNotIn("SSH_ASKPASS", env)
        self.assertEqual(env["GCM_INTERACTIVE"], "never")

    def test_failure_is_silent_and_disabled_starts_no_git(self):
        self.assertIsNone(self.check(FakeGit(remote=None)))
        self.assertIsNone(self.check(mock.Mock(side_effect=OSError("boom"))))
        paths.update_check_file().unlink(missing_ok=True)
        settings.set_value("updates.check", "false")
        fake = mock.Mock()
        with mock.patch("agyswap.update_check.find_git") as finder:
            self.assertIsNone(self.check(fake))
        finder.assert_not_called()
        fake.assert_not_called()

    def test_cli_prints_the_notice_on_stderr_but_not_with_json(self):
        accounts.save_sequence({"activeAccountNumber": None, "accounts": {}})
        with mock.patch("agyswap.update_check.subprocess.run", FakeGit()), \
                mock.patch("agyswap.state.read_live_login", return_value=live()):
            code, out, err = run(["list"])
            self.assertEqual(code, 0)
            self.assertIn("newer agyswap", err)
            self.assertNotIn("newer agyswap", out)
            code, out, err = run(["list", "--json"])
            self.assertNotIn("newer agyswap", err)

    def test_upgrade_pulls_fast_forward_in_the_checkout(self):
        (self.home / "pyproject.toml").write_text('name = "agyswap"\n', encoding="utf-8")
        fake = FakeGit(after=REMOTE)

        def pull_changes_project(argv, **kwargs):
            if argv[3] == "pull":
                (self.home / "pyproject.toml").write_text('name = "agyswap"\nversion = "2"\n', encoding="utf-8")
            return fake(argv, **kwargs)

        out = io.StringIO()
        with mock.patch("agyswap.update_check.subprocess.run", pull_changes_project), contextlib.redirect_stdout(out), \
                contextlib.redirect_stderr(err := io.StringIO()):
            self.assertEqual(cli.dispatch(["upgrade"]), 0)
        pull = next(argv for argv, _ in fake.calls if argv[3] == "pull")
        self.assertEqual(pull, [self.git, "-C", str(self.home), "pull", "--ff-only"])
        self.assertIn(f"agyswap upgraded: {LOCAL[:7]} -> {REMOTE[:7]}", out.getvalue())
        self.assertIn("new terminal", out.getvalue())
        self.assertIn(str(self.home), out.getvalue())

    def test_upgrade_failure_and_missing_git(self):
        with mock.patch("agyswap.update_check.subprocess.run", FakeGit(pull_rc=1)):
            code, _, err = run(["update"])
        self.assertEqual(code, 1)
        self.assertIn("--ff-only failed", err)
        with mock.patch("agyswap.update_check.find_git", return_value=None):
            self.assertEqual(run(["upgrade"])[0], 1)
        with mock.patch("agyswap.update_check.checkout", return_value=None):
            self.assertIn("not running from its git checkout", run(["upgrade"])[2])


class FindGitTest(TempHome):
    def test_skips_the_current_directory_and_relative_entries(self):
        cwd, good = self.home / "cwd", self.home / "tools"
        for d in (cwd, good):
            d.mkdir()
            (d / "git.exe").write_bytes(b"")
        old = os.getcwd()
        os.chdir(cwd)
        self.addCleanup(os.chdir, old)
        with mock.patch.dict(os.environ, {"PATH": os.pathsep.join([".", "cwd", "", f'"{good}"']),
                                          "ProgramW6432": "", "ProgramFiles": str(self.home / "none"),
                                          "LOCALAPPDATA": str(self.home / "none")}):
            self.assertEqual(REAL_FIND_GIT(), str(good / "git.exe"))
        with mock.patch.dict(os.environ, {"PATH": ".", "ProgramW6432": "", "ProgramFiles": str(self.home),
                                          "LOCALAPPDATA": str(self.home / "none")}):
            (self.home / "Git" / "cmd").mkdir(parents=True)
            (self.home / "Git" / "cmd" / "git.exe").write_bytes(b"")
            self.assertEqual(REAL_FIND_GIT(), str(self.home / "Git" / "cmd" / "git.exe"))

    def test_unc_entries_are_never_tried_and_install_folders_come_first(self):
        tried = []
        isfile = lambda p: tried.append(p) or p.startswith(str(self.home))  # noqa: E731
        pf, early = self.home / "pf", self.home / "early"
        with mock.patch.dict(os.environ, {"PATH": os.pathsep.join([r"\\server\share\bin", str(early)]), "ProgramW6432": "",
                                          "ProgramFiles": str(pf), "LOCALAPPDATA": str(self.home / "none")}), \
                mock.patch("agyswap.update_check.os.path.isfile", isfile):
            self.assertEqual(REAL_FIND_GIT(), str(pf / "Git" / "cmd" / "git.exe"))
            os.environ["ProgramFiles"] = ""
            os.environ["LOCALAPPDATA"] = ""
            self.assertEqual(REAL_FIND_GIT(), str(early / "git.exe"))
        self.assertFalse([p for p in tried if p.startswith("\\\\")])

    def test_checkout_is_this_repository(self):
        self.assertEqual(REAL_CHECKOUT(), Path(update_check.__file__).resolve().parents[2])

    def test_checkout_needs_a_real_git_folder(self):
        import _winapi

        root = self.home / "copy"
        (root / "src" / "agyswap").mkdir(parents=True)
        (root / "pyproject.toml").write_text('name = "agyswap"\n', encoding="utf-8")
        fake_file = str(root / "src" / "agyswap" / "update_check.py")
        with mock.patch.object(update_check, "__file__", fake_file):
            (root / ".git").mkdir()
            self.assertIsNone(REAL_CHECKOUT())  # empty: git would climb to a parent repository
            (root / ".git" / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
            self.assertEqual(REAL_CHECKOUT(), root)
            shutil.rmtree(root / ".git")
            (root / ".git").write_text("gitdir: C:/elsewhere\n", encoding="utf-8")
            self.assertIsNone(REAL_CHECKOUT())
            (root / ".git").unlink()
            elsewhere = self.home / "elsewhere"
            elsewhere.mkdir()
            (elsewhere / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
            _winapi.CreateJunction(str(elsewhere), str(root / ".git"))
            self.assertIsNone(REAL_CHECKOUT())


REAL_FIND_GIT = update_check.find_git  # kept before TempHome replaces it
REAL_CHECKOUT = update_check.checkout


class PurgeTest(TempHome):
    def setUp(self):
        super().setUp()
        self.write_token("default", "me@gmail.com")
        accounts.save_sequence({"activeAccountNumber": 1, "accounts": {
            "1": {"email": "me@gmail.com", "folder": "default"},
            "2": {"email": "api-key-2@agyswap.local", "folder": "slot2", "kind": "apikey"}}})
        settings.set_value("ui.theme", "dark")
        (self.home / "notes.txt").write_text("mine", encoding="utf-8")
        self.store = DictStore(token_blob("me@gmail.com"))
        self.store.items[paths.apikey_target("slot2")] = mock.Mock(blob=b"key")
        self.err = self.enterContext(contextlib.redirect_stderr(io.StringIO()))
        p = mock.patch("agyswap.manage.current_user_sid", return_value=SID)
        p.start()
        self.addCleanup(p.stop)

    def assert_untouched(self):
        self.assertTrue(paths.sequence_file().exists())
        self.assertIn(paths.apikey_target("slot2"), self.store.items)

    def test_cancel_changes_nothing_and_the_prompt_lists_every_item(self):
        msgs = manage.purge(confirm=lambda prompt: False, store=self.store)
        self.assertEqual(msgs, [("dim", "Cancelled")])
        self.assert_untouched()
        for name in (f"profiles{os.sep}", "sequence.json", "settings.json", ".auto.lock",
                     f"Credential Manager entry {paths.apikey_target('slot2')}"):
            self.assertIn(f"  - {name}\n", self.err.getvalue())
        self.assertNotIn("notes.txt", self.err.getvalue())

    def test_refuses_links_foreign_owners_and_folders_without_markers(self):
        import _winapi

        outside = self.home.parent / f"{self.home.name}-outside"
        outside.mkdir()
        self.addCleanup(shutil.rmtree, outside)
        (outside / "keep.txt").write_text("keep", encoding="utf-8")
        _winapi.CreateJunction(str(outside), str(self.home / "backup"))
        with self.assertRaises(AgyswapError) as e:
            manage.purge(confirm=lambda prompt: True, store=self.store)
        self.assertIn("junction", str(e.exception))
        os.rmdir(self.home / "backup")
        link = self.home.parent / f"{self.home.name}-link"
        _winapi.CreateJunction(str(self.home), str(link))
        self.addCleanup(os.rmdir, link)
        with mock.patch.dict(os.environ, {"AGYSWAP_HOME": str(link)}), self.assertRaises(AgyswapError):
            manage.purge(confirm=lambda prompt: True, store=self.store)
        with mock.patch("agyswap.fsutil.owner_sid", return_value="S-1-5-21-7-7-7-1002"), self.assertRaises(AgyswapError):
            manage.purge(confirm=lambda prompt: True, store=self.store)
        self.assert_untouched()
        self.assertTrue((outside / "keep.txt").exists())
        paths.sequence_file().rename(self.home / "sequence.bak")
        with self.assertRaises(AgyswapError) as e:
            manage.purge(confirm=lambda prompt: True, store=self.store)
        self.assertIn("does not look like", str(e.exception))
        self.assertTrue(paths.settings_file().exists())

    def test_rechecks_after_the_prompt_before_deleting_anything(self):
        def switch_meanwhile(prompt):
            paths.pending_switch_file().write_text("{}", encoding="utf-8")
            return True

        with self.assertRaises(AgyswapError):
            manage.purge(confirm=switch_meanwhile, store=self.store)
        self.assert_untouched()
        paths.pending_switch_file().unlink()
        with mock.patch("agyswap.autoswitch.loop_running", side_effect=[False, True]), self.assertRaises(AgyswapError) as e:
            manage.purge(confirm=lambda prompt: True, store=self.store)
        self.assertIn("auto is running", str(e.exception))
        self.assert_untouched()

    def test_damaged_roster_still_finds_live_runs(self):
        paths.sequence_file().write_text("{", encoding="utf-8")
        ghost = paths.profile_home("ghost")
        ghost.mkdir()
        alive = lambda path: 4242 if "ghost" in str(path) else None  # noqa: E731
        with mock.patch("agyswap.run._runner_alive", alive), self.assertRaises(AgyswapError) as e:
            manage.purge(confirm=lambda prompt: True, store=self.store)
        self.assertIn("ghost", str(e.exception))
        self.assertEqual(manage.purge(confirm=lambda prompt: False, store=self.store), [("dim", "Cancelled")])
        self.assertIn("cannot be named", self.err.getvalue())

    def test_remove_tree_removes_a_link_but_not_its_target(self):
        import _winapi

        target = self.home / "target"
        target.mkdir()
        (target / "keep.txt").write_text("keep", encoding="utf-8")
        _winapi.CreateJunction(str(target), str(self.home / "link"))
        manage.remove_tree(self.home / "link")
        self.assertFalse(os.path.lexists(self.home / "link"))
        self.assertTrue((target / "keep.txt").exists())

    def test_removes_only_agyswaps_own_data(self):
        msgs = manage.purge(confirm=lambda prompt: True, store=self.store)
        self.assertEqual(msgs[0][0], "ok")
        self.assertEqual(sorted(p.name for p in self.home.iterdir()), ["notes.txt"])
        self.assertNotIn(paths.apikey_target("slot2"), self.store.items)
        self.assertIn(paths.KEYRING_TARGET, self.store.items)  # the global login stays
        self.assertTrue(any("left alone" in text for _, text in msgs))

    def test_refuses_a_pending_switch_or_a_live_run(self):
        paths.pending_switch_file().write_text("{}", encoding="utf-8")
        with self.assertRaises(AgyswapError):
            manage.purge(confirm=lambda prompt: True, store=self.store)
        paths.pending_switch_file().unlink()
        with mock.patch("agyswap.run._runner_alive", return_value=4242), self.assertRaises(AgyswapError):
            manage.purge(confirm=lambda prompt: True, store=self.store)
        self.assertTrue(paths.sequence_file().exists())

    def test_cli_answers_no_without_a_terminal(self):
        code, out, err = run(["purge"])
        self.assertEqual(code, 0)
        self.assertIn("Cancelled", out)
        self.assertTrue(paths.sequence_file().exists())
