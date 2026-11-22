"""agyswap auto: poll usage and switch the global login before the active account hits its limit.

The policy, the events, the state file and the --once exit codes follow cswap 1.0.0's autoswitch.py and lines.py (MIT License, claude-swap):
each window of the active account is judged against its own switch line (the account's threshold override, else autoswitch.threshold; the
weekly windows use autoswitch.weeklyThreshold once it is set); a candidate must sit below every one of its own lines and beat the active
account by hysteresisPct points of line headroom; cooldownSeconds bounds proactive switches, and at-limit bypasses it; after unhealthyTicks
polls without readable usage the engine fails over; when every account is over its lines it moves only by the ratio margin
(HORIZON_HEADROOM_RATIO, capped at SPENT_HEADROOM_PCT) and never undoes its own last move until the account it left has recovered.

Differences from cswap, all because usage comes from starting agy (seconds per query, no free API):
- The schedule is the shared usage cache's: the active account when its plan is due, one stale candidate per tick, and every candidate only
  when a switch could be near (a window within ESCALATION_MARGIN_PCT of its line, or failover due this tick).
- A tick counts towards failover when the active account's query fails or its usage is unknown for another reason; ticks spent waiting out
  the query backoff do not count again. The latest attempt failing makes the account unknown even if the cache holds a value (cswap #208).
- An escape (at-limit, failover) lands on an account below its lines when one exists and ranks by headroom, never by reset time (cswap #342, #305).
- After a manual switch, consume-first and priority-consume-first stay on the chosen account until it reaches a line (cswap #598).
- With --model, when every candidate is full and the active account's Gemini limit is used up too, the target is picked on Gemini alone (cswap #403).
- next-available (an agyswap extension) ranks qualifying candidates by sequence order after the active account.
- Freshening a target's token is agy's own job; a target whose query asks for a login is quarantined until its login file changes.
A switch is always switch.switch_to: backup, save-back, write, read-back and restore are never reimplemented here.
"""
from __future__ import annotations

import contextlib
import enum
import math
import random
import sys
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from agyswap import accounts, paths, run, settings, switch, usage
from agyswap.credman import WinCred
from agyswap.errors import AgyswapError, CredentialError
from agyswap.fsutil import atomic_write_json, file_lock, read_json, sha256_hex
from agyswap.login import read_live_login, read_token_file
from agyswap.output import SCHEMA_VERSION
from agyswap.pace import parse_ts
from agyswap.printer import warning
from agyswap.state import collect, load_context, sync_active_login
from agyswap.switch import model_matches, parse_models
from agyswap.usage_store import EXHAUSTED_INTERVAL_S, SERVE_TTL_S, URGENT_INTERVAL_S, Entry, UsageCache

STATE_SCHEMA_VERSION = 1
HORIZON_HEADROOM_RATIO = 2.0
SPENT_HEADROOM_PCT = 3.0
RECOVERY_HYSTERESIS_S = 300.0
RECOVERY_HORIZON_S = 4 * 3600.0
ESCALATION_MARGIN_PCT = 15.0
RESET_SLACK_S = 60.0
MAX_SLEEP_S = EXHAUSTED_INTERVAL_S
NO_RESET_FALLBACK_S = 300.0
HEALTH_STALE_S = 3600.0  # persisted failover counters older than this start over (cswap: twice its idle-hold cap)
PROACTIVE = ("proactive", "consume-first", "priority")
HEALTH_KEYS = ("unhealthyTicks", "healthAccount", "healthTickAt")


class Outcome(enum.IntEnum):
    """One tick's outcome; the values are the --once exit codes."""
    SWITCHED = 0
    ERROR = 1
    NO_ACTION = 2
    BLOCKED = 3


class Skip(AgyswapError):
    """Raised under .lock to call one target off (a live agyswap run holds its login, cswap #508)."""


class Stale(AgyswapError):
    """Raised under .lock when the tick's decision no longer holds: the active account changed hands or the target was disabled meanwhile."""


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def pct_label(value: float) -> str:
    return f"{value:.10g}"


