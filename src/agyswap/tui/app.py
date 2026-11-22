"""The agyswap textual application: themes, account rendering, modals, the redraw and query loop, and every action that changes state.

Adapted from cswap 1.0.0's tui/app.py, theme.py, widgets.py and modals.py (MIT License, claude-swap). Screens are in screens.py.
Blocking work (file locks, Credential Manager, agy) always runs in thread workers. Redraws read the usage cache every
POLL_INTERVAL_S; agy starts only on the cache plan, in a separate lane that the auto page turns off while its engine queries (cswap #496).
Differences from cswap: switching asks for confirmation first, because a switch is global and may move the Antigravity App too;
there is no add-token form, login, unclaimed or menubar entry.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from functools import partial

from rich.text import Text
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.reactive import reactive
from textual.screen import ModalScreen
from textual.theme import Theme
from textual.widgets import Button, Label, Static
from textual.worker import WorkerState

from agyswap import accounts, output, pace, settings, usage
from agyswap.output import format_age
from agyswap.tui.data import ActionResult, Snapshot, SnapshotSource, run_action
from agyswap.usage_store import SERVE_TTL_S, STALE_OK_S, Entry

WARN_PCT, CRIT_PCT = 70.0, 90.0


def _theme(name: str, dark: bool, c: dict) -> Theme:
    return Theme(name=name, primary=c["accent"], secondary=c["muted"], accent=c["accent"], foreground=c["fg"],
                 background=c["bg"], surface=c["surface"], panel=c["panel"], success=c["ok"], warning=c["warn"],
                 error=c["crit"], dark=dark, variables={
                     "footer-key-foreground": c["accent"], "block-cursor-background": c["panel"],
                     "block-cursor-foreground": c["fg"], "block-cursor-text-style": "none", "track": c["track"]})


# cswap's palettes with a blue accent in place of its terracotta
DARK = _theme("agyswap-dark", True, dict(accent="#6fa8dc", muted="#8a8a8a", fg="#e6e6e6", bg="#141414", surface="#1e1e1e",
                                          panel="#262626", ok="#87af87", warn="#d7af5f", crit="#d75f5f", track="#3a3a3a"))
LIGHT = _theme("agyswap-light", False, dict(accent="#2b5f8f", muted="#5d5d5d", fg="#232323", bg="#f8f8f6", surface="#ecebe7",
                                             panel="#dddbd5", ok="#3d6b3d", warn="#795911", crit="#ad3128", track="#c9c7c1"))


@dataclass(frozen=True)
class Palette:
    """Rich renderables bake colour in at render time, so they read the active theme through this."""
    accent: str
    fg: str
    muted: str
    ok: str
    warn: str
    crit: str
    track: str

    @classmethod
    def of(cls, theme: Theme) -> "Palette":
        return cls(theme.primary, theme.foreground, theme.secondary, theme.success, theme.warning, theme.error,
                   theme.variables.get("track", "#3a3a3a"))

    def severity(self, pct: float) -> str:
        return self.crit if pct >= CRIT_PCT else self.warn if pct >= WARN_PCT else self.ok


# ---------- account rendering (cswap widgets.py) ----------
def bar(pct: float, width: int, palette: Palette, *, line: float | None = None, stale: bool = False) -> Text:
    """Severity-coloured fill, the track, and a tick at the switch line."""
    text, full = Text(), int(min(max(pct, 0.0), 100.0) / 100.0 * width)
    tick = min(width - 1, max(0, round(line / 100.0 * width))) if line is not None else None
    fill = f"{palette.severity(pct)}{' dim' if stale else ''}"
    for i in range(width):
        if i == tick:
            text.append("┃", style=palette.warn)
        else:
            text.append("━" if i < full else "─", style=fill if i < full else palette.track)
    return text


def window_rows(entry: Entry, now: float) -> list[tuple[str, float, str, bool]]:
    """(label, pct, suffix, weekly) for each limit agy reported, in agy's order: every model category with its 7d and 5h windows."""
    rows = []
    for w in (entry.last_good or {}).get("scoped") or []:
        if not isinstance(w, dict) or not isinstance(w.get("pct"), (int, float)):
            continue
        weekly = w.get("window", "7d") == "7d"
        cell, ts = output.reset_strings(w.get("resets_at"), now), pace.parse_ts(w.get("resets_at"))
        suffix = "" if cell is None else "reset passed" if ts is not None and ts <= now else f"resets {cell[0]} · {cell[1]}"
        if w["pct"] >= 100:
            suffix += "  (!)"
        elif weekly and (p := pace.compute(w, entry.fetched_at)) is not None and p.ahead:
            suffix += "  (ahead of pace)"
        rows.append((f"{w['name']} {w.get('window', '7d')}", float(w["pct"]), suffix.strip(), weekly))
    return rows


