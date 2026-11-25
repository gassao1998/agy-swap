"""The usage cache cache\\usage.json and the polling pace. One row per account, keyed by account folder name (not by number).

Every query starts an agy process, so the pace is more conservative than cswap's; STALE_OK_S must be longer than the longest normal interval,
or candidate accounts would always become unavailable between two queries.
"""
from __future__ import annotations

import time
import uuid
from dataclasses import dataclass
from pathlib import Path

from agyswap import paths
from agyswap.fsutil import atomic_write_json, file_lock, read_json
from agyswap.usage import mark_cached  # imported by name: gemini_pcts and poll_interval take a parameter called usage

SCHEMA_VERSION = 1
SERVE_TTL_S = 180.0
ACTIVE_INTERVAL_S = 300.0
URGENT_INTERVAL_S = 120.0
URGENT_MARGIN_PCT = 15.0
CANDIDATE_INTERVAL_S = 600.0
EXHAUSTED_INTERVAL_S = 600.0
BACKOFF_BASE_S = 300.0
BACKOFF_CAP_S = 1800.0
STALE_OK_S = 900.0
# A query claims its account for longer than agy's 90-second timeout, so a second process does not start agy for the same account meanwhile (cswap #157, #85)
CLAIM_S = 150.0
# import-usage --hold is bounded, so a typo cannot stop this machine from ever querying an account again
HOLD_CAP_S = 86400.0


@dataclass(frozen=True)
class Entry:
    sentinel: str | None = None  # no_credentials | relogin_required | keychain_unavailable | api_key | foreign_credential
    last_good: dict | None = None
    fetched_at: float | None = None
    age_s: float | None = None
    last_error: str | None = None
    backoff_until: float | None = None
    consecutive_failures: int = 0
    next_poll_at: float | None = None
    poll_interval_s: float | None = None
    relogin: bool = False
    relogin_sha: str | None = None  # hash of the login when a re-login was decided; once the login changes it should be queried again (cswap #456, #599)
    last_attempt_at: float | None = None
    via_folder: bool = False  # the active account was read through its folder copy because Credential Manager was unreadable
    hold_until: float | None = None  # import-usage --hold: this machine does not query the account before then
    claim_until: float | None = None  # another process's query in flight (cswap #157, #85)

    def claimed(self, now: float) -> bool:
        return _claim_live(self.claim_until, now)

    def fresh(self, now: float, ttl: float = SERVE_TTL_S) -> bool:
        return self.fetched_at is not None and now - self.fetched_at <= ttl

    def in_backoff(self, now: float) -> bool:
        """No query now: after a failure (backoff) or while an imported reading is held."""
        return any(t is not None and now < t for t in (self.backoff_until, self.hold_until))

    def due(self, now: float) -> bool:
        """Whether the CLI (list, status) should query now: the cache is past its serve period and not in backoff."""
        return not self.fresh(now) and not self.in_backoff(now)

    def decision_value(self) -> dict | str | None:
        """The value for programmatic decisions: a sentinel first; otherwise the last good value if recent enough; too old is None (unknown)."""
        if self.sentinel is not None:
            return self.sentinel
        if self.last_good is not None and self.age_s is not None and self.age_s <= STALE_OK_S:
            return self.last_good
        return None


def _num(value) -> float | None:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _claim_live(until: float | None, now: float) -> bool:
    """A claim holds until claimUntil, but never longer than CLAIM_S from now: a clock change or a damaged file cannot lock an account out."""
    return until is not None and now < until <= now + CLAIM_S


def _count(value) -> int:
    """A hand-edited or damaged cache field counts as 0, so one field cannot crash list."""
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0


def gemini_pcts(usage: dict | None) -> list[float]:
    return [w["pct"] for w in ((usage or {}).get("five_hour"), (usage or {}).get("seven_day"))
            if isinstance(w, dict) and isinstance(w.get("pct"), (int, float))]


def poll_interval(usage: dict, *, active: bool, threshold: float, previous: dict | None = None) -> float:
    pcts = gemini_pcts(usage)
    if any(p >= 100 for p in pcts):
        return EXHAUSTED_INTERVAL_S
    if not active:
        return CANDIDATE_INTERVAL_S
    before = max(gemini_pcts(previous), default=None)
    now_max = max(pcts, default=0.0)
    rising = before is not None and now_max > before
    return URGENT_INTERVAL_S if rising and now_max >= threshold - URGENT_MARGIN_PCT else ACTIVE_INTERVAL_S


def backoff_seconds(failures: int) -> float:
    # Clamp the exponent before multiplying: after thousands of failures 2 ** n overflows when converted to float (cswap #179)
    return min(BACKOFF_CAP_S, BACKOFF_BASE_S * 2 ** min(max(0, failures - 1), 16))


