import contextlib
import io
import json
import unittest
from unittest import mock

from agyswap import cli
from tests.helpers import TempHome, live


def run(argv):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            code = cli.dispatch(argv)
        except SystemExit as e:
            code = e.code
    return code, out.getvalue(), err.getvalue()


class TranslateTest(unittest.TestCase):
    def test_subcommands_map_to_legacy_flags(self):
        t = cli.translate_subcommand
        self.assertEqual(t(["list", "--json"]), ["--list", "--json"])
        self.assertEqual(t(["ls"]), ["--list"])
        self.assertEqual(t(["switch"]), ["--switch"])
        self.assertEqual(t(["switch", "--strategy", "best"]), ["--switch", "--strategy", "best"])
        self.assertEqual(t(["switch", "school", "--force"]), ["--switch-to", "school", "--force"])
        self.assertEqual(t(["rm", "2"]), ["--remove-account", "2"])
        self.assertEqual(t(["update"]), ["--upgrade"])
        self.assertEqual(t(["--list"]), ["--list"])

    def test_options_before_positional(self):
        t = cli.translate_subcommand
        self.assertEqual(t(["switch", "--force", "school"]), ["--switch-to", "school", "--force"])
        self.assertEqual(t(["switch", "--json", "2"]), ["--switch-to", "2", "--json"])
        self.assertEqual(t(["switch", "--strategy", "best", "--json"]), ["--switch", "--strategy", "best", "--json"])
        self.assertEqual(t(["export", "--full", "out.json"]), ["--export", "out.json", "--full"])
        self.assertEqual(t(["export", "--account", "2", "out.json"]), ["--export", "out.json", "--account", "2"])
        self.assertEqual(t(["import", "--force", "in.json"]), ["--import", "in.json", "--force"])
        self.assertEqual(t(["import-usage", "--hold", "60", "u.json"]), ["--import-usage", "u.json", "--hold", "60"])
        self.assertEqual(t(["add-token", "--slot", "2", "-"]), ["--add-token", "-", "--slot", "2"])
        self.assertEqual(t(["add-token", "--slot", "2"]), ["--add-token", "--slot", "2"])


class FlagCheckTest(TempHome):
    def assert_usage_error(self, argv, fragment):
        code, _, err = run(argv)
        self.assertEqual(code, 2, (argv, err))
        self.assertIn(fragment, err)

    def test_misplaced_flags(self):
        self.assert_usage_error(["add", "--json"], "--json can only be used with")
        self.assert_usage_error(["status", "--token-status"], "--token-status can only be used with 'list'")
        self.assert_usage_error(["list", "--json", "--token-status"], "cannot be combined with --json")
        self.assert_usage_error(["switch", "2", "--strategy", "best"], "--strategy can only be used with")
        self.assert_usage_error(["switch", "--model", "gemini"], "--model can only be used with")
        self.assert_usage_error(["list", "--slot", "3"], "--slot can only be used with")
        self.assert_usage_error(["status", "--email", "a@b.c"], "--email can only be used with")
        self.assert_usage_error(["list", "--force"], "--force can only be used with")
        self.assert_usage_error(["import-usage", "x.json", "--hold", "-1"], "--hold must be")
        self.assert_usage_error(["export", ""], "requires a non-empty")
        self.assert_usage_error([], "no command given")

    def test_valid_combinations_reach_dispatch(self):
        with mock.patch("agyswap.state.read_live_login", return_value=live()):
            code, _, err = run(["export", str(self.home / "out.json"), "--account", "2", "--full", "--force"])
        self.assertEqual(code, 1, err)
        self.assertIn("no accounts to export", err)
        with mock.patch("agyswap.manage.add_token", return_value=[("ok", "added")]) as fake:
            for argv in (["add-token", "-", "--email", "a@b.c"], ["add-token", "--slot", "2", "-"], ["add-token"]):
                self.assertEqual(run(argv)[0], 0, argv)
        self.assertEqual([c.args[0] for c in fake.call_args_list], ["-", "-", ""])
        self.assertEqual(fake.call_args_list[0].kwargs, {"slot": None, "email": "a@b.c"})
        self.assertEqual(fake.call_args_list[1].kwargs, {"slot": 2, "email": None})

    def test_predispatched_subcommands(self):
        self.assertEqual(run(["config", "list"])[0], 0)
        self.assertEqual(run(["swap", "1"])[0], 2)
        self.assertEqual(run(["auto", "--strategy", "nope"])[0], 2)
        self.assertEqual(run(["config", "--json", "set", "a", "b"])[0], 2)

    def test_retired_and_not_planned(self):
        for argv, fragment in ((["use", "school"], "switch"), (["usage"], "list"),
                               (["add", "school"], "login --alias school"),
                               (["run", "--profile", "school", "--", "-p", "x"], "run <account>"),
                               (["unclaimed"], "not planned"), (["menubar"], "not planned")):
            code, _, err = run(argv)
            self.assertEqual(code, 1, argv)
            self.assertIn(fragment, err)

    def test_pending_switch_warns_for_every_command(self):
        (self.home / "pending_switch.json").write_text('{"from": 1, "to": 2}', encoding="utf-8")
        for argv in (["import", "missing.json"], ["run", "--profile", "x"], ["config", "list"], ["map"]):
            _, _, err = run(argv)
            self.assertIn("the last switch did not finish", err, argv)
            self.assertIn("agyswap switch 1 or agyswap switch 2", err, argv)
        code, out, err = run(["config", "list", "--json"])
        self.assertEqual(json.loads(out)["schemaVersion"], 1)  # the warning goes to stderr only
        self.assertNotIn("the last switch did not finish", run(["--version"])[2])

    def test_option_forms_accepted(self):
        for argv in (["import", "--force", "in.json"], ["import-usage", "--hold", "60", "u.json"]):
            code, out, err = run(argv)
            self.assertEqual(code, 1, (argv, err))
            self.assertIn("not found", err)
        with mock.patch("agyswap.state.read_live_login", return_value=live()):
            self.assertIn("no accounts to export", run(["export", "--full", str(self.home / "out.json")])[2])
        self.assertEqual(run(["config", "list", "--json"])[0], 0)
        self.assertEqual(run(["config", "get", "k", "--json"])[0], 1)
        self.assertEqual(run(["config", "set", "a", "b", "--json"])[0], 2)
        # --debug after the subcommand is accepted too (without really enabling debug logging, so the log file is not left open in the temp folder)
        self.assertTrue(cli.build_config_parser().parse_args(["list", "--debug"]).debug)
        self.assertFalse(cli.build_config_parser().parse_args(["list"]).debug)

    def test_slot_must_be_positive(self):
        self.assert_usage_error(["add", "--slot", "0"], "1 or greater")
        self.assert_usage_error(["login", "--slot", "-1"], "1 or greater")

    def test_run_profile_equals_form_is_retired(self):
        code, _, err = run(["run", "--profile=school", "--", "-p", "x"])
        self.assertEqual(code, 1)
        self.assertIn("run <account>", err)

    def test_version_and_help(self):
        code, out, _ = run(["--version"])
        self.assertEqual(code, 0)
        self.assertTrue(out.startswith("agyswap "))
        code, out, _ = run(["help"])
        self.assertEqual(code, 0)
        self.assertIn("switch <num|email|alias>", out)
        code, out, _ = run(["auto", "--help"])
        self.assertEqual(code, 0)
        self.assertIn("Exit codes with --once", out)


if __name__ == "__main__":
    unittest.main()
