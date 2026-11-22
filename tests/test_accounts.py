import unittest

from agyswap import accounts, settings
from agyswap.errors import AccountNotFoundError, ConfigError
from tests.helpers import TempHome


def sample():
    return {"activeAccountNumber": 1, "accounts": {
        "1": {"email": "me@gmail.com", "alias": "default", "folder": "default"},
        "2": {"email": "Me@School.edu", "alias": "school", "folder": "school"},
        "3": {"email": "slot-3@agyswap.local", "folder": "slot3"},
    }}


class ResolveTest(unittest.TestCase):
    def test_number_alias_email(self):
        data = sample()
        self.assertEqual(accounts.resolve(data, "2"), "2")
        self.assertEqual(accounts.resolve(data, "02"), "2")
        self.assertEqual(accounts.resolve(data, "SCHOOL"), "2")
        self.assertEqual(accounts.resolve(data, "me@school.edu"), "2")
        self.assertEqual(accounts.resolve(data, "me@gmail.com"), "1")

    def test_not_found_and_ambiguous(self):
        with self.assertRaises(AccountNotFoundError):
            accounts.resolve(sample(), "9")
        with self.assertRaises(AccountNotFoundError):
            accounts.resolve(sample(), "nobody")
        data = sample()
        data["accounts"]["3"]["email"] = "me@gmail.com"
        with self.assertRaises(ConfigError):
            accounts.resolve(data, "me@gmail.com")

    def test_alias_rules(self):
        self.assertEqual(accounts.normalize_alias(" Work.Main_1 "), "work.main_1")
        # An alias also serves as the folder name: . and .. would escape profiles, Windows strips trailing dots, and reserved names cannot become folders
        for bad in ("", "123", "-x", "a b", chr(0x4E2D) + chr(0x6587), ".", "..", "work.", ".hidden", "con", "NUL", "com1", "lpt9.x"):
            with self.assertRaises(ValueError, msg=bad):
                accounts.normalize_alias(bad)
        self.assertEqual(accounts.normalize_alias("console"), "console")

    def test_unicode_digits_are_not_numbers(self):
        for ident in (chr(0xB2), chr(0x2460), chr(0xFF11)):
            with self.assertRaises(AccountNotFoundError):
                accounts.resolve(sample(), ident)

    def test_threshold_and_priority(self):
        self.assertEqual(accounts.normalize_threshold("85"), 85.0)
        for bad in (49.9, 100, "x", True, float("nan")):
            with self.assertRaises(ValueError):
                accounts.normalize_threshold(bad)
        self.assertEqual(accounts.normalize_priority("0010"), 10)
        for bad in (-1, 1001, "1.5", True, chr(0xFF11)):
            with self.assertRaises(ValueError):
                accounts.normalize_priority(bad)
        self.assertIsNone(accounts.account_threshold({"threshold": 300}))

    def test_placeholder(self):
        self.assertEqual(accounts.placeholder_email(4), "slot-4@agyswap.local")
        self.assertTrue(accounts.is_placeholder("slot-4@agyswap.local"))
        self.assertFalse(accounts.is_placeholder("me@gmail.com"))