class UsageCache:
    def __init__(self, path: Path | None = None, clock=time.time):
        self.path = path or paths.usage_cache_file()
        self.clock = clock

    def _rows(self) -> dict:
        data = read_json(self.path, {})
        rows = data.get("accounts") if isinstance(data, dict) else None
        return rows if isinstance(rows, dict) else {}

    def entry(self, folder: str) -> Entry:
        row = self._rows().get(folder)
        if not isinstance(row, dict):
            return Entry()
        fetched_at = _num(row.get("fetchedAt"))
        last_good = mark_cached(row["lastGood"]) if isinstance(row.get("lastGood"), dict) else None
        return Entry(
            last_good=last_good,
            fetched_at=fetched_at,
            age_s=(self.clock() - fetched_at) if fetched_at is not None else None,
            last_error=row.get("lastError") if isinstance(row.get("lastError"), str) else None,
            backoff_until=_num(row.get("backoffUntil")),
            consecutive_failures=_count(row.get("consecutiveFailures")),
            next_poll_at=_num(row.get("nextPollAt")),
            poll_interval_s=_num(row.get("pollIntervalS")),
            relogin=row.get("relogin") is True,
            relogin_sha=row.get("reloginSha") if isinstance(row.get("reloginSha"), str) else None,
            last_attempt_at=_num(row.get("lastAttemptAt")),
            hold_until=_num(row.get("holdUntil")),
            claim_until=_num(row.get("claimUntil")) if isinstance(row.get("claimId"), str) else None,
        )

    def _mutate(self, folder: str, update) -> None:
        with file_lock(self.path.with_name(".usage.lock")):
            rows = self._rows()
            row = rows.get(folder) if isinstance(rows.get(folder), dict) else {}
            update(row)
            rows[folder] = row
            atomic_write_json(self.path, {"schemaVersion": SCHEMA_VERSION, "accounts": rows})

    def forget(self, folder: str) -> None:
        """Cleared together with the account when it is removed, so a later account folder of the same name does not inherit someone else's usage."""
        with file_lock(self.path.with_name(".usage.lock")):
            rows = self._rows()
            if rows.pop(folder, None) is not None:
                atomic_write_json(self.path, {"schemaVersion": SCHEMA_VERSION, "accounts": rows})

    def claim(self, folder: str, seen: Entry | None = None) -> str | None:
        """Claim the account before starting agy (cswap #157, #85). Returns the claim id, or None while another live claim holds it, or when the row changed
        since seen (the reading the caller decided on): another process just finished a query, so the caller re-reads instead of starting agy."""
        token, got = uuid.uuid4().hex, []

        def update(row):
            now = self.clock()
            if isinstance(row.get("claimId"), str) and _claim_live(_num(row.get("claimUntil")), now):
                return
            if seen is not None and (_num(row.get("lastAttemptAt")), _num(row.get("fetchedAt"))) != (seen.last_attempt_at, seen.fetched_at):
                return
            row.update(claimId=token, claimUntil=now + CLAIM_S)
            got.append(token)

        self._mutate(folder, update)
        return got[0] if got else None

    def release(self, folder: str, claim: str) -> None:
        """Drop a claim whose query ended without a result (an exception, Ctrl-C), so the account is not held for CLAIM_S."""
        self._mutate(folder, lambda row: self._owns(row, claim))

    @staticmethod
    def _owns(row: dict, claim: str | None) -> bool:
        """A result written under a claim lands only while that claim still holds: a late answer never overwrites a newer one."""
        if claim is None:
            return True
        if row.get("claimId") != claim:
            return False
        row.pop("claimId", None)
        row.pop("claimUntil", None)
        return True

    def record_success(self, folder: str, usage: dict, interval_s: float, claim: str | None = None) -> None:
        def update(row):
            if not self._owns(row, claim):
                return
            now = self.clock()
            row.update(fetchedAt=now, lastGood=usage, lastAttemptAt=now, pollIntervalS=interval_s,
                       nextPollAt=now + interval_s, consecutiveFailures=0)
            for key in ("lastError", "backoffUntil", "relogin", "reloginSha"):
                row.pop(key, None)

        self._mutate(folder, update)

    def adopt(self, readings: dict[str, tuple[dict, float]], hold_s: float | None = None) -> set[str]:
        """Take in readings another machine made, {folder: (usage, age in seconds)} (cswap 1.0.0 UsageStore.adopt, MIT). A reading lands only when it is
        newer than this machine's own; with hold_s no query starts for those accounts until then (capped at HOLD_CAP_S), 0 lifts an earlier hold,
        None leaves it. Returns the folders whose reading was adopted."""
        now, adopted = self.clock(), set()
        with file_lock(self.path.with_name(".usage.lock")):
            rows = self._rows()
            for folder, (usage, age_s) in readings.items():
                row = rows.get(folder) if isinstance(rows.get(folder), dict) else {}
                fetched, mine = now - age_s, _num(row.get("fetchedAt"))
                if mine is None or fetched > mine:
                    row.update(fetchedAt=fetched, lastGood=usage, consecutiveFailures=0, pollIntervalS=CANDIDATE_INTERVAL_S,
                               nextPollAt=fetched + CANDIDATE_INTERVAL_S)
                    row.pop("lastError", None)
                    row.pop("backoffUntil", None)
                    adopted.add(folder)
                if hold_s is not None and hold_s > 0:
                    row["holdUntil"] = now + min(hold_s, HOLD_CAP_S)
                elif hold_s is not None:
                    row.pop("holdUntil", None)
                rows[folder] = row
            atomic_write_json(self.path, {"schemaVersion": SCHEMA_VERSION, "accounts": rows})
        return adopted

    def record_failure(self, folder: str, error: str, relogin: bool = False, cred_sha: str | None = None,
                       claim: str | None = None) -> None:
        """relogin is set only when agy explicitly asks for a login; a timeout or unreadable output does not count as an expired login (cswap #224, #254)."""
        def update(row):
            if not self._owns(row, claim):
                return
            now = self.clock()
            failures = _count(row.get("consecutiveFailures")) + 1
            wait = backoff_seconds(failures)
            row.update(lastAttemptAt=now, lastError=error, consecutiveFailures=failures,
                       backoffUntil=now + wait, nextPollAt=now + wait)
            if relogin:
                row["relogin"] = True
                if cred_sha:
                    row["reloginSha"] = cred_sha
            else:
                row.pop("relogin", None)
                row.pop("reloginSha", None)

        self._mutate(folder, update)