def lines_for(record: dict, threshold: float, weekly: float | None) -> tuple[float, float]:
    """(5h line, weekly line) for one account: its own threshold, else the global one; the weekly line once it is set."""
    own = accounts.account_threshold(record)
    line = own if own is not None else threshold
    return line, (weekly if weekly is not None else line)


def _name(text: Text, record: dict, palette: Palette, num_style: str) -> None:
    text.append(f"{record.get('_num', ''):>2}  ", style=num_style)
    if record.get("alias"):
        text.append(record["alias"], style=f"bold {palette.accent}")
        text.append(f" ({record.get('email', '')})", style=palette.fg)
    else:
        text.append(record.get("email", ""), style=palette.fg)


def card_text(snap: Snapshot, num: str, width: int, palette: Palette, *, threshold: float, weekly: float | None,
              now: float | None = None) -> Text:
    now = time.time() if now is None else now
    record, entry = {**snap.record(num), "_num": num}, snap.entries[num]
    text = Text()
    _name(text, record, palette, f"bold {palette.fg}")
    if num == snap.ctx.active_number:
        text.append("   ● active", style=f"bold {palette.accent}")
    if record.get("disabled"):
        text.append("   (disabled)", style=palette.muted)
    own = accounts.account_threshold(record)
    if own is not None:
        text.append(f"   th {own:g}%", style=palette.muted)
    if entry.fetched_at is not None and entry.age_s is not None and entry.age_s >= SERVE_TTL_S:
        text.append(f"   · {format_age(entry.fetched_at, now)}", style=palette.muted)
    if entry.via_folder:
        text.append(f"\n    {output.VIA_FOLDER_NOTE}", style=palette.muted)
    if entry.sentinel is not None:
        text.append(f"\n    ⚠ {output.SENTINEL_NOTES.get(entry.sentinel, entry.sentinel)}",
                    style=palette.muted if entry.sentinel == "api_key" else palette.warn)
        return text
    rows = window_rows(entry, now)
    if not rows:
        note = output.ERROR_NOTES.get(entry.last_error, entry.last_error) if entry.last_error else ""
        text.append("\n    usage unavailable" + (f" · {note}" if note else ""), style=palette.muted)
        return text
    stale = entry.age_s is not None and entry.age_s > STALE_OK_S
    five, week = lines_for(record, threshold, weekly)
    label_w = max(len(r[0]) for r in rows)
    bar_w = max(10, min(30, width - label_w - 40))
    for label, pct, suffix, is_weekly in rows:
        text.append(f"\n    {label:<{label_w}} ", style=palette.muted)
        text.append(bar(pct, bar_w, palette, line=week if is_weekly else five, stale=stale))
        text.append(f" {pct:3.0f}%", style=f"{palette.severity(pct)}{' dim' if stale else ''}")
        if suffix:
            text.append(f"  {suffix}", style=palette.muted)
    if entry.last_error and stale:
        text.append(f"\n    latest query failed ({output.ERROR_NOTES.get(entry.last_error, entry.last_error)})", style=palette.muted)
    return text


