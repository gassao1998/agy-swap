"""The dashboard: cache-only redraws and the query pace (cswap #496), --no-mouse (cswap #390), the auto page (cswap #219, #587), theme detection (cswap #177),
the CLI wiring, and the screens through textual's headless Pilot. The Pilot tests skip when textual is not installed."""
import contextlib
import importlib.util
import io
import sys
import threading
import time
import unittest
from unittest import mock

from agyswap import accounts, autoswitch, cli, settings, usage
from agyswap.errors import AgyswapError
from agyswap.tui import appearance
from agyswap.tui.data import SnapshotSource, run_action
from agyswap.usage import FetchResult, parse_usage
from agyswap.usage_store import UsageCache
from tests.helpers import TempHome, live
from tests.test_usage import FREE

HAS_TEXTUAL = importlib.util.find_spec("textual") is not None


class Counter:
    def __init__(self, result):
        self.calls, self.result = 0, result

    def __call__(self, *_args, **_kwargs):
        self.calls += 1
        return self.result


class Clock:
    def __init__(self, t=1_800_000_000.0):
        self.t = t

    def __call__(self):
        return self.t


class Roster(TempHome):
    def setUp(self):
        super().setUp()
        self.write_token("default", "me@gmail.com")
        self.write_token("school", "me@school.edu")
        accounts.save_sequence({"activeAccountNumber": 1, "accounts": {
            "1": {"email": "me@gmail.com", "alias": "default", "folder": "default"},
            "2": {"email": "me@school.edu", "alias": "school", "folder": "school"}}})
        self.read_live = Counter(live("me@gmail.com"))
        self.agy = Counter(FetchResult(usage=parse_usage(FREE)))


class SnapshotSourceTest(Roster):
    def test_ten_redraws_read_the_cache_only(self):
        """cswap #496: ten redraws three seconds apart make one Credential Manager read and start agy zero times."""
        clock = Clock()
        source = SnapshotSource(read_live=self.read_live, fetcher=self.agy, clock=clock)
        for _ in range(10):
            snap = source.take()
            clock.t += 3
        self.assertEqual((self.read_live.calls, self.agy.calls), (1, 0))
        self.assertEqual(snap.ctx.active_number, "1")
        self.assertEqual(snap.numbers, ["1", "2"])
        clock.t += 60
        source.take()
        self.assertEqual(self.read_live.calls, 2)

    def test_a_failed_read_waits_too(self):
        clock, failing = Clock(), Counter(live(error="CredReadW failed, error code 5"))
        source = SnapshotSource(read_live=failing, fetcher=self.agy, clock=clock)
        for _ in range(10):
            source.take()
            clock.t += 5
        self.assertEqual(failing.calls, 1)

    def test_refresh_follows_the_cache_plan(self):
        clock = Clock()
        source = SnapshotSource(read_live=self.read_live, fetcher=self.agy, clock=clock)
        self.assertTrue(source.refresh())
        self.assertEqual(self.agy.calls, 2)  # nothing cached yet: both accounts once
        for _ in range(10):
            clock.t += 3
            self.assertFalse(source.refresh())  # nothing due: the app skips the extra redraw
        self.assertEqual(self.agy.calls, 2)
        clock.t += 300  # the active account's 300-second plan is due, the candidate's 600 is not
        source.refresh()
        self.assertEqual(self.agy.calls, 3)
        source.refresh(full=True)
        self.assertEqual(self.agy.calls, 5)
        self.assertIsNotNone(UsageCache(clock=clock).entry("school").fetched_at)

    def test_forget_live_reads_again(self):
        source = SnapshotSource(read_live=self.read_live, fetcher=self.agy, clock=Clock())
        source.take()
        source.forget_live()
        source.take()
        self.assertEqual(self.read_live.calls, 2)

    def test_forget_live_never_waits_for_a_read(self):
        """forget_live runs on the UI thread: a Credential Manager read in progress must not block it, and that read is not kept."""
        reading, release = threading.Event(), threading.Event()

        def slow_read():
            reading.set()
            release.wait(5)
            return live("me@gmail.com")
        source = SnapshotSource(read_live=slow_read, fetcher=self.agy, clock=Clock())
        reader = threading.Thread(target=source.live)
        reader.start()
        self.assertTrue(reading.wait(5))
        forget = threading.Thread(target=source.forget_live)
        forget.start()
        forget.join(1)
        blocked = forget.is_alive()
        release.set()
        reader.join(5)
        forget.join(5)
        self.assertFalse(blocked)
        self.assertIsNone(source._live)  # the read that overlapped forget_live may predate a switch

    def test_closing_cancels_queries(self):
        """Once the app is closing no agy starts, the running one is told to stop, and nothing is recorded for the account."""
        seen = []

        def agy(env, cancel):
            seen.append(cancel())
            source.closing.set()
            raise usage.Cancelled("cancelled")  # what usage.fetch raises once cancel() turns true
        source = SnapshotSource(read_live=self.read_live, fetcher=agy, clock=Clock())
        with self.assertRaises(usage.Cancelled):
            source.refresh(full=True)
        self.assertEqual(seen, [False])  # the second account never started
        self.assertIsNone(UsageCache(clock=Clock()).entry("default").fetched_at)
        with self.assertRaises(usage.Cancelled):
            source.fetcher_for()({})


