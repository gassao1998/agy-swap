"""ctypes wrapper for Windows Credential Manager (advapi32 CredReadW, CredWriteW, CredDeleteW, CredFree).

Token content stays in memory as bytes; this module never prints or logs it. Error codes:
ERROR_NOT_FOUND (1168) means the entry does not exist, and anything else raises CredentialError with the code.
"""
from __future__ import annotations

import ctypes
from ctypes import wintypes
from dataclasses import dataclass

from agyswap.errors import CredentialError

ERROR_NOT_FOUND = 1168
CRED_TYPE_GENERIC = 1
CRED_PERSIST_LOCAL_MACHINE = 2
CRED_MAX_CREDENTIAL_BLOB_SIZE = 5 * 512
# FILETIME counts 100-nanosecond ticks from 1601-01-01
_EPOCH_DIFF_S = 11644473600


class FILETIME(ctypes.Structure):
    _fields_ = [("dwLowDateTime", wintypes.DWORD), ("dwHighDateTime", wintypes.DWORD)]


class CREDENTIALW(ctypes.Structure):
    _fields_ = [
        ("Flags", wintypes.DWORD),
        ("Type", wintypes.DWORD),
        ("TargetName", wintypes.LPWSTR),
        ("Comment", wintypes.LPWSTR),
        ("LastWritten", FILETIME),
        ("CredentialBlobSize", wintypes.DWORD),
        ("CredentialBlob", ctypes.POINTER(ctypes.c_ubyte)),
        ("Persist", wintypes.DWORD),
        ("AttributeCount", wintypes.DWORD),
        ("Attributes", ctypes.c_void_p),
        ("TargetAlias", wintypes.LPWSTR),
        ("UserName", wintypes.LPWSTR),
    ]


PCREDENTIALW = ctypes.POINTER(CREDENTIALW)


@dataclass(frozen=True)
class Credential:
    target: str
    blob: bytes
    type: int = CRED_TYPE_GENERIC
    persist: int = CRED_PERSIST_LOCAL_MACHINE
    username: str | None = None
    flags: int = 0
    comment: str | None = None
    target_alias: str | None = None
    attribute_count: int = 0
    last_written: float | None = None  # epoch seconds

    def __repr__(self) -> str:  # keeps tokens out of debug output
        return f"Credential(target={self.target!r}, blob=<{len(self.blob)} bytes>, username={self.username!r})"


def filetime_to_epoch(ft: FILETIME) -> float | None:
    ticks = (ft.dwHighDateTime << 32) | ft.dwLowDateTime
    return ticks / 1e7 - _EPOCH_DIFF_S if ticks else None


def _real_api():
    api = ctypes.WinDLL("advapi32", use_last_error=True)
    api.CredReadW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.POINTER(PCREDENTIALW)]
    api.CredReadW.restype = wintypes.BOOL
    api.CredWriteW.argtypes = [PCREDENTIALW, wintypes.DWORD]
    api.CredWriteW.restype = wintypes.BOOL
    api.CredDeleteW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD]
    api.CredDeleteW.restype = wintypes.BOOL
    api.CredFree.argtypes = [ctypes.c_void_p]
    api.CredFree.restype = None
    return api


class WinCred:
    """api and get_last_error can be replaced with fakes, so tests never touch the real Credential Manager."""

    def __init__(self, api=None, get_last_error=None):
        self._api = api
        self._get_last_error = get_last_error or ctypes.get_last_error

    @property
    def api(self):
        if self._api is None:
            self._api = _real_api()
        return self._api

    def read(self, target: str, cred_type: int = CRED_TYPE_GENERIC) -> Credential | None:
        """Return None when the entry does not exist; any other failure raises CredentialError."""
        pcred = PCREDENTIALW()
        if not self.api.CredReadW(target, cred_type, 0, ctypes.byref(pcred)):
            code = self._get_last_error()
            if code == ERROR_NOT_FOUND:
                return None
            raise CredentialError(f"CredReadW failed for {target}, error code {code}", code)
        try:
            c = pcred.contents
            size = c.CredentialBlobSize
            blob = ctypes.string_at(c.CredentialBlob, size) if size and c.CredentialBlob else b""
            return Credential(
                target=c.TargetName or target,
                blob=blob,
                type=c.Type,
                persist=c.Persist,
                username=c.UserName,
                flags=c.Flags,
                comment=c.Comment,
                target_alias=c.TargetAlias,
                attribute_count=c.AttributeCount,
                last_written=filetime_to_epoch(c.LastWritten),
            )
        finally:
            self.api.CredFree(pcred)

    def write(self, target: str, blob: bytes, *, username: str | None,
              persist: int = CRED_PERSIST_LOCAL_MACHINE, cred_type: int = CRED_TYPE_GENERIC) -> None:
        """Write with the attributes measured on a real agy login: generic type, no Comment, TargetAlias or Attributes, Flags 0."""
        if len(blob) > CRED_MAX_CREDENTIAL_BLOB_SIZE:
            raise CredentialError(f"Login is {len(blob)} bytes, over the Credential Manager limit of {CRED_MAX_CREDENTIAL_BLOB_SIZE}")
        buf = (ctypes.c_ubyte * len(blob)).from_buffer_copy(blob) if blob else None
        cred = CREDENTIALW()
        cred.Flags = 0
        cred.Type = cred_type
        cred.TargetName = target
        cred.CredentialBlobSize = len(blob)
        cred.CredentialBlob = ctypes.cast(buf, ctypes.POINTER(ctypes.c_ubyte)) if buf is not None else None
        cred.Persist = persist
        cred.UserName = username
        if not self.api.CredWriteW(ctypes.byref(cred), 0):
            code = self._get_last_error()
            raise CredentialError(f"CredWriteW failed for {target}, error code {code}", code)

    def delete(self, target: str, cred_type: int = CRED_TYPE_GENERIC) -> bool:
        """Return True when deleted, False when it was not there; any other failure raises CredentialError."""
        if self.api.CredDeleteW(target, cred_type, 0):
            return True
        code = self._get_last_error()
        if code == ERROR_NOT_FOUND:
            return False
        raise CredentialError(f"CredDeleteW failed for {target}, error code {code}", code)