def mini_text(snap: Snapshot, num: str, palette: Palette) -> Text:
    """One line for an inactive account: the Gemini 5h and 7d figures, and any category at its limit (cswap mini_account_text)."""
    record, entry = {**snap.record(num), "_num": num}, snap.entries[num]
    text = Text(no_wrap=True, overflow="ellipsis")
    _name(text, record, palette, f"bold {palette.muted}")
    if record.get("disabled"):
        text.append("  (disabled)", style=palette.muted)
    text.append("   ")
    if entry.sentinel is not None:
        text.append(output.SENTINEL_NOTES.get(entry.sentinel, entry.sentinel), style=palette.warn)
        return text
    value, parts = entry.last_good or {}, []
    for key, label in (("five_hour", "5h"), ("seven_day", "7d")):
        w = value.get(key)
        if isinstance(w, dict) and isinstance(w.get("pct"), (int, float)):
            parts.append((f"{label} {w['pct']:.0f}%", palette.severity(w["pct"])))
    parts += [(f"{w['name']} {w.get('window', '7d')} (!)", palette.crit) for w in value.get("scoped") or []
              if isinstance(w, dict) and isinstance(w.get("pct"), (int, float)) and w["pct"] >= 100]
    for i, (part, style) in enumerate(parts or [("usage unknown", palette.muted)]):
        if i:
            text.append(" · ", style=palette.track)
        text.append(part, style=style)
    return text


# ---------- modals (cswap modals.py) ----------
class ConfirmModal(ModalScreen[bool]):
    """Yes/No; dismisses True only on an explicit yes. Left/right move between the buttons, y/n answer, Esc cancels."""

    AUTO_FOCUS = "#no"  # Enter alone never confirms
    # focus_next would wrap from Cancel round to the confirm button, so the keys name their button instead
    BINDINGS = [Binding("y", "answer(True)", show=False), Binding("n,escape", "answer(False)", show=False),
                Binding("left,shift+tab", "focus_button('yes')", show=False),
                Binding("right,tab", "focus_button('no')", show=False)]

    def __init__(self, message: str, *, title: str = "Confirm", yes_label: str = "Yes") -> None:
        super().__init__()
        self._message, self._title, self._yes = message, title, yes_label

    def compose(self) -> ComposeResult:
        with Vertical(classes="modal-box"):
            yield Label(self._title, classes="modal-title")
            yield Static(self._message, classes="modal-body", markup=False)
            with Horizontal(classes="modal-buttons"):
                yield Button(self._yes, id="yes")
                yield Button("Cancel", id="no")
            yield Static(f"← → · enter  ·  y {self._yes.lower()}  ·  n / esc cancel", classes="modal-hint")

    def action_focus_button(self, name: str) -> None:
        self.query_one(f"#{name}", Button).focus()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "yes")

    def action_answer(self, yes: bool) -> None:
        self.dismiss(yes)


class OutputModal(ModalScreen[None]):
    BINDINGS = [Binding("escape,q,enter", "close", "Close", show=False)]

    def __init__(self, title: str, body: str) -> None:
        super().__init__()
        self._title, self._body = title, body

    def compose(self) -> ComposeResult:
        with Vertical(classes="modal-box modal-wide"):
            yield Label(self._title, classes="modal-title")
            with VerticalScroll(classes="modal-output"):
                yield Static(Text.from_ansi(self._body or "(no output)"))
            yield Static("esc close", classes="modal-hint")

    def action_close(self) -> None:
        self.dismiss(None)


