"""List running agy and Antigravity App processes (tasklist). The App's image name has not been measured on a real install."""
from __future__ import annotations

import csv
import io
import subprocess

from agyswap.paths import system_exe

IMAGES = ("agy.exe", "antigravity.exe")


def parse_tasklist(text: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    for row in csv.reader(io.StringIO(text)):
        name = row[0].lower() if row else ""
        if name in IMAGES:
            counts[name] = counts.get(name, 0) + 1
    return counts


def process_started(pid: object) -> int | None:
    """Creation time (FILETIME ticks) of a running process, or None when no process with this PID is running.
    Together with the PID it names one process even after Windows hands the PID to another one (cswap #508)."""
    if isinstance(pid, bool) or not isinstance(pid, int) or not 0 < pid < 2 ** 32:
        return None
    try:
        import ctypes
        from ctypes import wintypes

        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        k32.OpenProcess.restype = wintypes.HANDLE
        k32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        k32.GetExitCodeProcess.restype = wintypes.BOOL
        k32.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4
        k32.GetProcessTimes.restype = wintypes.BOOL
        k32.CloseHandle.argtypes = [wintypes.HANDLE]
        k32.CloseHandle.restype = wintypes.BOOL
        handle = k32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return None
        try:
            code = wintypes.DWORD()
            # An exited process lives on while someone holds a handle to it; 259 is STILL_ACTIVE
            if not k32.GetExitCodeProcess(handle, ctypes.byref(code)) or code.value != 259:
                return None
            times = [wintypes.FILETIME() for _ in range(4)]
            if not k32.GetProcessTimes(handle, *(ctypes.byref(t) for t in times)):
                return None
            return (times[0].dwHighDateTime << 32) | times[0].dwLowDateTime
        finally:
            k32.CloseHandle(handle)
    except (OSError, AttributeError, ValueError):
        return None


def running() -> dict[str, int]:
    try:
        # tasklist writes in the console code page (cp950 on Traditional Chinese Windows); decoding it as UTF-8 garbles it
        out = subprocess.run([system_exe("tasklist"), "/FO", "CSV", "/NH"], capture_output=True, encoding="oem",
                             errors="replace", timeout=15).stdout
    except (OSError, subprocess.TimeoutExpired):
        return {}
    return parse_tasklist(out or "")
