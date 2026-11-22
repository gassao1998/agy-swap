"""Terminal output styling: one accent color and dim secondary text, the same approach as cswap's printer.py; no color when not a terminal.
stdout and stderr are checked separately: with `agyswap list 2> err.log` the stderr copy must not carry color codes."""
from __future__ import annotations

import os
import sys

_RESET, _BOLD, _DIM = "\033[0m", "\033[1m", "\033[2m"
_ACCENT, _MUTED, _RED, _YELLOW = "\033[38;5;173m", "\033[38;5;250m", "\033[31m", "\033[33m"
_STD_HANDLES = {"out": -11, "err": -12}
_colors: dict[str, bool] = {}


def colors_enabled(stream=None) -> bool:
    key = "err" if stream is sys.stderr else "out"
    if key not in _colors:
        target = sys.stderr if key == "err" else sys.stdout
        if os.environ.get("NO_COLOR") is not None:
            _colors[key] = False
        elif os.environ.get("FORCE_COLOR") is not None:
            _colors[key] = True
        else:
            _colors[key] = bool(hasattr(target, "isatty") and target.isatty() and _enable_vt(_STD_HANDLES[key]))
    return _colors[key]


def _enable_vt(std_handle: int) -> bool:
    """Turn on the console's VT mode; any failed step returns False so raw escape codes are never printed."""
    try:
        import ctypes
        from ctypes import wintypes

        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.GetStdHandle.argtypes = [wintypes.DWORD]
        k32.GetStdHandle.restype = wintypes.HANDLE
        k32.GetConsoleMode.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        k32.GetConsoleMode.restype = wintypes.BOOL
        k32.SetConsoleMode.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        k32.SetConsoleMode.restype = wintypes.BOOL
        handle = k32.GetStdHandle(std_handle & 0xFFFFFFFF)
        if not handle or handle == ctypes.c_void_p(-1).value:
            return False
        mode = wintypes.DWORD()
        if not k32.GetConsoleMode(handle, ctypes.byref(mode)):
            return False
        return bool(mode.value & 0x0004) or bool(k32.SetConsoleMode(handle, mode.value | 0x0004))
    except (OSError, AttributeError, ValueError):
        return False


def _style(text: str, *codes: str, stream=None) -> str:
    return f"{''.join(codes)}{text}{_RESET}" if colors_enabled(stream) else text


def accent(text: str) -> str:
    return _style(text, _ACCENT)


def muted(text: str) -> str:
    return _style(text, _MUTED)


def dimmed(text: str) -> str:
    return _style(text, _DIM)


def bolded(text: str) -> str:
    return _style(text, _BOLD)


def bold_accent(text: str) -> str:
    return _style(text, _BOLD, _ACCENT)


def error(msg: str) -> None:
    print(_style(msg, _RED, stream=sys.stderr), file=sys.stderr)


def warning(msg: str, file=None) -> None:
    print(_style(msg, _YELLOW, stream=file), file=file)
