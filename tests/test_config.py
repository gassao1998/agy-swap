"""config list, get, set, unset and path through the CLI."""
import json

from agyswap import paths, settings
from agyswap.fsutil import file_lock
from tests.helpers import TempHome
from tests.test_cli import run


class ConfigTest(TempHome):
    def test_list_get_set_unset_path(self):
        code, out, _ = run(["config"])
        self.assertEqual(code, 0)
        self.assertIn("autoswitch.threshold", out)
        self.assertIn("(default)", out)
        code, out, err = run(["config", "set", "autoswitch.threshold", "80"])
        self.assertEqual((code, out.strip()), (0, "autoswitch.threshold = 80"))
        self.assertIn("no agyswap auto loop is running", err)
        self.assertEqual(run(["config", "get", "autoswitch.threshold"])[1].strip(), "80")
        payload = json.loads(run(["config", "get", "autoswitch.threshold", "--json"])[1])
        self.assertEqual(payload, {"schemaVersion": 1, "key": "autoswitch.threshold", "value": 80.0, "isSet": True})
        listed = json.loads(run(["config", "list", "--json"])[1])
        self.assertEqual(listed["path"], str(paths.settings_file()))
        self.assertEqual({r["key"] for r in listed["settings"]}, set(settings.SPECS))
        code, out, _ = run(["config", "unset", "autoswitch.threshold"])
        self.assertEqual((code, out.strip()), (0, "autoswitch.threshold unset (default: 90)"))
        code, _, err = run(["config", "unset", "autoswitch.threshold"])
        self.assertEqual(code, 0)
        self.assertIn("is not set", err)
        self.assertEqual(run(["config", "path"])[1].strip(), str(paths.settings_file()))

    def test_strict_set_and_misspelt_keys(self):
        code, _, err = run(["config", "set", "autoswitch.threshhold", "80"])
        self.assertEqual(code, 1)
        self.assertIn("did you mean autoswitch.threshold?", err)
        code, out, _ = run(["config", "get", "nope", "--json"])
        self.assertEqual((code, json.loads(out)["error"]["type"]), (1, "ConfigError"))
        self.assertEqual(run(["config", "set", "autoswitch.threshold", "20"])[0], 1)
        self.assertEqual(run(["config", "set", "usage.headerProbe", "true"])[0], 1)
        paths.settings_file().write_text('{"autoswitch": {"treshold": 80}}', encoding="utf-8")
        code, _, err = run(["config", "list"])
        self.assertEqual(code, 0)
        self.assertIn("autoswitch.treshold (did you mean autoswitch.threshold?)", err)
        self.assertEqual(run(["config", "set", "a", "b", "--json"])[0], 2)

    def test_no_auto_note_while_the_loop_runs_or_for_other_keys(self):
        self.assertNotIn("auto loop", run(["config", "set", "ui.theme", "dark"])[2])
        with file_lock(paths.auto_lock_file()):
            self.assertNotIn("auto loop", run(["config", "set", "autoswitch.cooldownSeconds", "60"])[2])
