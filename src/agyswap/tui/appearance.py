"""Terminal background detection for ui.theme=auto (cswap #177).

Adapted from cswap 1.0.0's appearance.py (MIT License, claude-swap): send OSC 11 (background colour) and DA1 (device attributes),
read up to the DA1 reply, which marks the point after any colour reply, and classify the rgb by luminance. cswap skips Windows;
this version reads the replies through the Windows console instead. The probe runs before textual starts, with a one-second cap,
and the console input buffer is flushed afterwards, so a late reply never reaches the shell or the app as keystrokes. No reply,
a pipe, or any failure gives None, which resolves to dark.
"""
from __future__ import annotations

import os
import re
import sys
import time

_QUERY = "\x1b]11;?\x07\x1b[c"
TIMEOUT_S = 1.0
_MAX_REPLY = 256
_RGB = re.compile(r"\x1b\]11;rgb:([0-9a-fA-F]+)/([0-9a-fA-F]+)/([0-9a-fA-F]+)(?:\x07|\x1b\\)")
_HEX = re.compile(r"\x1b\]11;#([0-9a-fA-F]{6})(?:\x07|\x1b\\)")
_DA1_REPLY = re.compile(r"\x1b\[\?[0-9;:]*c")
_ENABLE_VIRTUAL_TERMINAL_INPUT = 0x0200


def classify(reply: str | None) -> str | None:
    """light or dark from an OSC 11 reply; None when there is no colour in it."""
    m = _RGB.search(reply or "")
    if m:
        r, g, b = (int(h, 16) / (16 ** len(h) - 1) for h in m.groups())
    else:
        m = _HEX.search(reply or "")
        if not m:
            return None
        r, g, b = (int(m.group(1)[i:i + 2], 16) / 255 for i in (0, 2, 4))
    return "light" if 0.2126 * r + 0.7152 * g + 0.0722 * b > 0.5 else "dark"


def read_reply(read_char, timeout: float = TIMEOUT_S, clock=time.monotonic) -> str:
    """Collect reply characters until the DA1 reply arrives or the timeout passes. read_char returns one character or None when
    nothing is waiting."""
    buf, deadline = "", clock() + timeout
    while clock() < deadline and len(buf) < _MAX_REPLY:
        ch = read_char()
        if ch is None:
            time.sleep(0.01)
            continue
        buf += ch
        if _DA1_REPLY.search(buf):
            break
    return buf


def _console_query() -> str | None:
    if os.name != "nt":
        return None
    try:
        if not (sys.stdin.isatty() and sys.stdout.isatty()):
            return None
        import ctypes
        import msvcrt
        from ctypes import wintypes

        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.GetStdHandle.argtypes = [wintypes.DWORD]
        k32.GetStdHandle.restype = wintypes.HANDLE
        k32.GetConsoleMode.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        k32.SetConsoleMode.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        k32.FlushConsoleInputBuffer.argtypes = [wintypes.HANDLE]
        handle = k32.GetStdHandle(-10 & 0xFFFFFFFF)  # STD_INPUT_HANDLE
        mode = wintypes.DWORD()
        if not handle or not k32.GetConsoleMode(handle, ctypes.byref(mode)):
            return None
        if not k32.SetConsoleMode(handle, mode.value | _ENABLE_VIRTUAL_TERMINAL_INPUT):
            return None
        try:
            sys.stdout.write(_QUERY)
            sys.stdout.flush()
            return read_reply(lambda: msvcrt.getwch() if msvcrt.kbhit() else None)
        finally:
            k32.SetConsoleMode(handle, mode.value)
            k32.FlushConsoleInputBuffer(handle)
    except (OSError, AttributeError, ValueError, ImportError):
        return None


def resolve_theme(setting: str, query=None) -> str:
    """dark and light pass through without probing; auto probes once and falls back to dark."""
    if setting in ("dark", "light"):
        return setting
    try:
        return classify((query or _console_query)()) or "dark"
    except Exception:  # detection must never stop the dashboard from starting
        return "dark"
