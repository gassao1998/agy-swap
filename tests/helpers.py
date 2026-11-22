"""Shared test helpers: a temporary AGYSWAP_HOME per test, the permission lockdown replaced by a fake, fake tokens as login content,
and read-only DACL helpers for the tests that use the real lockdown on temp folders."""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agyswap import paths
from agyswap.credman import Credential
from agyswap.login import LiveLogin

SECRET = "SECRET-TOKEN-VALUE"


def token_blob(email: str, secret: str = SECRET, expiry: str | None = None) -> bytes:
    token = {"access_token": secret, "refresh_token": secret + "-r" if secret else "", "token_type": "Bearer"}
    if expiry:
        token["expiry"] = expiry
    return json.dumps({"auth_method": "oauth", "id_token_email": email, "token": token}).encode("utf-8")


def live(email: str | None = None, blob: bytes | None = None, error: str | None = None, code: int | None = None) -> LiveLogin:
    if error:
        return LiveLogin(error=error, code=code)
    if email is None and blob is None:
        return LiveLogin()
    return LiveLogin(cred=Credential(target=paths.KEYRING_TARGET, blob=blob or token_blob(email),
                                     username=paths.KEYRING_USERNAME, last_written=1_795_000_000.0))


class FakeStore:
    """A fake Credential Manager with the same interface as WinCred's read, write and delete. fail_writes makes the first few writes fail,
    corrupt_writes makes the first few writes store something different from what was asked, and read_error makes reads raise."""

    def __init__(self, blob: bytes | None = None):
        self.cred = None
        self.writes: list[bytes] = []
        self.fail_writes = 0
        self.corrupt_writes = 0
        self.read_error: int | None = None
        self.on_write = None
        if blob is not None:
            self._set(blob)

    def _set(self, blob, username=paths.KEYRING_USERNAME, persist=2):
        self.cred = Credential(target=paths.KEYRING_TARGET, blob=blob, username=username, persist=persist)

    def read(self, target):
        if self.read_error:
            from agyswap.errors import CredentialError
            raise CredentialError(f"CredReadW failed for {target}, error code {self.read_error}", self.read_error)
        return self.cred

    def write(self, target, blob, *, username, persist=2, cred_type=1):
        self.writes.append(blob)
        if self.on_write:
            self.on_write()
        if self.fail_writes:
            self.fail_writes -= 1
            from agyswap.errors import CredentialError
            raise CredentialError(f"CredWriteW failed for {target}, error code 5", 5)
        if self.corrupt_writes:
            self.corrupt_writes -= 1
            blob = blob + b" "
        self._set(blob, username, persist)

    def delete(self, target):
        existed, self.cred = self.cred is not None, None
        return existed


class DictStore:
    """A fake Credential Manager that keeps every target apart (gemini:antigravity and the agyswap:apikey:<folder> entries)."""

    def __init__(self, live_blob: bytes | None = None):
        self.items: dict[str, Credential] = {}
        self.writes: list[str] = []
        self.fail_writes = 0
        if live_blob is not None:
            self.items[paths.KEYRING_TARGET] = Credential(target=paths.KEYRING_TARGET, blob=live_blob,
                                                          username=paths.KEYRING_USERNAME)

    def read(self, target):
        return self.items.get(target)

    def write(self, target, blob, *, username, persist=2, cred_type=1):
        self.writes.append(target)
        if self.fail_writes:
            self.fail_writes -= 1
            from agyswap.errors import CredentialError
            raise CredentialError(f"CredWriteW failed for {target}, error code 5", 5)
        self.items[target] = Credential(target=target, blob=blob, username=username, persist=persist)

    def delete(self, target):
        return self.items.pop(target, None) is not None


def process_sid() -> str:
    """This process's user SID, read from its token through ctypes (whoami is blocked by the safety net)."""
    import ctypes
    from ctypes import wintypes

    advapi, kernel = ctypes.WinDLL("advapi32", use_last_error=True), ctypes.WinDLL("kernel32", use_last_error=True)
    advapi.GetTokenInformation.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
    advapi.ConvertSidToStringSidW.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)]
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    buf, size, text = ctypes.create_string_buffer(256), wintypes.DWORD(), ctypes.c_void_p()
    # -4: the current process token pseudo handle; 1: TokenUser, whose first field points at the SID
    if not advapi.GetTokenInformation(wintypes.HANDLE(-4), 1, buf, 256, ctypes.byref(size)):
        raise ctypes.WinError(ctypes.get_last_error())
    if not advapi.ConvertSidToStringSidW(ctypes.c_void_p.from_buffer(buf), ctypes.byref(text)):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        return ctypes.wstring_at(text.value)
    finally:
        kernel.LocalFree(text)