class RunActionTest(unittest.TestCase):
    def test_results(self):
        self.assertEqual(run_action(lambda: {"switched": True}).payload, {"switched": True})
        self.assertEqual(run_action(lambda: [("ok", "a"), ("dim", "b")]).text, "a\nb")

        def boom():
            print("before")
            raise AgyswapError("nope")
        result = run_action(boom)
        self.assertFalse(result.ok)
        self.assertEqual(result.text, "before\nError: nope")
        self.assertFalse(run_action(lambda: input("prompt? ")).ok)  # an unexpected prompt reads EOF, never blocks

    def test_other_threads_keep_their_streams(self):
        """sys.std* are process-wide: while an action runs, another thread's output still goes where it went before, not into the
        action's result, and the original streams are back afterwards."""
        inside, done, outer = threading.Event(), threading.Event(), io.StringIO()

        def action():
            print("mine")
            inside.set()
            done.wait(5)
            return [("ok", "done")]
        with contextlib.redirect_stdout(outer):
            box = []
            worker = threading.Thread(target=lambda: box.append(run_action(action)))
            worker.start()
            self.assertTrue(inside.wait(5))
            print("theirs")
            done.set()
            worker.join(5)
            self.assertIs(sys.stdout, outer)
        self.assertEqual(box[0].text, "mine\ndone")
        self.assertEqual(outer.getvalue(), "theirs\n")


