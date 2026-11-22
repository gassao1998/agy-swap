"""Identifying a login blob: only id_token_email, the access token's expiry and hashes are taken; the tokens themselves never leave memory."""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from agyswap import paths
from agyswap.credman import Credential, WinCred
from agyswap.errors import CredentialError
from agyswap.fsutil import read_bytes, sha256_hex


def _parse(blob: bytes | None) -> dict | None:
    if not blob:
        return None
    try:
        data = json.loads(blob.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _text(value) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def login_email(blob: bytes | None) -> str | None:
    return _text((_parse(blob) or {}).get("id_token_email"))


def usable_login(blob: bytes | None) -> bool:
    """A login that can be stored in an account folder: JSON, has an email, and both the access and refresh token under token are non-empty.
    When a refresh fails agy may blank the tokens, and writing that to the folder would overwrite a login that still works (cswap #383, #444)."""
    data = _parse(blob)
    token = data.get("token") if data else None
    return bool(data and _text(data.get("id_token_email")) and isinstance(token, dict)
                and _text(token.get("access_token")) and _text(token.get("refresh_token")))


def login_expiry(blob: bytes | None) -> float | None:
    """Expiry of the access token (epoch seconds). Used to tell which of two logins for the same account is newer."""
    token = (_parse(blob) or {}).get("token")
    value = token.get("expiry") if isinstance(token, dict) else None
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except (ValueError, OverflowError, OSError):
        return None


def read_token_file(path: Path) -> bytes | None:
    try:
        return read_bytes(path)
    except OSError:
        return None


def read_token_file_stat(path: Path) -> tuple[bytes, float] | None:
    """Read the content and the modification time from one open, so the file cannot be swapped between two calls."""
    try:
        with open(path, "rb") as f:
            return f.read(), os.fstat(f.fileno()).st_mtime
    except OSError:
        return None


@dataclass(frozen=True)
class LiveLogin:
    """State of gemini:antigravity in Credential Manager. A set error means it could not be read (not that it is absent)."""

    cred: Credential | None = None
    error: str | None = None

    @property
    def present(self) -> bool:
        return self.cred is not None

    @property
    def email(self) -> str | None:
        return login_email(self.cred.blob) if self.cred else None

    @property
    def sha(self) -> str | None:
        return sha256_hex(self.cred.blob) if self.cred else None


def read_live_login(store: WinCred | None = None) -> LiveLogin:
    try:
        return LiveLogin(cred=(store or WinCred()).read(paths.KEYRING_TARGET))
    except (CredentialError, OSError) as e:
        return LiveLogin(error=str(e))
