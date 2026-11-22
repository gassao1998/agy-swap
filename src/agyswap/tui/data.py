"""The dashboard's data service: cache-only snapshots, background agy queries on the section 4.3 pace, and captured actions.

Adapted from cswap 1.0.0's tui/data.py and snapshot_source.py (MIT License, claude-swap). The difference (cswap #496): cswap reads a free
usage API every few seconds, while every agyswap query starts an agy process, so a redraw never queries. take() only reads the usage
cache, and refresh() starts agy for the accounts whose cache plan (nextPollAt) is due. The Credential Manager read is kept in memory
for LIVE_TTL_S, and a failed read waits out the same time. Nothing here imports textual, so it is tested without it.
Everything is blocking (file locks, Credential Manager, agy) and runs in a thread worker, never on the UI loop.
"""
from __future__ import annotations

import io
import sys
import threading
import time
from dataclasses import dataclass, field

from agyswap import accounts, usage
from agyswap.errors import AgyswapError
from agyswap.login import LiveLogin, read_live_login
from agyswap.state import Context, collect, load_context
from agyswap.usage_store import Entry, UsageCache

LIVE_TTL_S = 60.0


@dataclass(frozen=True)
class Snapshot:
    ctx: Context
    entries: dict[str, Entry]
    taken_at: float

    @property
    def numbers(self) -> list[str]:
        return accounts.numbers(self.ctx.data) if self.ctx.data else []

    def record(self, num: str) -> dict:
        return self.ctx.data["accounts"][num]


def due(entry: Entry, now: float) -> bool:
    """Query when the cache plan says so, never in backoff or while another process holds the account (cswap #157, #85)."""
    if entry.in_backoff(now) or entry.claimed(now):
        return False
    return entry.fetched_at is None or entry.next_poll_at is None or now >= entry.next_poll_at


class SnapshotSource:
    def __init__(self, *, read_live=read_live_login, fetcher=usage.fetch, clock=time.time):
        self.read_live = read_live
        self.fetcher = fetcher
        self.clock = clock
        self._live: LiveLogin | None = None
        self._live_at = 0.0
        self._gen = 0
        self._lock = threading.Lock()
        self.closing = threading.Event()  # the app is closing: no agy starts, and the one running is ended

    def live(self) -> LiveLogin:
        """cswap #496: one Credential Manager read per LIVE_TTL_S, a failed one included. The lock only orders the readers."""
        with self._lock:
            now, gen, cached, at = self.clock(), self._gen, self._live, self._live_at
            if cached is not None and 0 <= now - at < LIVE_TTL_S:
                return cached
            login = self.read_live()
            if gen == self._gen:  # after a forget_live meanwhile, this read may predate the switch: do not keep it
                self._live, self._live_at = login, now
            return login

    def forget_live(self) -> None:
        """After a switch the remembered login is wrong: the next redraw reads Credential Manager again. It runs on the UI thread,
        so it never waits for a read in progress."""
        self._live = None
        self._gen += 1

    def fetcher_for(self, stopped=None):
        """agy for the dashboard: none starts once the app is closing or stopped() is true, and the one running is ended."""
        def cancelled() -> bool:
            return self.closing.is_set() or (stopped is not None and stopped())

        def run(env):
            if cancelled():
                raise usage.Cancelled("the usage query was cancelled")
            return self.fetcher(env, cancel=cancelled)
        return run

    def _context(self) -> Context:
        return load_context(read_live=self.live, quiet=True)

    def take(self) -> Snapshot:
        """A redraw: the roster and the usage cache, never an agy start."""
        ctx = self._context()
        nums = accounts.numbers(ctx.data) if ctx.data else []
        cache = UsageCache(clock=self.clock)
        return Snapshot(ctx, collect(ctx, nums, cache=cache, query=False), self.clock())

    def refresh(self, *, full: bool = False, stopped=None) -> bool:
        """Start agy, one account at a time, for the accounts due on the cache plan (with full, every account not in backoff).
        Returns whether any account was due; raises usage.Cancelled when closing or stopped() ends it early."""
        ctx = self._context()
        if not ctx.data:
            return False
        cache, now = UsageCache(clock=self.clock), self.clock()
        nums = accounts.numbers(ctx.data)
        plan = {n for n in nums if full or due(cache.entry(ctx.data["accounts"][n].get("folder") or n), now)}
        if plan:
            collect(ctx, nums, fetcher=self.fetcher_for(stopped), cache=cache, fetch=plan)
        return bool(plan)


@dataclass
class ActionResult:
    ok: bool
    text: str
    payload: dict | None = field(default=None)


class _Routed:
    """A sys.std* stand-in: the thread that installed it reads and writes `mine`, every other thread the stream it replaced."""

    def __init__(self, other, mine) -> None:
        self._other, self._mine, self._owner = other, mine, threading.get_ident()

    def __getattr__(self, name):
        return getattr(self._mine if threading.get_ident() == self._owner else self._other, name)


def run_action(fn) -> ActionResult:
    """Run a switch or manage call with stdout and stderr captured. stdin reads as an empty stream, so an unexpected prompt
    reads EOF instead of freezing the app (cswap run_action). manage calls return [(style, text)], switch calls a payload dict.
    sys.std* are process-wide while the draw, fetch and engine threads keep running, so only this thread is redirected."""
    buf = io.StringIO()
    names = ("stdin", "stdout", "stderr")
    saved = [getattr(sys, n) for n in names]
    routed = [_Routed(old, mine) for old, mine in zip(saved, (io.StringIO(), buf, buf))]
    for name, stream in zip(names, routed):
        setattr(sys, name, stream)
    try:
        try:
            result = fn()
        except AgyswapError as e:
            return ActionResult(False, f"{buf.getvalue()}Error: {e}".strip())
        except EOFError:
            return ActionResult(False, f"{buf.getvalue()}Error: interactive input is not available here".strip())
    finally:
        for name, stream, old in zip(names, routed, saved):
            if getattr(sys, name) is stream:  # textual puts its own streams back when it closes, possibly mid-action
                setattr(sys, name, old)
    if isinstance(result, dict):
        return ActionResult(True, buf.getvalue().strip(), result)
    lines = [text for _style, text in result or []]
    return ActionResult(True, "\n".join([buf.getvalue().strip(), *lines]).strip())