class AppearanceTest(unittest.TestCase):
    def test_classify(self):
        self.assertEqual(appearance.classify("\x1b]11;rgb:0000/0000/0000\x1b\\"), "dark")
        self.assertEqual(appearance.classify("\x1b]11;rgb:ffff/ffff/ffff\x07\x1b[?61;6c"), "light")
        self.assertEqual(appearance.classify("\x1b]11;#fafafa\x07"), "light")
        self.assertIsNone(appearance.classify("\x1b[?61;6c"))
        self.assertIsNone(appearance.classify("]11;rgb:ffff/ffff/ffff\x07"))  # no ESC: echoed input, not a reply

    def test_read_reply_stops_at_da1_or_the_timeout(self):
        chars = iter("\x1b]11;rgb:ffff/ffff/ffff\x07\x1b[?61;6cLEFTOVER")
        self.assertTrue(appearance.read_reply(lambda: next(chars)).endswith("c"))
        clock = Clock(0.0)

        def nothing():
            clock.t += 0.5
            return None
        with mock.patch("agyswap.tui.appearance.time.sleep"):
            self.assertEqual(appearance.read_reply(nothing, timeout=1.0, clock=clock), "")

    def test_resolve_theme(self):
        """cswap #177: no reply, or a failing probe, gives dark; an explicit setting never probes."""
        probe = Counter(None)
        self.assertEqual(appearance.resolve_theme("light", probe), "light")
        self.assertEqual(probe.calls, 0)
        self.assertEqual(appearance.resolve_theme("auto", probe), "dark")
        self.assertEqual(appearance.resolve_theme("auto", lambda: "\x1b]11;#ffffff\x07"), "light")
        self.assertEqual(appearance.resolve_theme("auto", mock.Mock(side_effect=OSError)), "dark")

    def probe(self, typed: str, *, vt_output=True):
        """_console_query against a fake console: (reply, console modes afterwards, mode sets, flushes, written, keys left)."""
        import ctypes

        modes, sets, flushes, keys, written = {10: 0x01F7, 11: 0x0003}, [], [], list(typed), []

        def get_std(h):
            return {-10 & 0xFFFFFFFF: 10, -11 & 0xFFFFFFFF: 11}[h]

        def get_mode(h, ref):
            ref._obj.value = modes[h]
            return 1

        def set_mode(h, m):
            sets.append((h, m))
            if h == 11 and m & 0x0004 and not vt_output:
                return 0
            modes[h] = m
            return 1

        def flush(h):
            flushes.append(h)
            return 1
        k32 = mock.Mock(GetStdHandle=get_std, GetConsoleMode=get_mode, SetConsoleMode=set_mode, FlushConsoleInputBuffer=flush)
        tty = mock.Mock(isatty=lambda: True, write=written.append)
        with mock.patch.object(ctypes, "WinDLL", return_value=k32), mock.patch("sys.stdin", tty), mock.patch("sys.stdout", tty), \
                mock.patch("msvcrt.kbhit", lambda: bool(keys)), mock.patch("msvcrt.getwch", lambda: keys.pop(0)), \
                mock.patch.object(appearance, "_LATE_REPLY_S", 0.05):
            reply = appearance._console_query()
        return reply, modes, sets, flushes, "".join(written), "".join(keys)

    @unittest.skipUnless(sys.platform == "win32", "the Windows console probe")
    def test_console_probe_restores_and_keeps_type_ahead(self):
        """VT output is on only for the query and both console modes are put back; after a DA1 reply the keys typed since are kept,
        while without one the input buffer is flushed so a late reply cannot reach the app or the shell."""
        reply, modes, sets, flushes, written, left = self.probe("x" * 300 + "\x1b]11;rgb:ffff/ffff/ffff\x07\x1b[?61;6cqq")
        self.assertEqual(appearance.classify(reply), "light")  # keys typed before it did not cut the reply short
        self.assertEqual(modes, {10: 0x01F7, 11: 0x0003})
        self.assertIn((11, 0x0007), sets)
        self.assertEqual((flushes, left, written), ([], "qq", appearance._QUERY))
        reply, modes, _sets, flushes, _written, _left = self.probe("")
        self.assertIsNone(appearance.classify(reply))
        self.assertEqual((modes, flushes), ({10: 0x01F7, 11: 0x0003}, [10]))
        reply, modes, _sets, _flushes, written, _left = self.probe("", vt_output=False)
        self.assertIsNone(reply)  # a console without VT output is never sent the query
        self.assertEqual((written, modes), ("", {10: 0x01F7, 11: 0x0003}))

    def test_no_console_no_probe(self):
        with mock.patch("sys.stdin", io.StringIO()), mock.patch("sys.stdout", io.StringIO()) as out:
            self.assertIsNone(appearance._console_query())
        self.assertEqual(out.getvalue(), "")


def fake_engines(linger=0.0):
    """A stand-in for autoswitch.Engine on the auto page, and the list of every one made. It runs until stopped, then takes
    `linger` seconds to finish its tick; `peak` is the most loops that ever ran at once, and `crash` makes the next loop raise."""
    made = []

    class FakeEngine:
        peak, running, crash, lock = 0, 0, None, threading.Lock()

        def __init__(self, cfg, on_event, *, dry_run=False, fetcher=None):
            self.cfg, self.on_event, self.dry_run, self._stop = cfg, on_event, dry_run, threading.Event()
            made.append(self)

        @property
        def stopped(self):
            return self._stop.is_set()

        def run_loop(self):
            cls = type(self)
            with cls.lock:
                cls.running += 1
                cls.peak = max(cls.peak, cls.running)
            try:
                if cls.crash is not None:
                    raise cls.crash
                self.on_event(autoswitch.event("no-switch", reason="below-threshold"))
                self._stop.wait(10)
                time.sleep(linger)
                return 0
            finally:
                with cls.lock:
                    cls.running -= 1

        def stop(self):
            self._stop.set()
    return FakeEngine, made


def run_cli(argv):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            code = cli.dispatch(argv)
        except SystemExit as e:
            code = e.code
    return code, err.getvalue()


