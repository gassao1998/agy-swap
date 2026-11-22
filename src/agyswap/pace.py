"""Burn rate of the weekly window. The algorithm and the three constants follow cswap 1.0.0's pace.py (MIT License, claude-swap):
a seven-day period, nothing reported within 24 hours of a reset, and "ahead" only when usage is 15 percentage points over the expected figure. Only fetched_at is used as the reference time, never the current time.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

WEEKLY_PERIOD_S = 7 * 86400.0
SUPPRESS_AFTER_RESET_S = 24 * 3600.0
AHEAD_THRESHOLD_PCT = 15.0
FIVE_HOUR_PERIOD_S = 5 * 3600.0
UNSTARTED_SLACK_S = 120.0


def unstarted(window: dict, fetched_at: float | None) -> bool:
    """A window that has not been used yet: 0% and a reset time of about the fetch time plus one period (agy pushes it back on every query)."""
    ts = parse_ts(window.get("resets_at"))
    if fetched_at is None or ts is None or window.get("pct") != 0:
        return False
    period = FIVE_HOUR_PERIOD_S if window.get("window") == "5h" else WEEKLY_PERIOD_S
    return abs(ts - fetched_at - period) <= UNSTARTED_SLACK_S


@dataclass(frozen=True)
class Pace:
    expected_pct: float
    actual_pct: float
    elapsed_s: float
    period_s: float
    ahead: bool


def parse_ts(value: object) -> float | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except (ValueError, OverflowError, OSError):  # a time without a zone goes through localtime, which raises OSError on Windows for extreme years
        return None


def compute(window: dict | None, fetched_at: float | None, period_s: float = WEEKLY_PERIOD_S) -> Pace | None:
    if not isinstance(window, dict) or fetched_at is None:
        return None
    pct = window.get("pct")
    next_reset = parse_ts(window.get("resets_at"))
    if not isinstance(pct, (int, float)) or isinstance(pct, bool) or next_reset is None:
        return None
    delta = next_reset - fetched_at
    if delta >= 0:
        # Unlike cswap: for an unused window agy answers "now + 7 days", a second or two over one period, so a modulo would misread it as almost a full week elapsed
        elapsed = period_s - min(delta, period_s)
    else:
        # The cache is older than the reset time: the modulo lands back in the current period
        remaining = delta % period_s
        elapsed = 0.0 if remaining == 0 else period_s - remaining
    if elapsed < SUPPRESS_AFTER_RESET_S:
        return None
    expected = min(100.0, elapsed / period_s * 100.0)
    return Pace(expected, float(pct), elapsed, period_s, float(pct) - expected >= AHEAD_THRESHOLD_PCT)


def projected_exhaustion(p: Pace, fetched_at: float) -> float | None:
    if p.elapsed_s <= 0 or p.actual_pct <= 0:
        return None
    if p.actual_pct >= 100:
        return fetched_at
    return fetched_at + (100.0 - p.actual_pct) / (p.actual_pct / p.elapsed_s)


def will_last(p: Pace) -> bool | None:
    if p.actual_pct <= 0:
        return True
    if p.elapsed_s <= 0:
        return None
    return p.actual_pct + p.actual_pct / p.elapsed_s * (p.period_s - p.elapsed_s) <= 100.0
