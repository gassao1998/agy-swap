"""Check that the safety net really blocks: if these tests fail, other tests may touch the real Credential Manager or real data."""
import ctypes
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from agyswap import paths
from agyswap.credman import WinCred
from tests import guard


class GuardTest(unittest.TestCase):
    def test_real_credential_functions_are_blocked(self):
        for name in ("CredWriteW", "CredDeleteW", "CredReadW"):
            with self.assertRaises(guard.GuardTripped):
                getattr(ctypes.WinDLL("advapi32", use_last_error=True), name)
        with self.assertRaises(guard.GuardTripped):
            WinCred().write(paths.KEYRING_TARGET, b"x", username=paths.KEYRING_USERNAME)
        with self.assertRaises(guard.GuardTripped):
            WinCred().delete(paths.KEYRING_TARGET)

    def test_real_data_folders_are_blocked(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "probe"
            source.write_bytes(b"")
            for root in guard.PROTECTED:
                target = Path(root) / "guard-probe.tmp"
                with self.assertRaises(guard.GuardTripped):
                    open(target, "rb")
                with self.assertRaises(guard.GuardTripped):
                    os.replace(source, target)
                self.assertTrue(source.exists())

    def test_agy_and_acl_tools_are_blocked(self):
        for argv in (["agy", "-p", "/usage"], ["icacls", "."], ["whoami", "/user"], ["cmdkey", "/list"], ["git", "pull"]):
            with self.assertRaises(guard.GuardTripped):
                subprocess.run(argv, capture_output=True)

    def test_default_home_is_not_the_real_one(self):
        self.assertFalse(guard.protected(paths.base()))
        self.assertFalse(paths.agy_bin().exists())


if __name__ == "__main__":
    unittest.main()