class CliTest(TempHome):
    def test_without_textual(self):
        with mock.patch("agyswap.tui.available", return_value=False):
            for argv in (["tui"], ["watch"], ["--watch"]):
                code, err = run_cli(argv)
                self.assertEqual(code, 1)
                self.assertIn("textual", err)

    def test_dispatch(self):
        with mock.patch("agyswap.tui.available", return_value=True), mock.patch("agyswap.tui.run", return_value=0) as run:
            self.assertEqual(run_cli(["tui", "--no-mouse"])[0], 0)
            self.assertEqual(run_cli(["watch"])[0], 0)
        self.assertEqual(run.call_args_list, [mock.call("dashboard", no_mouse=True), mock.call("watch", no_mouse=False)])
        code, err = run_cli(["list", "--no-mouse"])
        self.assertEqual(code, 2)
        self.assertIn("--no-mouse can only be used with", err)

    def test_bare_command(self):
        """A bare agyswap opens the dashboard only in a terminal with textual installed; otherwise the usage error stays."""
        tty = mock.Mock(isatty=lambda: True)
        for has, expected in ((True, 0), (False, 2)):
            with mock.patch("agyswap.tui.available", return_value=has), mock.patch("agyswap.tui.run", return_value=0) as run, \
                    mock.patch("sys.stdin", tty), mock.patch("sys.stdout", tty), contextlib.redirect_stderr(io.StringIO()):
                try:
                    code = cli.dispatch([])
                except SystemExit as e:
                    code = e.code
            self.assertEqual((code, run.called), (expected, has))
        with mock.patch("agyswap.tui.run") as run:
            self.assertEqual(run_cli([])[0], 2)  # not a terminal
        run.assert_not_called()