class StorageTest(TempHome):
    def test_sequence_round_trip_and_corrupt(self):
        self.assertIsNone(accounts.load_sequence())
        data = sample()
        accounts.save_sequence(data)
        loaded = accounts.load_sequence()
        self.assertEqual(loaded["sequence"], [1, 2, 3])
        self.assertIn("lastUpdated", loaded)
        (self.home / "sequence.json").write_text("{torn", encoding="utf-8")
        with self.assertRaises(ConfigError):
            accounts.load_sequence()
        for bad in ('{"accounts": {"abc": {}}}', '{"accounts": {"1": null}}', '{"accounts": {"01": {}}}',
                    '{"accounts": {"0": {}}}', '{"accounts": {}, "activeAccountNumber": "1"}'):
            (self.home / "sequence.json").write_text(bad, encoding="utf-8")
            with self.assertRaises(ConfigError, msg=bad):
                accounts.load_sequence()

    def test_missing_folder_defaults_to_number_and_follows_swap(self):
        from agyswap import manage, migrate
        from tests.helpers import live

        accounts.save_sequence({"activeAccountNumber": 1, "accounts": {
            "1": {"email": "a@x.com"}, "2": {"email": "b@x.com", "folder": ""}}})
        self.assertEqual({n: r["folder"] for n, r in accounts.load_sequence()["accounts"].items()}, {"1": "1", "2": "2"})
        migrate.migrate(lambda: live())
        manage.swap("1", "2")
        data = accounts.load_sequence()
        # the folders move with the records, so each email still resolves to its own login
        self.assertEqual((data["accounts"]["1"]["email"], data["accounts"]["1"]["folder"]), ("b@x.com", "2"))
        self.assertEqual((data["accounts"]["2"]["email"], data["accounts"]["2"]["folder"]), ("a@x.com", "1"))

    def test_shared_folders_and_non_text_fields_are_refused(self):
        for bad in ('{"accounts": {"1": {"folder": "Work"}, "2": {"folder": "work"}}}',
                    '{"accounts": {"1": {}, "2": {"folder": "1"}}}',
                    '{"accounts": {"1": {"email": 5}}}', '{"accounts": {"1": {"alias": ["a"]}}}',
                    '{"accounts": {"1": {"folder": null}}}', '{"accounts": {"1": {"email": {"x": 1}}}}'):
            (self.home / "sequence.json").write_text(bad, encoding="utf-8")
            with self.assertRaises(ConfigError, msg=bad):
                accounts.load_sequence()

    def test_profile_home_stays_inside_profiles(self):
        from agyswap import paths
        self.assertEqual(paths.profile_home("school").parent, paths.profiles_dir())
        for bad in ("..", ".", "", "a\\..\\..", "../x", "C:x", "work.", " x"):
            with self.assertRaises(ConfigError, msg=bad):
                paths.profile_home(bad)

    def test_corrupt_mappings_block_writes(self):
        accounts.set_mapping(self.home, "default", "me@gmail.com")
        (self.home / "mappings.json").write_text("{torn", encoding="utf-8")
        self.assertEqual(accounts.load_mappings(), {})
        with self.assertRaises(ConfigError):
            accounts.set_mapping(self.home / "x", "school", "me@school.edu")
        with self.assertRaises(ConfigError):
            accounts.remove_mapping(self.home)
        self.assertEqual((self.home / "mappings.json").read_text(encoding="utf-8"), "{torn")

    def test_mapping_nearest_ancestor(self):
        project = self.home / "work" / "proj"
        deep = project / "src" / "pkg"
        deep.mkdir(parents=True)
        accounts.set_mapping(self.home / "work", "default", "me@gmail.com")
        accounts.set_mapping(project, "school", "me@school.edu")
        key, entry = accounts.resolve_mapping(deep)
        self.assertEqual(entry["folder"], "school")
        self.assertEqual(accounts.resolve_mapping(self.home / "work")[1]["folder"], "default")
        self.assertIsNone(accounts.resolve_mapping(self.home))
        self.assertTrue(accounts.remove_mapping(project))
        self.assertFalse(accounts.remove_mapping(project))
        self.assertEqual(accounts.resolve_mapping(deep)[1]["folder"], "default")


class SettingsTest(TempHome):
    def test_defaults_and_clamp(self):
        self.assertEqual(settings.load()["autoswitch.threshold"], 90.0)
        self.assertIs(settings.load()["run.shareHistory"], False)
        (self.home / "settings.json").write_text(
            '{"autoswitch": {"threshold": 120, "unhealthyTicks": 0.5, "strategy": "nope", '
            '"includeApiKeyAccounts": "false"}, "keep": {"x": 1}}', encoding="utf-8")
        s = settings.load()
        self.assertEqual(s["autoswitch.threshold"], 99.9)
        self.assertEqual(s["autoswitch.unhealthyTicks"], 1)
        self.assertEqual(s["autoswitch.strategy"], "best")
        self.assertIs(s["autoswitch.includeApiKeyAccounts"], False)

    def test_set_strict_and_preserves_unknown(self):
        (self.home / "settings.json").write_text('{"keep": {"x": 1}}', encoding="utf-8")
        self.assertEqual(settings.set_value("autoswitch.threshold", "80"), 80.0)
        for key, value in (("autoswitch.threshold", "120"), ("autoswitch.strategy", "worst"),
                           ("run.shareHistory", "maybe"), ("usage.headerProbe", "true"), ("nope.key", "1")):
            with self.assertRaises(ConfigError):
                settings.set_value(key, value)
        self.assertEqual(settings.load()["autoswitch.threshold"], 80.0)
        self.assertTrue(settings.unset_value("autoswitch.threshold"))
        self.assertFalse(settings.unset_value("autoswitch.threshold"))
        self.assertIn('"keep"', (self.home / "settings.json").read_text(encoding="utf-8"))

    def test_unknown_keys_are_reported_with_suggestion(self):
        (self.home / "settings.json").write_text(
            '{"autoswitch": {"cooldownSecond": 10, "threshold": 80}, "ui": {"theme": "dark"}, "keep": {"x": 1}}',
            encoding="utf-8")
        self.assertEqual(settings.unknown_keys(), ["autoswitch.cooldownSecond (did you mean autoswitch.cooldownSeconds?)"])

    def test_corrupt_file_blocks_writes(self):
        (self.home / "settings.json").write_text("{bad", encoding="utf-8")
        self.assertEqual(settings.load()["autoswitch.threshold"], 90.0)
        with self.assertRaises(ConfigError):
            settings.set_value("autoswitch.threshold", "80")


if __name__ == "__main__":
    unittest.main()
