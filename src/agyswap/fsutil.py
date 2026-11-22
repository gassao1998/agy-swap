"""Atomic file writes, JSON reads and writes, hashing, file locks and account folder permissions."""
from __future__ import annotations

import contextlib
import ctypes
import hashlib
import json
import os
import re
import stat
import subprocess
import tempfile
import time
from ctypes import wintypes
from pathlib import Path

from agyswap import paths
from agyswap.errors import AgyswapError, ConfigError
from agyswap.paths import system_exe

# When antivirus or an indexer happens to hold a file open, Windows returns ERROR_ACCESS_DENIED, ERROR_SHARING_VIOLATION
# or ERROR_LOCK_VIOLATION, usually released within milliseconds (cswap PR #462 measured about 40% of replaces hitting it).
# A persistent permission problem is still raised once the retries run out.
_TRANSIENT_WIN_ERRORS = frozenset({5, 32, 33})
_ATTEMPTS = 10


def _retry(fn, attempts: int = _ATTEMPTS):
    delay = 0.002
    for attempt in range(attempts):
        try:
            return fn()
        except OSError as e:
            if getattr(e, "winerror", None) not in _TRANSIENT_WIN_ERRORS or attempt == attempts - 1:
                raise
            time.sleep(delay)
            delay = min(delay * 2, 0.25)


def read_bytes(path: Path) -> bytes:
    return _retry(lambda: Path(path).read_bytes())


def replace_with_retry(src, dst) -> None:
    _retry(lambda: os.replace(src, dst))


def _write_temp(path: Path, data: bytes) -> str:
    """Write the temp file in the target's own folder, so it inherits that folder's permissions from the start."""
    ensure_data_root()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise
    return tmp