CSS = """
Screen { background: $background; }
#accounts-panel { height: auto; padding: 1 3; border-bottom: solid $panel; }
#auto-active-panel { height: auto; padding: 1 3 0 3; }
#menu-title, #list-title { height: 1; margin-top: 1; padding: 0 3; color: $secondary; }
#menu, #accounts { background: $background; padding: 1 2 0 1; scrollbar-size-vertical: 1; }
#menu ListItem, #accounts ListItem { background: transparent; padding: 0 1; border-left: thick $background; }
#accounts ListItem { margin-bottom: 1; }
#menu ListItem.-highlight, #accounts ListItem.-highlight { color: $foreground; background: $surface; text-style: none;
    border-left: thick $primary; }
#accounts ListItem.flash { background: $panel; }
#auto-top { height: auto; padding: 1 2; }
#auto-title-row { height: 1; margin-bottom: 1; }
#mode-badge { width: auto; text-style: bold; }
#mode-badge.dry { background: $panel; color: $warning; }
#mode-badge.live { background: $primary; color: $background; }
#auto-summary { width: 1fr; color: $secondary; padding-left: 2; }
#candidates { height: auto; }
#event-log { border-top: solid $panel; padding: 0 1; scrollbar-size-vertical: 1; }
ConfirmModal, OutputModal { align: center middle; background: $background 60%; }
.modal-box { width: 70; max-width: 90%; height: auto; max-height: 80%; background: $surface; border: round $panel; padding: 1 2; }
.modal-wide { width: 90; }
.modal-title { text-style: bold; color: $primary; margin-bottom: 1; }
.modal-body { margin-bottom: 1; }
.modal-buttons { height: auto; align-horizontal: right; margin-top: 1; }
.modal-buttons Button { border: none; height: 1; min-width: 12; padding: 0 2; margin-left: 2; background: $panel; }
.modal-buttons Button:focus { background: $primary; color: $background; text-style: bold; }
.modal-hint { height: 1; margin-top: 1; color: $secondary; }
.modal-output { height: auto; max-height: 20; background: $background; padding: 1; }
"""


