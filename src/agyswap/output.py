"""The --json projection and the text layout. Key names, the conventions for optional keys and the empty values follow cswap 1.0.0's json_output.py (MIT
License, claude-swap): schemaVersion is always 1, keys are camelCase, and an optional key with no value is left out entirely.
"""
from __future__ import annotations

import time
from datetime import datetime, timezone

from agyswap import accounts, pace
from agyswap.printer import dimmed, muted
from agyswap.usage_store import SERVE_TTL_S, Entry

SCHEMA_VERSION = 1


# ---------- time ----------
def iso(epoch_s: float) -> str:
    return datetime.fromtimestamp(epoch_s, tz=timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def reset_strings(resets_at: str | None, now: float | None = None) -> tuple[str, str] | None:
    """(countdown, local clock), computed at every output so a long-lived cache does not leave the countdown stuck at the moment of the query."""
    ts = pace.parse_ts(resets_at)
    if ts is None:
        return None
    now = time.time() if now is None else now
    left = max(0, int(ts - now))
    days, rest = divmod(left, 86400)
    hours, rest = divmod(rest, 3600)
    countdown = f"{days}d {hours}h" if days else (f"{hours}h {rest // 60}m" if hours else f"{rest // 60}m")
    try:
        # Convert from UTC: Windows localtime raises OSError for negative or extreme years
        reset_local = datetime.fromtimestamp(ts, tz=timezone.utc).astimezone()
        now_local = datetime.fromtimestamp(now, tz=timezone.utc).astimezone()
    except (OSError, OverflowError, ValueError):
        return None
    clock = reset_local.strftime("%H:%M") if reset_local.date() == now_local.date() else (
        f"{reset_local.month}/{reset_local.day} {reset_local:%H:%M}")
    return countdown, clock


def format_age(epoch_s: float, now: float | None = None) -> str:
    elapsed = int((time.time() if now is None else now) - epoch_s)
    if elapsed < 60:
        return "just now"
    if elapsed < 3600:
        return f"{elapsed // 60}m ago"
    if elapsed < 86400:
        return f"{elapsed // 3600}h ago"
    return f"{elapsed // 86400}d ago"


# ---------- JSON ----------
def window_json(w: dict, fetched_at: float | None = None, weekly: bool = False, now: float | None = None) -> dict:
    out: dict = {"pct": w["pct"]}
    if w.get("resets_at"):
        out["resetsAt"] = w["resets_at"]
        cell = reset_strings(w["resets_at"], now)
        if cell:
            out["countdown"], out["clock"] = cell
    if weekly and fetched_at is not None:
        p = pace.compute(w, fetched_at)
        if p is not None:
            out["expectedPct"] = round(p.expected_pct, 1)
            out["aheadOfPace"] = p.ahead
            eta = pace.projected_exhaustion(p, fetched_at)
            if eta is not None:
                out["projectedExhaustionAt"] = iso(eta)
            lasts = pace.will_last(p)
            if lasts is not None:
                out["willLastToReset"] = lasts
    return out


def usage_to_json(usage: dict, fetched_at: float | None = None, now: float | None = None) -> dict:
    out: dict = {}
    if "five_hour" in usage:
        out["fiveHour"] = window_json(usage["five_hour"], now=now)
    if "seven_day" in usage:
        out["sevenDay"] = window_json(usage["seven_day"], fetched_at, weekly=True, now=now)
    if "scoped" in usage:
        out["scoped"] = []
        for w in usage["scoped"]:
            item = window_json(w, fetched_at, weekly=w.get("window") == "7d", now=now)
            item["name"] = w["name"]
            item["window"] = w.get("window", "7d")
            out["scoped"].append(item)
    return out


def usage_fields(value: dict | str | None, fetched_at: float | None,
                 now: float | None = None) -> tuple[str, dict | None]:
    if isinstance(value, dict):
        return "ok", usage_to_json(value, fetched_at, now)
    if isinstance(value, str):
        return value, None
    return "unavailable", None


def freshness_fields(entry: Entry, usage_present: bool, now: float) -> dict:
    out: dict = {}
    if entry.fetched_at is None:
        return out
    age = round(max(0.0, now - entry.fetched_at), 1)
    if usage_present:
        out["usageFetchedAt"] = iso(entry.fetched_at)
        out["usageAgeSeconds"] = age
        return out
    if entry.last_good is not None:
        out["lastGoodUsage"] = usage_to_json(entry.last_good, entry.fetched_at, now)
        out["lastGoodFetchedAt"] = iso(entry.fetched_at)
        out["lastGoodAgeSeconds"] = age
    return out


def failure_fields(status: str, entry: Entry, now: float) -> dict:
    if status != "unavailable" or not entry.last_error:
        return {}
    out = {"usageError": entry.last_error}
    if entry.backoff_until is not None and now < entry.backoff_until:
        out["usageRetryAt"] = iso(entry.backoff_until)
    return out


def usage_block(entry: Entry, now: float) -> dict:
    """usageStatus, usage and the freshness fields that go with them, shared by list and status."""
    status, usage = usage_fields(entry.decision_value(), entry.fetched_at, now)
    out = {"usageStatus": status, "usage": usage}
    out.update(freshness_fields(entry, usage is not None, now))
    if usage is None:
        out.update(failure_fields(status, entry, now))
    if entry.via_folder:
        out["globalLoginUnchecked"] = True  # agyswap addition: usage came from the folder copy, Credential Manager was unreadable
    return out


def account_row(number: int, record: dict, active: bool, entry: Entry, now: float) -> dict:
    block = usage_block(entry, now)
    row = {
        "number": number,
        "email": record.get("email", ""),
        "organizationName": "",
        "organizationUuid": "",
        "isOrganization": False,
        "active": active,
        "usageStatus": block.pop("usageStatus"),
        "usage": block.pop("usage"),
    }
    if record.get("alias"):
        row["alias"] = record["alias"]
    if record.get("disabled"):
        row["disabled"] = True
    threshold = accounts.account_threshold(record)
    if threshold is not None:
        row["threshold"] = threshold
    row.update(block)
    return row


# ---------- text ----------
SENTINEL_NOTES = {
    "no_credentials": "no login file",
    "relogin_required": "re-login needed — agy is asking for authorization; log in again, then query again",
    "keychain_unavailable": "Credential Manager unavailable — try again shortly",
    "api_key": "API key (no quota)",
    "foreign_credential": "live credential belongs to another account — a switch repairs it",
}
ERROR_NOTES = {
    "timeout": "agy did not answer within 90 seconds",
    "parse": "could not parse agy's output",
    "spawn": "agy executable not found or could not be started",
    "auth": "agy asked for a new login",
}


WINDOW_ORDER = {"5h": 0, "7d": 1}


def _window_body(w: dict, fetched_at: float | None, now: float | None) -> str:
    if w["pct"] >= 100:
        marker = "  (!)"
    else:
        p = pace.compute(w, fetched_at) if w.get("window") == "7d" else None
        marker = "  (ahead of pace)" if p and p.ahead else ""
    cell = reset_strings(w.get("resets_at"), now)
    body = f"{w['pct']:>3.0f}%"
    reset_ts = pace.parse_ts(w.get("resets_at"))
    if cell and reset_ts is not None and reset_ts < (time.time() if now is None else now):
        body += f"   resets {cell[1]:<12}  passed (this figure is from before the reset)"  # not printed as "in 0m" (cswap #376)
    elif cell:
        body += f"   resets {cell[1]:<12}  in {cell[0]}"
    return body + marker


def usage_rows(usage: dict, fetched_at: float | None, now: float | None = None) -> list[tuple[str, str]]:
    """(tree mark, text) per line: each model category on a line of its own and its limits under it, 5h before 7d, marked with
    ├ and └ like cswap's. Keeping the category name off the limit lines keeps them short enough for a phone over SSH.
    Gemini's 7d and 5h are already in scoped, so they are not printed twice."""
    groups: dict[str, list[dict]] = {}
    for w in usage.get("scoped") or []:
        groups.setdefault(w["name"], []).append(w)
    rows = []
    for name, windows in groups.items():
        rows.append(("", name))
        windows.sort(key=lambda w: WINDOW_ORDER.get(w.get("window", "7d"), len(WINDOW_ORDER)))
        for i, w in enumerate(windows):
            rows.append(("└" if i == len(windows) - 1 else "├", f"{w.get('window', '7d')}: {_window_body(w, fetched_at, now)}"))
    return rows


def usage_lines(usage: dict, fetched_at: float | None, now: float | None = None) -> list[str]:
    """usage_rows as plain text."""
    return [f"{mark} {text}" if mark else text for mark, text in usage_rows(usage, fetched_at, now)]


VIA_FOLDER_NOTE = "read from the account folder; the global login was not checked"  # the reason is in the warning above the list


def entry_lines(entry: Entry, now: float) -> list[str]:
    lines = _entry_lines(entry, now)
    return [dimmed(VIA_FOLDER_NOTE)] + lines if entry.via_folder else lines


def _entry_lines(entry: Entry, now: float) -> list[str]:
    if entry.sentinel is not None:
        out = [dimmed(SENTINEL_NOTES.get(entry.sentinel, entry.sentinel))]
        if entry.last_good is not None and entry.fetched_at is not None and entry.sentinel != "api_key":
            top = max((w["pct"] for w in entry.last_good.get("scoped") or []), default=None)
            if top is not None:
                out.append(f"{dimmed('└')} {muted(f'last seen {top:.0f}% used · {format_age(entry.fetched_at, now)}')}")
        return out
    if entry.last_good is not None:
        rows = usage_rows(entry.last_good, entry.fetched_at, now)
        if rows and entry.age_s is not None and entry.age_s > SERVE_TTL_S:
            # The age of stale data goes on the first line: marking only the last line makes the lines above look freshly fetched (cswap #376)
            rows.insert(0, ("", f"figures below were fetched {format_age(entry.fetched_at, now)}"))
        if entry.last_error and entry.age_s is not None and entry.age_s > SERVE_TTL_S:
            rows.append(("", f"latest fetch failed ({ERROR_NOTES.get(entry.last_error, entry.last_error)})"))
        return [f"{dimmed(mark)} {muted(text)}" if mark else muted(text) for mark, text in rows]
    detail = "usage unavailable"
    if entry.last_error:
        detail += f" ({ERROR_NOTES.get(entry.last_error, entry.last_error)})"
    return [dimmed(detail)]