@unittest.skipUnless(HAS_TEXTUAL, "textual is not installed (the tui extra)")
class PilotTest(Roster, unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        super().setUp()
        UsageCache().record_success("school", parse_usage(FREE), 600.0)

    async def asyncSetUp(self):
        import asyncio

        asyncio.get_running_loop().set_debug(False)  # the test case turns asyncio debug on, which reports every slow textual step

    def app(self, **kwargs):
        from agyswap.tui.app import AgyswapApp

        self.source = SnapshotSource(read_live=self.read_live, fetcher=self.agy)
        return AgyswapApp(source=self.source, theme_setting="dark", **kwargs)

    async def settle(self, app, pilot):
        from textual.worker import WorkerFailed

        for _ in range(3):
            waiting = [w for w in app.workers if w.group != "engine"]  # an auto engine runs until it is stopped
            if waiting:
                with contextlib.suppress(WorkerFailed):  # the app reports a failed worker itself
                    await app.workers.wait_for_complete(waiting)
            await pilot.pause()

    async def until(self, pilot, condition, timeout=5.0):
        end = time.monotonic() + timeout
        while not condition():
            self.assertLess(time.monotonic(), end, "timed out")
            await pilot.pause(0.05)

    async def test_dashboard_redraws_within_the_limits(self):
        """Same as above (cswap #496), through the app: ten ticks make one Credential Manager read and query only the account the cache plan has due."""
        app = self.app()
        async with app.run_test(size=(120, 40)) as pilot:
            await self.settle(app, pilot)
            for _ in range(10):
                app._tick()
                await self.settle(app, pilot)
            panel = str(app.screen.query_one("#accounts-panel").render())
            self.assertIn("me@gmail.com", panel)
            self.assertIn("● active", panel)
            self.assertIn("school", panel)
        self.assertEqual(self.read_live.calls, 1)
        self.assertEqual(self.agy.calls, 1)  # the active account had no reading; school's 600-second plan is not due

    async def test_quitting_ends_a_running_query(self):
        """Quitting while agy runs: the query is cancelled at once instead of running to its end, and no worker calls back into the
        closed app."""
        import asyncio

        started, cancelled = threading.Event(), threading.Event()

        def slow_agy(env, cancel):
            started.set()
            end = time.monotonic() + 5
            while time.monotonic() < end:
                if cancel():
                    cancelled.set()
                    raise usage.Cancelled("cancelled")
                time.sleep(0.01)
            return FetchResult(usage=parse_usage(FREE))
        app, done = self.app(), threading.Event()
        self.source.fetcher, refresh = slow_agy, self.source.refresh

        def tracked(**kwargs):
            try:
                return refresh(**kwargs)
            finally:
                done.set()
        self.source.refresh = tracked
        async with app.run_test(size=(120, 40)) as pilot:
            self.assertTrue(await asyncio.to_thread(started.wait, 5))
            while app.snapshot is None:  # the first redraw holds .lock briefly; let it finish before the temp folder goes
                await pilot.pause(0.05)
            app.exit()
        self.assertTrue(await asyncio.to_thread(cancelled.wait, 1))
        self.assertTrue(await asyncio.to_thread(done.wait, 5))  # the claim is released before the temp folder goes
        self.assertIsNone(UsageCache().entry("default").fetched_at)

    async def test_switch_asks_first(self):
        from agyswap.tui.screens import SwitchScreen

        with mock.patch("agyswap.switch.switch_to", return_value={"switched": True, "message": "ok", "warnings": []}) as switch_to:
            app = self.app()
            async with app.run_test(size=(120, 40)) as pilot:
                await self.settle(app, pilot)
                await pilot.press("s")
                await self.settle(app, pilot)
                self.assertIsInstance(app.screen, SwitchScreen)
                await pilot.press("down", "enter")
                await pilot.press("n")
                await self.settle(app, pilot)
                switch_to.assert_not_called()
                await pilot.press("s", "down", "enter", "y")
                await self.settle(app, pilot)
        switch_to.assert_called_once_with("2")
        self.assertEqual(self.read_live.calls, 2)  # read again after the switch

    async def test_prompts_never_confirm_by_accident(self):
        """Cancel has the focus, no key moves it round to the confirm button, a second prompt never stacks, and disabling an account
        asks first too (its submenu opens on the first account, so a double Enter would toggle it)."""
        from agyswap.tui.app import ConfirmModal

        with mock.patch("agyswap.manage.add") as add, mock.patch("agyswap.manage.set_disabled", return_value=[]) as set_disabled:
            app = self.app()
            async with app.run_test(size=(120, 40)) as pilot:
                await self.settle(app, pilot)
                app.action_add_current()
                app.action_add_current()
                await pilot.pause()
                self.assertEqual(sum(isinstance(s, ConfirmModal) for s in app.screen_stack), 1)
                self.assertEqual(app.screen.focused.id, "no")
                await pilot.press("right", "tab")
                self.assertEqual(app.screen.focused.id, "no")
                await pilot.press("left")
                self.assertEqual(app.screen.focused.id, "yes")
                await pilot.press("right", "enter")
                await self.settle(app, pilot)
                self.assertNotIsInstance(app.screen, ConfirmModal)
                app.do_toggle_disabled("1")
                await pilot.pause()
                self.assertIsInstance(app.screen, ConfirmModal)
                await pilot.press("enter")
                app.do_toggle_disabled("9")  # removed meanwhile: a warning, not a crash
                app.do_toggle_disabled("2")
                await pilot.press("y")
                await self.settle(app, pilot)
        add.assert_not_called()
        set_disabled.assert_called_once_with("2", True)

    async def test_menu_renders_never_interleave(self):
        """A selection and a key run on different message queues: overlapping renders leave one copy of the menu, and an open
        account submenu follows the roster."""
        import asyncio

        app = self.app()
        async with app.run_test(size=(120, 40)) as pilot:
            await self.settle(app, pilot)
            screen = app.screen
            await asyncio.gather(screen._push("remove account", screen._accounts("remove")), screen._pop())
            await pilot.pause()
            self.assertEqual(len(screen.query_one("#menu").children), 8)
            await screen._push("remove account", screen._accounts("remove"))
            data = accounts.load_sequence()
            data["accounts"]["3"] = {"email": "third@gmail.com", "folder": "third"}
            accounts.save_sequence(data)
            app.redraw()
            await self.settle(app, pilot)
            self.assertEqual(len(screen.query_one("#menu").children), 4)  # three accounts and back

    def test_stale_figures_look_stale(self):
        """An inactive account's one-liner dims a reading older than STALE_OK_S and gives its age, as the full card does."""
        from agyswap.tui.app import DARK, Palette, mini_text

        now = time.time()
        UsageCache(clock=lambda: now - 7200).record_success("school", parse_usage(FREE), 600.0)
        snap = SnapshotSource(read_live=self.read_live, fetcher=self.agy).take()
        line = mini_text(snap, "2", Palette.of(DARK), now=now)
        self.assertIn("2h ago", line.plain)
        self.assertTrue(all("dim" in str(span.style) for span in line.spans if "%" in line.plain[span.start:span.end]))

    async def test_stale_snapshot_and_errors_show_everywhere(self):
        """A frozen snapshot says so on the dashboard and the switch page, a fetch lane failing the same way every tick warns once,
        and a redraw asked for during a draw runs after it instead of being dropped."""
        import dataclasses

        from agyswap.tui.screens import SwitchScreen

        app = self.app()
        async with app.run_test(size=(120, 40)) as pilot:
            await self.settle(app, pilot)
            app.snapshot = dataclasses.replace(app.snapshot, taken_at=time.time() - 180)
            app._update_status()
            await pilot.pause()
            self.assertIn("snapshot 3m ago", str(app.screen.query_one("#accounts-panel").render()))
            await pilot.press("s")
            await pilot.pause()
            self.assertIsInstance(app.screen, SwitchScreen)
            self.assertIn("snapshot 3m ago", str(app.screen.query_one("#list-title").render()))
            await pilot.press("escape")
            with mock.patch.object(self.source, "refresh", side_effect=AgyswapError("roster unreadable")), \
                    mock.patch.object(app, "notify") as notify:
                for _ in range(3):
                    app._tick()
                    await self.settle(app, pilot)
            self.assertEqual(sum("fetch failed" in str(c.args[0]) for c in notify.call_args_list), 1)
            app._drawing = True  # a draw in progress
            app.redraw()
            self.assertFalse([w for w in app.workers if w.group == "draw" and not w.is_finished])
            app._apply(app.snapshot)  # that draw lands: the asked-for one starts now
            self.assertTrue(app._drawing)
            await self.settle(app, pilot)

    async def test_switch_page_best_pick_and_watch_focus(self):
        from agyswap.tui.app import ConfirmModal
        from agyswap.tui.screens import DashboardScreen, SwitchScreen, WatchScreen

        app = self.app()
        async with app.run_test(size=(120, 40)) as pilot:
            await self.settle(app, pilot)
            await pilot.press("s", "b")  # like Enter, b leaves the switch page before asking
            await pilot.pause()
            self.assertIsInstance(app.screen, ConfirmModal)
            self.assertFalse(any(isinstance(s, SwitchScreen) for s in app.screen_stack))
            await pilot.press("n", "w")
            await self.settle(app, pilot)
            self.assertIsInstance(app.screen, WatchScreen)
            self.assertIsNone(app.screen.focused)
            await pilot.press("down", "down")
            self.assertIsNone(app.screen.query_one("#accounts").index)  # scrolled, no cursor until s arms selection
            await pilot.press("escape")
            self.assertIsInstance(app.screen, DashboardScreen)

    async def test_watch_page_and_theme(self):
        from agyswap.tui.screens import WatchScreen

        app = self.app(start="watch")
        async with app.run_test(size=(120, 40)) as pilot:
            await self.settle(app, pilot)
            self.assertIsInstance(app.screen, WatchScreen)
            self.assertEqual(len(app.screen.query("AccountCard")), 2)
            await pilot.press("ctrl+t")
            self.assertEqual(app.theme, "agyswap-light")
            await pilot.press("escape")
            self.assertNotIsInstance(app.screen, WatchScreen)
            with mock.patch.object(app, "notify") as notify:
                app.apply_theme("auto")  # not probed at this start: dark, and the user is told why
            self.assertEqual(app.theme, "agyswap-dark")
            self.assertIn("next start", notify.call_args_list[0].args[0])
            app.apply_theme("light")
            with mock.patch.object(self.source, "take", side_effect=AgyswapError(r"C:\data\[red]draft")), \
                    mock.patch.object(app, "notify") as notify:
                app.redraw()
                await self.settle(app, pilot)
            self.assertIs(notify.call_args.kwargs.get("markup"), False)  # as markup, [red] vanished and the backslash with it
        self.assertEqual(settings.load()["ui.theme"], "light")

    async def test_auto_page_shows_settings_and_candidates(self):
        """cswap #219, #587: strategy, threshold, model categories, and each candidate's windows with reset times; the engine starts in dry-run."""
        settings.set_value("autoswitch.strategy", "weekly-first")
        settings.set_value("autoswitch.model", "claude")
        FakeEngine, engines = fake_engines()

        with mock.patch("agyswap.autoswitch.Engine", FakeEngine):
            app = self.app()
            async with app.run_test(size=(140, 50)) as pilot:
                await self.settle(app, pilot)
                await pilot.press("g")
                await self.settle(app, pilot)
                self.assertTrue(app.store_only)
                summary = str(app.screen.query_one("#auto-summary").render())
                for part in ("strategy weekly-first", "threshold 90%", "models claude"):
                    self.assertIn(part, summary)
                candidates = str(app.screen.query_one("#candidates").render())
                self.assertIn("school", candidates)
                self.assertIn("Claude and GPT models 7d", candidates)
                self.assertIn("resets", candidates)
                await pilot.press("t", "right", "right", "enter")
                await self.until(pilot, lambda: len(engines) == 2)  # restarted once for the new session threshold
                self.assertIn("threshold 92%", str(app.screen.query_one("#auto-summary").render()))
                await pilot.press("l", "n")  # going live is refused at the prompt
                await pilot.press("escape")
                await self.until(pilot, lambda: not app.store_only)
        self.assertTrue(all(e.dry_run for e in engines))
        self.assertEqual(len(engines), 2)
        self.assertTrue(engines[-1].stopped)
        self.assertEqual(settings.load()["autoswitch.threshold"], 90.0)  # the session threshold is never saved

    async def test_auto_page_never_runs_two_engines(self):
        """Going live, back to dry-run, and leaving: each new engine starts only after the old thread has returned, the badge says
        STOPPING meanwhile rather than the new mode, and the dashboard's queries resume only after the last engine is gone."""
        FakeEngine, engines = fake_engines(linger=0.4)  # a stopped engine takes a while to finish its tick
        with mock.patch("agyswap.autoswitch.Engine", FakeEngine):
            app = self.app()
            async with app.run_test(size=(140, 50)) as pilot:
                await self.settle(app, pilot)
                await pilot.press("g")
                await self.until(pilot, lambda: len(engines) == 1)
                badge = app.screen.query_one("#mode-badge")
                await pilot.press("l", "y")
                self.assertIn("STOPPING", str(badge.render()))
                await self.until(pilot, lambda: len(engines) == 2)
                self.assertIn("LIVE", str(badge.render()))
                await pilot.press("l")  # back to dry-run: the live engine may still be finishing
                self.assertIn("STOPPING", str(badge.render()))
                await self.until(pilot, lambda: len(engines) == 3)
                self.assertIn("DRY-RUN", str(badge.render()))
                await pilot.press("escape")
                self.assertTrue(app.store_only)  # the last engine is still finishing its tick
                await self.until(pilot, lambda: not app.store_only)
        self.assertEqual([e.dry_run for e in engines], [True, False, True])
        self.assertEqual(FakeEngine.peak, 1)
        self.assertTrue(all(e.stopped for e in engines))

    async def test_auto_page_threshold_and_crash(self):
        """Esc while adjusting puts the threshold back without a restart, l waits until the threshold is applied, and an engine
        that crashes says so instead of keeping its badge."""
        FakeEngine, engines = fake_engines()
        with mock.patch("agyswap.autoswitch.Engine", FakeEngine):
            app = self.app()
            async with app.run_test(size=(140, 50)) as pilot:
                await self.settle(app, pilot)
                await pilot.press("g")
                await self.until(pilot, lambda: len(engines) == 1)
                screen = app.screen
                await pilot.press("t", "right", "l")
                self.assertIs(app.screen, screen)  # no Go live prompt while adjusting
                await pilot.press("escape")
                self.assertIs(app.screen, screen)  # Esc ended the adjusting, not the page
                self.assertIn("threshold 90%", str(screen.query_one("#auto-summary").render()))
                self.assertEqual(app.threshold_pct, 90.0)
                FakeEngine.crash = RuntimeError("boom")
                await pilot.press("l", "y")
                await self.until(pilot, lambda: "STOPPED" in str(screen.query_one("#mode-badge").render()))
                await pilot.press("l")  # starts again in dry-run, without a prompt
                FakeEngine.crash = None
                await self.until(pilot, lambda: len(engines) == 3)
                self.assertIs(app.screen, screen)
                await pilot.press("escape")
        self.assertEqual([e.dry_run for e in engines], [True, False, True])

    def test_no_mouse(self):
        """cswap #390: --no-mouse leaves mouse tracking off."""
        from agyswap import tui
        from agyswap.tui.app import AgyswapApp

        with mock.patch.object(AgyswapApp, "run") as run, mock.patch("agyswap.tui.appearance._console_query", return_value=None):
            tui.run(no_mouse=True)
            tui.run()
        self.assertEqual([c.kwargs for c in run.call_args_list], [{"mouse": False}, {"mouse": True}])


if __name__ == "__main__":
    unittest.main()