class AgyswapApp(App):
    TITLE = "agyswap"
    CSS = CSS
    ENABLE_COMMAND_PALETTE = False
    BINDINGS = [Binding("ctrl+t", "toggle_theme", "Theme")]
    POLL_INTERVAL_S = 3.0
    SNAPSHOT_AGE_NOTE_S = 60.0

    snapshot: reactive[Snapshot | None] = reactive(None)
    refresh_status: reactive[str] = reactive("")
    busy: reactive[bool] = reactive(False)

    def __init__(self, *, start: str = "dashboard", theme_setting: str = "auto", detected: str | None = None,
                 source: SnapshotSource | None = None) -> None:
        super().__init__()
        self.source = source or SnapshotSource()
        self._start, self.theme_setting, self._detected = start, theme_setting, detected
        self.auto_page = False
        self.engines: set = set()  # auto engines whose threads have not returned yet (a stopped one may still be finishing a tick)
        self._drawing = self._fetching = self._full_next = False
        self._fetch_started: float | None = None
        self._last_error = ""
        loaded = settings.load()
        self.threshold_pct: float = loaded["autoswitch.threshold"]
        self.weekly_threshold_pct: float | None = loaded["autoswitch.weeklyThreshold"]

    def on_mount(self) -> None:
        from agyswap.tui.screens import DashboardScreen, WatchScreen

        self.register_theme(DARK)
        self.register_theme(LIGHT)
        self.theme = f"agyswap-{self._resolved()}"
        self.push_screen(DashboardScreen())
        if self._start == "watch":
            self.push_screen(WatchScreen())  # stacked, so Esc lands on the dashboard rather than exiting
        self.set_interval(self.POLL_INTERVAL_S, self._tick)
        self.set_interval(1.0, self._update_status)
        self._tick()

    @property
    def palette(self) -> Palette:
        return Palette.of(self.current_theme)

    @property
    def store_only(self) -> bool:
        """The auto page's engine is the only one querying, until its last engine thread has returned."""
        return self.auto_page or bool(self.engines)

    # -- redraw and query lanes --
    def _tick(self) -> None:
        if not self._drawing:
            self._drawing = True
            self.run_worker(self._draw_blocking, thread=True, group="draw", exit_on_error=False)
        if not self.store_only and not self._fetching:
            self._fetching, self._fetch_started = True, time.time()
            full, self._full_next = self._full_next, False
            self.run_worker(partial(self._fetch_blocking, full), thread=True, group="fetch", exit_on_error=False)

    def _call(self, fn, *args) -> None:
        """call_from_thread for a worker, skipped once the app is closing: the result has nowhere to go, and textual raises after it
        has closed."""
        if self.source.closing.is_set():
            return
        try:
            self.call_from_thread(fn, *args)
        except RuntimeError:
            if self.is_running:
                raise

    def on_unmount(self) -> None:
        """Quitting: no agy starts any more and the running one ends after a short grace, so the process exits promptly and leaves
        no agy behind (the auto page stops its engine when it unmounts, just before this)."""
        self.source.closing.set()
        for engine in list(self.engines):
            engine.stop()

    def _draw_blocking(self) -> None:
        self._call(self._apply, self.source.take())

    def _apply(self, snap: Snapshot) -> None:
        self._drawing, self._last_error = False, ""
        self.snapshot = snap
        self._update_status()

    def _fetch_blocking(self, full: bool) -> None:
        try:
            queried = self.source.refresh(full=full, stopped=lambda: self.store_only)
        except usage.Cancelled:  # closing, or the auto page took over the querying
            queried = False
        self._call(self._fetched, queried)

    def _fetched(self, queried: bool) -> None:
        self._fetching, self._fetch_started = False, None
        if queried:  # the cache changed; otherwise the tick's own redraw already showed everything
            self.redraw()

    def redraw(self) -> None:
        if not self._drawing:
            self._drawing = True
            self.run_worker(self._draw_blocking, thread=True, group="draw", exit_on_error=False)

    def _update_status(self) -> None:
        parts, now = [], time.time()
        if self.snapshot is not None and now - self.snapshot.taken_at >= self.SNAPSHOT_AGE_NOTE_S:
            parts.append(f"snapshot {format_age(self.snapshot.taken_at, now)}")
        if self._fetch_started is not None and now - self._fetch_started >= self.POLL_INTERVAL_S:
            parts.append(f"querying agy {now - self._fetch_started:.0f}s")
        self.refresh_status = " · ".join(parts)

    def on_worker_state_changed(self, event) -> None:
        if event.state not in (WorkerState.ERROR, WorkerState.CANCELLED):
            return
        group = event.worker.group
        if group == "draw":
            self._drawing = False
        elif group == "fetch":
            self._fetching, self._fetch_started = False, None
        elif group == "action":
            self.busy = False
        if event.state is WorkerState.CANCELLED:
            return
        msg = f"{group} failed: {event.worker.error}"
        if msg != self._last_error:
            self._last_error = msg
            self.notify(msg, severity="error" if group in ("action", "engine") else "warning", timeout=6)

    def action_refresh_full(self) -> None:
        self._full_next = True
        self._tick()
        self.notify("Querying agy for every account…", timeout=2)

    # -- actions (single-flight, captured, off-thread) --
    def start_action(self, label: str, fn) -> None:
        if self.busy:
            self.notify("Another action is still running", severity="warning")
            return
        self.busy = True
        self.run_worker(partial(self._action_blocking, label, fn), thread=True, group="action", exit_on_error=False)

    def _action_blocking(self, label: str, fn) -> None:
        self._call(self._action_done, label, run_action(fn))

    def _action_done(self, label: str, result: ActionResult) -> None:
        self.busy = False
        self.source.forget_live()
        self.redraw()
        payload = result.payload or {}
        if not result.ok:
            self.push_screen(OutputModal(f"{label} failed", result.text))
        elif "switched" in payload:
            for w in payload.get("warnings") or []:
                self.notify(w, severity="warning", timeout=8)
            self.notify(payload.get("message", ""), title="Switch",
                        severity="information" if payload["switched"] else "warning")
        elif result.text:
            self.notify(result.text.splitlines()[-1])

    def confirm(self, message: str, title: str, yes: str, then) -> None:
        if any(isinstance(s, ConfirmModal) for s in self.screen_stack):
            return  # a second Enter that arrived before the first prompt showed
        self.push_screen(ConfirmModal(message, title=title, yes_label=yes), lambda ok: then() if ok else None)

    def _label(self, num: str) -> str:
        record = self.snapshot.record(num) if self.snapshot and num in self.snapshot.numbers else {}
        return f"Account-{num} ({record.get('alias') or record.get('email', '?')})"

    def do_switch(self, num: str) -> None:
        from agyswap import switch

        self.confirm(f"Switch the global login to {self._label(num)}?\n\nNew agy processes and the Antigravity App follow it.",
                     "Switch account", "Switch", lambda: self.start_action(f"Switch to Account-{num}", partial(switch.switch_to, num)))

    def action_switch_best(self) -> None:
        from agyswap import switch

        models = switch.parse_models(settings.load()["autoswitch.model"])
        self.confirm("Switch the global login to the account with the most quota left?\n\n"
                     "New agy processes and the Antigravity App follow it.", "Switch (best)", "Switch",
                     lambda: self.start_action("Switch (best)", partial(switch.switch, strategy="best", models=models,
                                                                        fetcher=self.source.fetcher_for())))

    def do_toggle_disabled(self, num: str) -> None:
        """Asks first like every other change: the submenu opens on the first account, so a double Enter would toggle it."""
        from agyswap import manage

        if not (self.snapshot and num in self.snapshot.numbers):
            self.notify(f"Account-{num} is no longer managed", severity="warning")
            return
        disabled = bool(self.snapshot.record(num).get("disabled"))
        verb = "Enable" if disabled else "Disable"
        effect = ("Switch best and auto-switch may pick it again." if disabled
                  else "Switch best and auto-switch skip it until it is enabled again.")
        self.confirm(f"{verb} {self._label(num)}?\n\n{effect}", f"{verb} account", verb,
                     lambda: self.start_action(f"{verb} Account-{num}", partial(manage.set_disabled, num, not disabled)))

    def confirm_remove(self, num: str) -> None:
        from agyswap import manage

        self.confirm(f"Remove {self._label(num)}?\n\nIts account folder (login file, chat history) is deleted.",
                     "Remove account", "Remove",
                     lambda: self.start_action(f"Remove Account-{num}", partial(manage.remove, num, confirm=lambda _p: True)))

    def action_add_current(self) -> None:
        from agyswap import manage

        self.confirm("Add the current Credential Manager login as a managed account?", "Add account", "Add",
                     lambda: self.start_action("Add current login", manage.add))

    # -- navigation --
    def action_open_watch(self) -> None:
        from agyswap.tui.screens import WatchScreen

        if not isinstance(self.screen, WatchScreen):
            self.push_screen(WatchScreen())

    def action_open_auto(self) -> None:
        from agyswap.tui.screens import AutoScreen

        if not isinstance(self.screen, AutoScreen):
            self.push_screen(AutoScreen())

    # -- theme --
    def _resolved(self) -> str:
        return (self._detected or "dark") if self.theme_setting == "auto" else self.theme_setting

    def apply_theme(self, name: str) -> None:
        """auto resolves against the probe made before start; the terminal is never probed again mid-session (cswap #177)."""
        self.theme_setting = name
        self.theme = f"agyswap-{self._resolved()}"
        try:
            settings.set_value("ui.theme", name)
        except Exception as e:  # saving is best-effort; the UI keeps the new theme
            self.notify(f"Could not save the theme: {e}", severity="warning")

    def action_toggle_theme(self) -> None:
        order = ("dark", "light", "auto")
        name = order[(order.index(self.theme_setting) + 1) % 3] if self.theme_setting in order else "dark"
        self.apply_theme(name)
        self.notify(f"Theme: {name}")