def _isnum(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


# ---------- windows and lines (cswap lines.py) ----------
@dataclass(frozen=True)
class Window:
    label: str
    pct: float
    resets_at: str | None
    weekly: bool


def relevant_windows(value, models: tuple[str, ...] = ()) -> list[Window]:
    """5h and 7d (the Gemini category), then each scoped window matching models; a non-dict value has none."""
    if not isinstance(value, dict):
        return []
    out = []
    for key, label, weekly in (("five_hour", "5h", False), ("seven_day", "7d", True)):
        w = value.get(key)
        if isinstance(w, dict) and _isnum(w.get("pct")):
            out.append(Window(label, float(w["pct"]), w.get("resets_at"), weekly))
    if models:
        for s in value.get("scoped") or []:
            if isinstance(s, dict) and _isnum(s.get("pct")) and isinstance(s.get("name"), str) \
                    and any(model_matches(s["name"], m) for m in models):
                weekly = s.get("window") != "5h"
                out.append(Window(s["name"] if weekly else f"{s['name']} 5h", float(s["pct"]), s.get("resets_at"), weekly))
    return out


def headroom(value, models: tuple[str, ...] = ()) -> float | None:
    pcts = [w.pct for w in relevant_windows(value, models)]
    return 100.0 - max(pcts) if pcts else None


def line_headroom(pct: float, line: float, scale: float) -> float:
    """Policy headroom of one window on the threshold's scale: 100 - pct with equal lines; three anchors (0%, the line, 100%) otherwise."""
    if line == scale:
        return 100.0 - pct
    if pct <= line:
        return (100.0 - scale) + scale * (line - pct) / line
    return (100.0 - scale) * (100.0 - pct) / (100.0 - line)


@dataclass(frozen=True)
class Binding:
    label: str
    pct: float
    line: float
    resets_at: str | None
    line_headroom: float

    @property
    def over(self) -> bool:
        return self.pct >= self.line


def binding(windows: list[Window], threshold: float, weekly: float | None, scale: float) -> Binding | None:
    """The window with the least line headroom, over-line windows first; ties keep the first (5h, 7d, then scoped)."""
    best, best_key = None, None
    for w in windows:
        line = weekly if weekly is not None and w.weekly else threshold
        b = Binding(w.label, w.pct, line, w.resets_at, line_headroom(w.pct, line, scale))
        key = (not b.over, b.line_headroom)
        if best_key is None or key < best_key:
            best, best_key = b, key
    return best


def margin_met(candidate: float, active: float) -> bool:
    """Every account over its lines: move only for twice the active's headroom, capped at SPENT_HEADROOM_PCT points more."""
    return candidate >= min(active * HORIZON_HEADROOM_RATIO, active + SPENT_HEADROOM_PCT)


def _reset_ts(resets_at, now: float) -> float | None:
    ts = parse_ts(resets_at)
    return ts if ts is not None and ts > now else None


def weekly_reset(value, now: float) -> float | None:
    """The 7d window's reset; one already past counts as unknown, so a just-rolled-over account is never the soonest."""
    w = value.get("seven_day") if isinstance(value, dict) else None
    return _reset_ts(w.get("resets_at"), now) if isinstance(w, dict) else None


def earliest_recovery(values: dict, models: tuple[str, ...], now: float) -> float | None:
    """The earliest moment any exhausted account is usable again, or None when one of them has no provable reset."""
    earliest = None
    for value in values.values():
        blocked = [w for w in relevant_windows(value, models) if w.pct >= 100.0]
        if not blocked:
            continue
        stamps = [parse_ts(w.resets_at) for w in blocked]
        if any(ts is None for ts in stamps) or max(stamps) <= now:
            return None
        earliest = max(stamps) if earliest is None else min(earliest, max(stamps))
    return earliest


def due_candidate(candidates: list[str], entries: dict[str, Entry], now: float) -> str | None:
    """The one candidate polled this tick: past its plan and not in backoff, least recently tried first (never tried first of all)."""
    due = []
    for num in candidates:
        e = entries[num]
        if e.relogin or e.in_backoff(now) or (e.next_poll_at is not None and now < e.next_poll_at):
            continue
        due.append((e.last_attempt_at or e.fetched_at or 0.0, num))
    return min(due)[1] if due else None


# ---------- settings ----------
@dataclass(frozen=True)
class AutoSettings:
    threshold: float = 90.0
    weekly_threshold: float | None = None
    interval_seconds: float = 60.0
    cooldown_seconds: float = 300.0
    hysteresis_pct: float = 10.0
    strategy: str = "best"
    include_api_key_accounts: bool = False
    unhealthy_ticks: int = 3
    model: str | None = None

    @classmethod
    def load(cls, overrides: dict | None = None) -> "AutoSettings":
        """settings.json with the CLI flags laid over it ({dotted key: value or None}), clamped the same lenient way."""
        values = settings.load()
        for dotted, value in (overrides or {}).items():
            if value is not None:
                values[dotted] = settings.coerce(dotted, value)
        return cls(**{name: values[f"autoswitch.{key}"] for name, key in (
            ("threshold", "threshold"), ("weekly_threshold", "weeklyThreshold"), ("interval_seconds", "intervalSeconds"),
            ("cooldown_seconds", "cooldownSeconds"), ("hysteresis_pct", "hysteresisPct"), ("strategy", "strategy"),
            ("include_api_key_accounts", "includeApiKeyAccounts"), ("unhealthy_ticks", "unhealthyTicks"), ("model", "model"))})


# ---------- events ----------
def event(kind: str, **fields) -> dict:
    """Payloads are additive, as in cswap: consumers ignore unknown event kinds and fields."""
    return {"schemaVersion": SCHEMA_VERSION, "event": kind, "ts": now_iso(), **fields}


def _ref(data: dict, num: str | None) -> dict | None:
    return switch.ref(data, num) if num is not None else None


def human(ev: dict) -> str:
    kind = ev["event"]
    if kind == "poll":
        return _human_poll(ev)
    if kind == "switch":
        src = f"Account-{ev['from']['number']}" if ev.get("from") else "(none)"
        dst = f"Account-{ev['to']['number']} ({ev['to']['email']})" if ev.get("to") else "?"
        return f"{'[dry-run] would switch' if ev['dryRun'] else 'Switched'} {src} -> {dst} ({ev['trigger']})"
    if kind == "no-switch":
        return f"no switch: {ev['reason']}" + (f" ({ev['detail']})" if ev.get("detail") else "")
    if kind == "account-quarantined":
        return (f"Account-{ev['number']} ({ev['email']}) quarantined: {ev['reason']}. "
                f"Log it in again with 'agyswap login --slot {ev['number']}' to recover.")
    if kind == "account-unquarantined":
        return f"Account-{ev['number']} ({ev['email']}) back in rotation ({ev['reason']})"
    if kind == "all-exhausted":
        at = ev.get("earliestResetAt")
        return f"all accounts exhausted; earliest reset {at}" if at else "all accounts exhausted; no reset time known"
    if kind == "sleep":
        return f"sleeping {ev['seconds'] / 60:.0f}m (until {ev['until']})"
    if kind == "error":
        return f"error: {ev['message']}" + (" (will retry)" if ev.get("transient") else "")
    if kind == "config-warning":
        return f"warning: {ev['message']}"
    return kind


def _human_poll(ev: dict) -> str:
    if ev.get("active") is None:
        return "poll: no active account"
    num = str(ev["active"]["number"])
    wins, errors = ev.get("windowsPct", {}), ev.get("fetchErrors", {})

    def describe(n: str) -> str:
        if wins.get(n):
            return " · ".join(f"{label} {pct:.0f}%" for label, pct in wins[n].items())
        h = ev["headroomPct"].get(n)
        if h is not None:
            return f"{100 - h:.0f}%"
        return f"? ({errors[n]})" if errors.get(n) else "?"

    line = ev["threshold"]
    if wins.get(num):
        b = binding([Window(label, pct, None, not label.endswith("5h")) for label, pct in wins[num].items()],
                    ev["threshold"], ev.get("weeklyThreshold"), ev["threshold"])
        used, line = f"{b.label} {b.pct:.0f}% used", b.line
    elif ev["headroomPct"].get(num) is not None:
        used = f"{100 - ev['headroomPct'][num]:.0f}% used"
    else:
        used = f"usage unknown ({errors[num]})" if errors.get(num) else "usage unknown"
    others = ", ".join(f"#{n}: {describe(n)}" for n in ev["headroomPct"] if n != num)
    return f"Account-{num} ({ev['active']['email']}): {used} (switch at {pct_label(line)}%)" + (f" | others: {others}" if others else "")


# ---------- engine ----------
class Engine:
    """One engine per auto run. on_event receives every event dict; clock is wall time, because persisted timestamps must survive processes."""

    def __init__(self, cfg: AutoSettings, on_event, *, dry_run: bool = False, store=None, fetcher=None,
                 clock=time.time, switcher=None):
        self.cfg = cfg
        self.on_event = on_event
        self.dry_run = dry_run
        self.store = store or WinCred()
        self.fetcher = fetcher or usage.fetch
        self.clock = clock
        self.switcher = switcher or switch.switch_to
        self.models = parse_models(cfg.model)
        self._unhealthy = 0
        self._health_account: str | None = None
        self._health_op: tuple[str, int] | None = None  # ("set" | "add", n): this tick's change to the saved count
        self._tick_started: float | None = None
        self._model_check_done = not self.models
        self._config_checked = False
        self._sleep_until: float | None = None
        self._blocked_long = False
        self._stop = threading.Event()

    # -- state file --
    def _state_path(self):
        return paths.autoswitch_state_file()

    def _state_lock(self):
        return file_lock(self._state_path().with_name(".autoswitch_state.lock"))

    def _read_state(self) -> dict:
        state = read_json(self._state_path(), {})
        return state if isinstance(state, dict) else {}

    def _mutate_state(self, mutator) -> dict:
        """Read-modify-write under the state lock; never called in dry-run, which writes no state."""
        with self._state_lock():
            state = self._read_state()
            state["schemaVersion"] = STATE_SCHEMA_VERSION
            mutator(state)
            atomic_write_json(self._state_path(), state)
            return state

    def _emit(self, kind: str, **fields) -> None:
        try:
            self.on_event(event(kind, **fields))
        except Exception:  # a closed pipe or an unencodable console loses the line, never the unattended loop
            pass

    # -- per-account helpers --
    def _threshold(self, data: dict, num: str) -> float:
        own = accounts.account_threshold(data["accounts"].get(num, {}))
        return own if own is not None else self.cfg.threshold

    def _binding(self, data: dict, value, num: str, models: tuple[str, ...]) -> Binding | None:
        return binding(relevant_windows(value, models), self._threshold(data, num), self.cfg.weekly_threshold, self.cfg.threshold)

    def _recovery(self, data: dict, value, num: str, now: float, models: tuple[str, ...], lines: bool = True) -> float:
        """When the window holding this account back resets, as a sort key; inf when unknown or past."""
        windows = relevant_windows(value, models)
        if not windows:
            return math.inf
        if lines and self.cfg.weekly_threshold is not None:
            resets_at = self._binding(data, value, num, models).resets_at
        else:
            resets_at = max(windows, key=lambda w: w.pct).resets_at
        ts = _reset_ts(resets_at, now)
        return ts if ts is not None else math.inf

    # -- health counters (cswap #464) --
    def _saved_ticks(self, state: dict, current: str) -> int:
        ticks, at = state.get("unhealthyTicks"), state.get("healthTickAt")
        fresh = state.get("healthAccount") == current and _isnum(at) and 0 <= self.clock() - at <= HEALTH_STALE_S
        valid = isinstance(ticks, int) and not isinstance(ticks, bool) and ticks > 0
        return min(ticks, self.cfg.unhealthy_ticks) if fresh and valid else 0

    def _load_health(self, state: dict, current: str) -> None:
        self._unhealthy = self._saved_ticks(state, current)

    def _note_health(self, add: int | None) -> None:
        """This tick's observation of the active account: None when its usage was read (the count starts over), else the unhealthy ticks to
        add (0 while a backoff or another process's query excuses it). _persist_health merges it into the file."""
        if add is None:
            self._unhealthy, self._health_op = 0, ("set", 0)
        else:
            self._unhealthy = min(self._unhealthy + add, self.cfg.unhealthy_ticks)
            mode, n = self._health_op or ("add", 0)
            self._health_op = (mode, n + add)

    def _persist_health(self) -> None:
        """Merge this tick's observation into the saved counters under the state lock, on top of what another process saved meanwhile.
        A tick that evaluated no managed active account, or that switched (_perform clears the counters in its own write), changes nothing."""
        op, self._health_op = self._health_op, None
        account, started = self._health_account, self._tick_started
        if self.dry_run or op is None or account is None:
            return
        mode, n = op
        if not n and "healthAccount" not in self._read_state():
            return

        def store(state: dict) -> None:
            moved = state.get("lastSwitchAt")
            if _isnum(moved) and started is not None and moved >= started and str(state.get("lastSwitchTo")) != account:
                return  # another process switched away during this tick: the observation is about an account no longer active
            ticks = min((self._saved_ticks(state, account) if mode == "add" else 0) + n, self.cfg.unhealthy_ticks)
            if ticks:
                state.update(unhealthyTicks=ticks, healthAccount=account, healthTickAt=self.clock())
            else:
                for key in HEALTH_KEYS:
                    state.pop(key, None)

        self._mutate_state(store)

    def _counts(self, entry: Entry, now: float) -> bool:
        """Whether an unknown active account counts towards failover this tick: it was queried, or no backoff or other process's query excuses it."""
        return self._attempted(entry, now) or not (entry.in_backoff(now) or entry.claimed(self.clock()))

    # -- quarantine --
    def _folder_sha(self, data: dict, num: str) -> str | None:
        record = data["accounts"].get(num)
        try:
            blob = read_token_file(paths.token_path(record.get("folder") or num)) if record else None
        except AgyswapError:
            return None
        return sha256_hex(blob) if blob else None

    def _quarantine(self, data: dict, num: str) -> None:
        email, sha = data["accounts"][num].get("email", ""), self._folder_sha(data, num)
        self._mutate_state(lambda s: s.setdefault("quarantine", {}).__setitem__(
            num, {"email": email, "reason": "relogin_required", "at": now_iso(), "loginSha": sha}))
        self._emit("account-quarantined", number=num, email=email, reason="relogin_required")

    def _release_quarantines(self, state: dict, data: dict) -> dict:
        """An account whose login file changed (logged in again), or that was removed or replaced, is back in rotation."""
        held = state.get("quarantine")
        if not isinstance(held, dict) or not held:
            return state
        released = []
        for num, entry in held.items():
            entry = entry if isinstance(entry, dict) else {}
            record = data["accounts"].get(num)
            if record is None or (record.get("email") or "") != entry.get("email"):
                released.append((num, entry.get("email", ""), "account-replaced"))
            elif self._folder_sha(data, num) != entry.get("loginSha"):
                released.append((num, record.get("email", ""), "credentials-replaced"))
        if not released:
            return state
        state = self._mutate_state(lambda s: [s.get("quarantine", {}).pop(n, None) for n, _, _ in released])
        for num, email, reason in released:
            self._emit("account-unquarantined", number=num, email=email, reason=reason)
        return state

    # -- tick --
    def tick(self) -> Outcome:
        """Evaluate once: poll usage, maybe switch. Never raises an AgyswapError or an ordinary exception."""
        try:
            return self._tick()
        except AgyswapError as e:
            self._emit("error", message=str(e), transient=True)
            return Outcome.ERROR
        except Exception as e:  # safety net: an unattended loop must survive one bad tick
            self._emit("error", message=f"{type(e).__name__}: {e}", transient=True)
            return Outcome.ERROR
        finally:
            try:
                self._persist_health()
            except Exception as e:  # reported, never in place of the tick's own outcome
                self._emit("error", message=f"Cannot save the failover counters: {e}", transient=True)

    def _check_config(self) -> None:
        if self._config_checked:
            return
        self._config_checked = True
        for key in settings.unknown_keys():
            self._emit("config-warning", message=f"settings.json: unknown key {key}; it is ignored")

    def _tick(self) -> Outcome:
        cfg, now = self.cfg, self.clock()
        self._sleep_until, self._blocked_long = None, False
        self._health_op, self._tick_started = None, now
        self._check_config()
        state = self._read_state()
        ctx = load_context(read_live=lambda: read_live_login(self.store))
        if ctx.live.error:
            raise CredentialError(f"Cannot read Credential Manager ({ctx.live.error})")
        if not self.dry_run:
            note = sync_active_login(ctx)  # the active account's folder follows its refreshed login
            if note:
                warning(f"agyswap: {note}", file=sys.stderr)
        data = ctx.data
        current = ctx.live_owner
        if current is None:
            self._emit("poll", active=None, headroomPct={}, threshold=cfg.threshold,
                       **({"weeklyThreshold": cfg.weekly_threshold} if cfg.weekly_threshold is not None else {}))
            if ctx.live.present:
                self._emit("no-switch", reason="unmanaged-active-account", detail="run 'agyswap add' to include it in rotation")
            else:
                self._emit("no-switch", reason="no-active-account", detail="log in with 'agyswap login' and switch to an account first")
            return Outcome.NO_ACTION
        if ctx.foreign:
            self._emit("no-switch", reason="foreign-credential", detail=ctx.recorded_mismatch() or "")
            return Outcome.NO_ACTION
        if not self.dry_run or current != self._health_account:  # another process may have counted meanwhile; dry-run never saves, so it keeps its own
            self._load_health(state, current)
            self._health_account = current
        if not self.dry_run:
            state = self._release_quarantines(state, data)
        quarantined = set(state.get("quarantine") or {}) if isinstance(state.get("quarantine"), dict) else set()

        cache = UsageCache(clock=self.clock)
        nums = accounts.numbers(data)
        switchable = [n for n in nums if not data["accounts"][n].get("disabled") and data["accounts"][n].get("kind") != "apikey"
                      and switch.skip_reason(data, n, cache) is None]
        candidates = [n for n in switchable if n != current and n not in quarantined]
        entries, values = self._collect(ctx, cache, current, candidates, data, now)
        candidates = self._drop_relogins(data, candidates, entries, quarantined)
        hr = {n: headroom(values.get(n), self.models) for n in [current, *candidates]}
        self._emit_poll(data, current, candidates, entries, values, hr)
        self._check_models(candidates, values, current)

        priority_mode = cfg.strategy == "priority-consume-first"
        decided = self._classify(data, state, current, values, hr, entries, now)
        if isinstance(decided, Outcome):
            return decided
        trigger, detail = decided

        if trigger in PROACTIVE and self._in_cooldown(state):
            self._emit("no-switch", reason="cooldown")
            return Outcome.NO_ACTION
        if trigger in ("consume-first", "priority") and not candidates:
            self._emit("no-switch", reason="below-threshold", detail=detail)
            return Outcome.NO_ACTION
        if not candidates:
            self._blocked_long = True
            self._emit("no-switch", reason="no-candidates")
            return Outcome.BLOCKED

        ordered, any_known, active_reset = self._rank(data, trigger, values, hr, current, candidates, state, now, self.models)
        if trigger in ("consume-first", "priority") and ordered:
            # Two-phase commit: a below-line move was decided on cached data, so refresh and decide again on the fresh figures
            # (unlike cswap, the trigger is classified again: a refreshed active account over its line escapes now, not next tick)
            entries, values = self._collect(ctx, cache, current, candidates, data, now,
                                            extra={current, *candidates})
            candidates = self._drop_relogins(data, candidates, entries, quarantined)
            hr = {n: headroom(values.get(n), self.models) for n in [current, *candidates]}
            decided = self._classify(data, state, current, values, hr, entries, now)
            if isinstance(decided, Outcome):
                return decided
            trigger, detail = decided
            ordered, any_known, active_reset = self._rank(data, trigger, values, hr, current, candidates, state, now, self.models)
        active_ready = priority_mode and not data["accounts"][current].get("disabled") and self._ready(data, values, hr, current, self.models)
        if ordered and (trigger == "consume-first" or (trigger == "priority" and active_ready)) \
                and not entries_fresh(entries[current], self.clock()):
            self._emit("no-switch", reason="stale-usage",
                       detail=f"account {current} usage could not be refreshed this tick (backoff or a concurrent query); retrying")
            return Outcome.NO_ACTION

        if not ordered and self.models and trigger in ("at-limit", "failover", "proactive"):
            base = headroom(values.get(current))
            if base is not None and base <= 0:  # cswap #403: the active account cannot run even Gemini, so pick on Gemini alone
                ordered, _, _ = self._rank(data, "at-limit", values, {n: headroom(values.get(n)) for n in hr},
                                           current, candidates, state, now, ())
                trigger = "at-limit" if ordered else trigger

        if not ordered:
            return self._blocked(data, trigger, values, hr, candidates, any_known, active_reset, active_ready, priority_mode)

        left = (hr.get(current), self._recovery(data, values.get(current), current, now, self.models))
        skipped = False
        for num in ordered:
            if trigger == "consume-first" or (priority_mode and num in candidates):
                if not entries[num].fresh(self.clock()) or entries[num].consecutive_failures:
                    self._emit("no-switch", reason="stale-usage",
                               detail=f"account {num} usage could not be refreshed this tick (backoff or a concurrent query); retrying")
                    if priority_mode and not active_ready:
                        continue
                    return Outcome.NO_ACTION
            outcome = self._perform(data, current, num, trigger, left)
            if outcome is None:
                skipped = True
                continue
            return outcome
        self._emit("no-switch", reason="no-viable-target", detail="every target has a live agyswap run" if skipped else "")
        return Outcome.BLOCKED

    def _drop_relogins(self, data, candidates, entries, quarantined: set) -> list[str]:
        """Quarantine every candidate whose query asked for a login (not in dry-run) and leave it out."""
        for n in candidates:
            if entries[n].relogin and n not in quarantined and not self.dry_run:
                self._quarantine(data, n)
                quarantined.add(n)
        return [n for n in candidates if n not in quarantined]

    def _classify(self, data, state, current, values, hr, entries, now):
        """(trigger, below-line detail) for this reading of the active account, or the Outcome when the tick ends here. Updates the health count."""
        cfg = self.cfg
        ah = hr[current]
        if ah is None:
            self._note_health(1 if self._counts(entries[current], now) else 0)
            if self._unhealthy < cfg.unhealthy_ticks:
                self._emit("no-switch", reason="active-usage-unknown",
                           detail=f"{self._unhealthy}/{cfg.unhealthy_ticks} before failover")
                return Outcome.NO_ACTION
            return "failover", ""
        self._note_health(None)
        ab = self._binding(data, values.get(current), current, self.models)
        if ab.over:
            return ("at-limit" if ah <= 0 else "proactive"), ""
        detail = f"{ab.label} {pct_label(ab.pct)}% < {pct_label(ab.line)}%"
        if cfg.strategy not in ("consume-first", "priority-consume-first"):
            self._emit("no-switch", reason="below-threshold", detail=detail)
            return Outcome.NO_ACTION
        landed = state.get("lastSwitchTo")
        if landed is not None and str(landed) != current:  # cswap #598: a manual choice stands until it reaches a line
            self._emit("no-switch", reason="manual-switch",
                       detail=f"Account-{current} was chosen by hand; staying until it reaches a switch line")
            return Outcome.NO_ACTION
        return ("priority" if cfg.strategy == "priority-consume-first" else "consume-first"), detail

    def _blocked(self, data, trigger, values, hr, candidates, any_known, active_reset, active_ready, priority_mode) -> Outcome:
        if active_ready:
            self._emit("no-switch", reason="already-consuming-priority",
                       detail="no usable account has higher priority or an earlier weekly reset in this tier")
            return Outcome.NO_ACTION
        if priority_mode:
            self._emit("no-switch", reason="no-qualifying-candidate",
                       detail="no candidate is below every switch line with readable usage; waiting for recovery")
            return Outcome.BLOCKED
        if not any_known:
            self._emit("no-switch", reason="no-comparison", detail="no candidate has readable usage")
            return Outcome.BLOCKED
        if trigger == "consume-first":
            if active_reset is None:
                self._emit("no-switch", reason="reset-unknown",
                           detail="active account's weekly reset time is unknown; consume-first is idle until it is reported")
            else:
                self._emit("no-switch", reason="already-consuming-soonest", detail="no sooner-resetting account with room to spare")
            return Outcome.NO_ACTION
        if not all(hr.get(n) is not None and hr[n] <= 0 for n in candidates):
            self._emit("no-switch", reason="no-qualifying-candidate",
                       detail="no candidate is below its switch lines and better than the active account by the hysteresis margin, "
                              "or usage is unreadable this tick")
            return Outcome.BLOCKED
        self._blocked_long = True
        earliest = earliest_recovery(values, self.models, self.clock())
        if earliest is not None:
            self._sleep_until = earliest + RESET_SLACK_S
        self._emit("all-exhausted", earliestResetAt=(
            datetime.fromtimestamp(earliest, tz=timezone.utc).isoformat().replace("+00:00", "Z") if earliest else None))
        return Outcome.BLOCKED

    # -- collection --
    @staticmethod
    def _attempted(entry: Entry, since: float) -> bool:
        return entry.last_attempt_at is not None and entry.last_attempt_at >= since

    def _value(self, entry: Entry):
        """cswap #208: when the latest attempt failed, the account is unknown now, whatever the cache still holds."""
        if entry.sentinel is None and entry.consecutive_failures:
            return None
        return entry.decision_value()

    def _collect(self, ctx, cache: UsageCache, current: str, candidates: list[str], data: dict, now: float,
                 extra: set[str] | None = None):
        """The active account when its plan is due plus one stale candidate; every candidate when a switch could be near."""
        order = [current, *candidates]
        folder = {n: data["accounts"][n].get("folder") or n for n in order}
        if extra is None:
            pre = {n: cache.entry(folder[n]) for n in order}
            a = pre[current]
            plan = set()
            if a.fetched_at is None or a.next_poll_at is None or now >= a.next_poll_at:
                plan.add(current)
            pick = due_candidate(candidates, pre, now)
            if pick is not None:
                plan.add(pick)
            entries = collect(ctx, order, fetcher=self.fetcher, cache=cache, fetch=plan)
            value = self._value(entries[current])
            windows = relevant_windows(value, self.models)
            near = any(w.pct >= (self.cfg.weekly_threshold if self.cfg.weekly_threshold is not None and w.weekly
                                 else self._threshold(data, current)) - ESCALATION_MARGIN_PCT for w in windows)
            cap = self.cfg.unhealthy_ticks  # escalate only when this tick can actually reach failover
            failover_due = not windows and (self._unhealthy >= cap or (self._unhealthy + 1 >= cap and self._counts(entries[current], now)))
            if not (candidates and (near or failover_due)):
                return entries, {n: self._value(e) for n, e in entries.items()}
            extra = set(order)
        stale = {n for n in extra if not entries_fresh(cache.entry(folder[n]), self.clock())}
        entries = collect(ctx, order, fetcher=self.fetcher, cache=cache, fetch=stale)
        return entries, {n: self._value(e) for n, e in entries.items()}

    def _emit_poll(self, data, current, candidates, entries, values, hr) -> None:
        fields = {"active": _ref(data, current), "headroomPct": hr, "threshold": self._threshold(data, current)}
        errors = {n: entries[n].last_error for n in hr if values.get(n) is None and entries[n].last_error}
        if errors:
            fields["fetchErrors"] = errors
        wins = {n: {w.label: w.pct for w in relevant_windows(values.get(n), self.models)} for n in hr}
        wins = {n: w for n, w in wins.items() if w}
        if wins:
            fields["windowsPct"] = wins
        if self.cfg.weekly_threshold is not None:
            fields["weeklyThreshold"] = self.cfg.weekly_threshold
        self._emit("poll", **fields)

    def _check_models(self, candidates, values, current) -> None:
        """One-shot autoswitch.model typo guard, once every account's usage is readable (cswap #259, for --model)."""
        if self._model_check_done:
            return
        relevant = {n: values.get(n) for n in [current, *candidates]}
        if not all(isinstance(v, dict) for v in relevant.values()):
            return
        self._model_check_done = True
        missing = switch.inert_models(relevant, self.models)
        if missing:
            self._emit("config-warning", message=f"autoswitch.model: {', '.join(missing)} matches no account's usage category; "
                                                 "only the Gemini limits are being watched for it (typo?)")

    def _in_cooldown(self, state: dict) -> bool:
        last = state.get("lastSwitchAt")
        # Bounded both ways: a small clock step back still counts as just switched, a far-future stamp cannot freeze proactive moves
        return _isnum(last) and abs(self.clock() - last) < self.cfg.cooldown_seconds

    # -- ranking --
    def _ready(self, data, values, hr, num, models) -> bool:
        b = self._binding(data, values.get(num), num, models)
        h = hr.get(num)
        return h is not None and h > 0 and b is not None and not b.over

    def _rank(self, data, trigger, values, hr, current, candidates, state, now, models):
        if self.cfg.strategy == "priority-consume-first":
            return self._rank_priority(data, values, hr, current, candidates, now, models)
        recovered = self._left_recovered(data, state, values, hr, current, now, models)
        barred = self._no_return(data, trigger, state, values, hr, current, recovered, models)
        ranked = self._rank_candidates(data, trigger, values, hr, current, candidates, barred, now, models)
        if barred is not None and not ranked[0] and recovered:
            unbarred = self._rank_candidates(data, trigger, values, hr, current, candidates, None, now, models)
            if unbarred[0]:
                return unbarred
        return ranked

    def _rank_priority(self, data, values, hr, current, candidates, now, models):
        """Highest tier first, then the soonest weekly reset; only accounts below every line. An eligible active account wins equal keys."""
        def key(num):
            try:
                tier = accounts.normalize_priority(data["accounts"][num].get("priority", 0))
            except ValueError:
                tier = 0  # a hand-damaged value counts as the default tier
            reset = weekly_reset(values.get(num), now)
            return -tier, reset if reset is not None else math.inf

        active_key = key(current) if not data["accounts"][current].get("disabled") and self._ready(data, values, hr, current, models) else None
        ordered = [n for n in candidates if self._ready(data, values, hr, n, models) and (active_key is None or key(n) < active_key)]
        ordered.sort(key=key)
        return ordered, any(hr.get(n) is not None for n in candidates), weekly_reset(values.get(current), now)

    def _rank_candidates(self, data, trigger, values, hr, current, candidates, barred, now, models):
        cfg = self.cfg
        consume, weekly_first = cfg.strategy == "consume-first", cfg.strategy == "weekly-first"
        active_reset = weekly_reset(values.get(current), now) if consume else None
        bindings = {n: self._binding(data, values.get(n), n, models) for n in (*candidates, current)}
        over = {n: (b.over if b is not None else None) for n, b in bindings.items()}
        ab, ah = bindings[current], hr.get(current)
        active_lh = ab.line_headroom if ab is not None else ah
        measured = [over[n] for n in candidates if over[n] is not None]
        candidates_over = bool(measured) and all(measured)
        escape = trigger in ("at-limit", "failover")
        all_above = candidates_over if escape else bool(over[current]) and candidates_over
        any_under = any(over[n] is False and (hr.get(n) or 0) > 0 for n in candidates)
        nums = accounts.numbers(data)
        qualifying, any_known = [], False
        for num in candidates:
            h = hr.get(num)
            if h is None:
                continue
            any_known = True
            if h <= 0 or num == barred:
                continue
            b = bindings[num]
            reset = weekly_reset(values.get(num), now) if consume or weekly_first else None
            if escape:
                if b.over and any_under:  # cswap #342, #305: land below the lines when anything is
                    continue
                key = (-h, self._recovery(data, values.get(num), num, now, models, lines=False)) if all_above else (-b.line_headroom,)
            else:
                if b.over and not all_above:
                    continue
                if all_above:
                    if not margin_met(h, ah or 0.0):
                        continue
                    key = (-h, self._recovery(data, values.get(num), num, now, models, lines=False))
                elif consume:
                    if trigger == "consume-first" and (reset is None or active_reset is None or reset >= active_reset):
                        continue
                    key = (reset if reset is not None else math.inf, -b.line_headroom)
                else:
                    if active_lh is not None and b.line_headroom - active_lh < cfg.hysteresis_pct:
                        continue  # cswap #115: relative to the active account, in line headroom
                    if weekly_first:
                        key = (reset if reset is not None else math.inf, -b.line_headroom)
                    elif cfg.strategy == "next-available":
                        key = ((nums.index(num) - nums.index(current)) % len(nums),)
                    else:
                        key = (-b.line_headroom,)
            qualifying.append((key, num))
        qualifying.sort(key=lambda t: t[0])
        return [n for _, n in qualifying], any_known, active_reset

    def _no_return(self, data, trigger, state, values, hr, current, recovered, models) -> str | None:
        """The account this engine last left, while it is still barred: never undo the previous proactive move (cswap _no_return_account)."""
        came = state.get("lastSwitchFrom")
        if trigger not in ("proactive", "consume-first") or came is None:
            return None
        landed = state.get("lastSwitchTo")
        if landed is not None and str(landed) != current:
            return None  # a manual switch already undid that move
        barred = str(came)
        if not recovered:
            return barred
        h, ah = hr.get(barred), hr.get(current)
        if h is not None:
            if ah is not None:
                if margin_met(h, ah):
                    return None
            else:
                b = self._binding(data, values.get(barred), barred, models)
                if b is not None and not b.over:
                    return None
        return barred

    def _left_recovered(self, data, state, values, hr, current, now, models) -> bool:
        """Is the account we left better than when we left it? Burn on the active account alone can never answer yes (cswap _left_account_recovered)."""
        came = state.get("lastSwitchFrom")
        if came is None or "leftHeadroom" not in state:
            return True
        barred = str(came)
        h, ah = hr.get(barred), hr.get(current)
        left_h, left_rec, left_trigger = state.get("leftHeadroom"), state.get("leftRecoveryAt"), state.get("leftTrigger")
        failover = left_trigger == "failover" if left_trigger is not None else (left_h is None and left_rec is None)
        if failover:
            b = self._binding(data, values.get(barred), barred, models)
            if b is not None and not b.over:
                return True
            peer = self._recovery(data, values.get(barred), barred, now, models)
            mine = self._recovery(data, values.get(current), current, now, models)
            return (math.isfinite(mine) or peer - now <= RECOVERY_HORIZON_S) and peer < mine - RECOVERY_HYSTERESIS_S
        if h is not None:
            if ah is not None:
                if h > ah * HORIZON_HEADROOM_RATIO + SPENT_HEADROOM_PCT:
                    return True
            else:
                b = self._binding(data, values.get(barred), barred, models)
                if b is not None and not b.over:
                    return True
        if _isnum(left_h) and h is not None and h >= min(left_h + SPENT_HEADROOM_PCT, 100.0):
            return True
        was = left_rec if _isnum(left_rec) else math.inf
        return self._recovery(data, values.get(barred), barred, now, models) < was - RECOVERY_HYSTERESIS_S

    # -- switching --
    @staticmethod
    def _live_run_check(data: dict, target: str) -> None:
        folder = data["accounts"][target].get("folder") or target
        pid = run._runner_alive(paths.run_pid_file(folder))
        if pid is not None:
            raise Skip(f"Account-{target} has a live agyswap run (PID {pid})")

    def _perform(self, data: dict, current: str, target: str, trigger: str, left: tuple) -> Outcome | None:
        """Returns None when the target was called off under the lock (a live run), so the caller tries the next one."""
        if self.dry_run:
            try:
                self._live_run_check(data, target)  # read-only: predicts the target a real tick would call off
            except Skip:
                return None
            self._emit("switch", trigger=trigger, **{"from": _ref(data, current)}, to=_ref(data, target),
                       warnings=switch.process_warnings(), dryRun=True)
            return Outcome.SWITCHED
        # The state lock is held across recheck, switch and record, so a loop and a scheduled --once make one serialized decision
        with self._state_lock():
            state = self._read_state()
            if trigger in PROACTIVE and self._in_cooldown(state):
                self._emit("no-switch", reason="cooldown")
                return Outcome.NO_ACTION
            def check(ctx, num):
                if ctx.live_owner != current:
                    raise Stale(f"the active account is no longer Account-{current}")
                if ctx.data["accounts"][num].get("disabled"):
                    raise Stale(f"Account-{num} was disabled")
                self._live_run_check(ctx.data, num)

            try:
                payload = self.switcher(target, store=self.store, check=check)
            except Skip:
                return None
            except Stale as e:
                self._emit("no-switch", reason="state-changed", detail=f"{e}; deciding again next tick")
                return Outcome.NO_ACTION
            if not payload.get("switched"):
                self._emit("no-switch", reason="already-active", detail=payload.get("reason", ""))
                return Outcome.NO_ACTION
            headroom_left, recovery = left
            state.update(schemaVersion=STATE_SCHEMA_VERSION, lastSwitchAt=self.clock(), lastSwitchTo=target,
                         lastSwitchFrom=(payload.get("from") or {}).get("number"), leftHeadroom=headroom_left,
                         leftRecoveryAt=None if recovery == math.inf else recovery, leftTrigger=trigger)
            for key in HEALTH_KEYS:  # the counters belonged to the account just left
                state.pop(key, None)
            atomic_write_json(self._state_path(), state)
        self._unhealthy, self._health_op = 0, None
        self._emit("switch", trigger=trigger, **{"from": payload.get("from")}, to=payload.get("to"),
                   warnings=payload.get("warnings", []), dryRun=False)
        return Outcome.SWITCHED

    # -- loop --
    def stop(self) -> None:
        self._stop.set()

    def next_delay(self, outcome: Outcome) -> float:
        interval = self.cfg.interval_seconds
        if outcome is Outcome.BLOCKED:
            if self._sleep_until is not None:
                return min(max(self._sleep_until - self.clock(), interval), MAX_SLEEP_S)
            if self._blocked_long:
                return max(interval, NO_RESET_FALLBACK_S)
        delay = interval * (0.9 + 0.2 * random.random())
        # Never wait past the active account's own poll plan, but never poll faster than the urgent pace either
        try:
            data = accounts.load_sequence() or {}
            num = str(data.get("activeAccountNumber"))
            record = data.get("accounts", {}).get(num)
            if record:
                due = UsageCache(clock=self.clock).entry(record.get("folder") or num).next_poll_at
                if due is not None:
                    delay = min(delay, max(due - self.clock(), URGENT_INTERVAL_S))
        except Exception:  # best-effort: a damaged roster or cache keeps the plain interval (as cswap)
            pass
        return delay

    def run_loop(self) -> int:
        with contextlib.ExitStack() as stack:
            # Marks the loop as running for config set (cswap #90); a second loop simply runs without it
            with contextlib.suppress(AgyswapError, OSError):
                stack.enter_context(file_lock(paths.auto_lock_file(), retries=0))
            return self._loop()

    def _loop(self) -> int:
        while not self._stop.is_set():
            outcome = self.tick()
            delay = self.next_delay(outcome)
            if delay > self.cfg.interval_seconds * 1.5:
                until = datetime.now(timezone.utc) + timedelta(seconds=delay)
                self._emit("sleep", seconds=round(delay, 1), until=until.isoformat(timespec="seconds").replace("+00:00", "Z"))
            self._stop.wait(delay)
        return 0


def loop_running() -> bool:
    """Whether an agyswap auto loop holds its lock right now (cswap #90)."""
    try:
        with file_lock(paths.auto_lock_file(), retries=0):
            return False
    except AgyswapError:
        return True


def entries_fresh(entry: Entry, now: float) -> bool:
    """Fetched successfully within the serve period: an escalation does not start agy again for it."""
    return entry.fresh(now, SERVE_TTL_S) and not entry.consecutive_failures
