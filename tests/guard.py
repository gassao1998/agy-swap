"""The test safety net: no test can touch the real Credential Manager, the real agyswap folder or the real ~/.gemini, or run agy or icacls.

It uses sys.addaudithook: looking up advapi32's Cred* functions through ctypes, opening, renaming or deleting files, and starting subprocesses all raise audit events,
and the hook raises right there, so the action is blocked before it happens. The exception derives from BaseException, so `except Exception` cannot swallow it.
"""
from __future__ import annotations

import atexit
import os
import sys
import tempfile
from pathlib import Path

_REAL_HOME = Path(os.environ.get("USERPROFILE") or Path.home())
_REAL_LOCAL = Path(os.environ.get("LOCALAPPDATA") or _REAL_HOME / "AppData" / "Local")
PROTECTED = tuple(os.path.normcase(os.path.abspath(p)) for p in (_REAL_LOCAL / "agyswap", _REAL_HOME / ".gemini"))
BLOCKED_SYMBOLS = frozenset({"CredReadW", "CredWriteW", "CredDeleteW", "CredEnumerateW", "CredReadA",
                             "CredWriteA", "CredDeleteA"})
BLOCKED_EXES = frozenset({"agy", "icacls", "cmdkey", "whoami", "git"})  # git: no test may pull or reach the network
_PATH_EVENTS = frozenset({"open", "os.rename", "os.remove", "os.rmdir", "os.mkdir", "shutil.rmtree",
                          "os.chmod", "os.truncate", "os.link", "os.symlink"})
_installed = False


class GuardTripped(BaseException):
    pass


def protected(path) -> bool:
    if isinstance(path, bytes):
        path = os.fsdecode(path)
    if not isinstance(path, (str, os.PathLike)):
        return False
    p = os.path.normcase(os.path.abspath(os.fspath(path)))
    return any(p == root or p.startswith(root + os.sep) for root in PROTECTED)


def _exe_name(executable, args) -> str:
    first = executable
    if first is None:
        first = args[0] if isinstance(args, (list, tuple)) and args else (args.split()[0] if isinstance(args, str) and args.strip() else "")
    name = os.path.basename(os.fsdecode(first) if isinstance(first, bytes) else str(first)).lower()
    return name[:-4] if name.endswith(".exe") else name


def _hook(event: str, args: tuple) -> None:
    if event == "ctypes.dlsym":
        if len(args) > 1 and args[1] in BLOCKED_SYMBOLS:
            raise GuardTripped(f"a test tried to use the real Credential Manager function {args[1]}")
    elif event == "subprocess.Popen":
        if _exe_name(args[0], args[1]) in BLOCKED_EXES:
            raise GuardTripped(f"a test tried to run {args[1]!r}")
    elif event in _PATH_EVENTS:
        for value in args[:2]:
            if protected(value):
                raise GuardTripped(f"a test tried to touch real data: {event} {value}")


def install() -> None:
    """Install once; it then holds for the whole process (an audit hook cannot be removed). It also points the default AGYSWAP_HOME and AGY_BIN at a temp location that does not exist,
    so a test that forgets TempHome still cannot land in the real folders."""
    global _installed
    if _installed:
        return
    tmp = tempfile.TemporaryDirectory(prefix="agyswap-test-")
    atexit.register(tmp.cleanup)
    os.environ["AGYSWAP_HOME"] = str(Path(tmp.name) / "home")
    os.environ["AGY_BIN"] = str(Path(tmp.name) / "no-agy.exe")
    sys.addaudithook(_hook)
    _installed = True