def dacl_sddl(path) -> str:
    """The DACL of a file or folder in SDDL form, e.g. D:P(A;OICI;FA;;;S-1-5-21-...)."""
    import ctypes
    from ctypes import wintypes

    advapi, kernel = ctypes.WinDLL("advapi32", use_last_error=True), ctypes.WinDLL("kernel32", use_last_error=True)
    out = ctypes.POINTER(ctypes.c_void_p)
    advapi.GetNamedSecurityInfoW.argtypes = [wintypes.LPCWSTR, ctypes.c_int, wintypes.DWORD, out, out, out, out, out]
    advapi.GetNamedSecurityInfoW.restype = wintypes.DWORD
    advapi.ConvertSecurityDescriptorToStringSecurityDescriptorW.argtypes = [ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, out, ctypes.c_void_p]
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    descriptor, text = ctypes.c_void_p(), ctypes.c_void_p()
    rc = advapi.GetNamedSecurityInfoW(str(path), 1, 4, None, None, None, None, ctypes.byref(descriptor))
    if rc:
        raise ctypes.WinError(rc)
    try:
        if not advapi.ConvertSecurityDescriptorToStringSecurityDescriptorW(descriptor, 1, 4, ctypes.byref(text), None):
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            return ctypes.wstring_at(text.value)
        finally:
            kernel.LocalFree(text)
    finally:
        kernel.LocalFree(descriptor)


def set_dacl_sddl(path, sddl: str) -> None:
    """Set a file's or folder's DACL from SDDL, for example to plant another user's explicit entry."""
    import ctypes
    from ctypes import wintypes

    advapi, kernel = ctypes.WinDLL("advapi32", use_last_error=True), ctypes.WinDLL("kernel32", use_last_error=True)
    advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD,
                                                                            ctypes.POINTER(ctypes.c_void_p), ctypes.c_void_p]
    advapi.GetSecurityDescriptorDacl.argtypes = [ctypes.c_void_p, ctypes.POINTER(wintypes.BOOL), ctypes.POINTER(ctypes.c_void_p),
                                                 ctypes.POINTER(wintypes.BOOL)]
    advapi.SetNamedSecurityInfoW.argtypes = [wintypes.LPWSTR, ctypes.c_int, wintypes.DWORD] + [ctypes.c_void_p] * 4
    advapi.SetNamedSecurityInfoW.restype = wintypes.DWORD
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    descriptor, present, dacl, defaulted = ctypes.c_void_p(), wintypes.BOOL(), ctypes.c_void_p(), wintypes.BOOL()
    if not advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW(sddl, 1, ctypes.byref(descriptor), None):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        advapi.GetSecurityDescriptorDacl(descriptor, ctypes.byref(present), ctypes.byref(dacl), ctypes.byref(defaulted))
        flags = 0x4 | (0x80000000 if sddl.startswith("D:P") else 0x20000000)  # protected, or unprotected (inherits)
        rc = advapi.SetNamedSecurityInfoW(str(path), 1, flags, None, None, dacl, None)
        if rc:
            raise ctypes.WinError(rc)
    finally:
        kernel.LocalFree(descriptor)


class TempHome(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.home = Path(self._tmp.name)
        env = mock.patch.dict(os.environ, {"AGYSWAP_HOME": str(self.home), "NO_COLOR": "1"})
        env.start()
        self.addCleanup(env.stop)
        acl = mock.patch("agyswap.fsutil.restrict_to_current_user", return_value=True)
        acl.start()
        self.addCleanup(acl.stop)
        for name in ("agyswap.migrate.restrict_to_current_user",):
            p = mock.patch(name, return_value=True)
            p.start()
            self.addCleanup(p.stop)
        p = mock.patch("agyswap.update_check.find_git", return_value=None)  # the daily update check stays offline in tests
        p.start()
        self.addCleanup(p.stop)
        self.real_home = self.home / "userhome"
        p = mock.patch("agyswap.usage.real_home", return_value=str(self.real_home))
        p.start()
        self.addCleanup(p.stop)
        self.addCleanup(self._tmp.cleanup)

    def write_token(self, folder: str, email: str, **kwargs) -> Path:
        path = paths.token_path(folder)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(token_blob(email, **kwargs))
        return path
