"""Locations of the data files and of agy. Environment variables are re-read on every call so tests can point AGYSWAP_HOME at a temp folder."""
from __future__ import annotations

import functools
import os
from pathlib import Path

from agyswap.errors import ConfigError

KEYRING_TARGET = "gemini:antigravity"
KEYRING_USERNAME = "antigravity"
TOKEN_REL = Path(".gemini") / "antigravity-cli" / "antigravity-oauth-token"
# With these two variables agy skips Credential Manager and reads the login file under %USERPROFILE% (measured 2026-11-22)
SSH_ENV = {"SSH_CONNECTION": "127.0.0.1 0 127.0.0.1 22", "SSH_CLIENT": "127.0.0.1 0 22"}


def system_exe(name: str) -> str:
    """Full path of a Windows tool in System32. A bare name would let CreateProcess run a same-named program from the current folder first."""
    return os.path.join(os.environ.get("SystemRoot") or r"C:\Windows", "System32", name + ".exe")


def local_appdata() -> Path:
    return Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")


@functools.lru_cache(maxsize=1)
def real_home() -> str | None:
    """This Windows user's real home directory, asked from the process token and not from USERPROFILE, which agyswap run may have changed."""
    try:
        import ctypes
        from ctypes import wintypes

        userenv = ctypes.WinDLL("userenv", use_last_error=True)
        fn = userenv.GetUserProfileDirectoryW
        fn.argtypes = [wintypes.HANDLE, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
        fn.restype = wintypes.BOOL
        size = wintypes.DWORD(1024)
        buf = ctypes.create_unicode_buffer(size.value)
        # -4 is the pseudo handle for the current process token (GetCurrentProcessToken); it needs no separate open or close
        return buf.value if fn(wintypes.HANDLE(-4), buf, ctypes.byref(size)) and buf.value else None
    except (OSError, AttributeError):
        return None


def _inside(path: Path | str, root: str) -> bool:
    p, r = os.path.normcase(os.path.realpath(path)), os.path.normcase(os.path.realpath(root))
    return p.startswith(r.rstrip(os.sep) + os.sep)


def base() -> Path:
    """The data folder. It must lie inside the user's real profile folder: elsewhere (for example D:\\agyswap) other Windows users may be able to
    create or change files in it, and the logins written there would be exposed."""
    root = Path(os.environ.get("AGYSWAP_HOME") or local_appdata() / "agyswap")
    home = real_home()
    if not home or not _inside(root, home):
        raise ConfigError(f"The agyswap data folder {root} is not inside your user profile folder ({home or 'unknown'}); "
                          "set AGYSWAP_HOME to a folder under it")
    return root


def profiles_dir() -> Path:
    return base() / "profiles"


def profile_home(folder: str) -> Path:
    """Account folder. The name comes from the roster or an alias; anything that could escape profiles is rejected (.., separators, drive letters, trailing dots)."""
    bad = (not folder or folder in (".", "..") or folder != folder.strip() or folder.endswith(".")
           or any(c in folder for c in '\\/:'))
    home = profiles_dir() / folder
    if bad or os.path.normcase(os.path.abspath(home.parent)) != os.path.normcase(os.path.abspath(profiles_dir())):
        raise ConfigError(f"Invalid account folder name {folder!r}")
    return home


def token_path(folder: str) -> Path:
    return profile_home(folder) / TOKEN_REL


APIKEY_USERNAME = "agyswap"


def apikey_target(folder: str) -> str:
    """The Credential Manager entry holding an API key account's Gemini API key (API keys use entries of their own)."""
    profile_home(folder)  # the same name check as the account folder
    return f"agyswap:apikey:{folder}"


def run_pid_file(folder: str) -> Path:
    """Marks an agyswap run in progress for this account."""
    return profile_home(folder) / ".run.pid"


def agy_bin() -> Path:
    return Path(os.environ.get("AGY_BIN") or local_appdata() / "agy" / "bin" / "agy.exe")


def sequence_file() -> Path:
    return base() / "sequence.json"


def mappings_file() -> Path:
    return base() / "mappings.json"


def settings_file() -> Path:
    return base() / "settings.json"


def usage_cache_file() -> Path:
    return base() / "cache" / "usage.json"


def migrations_file() -> Path:
    return base() / ".migrations.json"


def pending_switch_file() -> Path:
    return base() / "pending_switch.json"


def autoswitch_state_file() -> Path:
    """Counters agyswap auto keeps across ticks and processes (cswap #464)."""
    return base() / "autoswitch_state.json"


def backup_dir() -> Path:
    return base() / "backup"


def last_live_file() -> Path:
    """Backup of the Credential Manager login taken before a switch, for manual restore."""
    return backup_dir() / "last-live.bin"


def log_file() -> Path:
    return base() / "agyswap.log"


def lock_file() -> Path:
    return base() / ".lock"


def auto_lock_file() -> Path:
    """Held by a running agyswap auto loop, so config set can tell whether a changed autoswitch setting takes effect (cswap #90)."""
    return base() / ".auto.lock"


def update_check_file() -> Path:
    return base() / "cache" / "update_check.json"
