"""The dashboard's data service: cache-only snapshots, background agy queries on the section 4.3 pace, and captured actions.

Adapted from cswap 1.0.0's tui/data.py and snapshot_source.py (MIT License, claude-swap). The difference (cswap #496): cswap reads a free
usage API every few seconds, while every agyswap query starts an agy process, so a redraw never queries. take() only reads the usage
cache, and refresh() starts agy for the accounts whose cache plan (nextPollAt) is due. The Credential Manager read is kept in memory
for LIVE_TTL_S, and a failed read waits out the same time. Nothing here imports textual, so it is tested without it.
Everything is blocking (file locks, Credential Manager, agy) and runs in a thread worker, never on the UI loop.
"""
from __future__ import annotations

import contextlib
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
        self._lock = threading.Lock()

    def live(self) -> LiveLogin:
        """cswap #496: one Credential Manager read per LIVE_TTL_S, a failed one included."""
        with self._lock:
            now = self.clock()
            if self._live is None or not 0 <= now - self._live_at < LIVE_TTL_S:
                self._live, self._live_at = self.read_live(), now
            return self._live

    def forget_live(self) -> None:
        """After a switch the remembered login is wrong: the next redraw reads Credential Manager again."""
        with self._lock:
            self._live = None

    def _context(self) -> Context:
        return load_context(read_live=self.live, quiet=True)

    def take(self) -> Snapshot:
        """A redraw: the roster and the usage cache, never an agy start."""
        ctx = self._context()
        nums = accounts.numbers(ctx.data) if ctx.data else []
        cache = UsageCache(clock=self.clock)
        return Snapshot(ctx, collect(ctx, nums, cache=cache, query=False), self.clock())

    def refresh(self, *, full: bool = False) -> None:
        """Start agy, one account at a time, for the accounts due on the cache plan (with full, every account not in backoff)."""
        ctx = self._context()
        if not ctx.data:
            return
        cache, now = UsageCache(clock=self.clock), self.clock()
        nums = accounts.numbers(ctx.data)
        plan = {n for n in nums if full or due(cache.entry(ctx.data["accounts"][n].get("folder") or n), now)}
        if plan:
            collect(ctx, nums, fetcher=self.fetcher, cache=cache, fetch=plan)


@dataclass
class ActionResult:
    ok: bool
    text: str
    payload: dict | None = field(default=None)


def run_action(fn) -> ActionResult:
    """Run a switch or manage call with stdout and stderr captured. stdin is swapped for an empty stream, so an unexpected prompt
    reads EOF instead of freezing the app (cswap run_action). manage calls return [(style, text)], switch calls a payload dict."""
    buf = io.StringIO()
    saved = sys.stdin
    sys.stdin = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
            try:
                result = fn()
            except AgyswapError as e:
                return ActionResult(False, f"{buf.getvalue()}Error: {e}".strip())
            except EOFError:
                return ActionResult(False, f"{buf.getvalue()}Error: interactive input is not available here".strip())
    finally:
        sys.stdin = saved
    if isinstance(result, dict):
        return ActionResult(True, buf.getvalue().strip(), result)
    lines = [text for _style, text in result or []]
    return ActionResult(True, "\n".join([buf.getvalue().strip(), *lines]).strip())