def atomic_write_bytes(path: Path, data: bytes) -> None:
    tmp = _write_temp(path, data)
    try:
        replace_with_retry(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def atomic_write_json(path: Path, data: object) -> None:
    atomic_write_bytes(path, (json.dumps(data, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))


def _replace_file_w(target: str, replacement: str, backup: str) -> None:
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    fn = k32.ReplaceFileW
    fn.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
                   wintypes.LPVOID, wintypes.LPVOID]
    fn.restype = wintypes.BOOL
    if not fn(target, replacement, backup, 0, None, None):
        raise ctypes.WinError(ctypes.get_last_error())


def replace_keep_backup(path: Path, data: bytes) -> None:
    """Replace path with ReplaceFileW, keeping the old file as .bak, then read back and compare.
    ReplaceFileW applies the old file's permissions to the new one, so a locked-down login file stays readable only by the current user."""
    tmp = _write_temp(path, data)
    try:
        if path.exists():
            _retry(lambda: _replace_file_w(str(path), tmp, str(path.with_name(path.name + ".bak"))))
        else:
            replace_with_retry(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise
    if sha256_hex(read_bytes(path)) != sha256_hex(data):
        raise AgyswapError(f"{path} does not match what was written when read back; the old file is at {path.name}.bak")


def read_json(path: Path, default=None, *, strict: bool = False):
    """Read JSON. A missing file returns default; a damaged one raises under strict (a write-back would overwrite a recoverable file) and returns default otherwise."""
    try:
        text = read_bytes(path).decode("utf-8")
    except FileNotFoundError:
        return default
    except (OSError, UnicodeDecodeError) as e:
        if strict:
            raise ConfigError(f"Could not read {path}: {e}") from e
        return default
    try:
        return json.loads(text)
    except ValueError as e:
        if strict:
            raise ConfigError(f"{path} is not valid JSON ({e}); fix or delete it and try again") from e
        return default


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


@contextlib.contextmanager
def file_lock(path: Path, retries: int = 3, delay: float = 1.0):
    """Take a non-blocking lock on byte 1 of path, retrying up to retries times.
    msvcrt locks are tied to the file handle, so opening the file again in the same process is also blocked; do not call another function that locks the same file while holding the lock."""
    import msvcrt

    ensure_data_root()
    path.parent.mkdir(parents=True, exist_ok=True)
    f = open(path, "a+b")
    try:
        for attempt in range(retries + 1):
            try:
                f.seek(0)
                msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
                break
            except OSError:
                if attempt == retries:
                    raise AgyswapError(f"Another agyswap is using {path}; try again shortly")
                time.sleep(delay)
        try:
            yield
        finally:
            f.seek(0)
            with contextlib.suppress(OSError):
                msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
    finally:
        f.close()


_SID_RE = re.compile(r"S-1-\d+(?:-\d+)+")


def current_user_sid() -> str | None:
    """Get the SID from the process token (whoami reads the token), not trusting the USERNAME and USERDOMAIN environment variables, which can be changed."""
    try:
        out = subprocess.run([system_exe("whoami"), "/user", "/fo", "csv", "/nh"], capture_output=True, encoding="oem",
                             errors="replace", timeout=30).stdout
    except (OSError, subprocess.TimeoutExpired):
        return None
    m = _SID_RE.search(out or "")
    return m.group(0) if m else None


# SYSTEM and Administrators may own the user's folders too (an elevated prompt makes Administrators the owner of what it creates); both already control the machine
_TRUSTED_OWNERS = ("S-1-5-18", "S-1-5-32-544")


def owner_sid(path: Path) -> str | None:
    """The owner of a file or folder as a SID string, or None when it cannot be read."""
    advapi = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    get_info = advapi.GetNamedSecurityInfoW
    out = ctypes.POINTER(ctypes.c_void_p)
    get_info.argtypes = [wintypes.LPCWSTR, ctypes.c_int, wintypes.DWORD, out, out, out, out, out]
    get_info.restype = wintypes.DWORD
    to_text = advapi.ConvertSidToStringSidW
    to_text.argtypes = [ctypes.c_void_p, out]
    to_text.restype = wintypes.BOOL
    local_free = kernel.LocalFree
    local_free.argtypes = [ctypes.c_void_p]
    local_free.restype = ctypes.c_void_p
    owner, descriptor, text = ctypes.c_void_p(), ctypes.c_void_p(), ctypes.c_void_p()
    # 1, 1: SE_FILE_OBJECT, OWNER_SECURITY_INFORMATION; owner points into descriptor, which is freed last
    if get_info(str(path), 1, 1, ctypes.byref(owner), None, None, None, ctypes.byref(descriptor)) != 0:
        return None
    try:
        if not to_text(owner, ctypes.byref(text)):
            return None
        try:
            return ctypes.wstring_at(text.value)
        finally:
            local_free(text)
    finally:
        local_free(descriptor)


def is_reparse_point(path) -> bool:
    """A junction, symbolic link or other reparse point (Path.is_symlink misses junctions). A missing path is not one."""
    try:
        return bool(os.lstat(path).st_file_attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT)
    except OSError:
        return False


def check_owner(path: Path, sid: str) -> bool:
    """False when the owner cannot be read. A path owned by another account was made by someone else, who keeps the right to change its permissions,
    so that raises instead of being trusted."""
    owner = owner_sid(path)
    if owner is None:
        return False
    if owner not in (sid, *_TRUSTED_OWNERS):
        raise AgyswapError(f"{path} is owned by another Windows account ({owner}), so it is not trusted; move it away and run again")
    return True


_DACL_INFO = 0x4 | 0x80000000  # DACL_SECURITY_INFORMATION | PROTECTED_DACL_SECURITY_INFORMATION: drops inherited and every other ACE


@contextlib.contextmanager
def private_descriptor(sid: str, inherit: bool):
    """A security descriptor whose protected DACL gives full control to sid alone, inherited by files and subfolders when inherit is set.
    Yields (descriptor, DACL); raises OSError when it cannot be built."""
    advapi = ctypes.WinDLL("advapi32", use_last_error=True)
    convert = advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW
    convert.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, ctypes.POINTER(ctypes.c_void_p), ctypes.c_void_p]
    convert.restype = wintypes.BOOL
    get_dacl = advapi.GetSecurityDescriptorDacl
    get_dacl.argtypes = [ctypes.c_void_p, ctypes.POINTER(wintypes.BOOL), ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(wintypes.BOOL)]
    get_dacl.restype = wintypes.BOOL
    local_free = ctypes.WinDLL("kernel32", use_last_error=True).LocalFree
    local_free.argtypes = [ctypes.c_void_p]
    local_free.restype = ctypes.c_void_p
    if not _SID_RE.fullmatch(sid):
        raise OSError(f"not a SID: {sid!r}")
    descriptor = ctypes.c_void_p()
    # SDDL_REVISION_1; FA is FILE_ALL_ACCESS
    if not convert(f"D:P(A;{'OICI' if inherit else ''};FA;;;{sid})", 1, ctypes.byref(descriptor), None):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        present, dacl, defaulted = wintypes.BOOL(), ctypes.c_void_p(), wintypes.BOOL()
        if not get_dacl(descriptor, ctypes.byref(present), ctypes.byref(dacl), ctypes.byref(defaulted)) or not present:
            raise ctypes.WinError(ctypes.get_last_error())
        yield descriptor, dacl
    finally:
        local_free(descriptor)


def restrict_to_current_user(path: Path, children: bool = True) -> bool:
    """Replace the DACL of the folder (or a single file) with one that gives full control to the current Windows user alone:
    inherited entries and every other explicit entry go, in one call, so a mid-way failure cannot leave the folder half done.
    A link is refused, since the call would change its target. With children, what is already inside (for example a migrated old folder) is reset
    to inherit only this folder's permissions."""
    sid = current_user_sid()
    if not sid:
        return False
    if is_reparse_point(path):
        raise AgyswapError(f"{path} is a junction or symbolic link, so it is not trusted; replace it with a real folder and run again")
    if not check_owner(path, sid):
        return False
    advapi = ctypes.WinDLL("advapi32", use_last_error=True)
    is_dir = path.is_dir()
    try:
        with private_descriptor(sid, inherit=is_dir) as (_, dacl):
            if children and is_dir:
                fn = advapi.TreeResetNamedSecurityInfoW
                fn.argtypes = [wintypes.LPWSTR, ctypes.c_int, wintypes.DWORD, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p,
                               ctypes.c_void_p, wintypes.BOOL, ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p]
                fn.restype = wintypes.DWORD
                # SE_FILE_OBJECT; KeepExplicit FALSE drops the explicit entries of what is inside; no progress callback
                return fn(str(path), 1, _DACL_INFO, None, None, dacl, None, False, None, 1, None) == 0
            fn = advapi.SetNamedSecurityInfoW
            fn.argtypes = [wintypes.LPWSTR, ctypes.c_int, wintypes.DWORD, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p]
            fn.restype = wintypes.DWORD
            return fn(str(path), 1, _DACL_INFO, None, None, dacl, None) == 0
    except OSError:
        return False


def create_private_file(folder: Path, prefix: str, data: bytes) -> Path:
    """Create a new file in folder that only the current Windows user can open from the moment it exists (CREATE_NEW with a protected
    DACL, no sharing), and write data through that same handle, so no other process can open it in between. Returns its path; on failure
    raises OSError or AgyswapError, and a file that could not be deleted again is named in the error."""
    import msvcrt
    import secrets

    sid = current_user_sid()
    if not sid:
        raise AgyswapError("Could not read the current Windows user's SID")
    path = folder / f"{prefix}{secrets.token_hex(8)}.tmp"
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    create = k32.CreateFileW
    create.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    create.restype = wintypes.HANDLE

    class SecurityAttributes(ctypes.Structure):
        _fields_ = [("nLength", wintypes.DWORD), ("lpSecurityDescriptor", ctypes.c_void_p), ("bInheritHandle", wintypes.BOOL)]

    with private_descriptor(sid, inherit=False) as (descriptor, _):
        sa = SecurityAttributes(ctypes.sizeof(SecurityAttributes), descriptor, False)
        # GENERIC_WRITE, share mode 0, CREATE_NEW, FILE_ATTRIBUTE_NORMAL
        handle = create(str(path), 0x40000000, 0, ctypes.byref(sa), 1, 0x80, None)
        if handle is None or handle == ctypes.c_void_p(-1).value:
            raise ctypes.WinError(ctypes.get_last_error())
    try:
        fd = msvcrt.open_osfhandle(handle, os.O_WRONLY | os.O_BINARY)
    except OSError:
        k32.CloseHandle.argtypes = [wintypes.HANDLE]
        k32.CloseHandle(handle)
        with contextlib.suppress(OSError):
            path.unlink()
        raise
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
    except BaseException:
        try:
            path.unlink()
        except OSError:
            raise AgyswapError(f"Writing {path} failed and it could not be deleted; it may hold part of the data, delete it by hand") from None
        raise
    return path


def _private_mkdir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    if not restrict_to_current_user(path):
        raise AgyswapError(f"Could not restrict {path} to the current user; not writing to it")


def ensure_data_root() -> Path:
    """Create the data folder locked down to the current user before anything is written into it.
    An existing data folder is locked down once by the migration (step v1-root-acl)."""
    root = paths.base()
    if not root.is_dir():
        try:
            _private_mkdir(root)
        except BaseException:
            with contextlib.suppress(OSError):
                root.rmdir()  # an empty folder that could not be locked down would be trusted from the next run on
            raise
    return root


def ensure_private_dir(path: Path) -> None:
    """Create the folder and lock down its permissions; failing to lock down raises, so callers never write a login into a folder that is too open."""
    ensure_data_root()
    _private_mkdir(path)
