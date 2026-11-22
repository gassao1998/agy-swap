"""The textual dashboard behind `agyswap tui`, `agyswap watch` and a bare `agyswap` in a terminal (cswap #496, #390, #219, #587, #177).

Adapted from cswap 1.0.0's tui package (MIT License, claude-swap). textual is an optional dependency (the tui extra), imported only
inside run(), so every other command works without it.
"""
from __future__ import annotations

import importlib.util

INSTALL_HINT = ("the dashboard needs the optional textual package: from the agyswap repository folder run "
                "uv tool install --reinstall --editable \".[tui]\"")


def available() -> bool:
    return importlib.util.find_spec("textual") is not None


def run(start: str = "dashboard", *, no_mouse: bool = False) -> int:
    """start="watch" opens on the watch page, stacked over the dashboard. no_mouse (cswap #390) leaves mouse tracking off, so text can be
    selected in a Windows Terminal pane."""
    from agyswap import settings
    from agyswap.tui.app import AgyswapApp
    from agyswap.tui.appearance import resolve_theme

    setting = settings.load()["ui.theme"]
    # Probed before textual's driver owns the console (cswap #177); only auto probes at all
    detected = resolve_theme(setting) if setting == "auto" else None
    app = AgyswapApp(start=start, theme_setting=setting, detected=detected)
    app.run(mouse=not no_mouse)
    return app.return_code or 0
