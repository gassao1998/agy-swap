"""The dashboard, switch, watch and auto-switch screens.

Adapted from cswap 1.0.0's tui/dashboard.py, widgets.py and autoview.py (MIT License, claude-swap). The dashboard shows the active
account in full and the others on one line each, with a nested menu below; the switch and watch pages list every account in full;
the auto page runs the auto engine in a thread, in dry-run until `l` is confirmed, and shows its settings, the ranked candidates
with each window and reset time (cswap #219, #587), and the event log.
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import replace
from functools import partial
from typing import TYPE_CHECKING

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import Screen
from textual.widgets import Footer, ListItem, ListView, RichLog, Static
from textual.worker import WorkerState

from agyswap import autoswitch, output, settings
from agyswap.switch import parse_models
from agyswap.tui.app import card_text, lines_for, mini_text, window_rows

if TYPE_CHECKING:
    from agyswap.tui.app import AgyswapApp

FLASH_S = 1.5
_BACK = ("← back", "back")


class AccountsPanel(Static):
    """The active account in full, the others as one-liners (show_minis=False on the auto page)."""

    def __init__(self, *, show_minis: bool = True, id: str | None = None) -> None:
        super().__init__(id=id)
        self._show_minis = show_minis

    def on_mount(self) -> None:
        self.watch(self.app, "snapshot", lambda _s: self.refresh(layout=True))
        self.watch(self.app, "theme", lambda _t: self.refresh(layout=True))

    def render(self) -> Text:
        app: AgyswapApp = self.app  # type: ignore[assignment]
        palette, snap = app.palette, app.snapshot
        if snap is None:
            return Text("loading…", style=palette.muted)
        if not snap.numbers:
            return Text("No accounts are managed yet. Add one with agyswap login, or Add current login below.", style=palette.muted)
        active, out = snap.ctx.active_number, Text()
        if active is not None:
            out.append(card_text(snap, active, (self.size.width or 80) - 2, palette,
                                 threshold=app.threshold_pct, weekly=app.weekly_threshold_pct))
        elif snap.ctx.live.error:
            out.append(f"Cannot read Credential Manager ({snap.ctx.live.error})", style=palette.warn)
        else:
            out.append("no active managed login" if not snap.ctx.unmanaged_live else
                       f"the active login ({snap.ctx.live.email or 'email unreadable'}) is not managed", style=palette.muted)
        if self._show_minis:
            for num in snap.numbers:
                if num != active:
                    out.append("\n")
                    out.append(mini_text(snap, num, palette))
        return out


class MenuItem(ListItem):
    def __init__(self, label: str, action_id: str) -> None:
        super().__init__(Static(label, markup=False))
        self.action_id = action_id


class DashboardScreen(Screen):
    BINDINGS = [Binding("s", "open_switch", "Switch accounts"), Binding("w", "app.open_watch", "Watch"),
                Binding("g", "app.open_auto", "Auto view"), Binding("f", "app.refresh_full", "Refresh usage", show=False),
                Binding("escape,left", "menu_back", "Back", show=False), Binding("q", "app.quit", "Quit"),
                Binding("j", "cursor(1)", show=False), Binding("k", "cursor(-1)", show=False)]

    app: "AgyswapApp"

    _ACCOUNT_MENUS = {"disable / enable": ("disable", True), "remove account": ("remove", False)}

    def __init__(self) -> None:
        super().__init__()
        self._stack: list[tuple[str, list[tuple[str, str]]]] = []
        self._menu_lock = asyncio.Lock()

    def compose(self) -> ComposeResult:
        yield AccountsPanel(id="accounts-panel")
        yield Static("", id="menu-title")
        yield ListView(id="menu")
        yield Footer()

    async def on_mount(self) -> None:
        self.query_one("#menu", ListView).focus()
        await self._push("menu", [("Switch account…", "switch"), ("Watch accounts", "watch"), ("Auto-switch view", "auto"),
                                  ("Add current login…", "add"), ("Disable / enable account…", "disable-menu"),
                                  ("Remove account…", "remove-menu"), ("Theme…", "theme-menu"), ("Quit", "quit")])
        self.watch(self.app, "snapshot", self._follow_roster)

    async def _follow_roster(self, _snap) -> None:
        """An open account submenu follows the roster: it may have opened before the first snapshot, or an account changed since."""
        if len(self._stack) < 2 or self._stack[-1][0] not in self._ACCOUNT_MENUS:
            return
        title, entries = self._stack[-1]
        fresh = self._accounts(*self._ACCOUNT_MENUS[title])
        if fresh != entries:
            self._stack[-1] = (title, fresh)
            await self._render_menu(keep_cursor=True)

    def _accounts(self, prefix: str, state: bool = False) -> list[tuple[str, str]]:
        snap = self.app.snapshot
        rows = []
        for num in snap.numbers if snap else []:
            record = snap.record(num)
            name = f"{record['alias']} ({record.get('email', '')})" if record.get("alias") else record.get("email", "")
            if state:
                name += "  (disabled)   → enable" if record.get("disabled") else "   → disable"
            rows.append((f"{num}  {name}", f"{prefix}:{num}"))
        return rows + [_BACK]

    async def _push(self, title: str, entries: list[tuple[str, str]]) -> None:
        self._stack.append((title, entries))
        await self._render_menu()

    async def _pop(self) -> None:
        if len(self._stack) > 1:
            self._stack.pop()
            await self._render_menu()

    async def _render_menu(self, keep_cursor: bool = False) -> None:
        # Keys run on the app's message queue and selections on this screen's, so two renders can overlap at the awaits below;
        # unlocked, both cleared before either extended and the menu came out doubled
        async with self._menu_lock:
            self.query_one("#menu-title", Static).update(" › ".join(t for t, _ in self._stack))
            menu = self.query_one("#menu", ListView)
            index, entries = menu.index or 0, self._stack[-1][1]
            await menu.clear()
            await menu.extend(MenuItem(label, action) for label, action in entries)
            menu.index = min(index, len(entries) - 1) if keep_cursor else 0

    async def on_list_view_selected(self, event: ListView.Selected) -> None:
        if not isinstance(event.item, MenuItem):
            return
        action, app = event.item.action_id, self.app
        kind, _, num = action.partition(":")
        if action == "back":
            await self._pop()
        elif action == "disable-menu":
            await self._push("disable / enable", self._accounts("disable", state=True))
        elif action == "remove-menu":
            await self._push("remove account", self._accounts("remove"))
        elif action == "theme-menu":
            await self._push("theme", [(f"{'●' if n == app.theme_setting else ' '} {n}", f"theme:{n}")
                                       for n in ("dark", "light", "auto")] + [_BACK])
        elif kind == "disable":
            app.do_toggle_disabled(num)
            await self._pop()
        elif kind == "remove":
            app.confirm_remove(num)
            await self._pop()
        elif kind == "theme":
            app.apply_theme(num)
            await self._pop()
        else:
            {"switch": self.action_open_switch, "watch": app.action_open_watch, "auto": app.action_open_auto,
             "add": app.action_add_current, "quit": app.exit}[action]()

    def action_open_switch(self) -> None:
        if not isinstance(self.app.screen, SwitchScreen):  # a second Enter that arrived before the page showed
            self.app.push_screen(SwitchScreen())

    async def action_menu_back(self) -> None:
        await self._pop()

    def action_cursor(self, step: int) -> None:
        menu = self.query_one("#menu", ListView)
        menu.action_cursor_down() if step > 0 else menu.action_cursor_up()


class AccountCard(Static):
    def __init__(self, num: str) -> None:
        super().__init__()
        self.num = num

    def render(self) -> Text:
        app: AgyswapApp = self.app  # type: ignore[assignment]
        if app.snapshot is None or self.num not in app.snapshot.numbers:
            return Text("")
        return card_text(app.snapshot, self.num, self.size.width or 80, app.palette,
                         threshold=app.threshold_pct, weekly=app.weekly_threshold_pct)


class AccountItem(ListItem):
    def __init__(self, num: str) -> None:
        super().__init__(AccountCard(num))
        self.num = num


class AccountListScreen(Screen):
    """Every account in full, live. SwitchScreen picks at once; WatchScreen only watches until `s` arms selection."""

    app: "AgyswapApp"
    TITLE_TEXT = ""

    def __init__(self) -> None:
        super().__init__()
        self._numbers: list[str] = []
        self._stamps: dict[str, float | None] = {}

    def compose(self) -> ComposeResult:
        yield Static("", id="list-title")
        yield ListView(id="accounts")
        yield Footer()

    def on_mount(self) -> None:
        self.query_one("#list-title", Static).update(self.title_text())
        self.watch(self.app, "snapshot", self._on_snapshot)

    def title_text(self) -> str:
        return self.TITLE_TEXT

    def cursor_on(self) -> bool:
        return True

    async def _on_snapshot(self, snap) -> None:
        if snap is None:
            return
        listview = self.query_one("#accounts", ListView)
        if snap.numbers != self._numbers:
            first = not self._numbers
            await listview.clear()
            await listview.extend(AccountItem(n) for n in snap.numbers)
            self._numbers = list(snap.numbers)
            if self.cursor_on() and snap.numbers:
                active = snap.ctx.active_number
                listview.index = snap.numbers.index(active) if first and active in snap.numbers else 0
        for card in listview.query(AccountCard):
            card.refresh(layout=True)
        stamps = {n: snap.entries[n].fetched_at for n in snap.numbers}
        for item in listview.query(AccountItem):  # briefly highlight rows whose reading just changed
            if self._stamps and stamps[item.num] is not None and stamps[item.num] != self._stamps.get(item.num):
                item.add_class("flash")
                self.set_timer(FLASH_S, partial(item.remove_class, "flash"))
        self._stamps = stamps


class SwitchScreen(AccountListScreen):
    TITLE_TEXT = "switch to which account?"
    BINDINGS = [Binding("enter", "pick", "Switch", priority=True), Binding("b", "switch_best", "Best pick"),
                Binding("escape,q,s", "app.pop_screen", "Back"), Binding("j", "cursor(1)", show=False),
                Binding("k", "cursor(-1)", show=False)]

    def on_mount(self) -> None:
        self.query_one("#accounts", ListView).focus()
        super().on_mount()

    def action_pick(self) -> None:
        listview = self.query_one("#accounts", ListView)
        if isinstance(listview.highlighted_child, AccountItem):
            num = listview.highlighted_child.num
            self.app.pop_screen()
            self.app.do_switch(num)

    def action_switch_best(self) -> None:
        self.app.pop_screen()  # as Enter does: the prompt and its outcome land on the dashboard
        self.app.action_switch_best()

    def action_cursor(self, step: int) -> None:
        listview = self.query_one("#accounts", ListView)
        listview.action_cursor_down() if step > 0 else listview.action_cursor_up()


class WatchScreen(AccountListScreen):
    """A hands-off monitor; `s` arms selection, Enter switches (after the confirmation) and keeps watching, Esc disarms, then leaves."""

    AUTO_FOCUS = ""  # (None means the app's "*") the list takes focus only while selecting, so no cursor shows unarmed
    BINDINGS = [Binding("s", "toggle_select", "Switch"), Binding("enter", "pick", "Confirm", priority=True),
                Binding("f", "app.refresh_full", "Refresh", show=False), Binding("escape,q", "back", "Back"),
                Binding("down,j", "nav(1)", show=False, priority=True), Binding("up,k", "nav(-1)", show=False, priority=True)]

    def __init__(self) -> None:
        super().__init__()
        self._selecting = False

    def on_mount(self) -> None:
        self.watch(self.app, "refresh_status", lambda _s: self.query_one("#list-title", Static).update(self.title_text()))
        super().on_mount()

    def title_text(self) -> str:
        if self._selecting:
            return "switch to which account? · enter confirm · esc cancel"
        status = self.app.refresh_status
        return f"watching all accounts · {status}" if status else "watching all accounts"

    def cursor_on(self) -> bool:
        return self._selecting

    def check_action(self, action: str, parameters: tuple) -> bool | None:
        return False if action == "pick" and not self._selecting else True

    def _set_selecting(self, on: bool) -> None:
        self._selecting = on
        listview, snap = self.query_one("#accounts", ListView), self.app.snapshot
        if on:
            active = snap.ctx.active_number if snap else None
            listview.index = snap.numbers.index(active) if snap and active in snap.numbers else 0
            listview.focus()
        else:
            listview.index = None
            self.set_focus(None)
        self.query_one("#list-title", Static).update(self.title_text())
        self.refresh_bindings()

    def action_toggle_select(self) -> None:
        self._set_selecting(not self._selecting)

    def action_pick(self) -> None:
        listview = self.query_one("#accounts", ListView)
        if self._selecting and isinstance(listview.highlighted_child, AccountItem):
            num = listview.highlighted_child.num
            self._set_selecting(False)
            self.app.do_switch(num)

    def action_back(self) -> None:
        self._set_selecting(False) if self._selecting else self.app.pop_screen()

    def action_nav(self, step: int) -> None:
        listview = self.query_one("#accounts", ListView)
        if self._selecting:
            listview.action_cursor_down() if step > 0 else listview.action_cursor_up()
        else:
            listview.scroll_down(animate=False) if step > 0 else listview.scroll_up(animate=False)


_EVENT_STYLES = {"switch": "accent", "error": "warn", "account-quarantined": "warn", "all-exhausted": "crit",
                 "config-warning": "warn"}


class AutoScreen(Screen):
    """The real auto engine in a thread worker, dry-run until `l` is confirmed. The threshold set with `t` and the arrows lasts
    for this page only and is never written to settings.json (as in cswap)."""

    BINDINGS = [Binding("l", "toggle_live", "Go live / dry-run"), Binding("t", "adjust", "Threshold"),
                Binding("left", "step(-1)", "-1%"), Binding("right", "step(1)", "+1%"), Binding("enter", "adjust_done", "Done"),
                Binding("escape,q", "back", "Back")]

    app: "AgyswapApp"

    def __init__(self) -> None:
        super().__init__()
        self._engine = None  # the engine this page runs; a stopped one is dropped at once, even while its thread finishes
        self._worker = None
        self._want: bool | None = None  # dry_run of the engine waiting to start
        self._live = False
        self._adjusting = False
        self.cfg = autoswitch.AutoSettings.load()
        self._configured = self.cfg.threshold
        self._entry = self.cfg.threshold

    def compose(self) -> ComposeResult:
        yield AccountsPanel(show_minis=False, id="auto-active-panel")
        with Vertical(id="auto-top"):
            with Horizontal(id="auto-title-row"):
                yield Static(" DRY-RUN ", id="mode-badge", classes="dry")
                yield Static("", id="auto-summary")
            yield Static("", id="candidates")
        yield RichLog(id="event-log", highlight=False, markup=False, wrap=True)
        yield Footer()

    def on_mount(self) -> None:
        self.app.auto_page = True
        self.app.threshold_pct, self.app.weekly_threshold_pct = self.cfg.threshold, self.cfg.weekly_threshold
        self._summary()
        self.watch(self.app, "snapshot", self._candidates)
        self.watch(self.app, "theme", lambda _t: (self._summary(), self._candidates(self.app.snapshot)))
        self._start(dry_run=True)

    def on_unmount(self) -> None:
        self._stop_engine()
        self._want = None
        self.app.threshold_pct = self._configured
        self.app.auto_page = False  # the dashboard's queries resume once the engine thread has returned too

    def _log(self, text: str, style: str | None = None) -> None:
        self.query_one("#event-log", RichLog).write(Text(text, style=style or self.app.palette.muted))

    def _badge(self, label: str, cls: str) -> None:
        badge = self.query_one("#mode-badge", Static)
        badge.update(label)
        badge.set_classes(cls)

    # -- engine --
    def _stop_engine(self) -> None:
        if self._engine is not None:
            self._engine.stop()
            self._engine = None

    def _start(self, *, dry_run: bool) -> None:
        """Stop the running engine and start one in the given mode once no older engine thread or dashboard query is left, so two
        engines never run together and the badge never shows a mode while an engine of the other mode can still switch."""
        self._stop_engine()
        self._want, self._live = dry_run, not dry_run
        self._launch()

    def _launch(self) -> None:
        if self._want is None or not self.is_attached:
            return
        if self.app.engines or self.app._fetching:
            self._badge(" STOPPING… " if self.app.engines else " STARTING… ", "dry")
            self.set_timer(0.1, self._launch)
            return
        dry_run, self._want, app = self._want, None, self.app
        engine = autoswitch.Engine(self.cfg, lambda ev: self._from_thread(engine, ev), dry_run=dry_run,
                                   fetcher=app.source.fetcher_for(lambda: engine.stopped))
        self._engine = engine
        app.engines.add(engine)
        self._worker = self.run_worker(partial(self._run_engine, app, engine), thread=True, group="engine", exit_on_error=False)
        self._badge(" DRY-RUN " if dry_run else " LIVE ", "dry" if dry_run else "live")
        self._log(f"— engine started: {'DRY-RUN (watching only)' if dry_run else 'LIVE (will switch accounts)'} —")

    @staticmethod
    def _run_engine(app, engine) -> int:
        try:
            return engine.run_loop()
        finally:
            app.engines.discard(engine)

    def on_worker_state_changed(self, event) -> None:
        """The running engine's loop ended without being stopped (it crashed): say so instead of keeping its badge."""
        if event.worker is not self._worker or self._engine is None or event.state not in (WorkerState.ERROR, WorkerState.SUCCESS):
            return
        self._engine = None
        self._badge(" STOPPED ", "dry")
        self._log(f"— engine stopped: {event.worker.error or 'its loop ended'}; press l to start it again in dry-run —",
                  self.app.palette.warn)

    def _from_thread(self, engine, ev: dict) -> None:
        try:
            self.app.call_from_thread(self._on_event, engine, ev)
        except Exception:  # the app is closing; the event has nowhere to go
            pass

    def _on_event(self, engine, ev: dict) -> None:
        if not self.is_attached:
            return
        if engine is not self._engine and not (ev["event"] == "switch" and not ev.get("dryRun")):
            return  # a stopped engine winding down; only a real switch it had already made is worth showing
        palette = self.app.palette
        role = _EVENT_STYLES.get(ev["event"])
        style = getattr(palette, role) if role else palette.muted if ev["event"] in ("poll", "no-switch", "sleep") else palette.fg
        line = Text(f"{time.strftime('%H:%M:%S')}  ", style=palette.muted)
        line.append(autoswitch.human(ev), style=style)
        self.query_one("#event-log", RichLog).write(line)
        if ev["event"] == "switch" and not ev.get("dryRun"):
            self.app.source.forget_live()
        self.app.redraw()  # the engine just wrote the usage cache

    def action_toggle_live(self) -> None:
        if self._live or (self._engine is None and self._want is None):  # back to dry-run, or a stopped engine starts again in it
            self._start(dry_run=True)
        else:
            self.app.confirm("Go live? agyswap will switch the global login automatically when the threshold is reached, "
                             "which may move the Antigravity App too.\n\n(The same as running agyswap auto in a terminal.)",
                             "Go live", "Go live", lambda: self._start(dry_run=False) if self.is_attached else None)

    def action_back(self) -> None:
        self._cancel_adjust() if self._adjusting else self.app.pop_screen()

    # -- session threshold --
    def check_action(self, action: str, parameters: tuple) -> bool | None:
        if action in ("step", "adjust_done"):
            return self._adjusting
        return not (action == "toggle_live" and self._adjusting)  # go live only on a threshold already applied

    def action_adjust(self) -> None:
        if self._adjusting:
            self._end_adjust()
            return
        self._adjusting, self._entry = True, self.cfg.threshold
        self._summary()
        self.refresh_bindings()

    def action_adjust_done(self) -> None:
        self._end_adjust()

    def action_step(self, delta: float) -> None:
        spec = settings.SPECS["autoswitch.threshold"]
        self._set_threshold(min(spec.hi, max(spec.lo, self.cfg.threshold + delta)))

    def _set_threshold(self, value: float) -> None:
        self.cfg = replace(self.cfg, threshold=value)
        self.app.threshold_pct = value
        self.query_one("#auto-active-panel", AccountsPanel).refresh()
        self._summary()
        self._candidates(self.app.snapshot)

    def _cancel_adjust(self) -> None:
        """Esc: the threshold goes back to what the engine runs with, and the engine is left alone."""
        self._set_threshold(self._entry)
        self._end_adjust()

    def _end_adjust(self) -> None:
        self._adjusting = False
        self._summary()
        self.refresh_bindings()
        if self.cfg.threshold != self._entry:  # the engine decides again at the new value right away
            self._start(dry_run=not self._live)
            self._log(f"— threshold set to {autoswitch.pct_label(self.cfg.threshold)}% for this session —")

    # -- summary and candidates (cswap #219, #587) --
    def _summary(self) -> None:
        cfg, palette, text = self.cfg, self.app.palette, Text()
        text.append(f"strategy {cfg.strategy} · ")
        text.append(f"threshold {autoswitch.pct_label(cfg.threshold)}%", style=palette.accent if self._adjusting else "")
        if cfg.threshold != self._configured:
            text.append(" (session)", style=palette.muted)
        if cfg.weekly_threshold is not None:
            text.append(f" · weekly {autoswitch.pct_label(cfg.weekly_threshold)}%")
        text.append(f" · models {cfg.model or 'Gemini only'} · every {cfg.interval_seconds:.0f}s")
        if self._adjusting:
            text.append("   ← → adjust · enter done", style=palette.muted)
        self.query_one("#auto-summary", Static).update(text)

    def _candidates(self, snap) -> None:
        if snap is None or not self.is_mounted:
            return
        palette, cfg, models, now = self.app.palette, self.cfg, parse_models(self.cfg.model), time.time()
        ranked = []
        for num in snap.numbers:
            record, entry = snap.record(num), snap.entries[num]
            if num == snap.ctx.active_number or record.get("disabled") or record.get("kind") == "apikey":
                continue
            line, weekly = lines_for(record, cfg.threshold, cfg.weekly_threshold)
            value = entry.decision_value()
            b = autoswitch.binding(autoswitch.relevant_windows(value, models), line,
                                   weekly if cfg.weekly_threshold is not None else None, cfg.threshold)
            block = Text()
            block.append(f"\n  {num:>2}  {record.get('alias') or record.get('email', '')}", style=palette.fg)
            if entry.sentinel is not None:
                block.append(f"  {output.SENTINEL_NOTES.get(entry.sentinel, entry.sentinel)}", style=palette.muted)
            elif b is None:
                block.append("  usage unknown", style=palette.muted)
            else:
                block.append(f"  {b.label} {b.pct:.0f}% used (line {autoswitch.pct_label(b.line)}%)", style=palette.severity(b.pct))
                for label, pct, suffix, _weekly in window_rows(entry, now):
                    block.append(f"\n        {label:<26} {pct:3.0f}%  {suffix}", style=palette.muted)
            ranked.append(((999.0 if entry.sentinel else 998.0) if b is None else -b.line_headroom, num, block))
        text = Text("Next best", style=palette.muted)
        if not ranked:
            text.append("\n  no other switchable accounts", style=palette.muted)
        for _key, _num, block in sorted(ranked, key=lambda r: (r[0], int(r[1]))):
            text.append(block)
        self.query_one("#candidates", Static).update(text)
