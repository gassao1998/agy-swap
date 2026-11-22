import unittest

from agyswap import usage

# Free plan (school): weekly limits for two categories only
FREE = ("Gemini Models\tWeekly Limit Remaining\t93%\t2026-11-28T10:45:55Z\n"
        "Claude and GPT models\tWeekly Limit Remaining\t100%\t2026-11-29T03:50:47Z\n")
# Google AI Pro (default, measured 2026-11-22): adds the 5-hour limits and a single model's limit
PRO = ("Gemini Models\tWeekly Limit Remaining\t100%\t2026-11-29T04:38:52Z\n"
       "Gemini Models\tFive Hour Limit Remaining\t100%\t2026-11-22T09:38:52Z\n"
       "Claude and GPT models\tWeekly Limit Remaining\t100%\t2026-11-29T04:38:52Z\n"
       "Claude and GPT models\tFive Hour Limit Remaining\t100%\t2026-11-22T09:38:52Z\n"
       "Gemini 4 Argon\tGemini 4 Argon Weekly Limit\t100%\t2026-11-29T04:38:52Z\n")
AUTH = "Authentication required. Please visit the URL to log in:\n  https://accounts.google.com/o/oauth2/x\n"


class ParseTest(unittest.TestCase):
    def test_free_tier(self):
        u = usage.parse_usage(FREE)
        self.assertEqual(u["seven_day"], {"pct": 7.0, "resets_at": "2026-11-28T10:45:55Z"})
        self.assertNotIn("five_hour", u)
        self.assertEqual([(w["name"], w["window"], w["pct"]) for w in u["scoped"]],
                         [("Gemini Models", "7d", 7.0), ("Claude and GPT models", "7d", 0.0)])

    def test_pro_tier_windows_and_per_model_limit(self):
        u = usage.parse_usage(PRO.replace("Gemini 4 Argon Weekly Limit\t100%", "Gemini 4 Argon Weekly Limit\t5%"))
        self.assertEqual(u["five_hour"], {"pct": 0.0, "resets_at": "2026-11-22T09:38:52Z"})
        self.assertEqual(u["seven_day"]["pct"], 0.0)  # Argon's 95% is not merged into the Gemini category
        argon = [w for w in u["scoped"] if w["name"] == "Gemini 4 Argon"]
        self.assertEqual(argon, [{"name": "Gemini 4 Argon", "window": "7d", "pct": 95.0,
                                  "resets_at": "2026-11-29T04:38:52Z"}])
        self.assertEqual(len(u["scoped"]), 5)

    def test_auth_empty_and_garbage(self):
        self.assertIsNone(usage.parse_usage(AUTH))
        self.assertTrue(usage.needs_login(AUTH))
        self.assertIsNone(usage.parse_usage(""))
        self.assertIsNone(usage.parse_usage("\x00\xffgarbage\tx\ty\nGemini\tWeekly\tabc%\tz"))
        self.assertIsNone(usage.parse_usage("Gemini Models\tDaily Something\t50%\t2026-11-28T00:00:00Z"))

    def test_control_characters_are_dropped(self):
        text = ("Gemini \x1b]52;c;ZXZpbA==\x07Models\x9b2J\tWeekly Limit Remaining\t93%\t2026-11-28T10:45:55Z\x1b[0m\x00\n"
                "Gemini 4 Argon\x85\tGemini 4 Argon Weekly Limit\t5%\t\x7f2026-11-29T04:38:52Z\n")
        u = usage.parse_usage(text)
        fields = [v for w in u["scoped"] for v in (w["name"], w["resets_at"])]
        for value in fields:
            self.assertNotRegex(value, r"[\x00-\x1f\x7f-\x9f]")
        self.assertEqual(u["scoped"][0]["name"], "Gemini ]52;c;ZXZpbA==Models2J")
        self.assertEqual(u["seven_day"]["resets_at"], "2026-11-28T10:45:55Z[0m")
        self.assertEqual(u["scoped"][1]["resets_at"], "2026-11-29T04:38:52Z")

    def test_odd_reset_times_do_not_crash(self):
        from agyswap import output, pace
        for value in ("0001-01-01T00:00:00", "9999-12-31T23:59:59", "garbage", "2026-11-28T10:45:55Z[0m", 5, None):
            self.assertIsNone(pace.parse_ts(value), value)
        rows = usage.parse_usage("Gemini Models\tWeekly Limit Remaining\t50%\t0001-01-01T00:00:00\n")
        self.assertEqual(output.usage_lines(rows, 1_800_000_000.0, 1_800_000_000.0), ["Gemini Models", "└ 7d:  50%"])

    def test_limits_grouped_under_their_category_5h_first(self):
        from agyswap import output
        rows = usage.parse_usage("Gemini Models\tWeekly Limit Remaining\t90%\t\n"
                                 "Gemini Models\tFive Hour Limit Remaining\t80%\t\n"
                                 "Claude and GPT models\tWeekly Limit Remaining\t70%\t\n")
        self.assertEqual(output.usage_lines(rows, None, 1_800_000_000.0),
                         ["Gemini Models", "├ 5h:  20%", "└ 7d:  10%", "Claude and GPT models", "└ 7d:  30%"])

    def test_classify(self):
        self.assertTrue(usage.classify(AUTH).relogin)
        self.assertEqual(usage.classify("").error, "parse")
        self.assertEqual(usage.classify("", timed_out=True).error, "timeout")
        self.assertIsNotNone(usage.classify(FREE).usage)

    def test_limits_for_model(self):
        u = usage.parse_usage(PRO)
        names = lambda m: sorted({(w["name"], w["window"]) for w in usage.limits_for_model(m, u)})
        self.assertEqual(names(None), [("Gemini Models", "5h"), ("Gemini Models", "7d")])
        self.assertEqual(names("gemini-3.8-flash-high"), [("Gemini Models", "5h"), ("Gemini Models", "7d")])
        self.assertEqual(names("gemini-4-argon-high"),
                         [("Gemini 4 Argon", "7d"), ("Gemini Models", "5h"), ("Gemini Models", "7d")])
        self.assertEqual(names("claude-opus-5-5-high"),
                         [("Claude and GPT models", "5h"), ("Claude and GPT models", "7d")])
        self.assertEqual(names("gpt-oss-120b-medium"),
                         [("Claude and GPT models", "5h"), ("Claude and GPT models", "7d")])
        self.assertEqual(usage.model_id(["-p", "q", "--model", "claude-sonnet-4-6"]), "claude-sonnet-4-6")
        self.assertEqual(usage.model_id(["--model=gpt-oss-120b-medium"]), "gpt-oss-120b-medium")
        self.assertIsNone(usage.model_id(["-p", "q"]))

    def test_fetch_with_fake_executable(self):
        import os
        import tempfile
        from pathlib import Path
        from unittest import mock
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, {"AGYSWAP_HOME": tmp}):
            self.assertEqual(usage.fetch(dict(os.environ), agy=Path(tmp) / "missing.exe").error, "spawn")
            fake = Path(tmp) / "agy.bat"
            fake.write_text("@echo off\r\necho Gemini Models\tWeekly Limit Remaining\t40%%\t2026-11-28T10:45:55Z\r\n",
                            encoding="ascii")
            result = usage.fetch(dict(os.environ), agy=fake, timeout=30)
            self.assertEqual(result.usage["seven_day"]["pct"], 60.0)

    def test_fetch_can_be_cancelled(self):
        """The dashboard closing ends a running agy after a short grace instead of waiting out the timeout."""
        import os
        import tempfile
        import time
        from pathlib import Path
        from unittest import mock
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, {"AGYSWAP_HOME": tmp}), \
                mock.patch.object(usage, "INTERRUPT_GRACE_S", 0.2):
            fake = Path(tmp) / "agy.bat"
            fake.write_text("@echo off\r\nping -n 30 127.0.0.1 >nul\r\n", encoding="ascii")
            started = time.monotonic()
            with self.assertRaises(usage.Cancelled):
                usage.fetch(dict(os.environ), agy=fake, timeout=60, cancel=lambda: time.monotonic() - started > 0.5)
            self.assertLess(time.monotonic() - started, 15)

    def test_envs(self):
        from pathlib import Path
        env = usage.profile_env(Path("C:/x/school"))
        self.assertEqual(env["USERPROFILE"], env["HOME"])
        self.assertIn("SSH_CONNECTION", env)
        import os
        from unittest import mock
        with mock.patch.dict(os.environ, {"SSH_CONNECTION": "1 2 3 4", "SSH_CLIENT": "1 2 3"}):
            self.assertNotIn("SSH_CONNECTION", usage.live_env())


if __name__ == "__main__":
    unittest.main()
