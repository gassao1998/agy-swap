"""The Credential Manager wrapper: everything runs against a fake advapi32 and never touches the real Credential Manager."""
import ctypes
import unittest

from agyswap import credman
from agyswap.credman import CREDENTIALW, ERROR_NOT_FOUND, WinCred
from agyswap.errors import CredentialError


class FakeApi:
    """Imitates the calling conventions of CredReadW, CredWriteW, CredDeleteW and CredFree, storing everything in memory."""

    def __init__(self):
        self.store = {}
        self.fail_code = None
        self.freed = 0
        self.last_error = 0
        self._keep = []

    def CredReadW(self, target, cred_type, flags, ppcred):
        if self.fail_code:
            self.last_error = self.fail_code
            return 0
        item = self.store.get(target)
        if item is None:
            self.last_error = ERROR_NOT_FOUND
            return 0
        blob = item["blob"]
        buf = (ctypes.c_ubyte * len(blob)).from_buffer_copy(blob)
        c = CREDENTIALW()
        c.Type, c.TargetName, c.Persist, c.UserName = item["type"], target, item["persist"], item["username"]
        c.CredentialBlobSize = len(blob)
        c.CredentialBlob = ctypes.cast(buf, ctypes.POINTER(ctypes.c_ubyte))
        c.LastWritten.dwHighDateTime, c.LastWritten.dwLowDateTime = 0x01DB0000, 0
        self._keep += [buf, c]
        ppcred._obj.contents = c
        return 1

    def CredWriteW(self, pcred, flags):
        if self.fail_code:
            self.last_error = self.fail_code
            return 0
        c = pcred._obj
        self.store[c.TargetName] = {
            "blob": ctypes.string_at(c.CredentialBlob, c.CredentialBlobSize),
            "type": c.Type, "persist": c.Persist, "username": c.UserName,
            "flags": c.Flags, "comment": c.Comment, "alias": c.TargetAlias, "attrs": c.AttributeCount,
        }
        return 1

    def CredDeleteW(self, target, cred_type, flags):
        if self.fail_code:
            self.last_error = self.fail_code
            return 0
        if self.store.pop(target, None) is None:
            self.last_error = ERROR_NOT_FOUND
            return 0
        return 1

    def CredFree(self, p):
        self.freed += 1


class CredmanTest(unittest.TestCase):
    def setUp(self):
        self.api = FakeApi()
        self.cred = WinCred(api=self.api, get_last_error=lambda: self.api.last_error)

    def test_missing_is_none(self):
        self.assertIsNone(self.cred.read("gemini:antigravity"))

    def test_write_then_read_round_trip_with_v1_attributes(self):
        blob = b'{"id_token_email":"a@b.c"}'
        self.cred.write("gemini:antigravity", blob, username="antigravity")
        stored = self.api.store["gemini:antigravity"]
        self.assertEqual((stored["type"], stored["persist"], stored["username"]), (1, 2, "antigravity"))
        self.assertEqual((stored["flags"], stored["comment"], stored["alias"], stored["attrs"]), (0, None, None, 0))
        got = self.cred.read("gemini:antigravity")
        self.assertEqual(got.blob, blob)
        self.assertEqual(got.username, "antigravity")
        self.assertIsNotNone(got.last_written)
        self.assertEqual(self.api.freed, 1)
        self.assertNotIn("a@b.c", repr(got))

    def test_other_error_codes_raise_with_code(self):
        self.api.fail_code = 5
        for call in (lambda: self.cred.read("x"), lambda: self.cred.write("x", b"1", username="u"),
                     lambda: self.cred.delete("x")):
            with self.assertRaises(CredentialError) as ctx:
                call()
            self.assertEqual(ctx.exception.code, 5)
            self.assertIn("5", str(ctx.exception))

    def test_delete(self):
        self.assertFalse(self.cred.delete("gemini:antigravity"))
        self.cred.write("gemini:antigravity", b"x", username="antigravity")
        self.assertTrue(self.cred.delete("gemini:antigravity"))
        self.assertNotIn("gemini:antigravity", self.api.store)

    def test_oversize_blob_refused_before_write(self):
        with self.assertRaises(CredentialError):
            self.cred.write("x", b"0" * (credman.CRED_MAX_CREDENTIAL_BLOB_SIZE + 1), username="u")
        self.assertEqual(self.api.store, {})

    def test_filetime_conversion(self):
        ft = credman.FILETIME()
        ft.dwHighDateTime, ft.dwLowDateTime = divmod((11644473600 + 1000) * 10_000_000, 1 << 32)
        self.assertAlmostEqual(credman.filetime_to_epoch(ft), 1000.0, places=3)
        self.assertIsNone(credman.filetime_to_epoch(credman.FILETIME()))


if __name__ == "__main__":
    unittest.main()
