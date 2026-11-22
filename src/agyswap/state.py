"""State shared by every command: the roster, the Credential Manager login, who is active, and collecting each account's usage."""
from __future__ import annotations

import contextlib
import dataclasses
import sys
import time
from dataclasses import dataclass
from typing import Callable

from agyswap import accounts, migrate, paths, settings, usage
from agyswap.errors import AgyswapError
from agyswap.fsutil import ensure_private_dir, file_lock, replace_keep_backup, sha256_hex
from agyswap.login import LiveLogin, login_email, login_expiry, read_live_login, read_token_file, usable_login
from agyswap.printer import warning
from agyswap.usage_store import Entry, UsageCache, poll_interval

UNKNOWN_EMAIL = "unknown@agyswap.local"


@dataclass
class Context:
    data: dict | None
    live: LiveLogin
    settings: dict

    def recorded(self) -> str | None:
        """The active account recorded in the roster (only if it is still in the roster)."""
        rec = (self.data or {}).get("activeAccountNumber")
        return str(rec) if rec is not None and str(rec) in self.data["accounts"] else None

    @property
    def live_owner(self) -> str | None:
        """Which account the Credential Manager login belongs to (by id_token_email; API key accounts do not count).
        When several accounts are registered under the same email (contamination), prefer the one whose folder copy is identical, then the roster's active account."""
        email = self.live.email if self.data is not None and not self.live.error else None
        if not email:
            return None
        hits = [n for n in accounts.numbers(self.data)
                if self.data["accounts"][n].get("kind") != "apikey"
                and (self.data["accounts"][n].get("email") or "").lower() == email.lower()]
        if len(hits) > 1:
            same = [n for n in hits if _folder_sha(self.data["accounts"][n], n) == self.live.sha]
            rec = self.recorded()
            hits = same or ([rec] if rec in hits else hits)
        return hits[0] if hits else None

    @property
    def active_number(self) -> str | None:
        """The active account. When the roster's account and the Credential Manager login belong to different managed accounts, the roster's one is still reported (as cswap does),
        and foreign flags it as foreign_credential. When Credential Manager cannot be read, fall back to the roster's account."""
        if self.data is None:
            return None
        if self.live.error:
            return self.recorded()
        owner = self.live_owner
        if owner is None:
            return None
        rec = self.recorded()
        return rec if rec is not None and rec != owner else owner

    @property
    def foreign(self) -> bool:
        active = self.active_number
        return active is not None and not self.live.error and self.live_owner not in (None, active)

    @property
    def unmanaged_live(self) -> bool:
        return self.live.present and self.live_owner is None

    def recorded_mismatch(self) -> str | None:
        """When the roster's active account differs from the Credential Manager one, return a one-line note (these two stages only warn and never change the roster)."""
        if self.data is None or self.live.error:
            return None
        rec, owner = self.recorded(), self.live_owner
        if rec == owner or (rec is not None and owner is None and self.live.present):
            return None  # the same, or a separate "not managed" warning applies
        shown = f"Account-{owner}" if owner else "no login"
        fix = f"agyswap switch {owner}" if owner else "agyswap switch <num|email|alias>"
        return f"The roster records {'Account-' + rec if rec else 'no account'} as active, but Credential Manager holds {shown}; run {fix} to settle it"


def _folder_sha(record: dict, num: str) -> str | None:
    try:
        blob = read_token_file(paths.token_path(record.get("folder") or num))
    except AgyswapError:
        return None
    return sha256_hex(blob) if blob else None


def load_context(read_live: Callable[[], LiveLogin] | None = None, quiet: bool = False) -> Context:
    cached: list[LiveLogin] = []

    def reader() -> LiveLogin:
        if not cached:
            cached.append((read_live or read_live_login)())
        return cached[0]

    for note in migrate.migrate(reader):
        if not quiet:
            warning(f"agyswap: {note}", file=sys.stderr)
    data = accounts.load_sequence()
    return Context(data=data, live=reader(), settings=settings.load())


# Results of store_login that leave the folder without this login: the caller must treat them as "not saved"
NOT_SAVED = {
    "unreadable": "its login file cannot be read right now",
    "missing": "its account folder does not exist",
    "other-email": "its account folder holds the login of another email",
}


def store_login(folder: str, blob: bytes) -> str:
    """Save an account's login back to its folder (keeping a .bak and reading back to compare). The caller must first confirm that blob belongs to this account,
    that it passed usable_login, and hold .lock. Returns saved, same, folder-newer (the folder holds a newer usable login for the same email because the account
    was logged in again separately, cswap #302; not overwritten), or one of NOT_SAVED: unreadable (temporarily unreadable; without knowing what is inside it is
    not overwritten), missing (the folder is never recreated: the account may have been removed since the caller read the roster) or other-email (the folder
    holds a usable login of another email, so it belongs to another account). A failed write raises AgyswapError or OSError."""
    home, path = paths.profile_home(folder), paths.token_path(folder)
    if not home.is_dir():
        return "missing"
    current = read_token_file(path)
    if current is not None and sha256_hex(current) == sha256_hex(blob):
        return "same"
    if current is None and path.exists():
        return "unreadable"
    if usable_login(current) and login_email(current).lower() != (login_email(blob) or "").lower():
        return "other-email"
    mine, theirs = login_expiry(blob), login_expiry(current)
    if mine is not None and theirs is not None and theirs > mine and usable_login(current):
        return "folder-newer"  # same email: another email returned other-email above
    ensure_private_dir(home)
    replace_keep_backup(path, blob)
    return "saved"


def sync_active_login(ctx: Context) -> str | None:
    """When the Credential Manager login belongs to the active account and differs from the folder copy, save it back to the folder.
    Returns the note to print to stderr, or None when nothing was done."""
    num = ctx.active_number
    if num is None or ctx.foreign or ctx.live_owner != num or not ctx.live.present:
        return None
    record = ctx.data["accounts"][num]
    blob = ctx.live.cred.blob
    if record.get("kind") == "apikey" or not usable_login(blob):
        return None
    current = read_token_file(paths.token_path(record.get("folder") or num))
    if current is not None and sha256_hex(current) == ctx.live.sha:
        return None
    try:
        with file_lock(paths.lock_file(), retries=0):
            try:
                result = store_login(record.get("folder") or num, blob)
                if result in ("missing", "other-email"):  # unreadable is temporary: the next list or status tries again
                    return f"The active account's login was not saved back to its folder: {NOT_SAVED[result]}"
            except AgyswapError as e:
                return f"The active account's login was not saved back to its folder: {e}"
            except OSError as e:
                return f"The active account's login was not saved back to its folder ({e.strerror or e})"
    except AgyswapError:
        pass  # another agyswap is switching or syncing; save next time
    return None


def threshold_for(ctx: Context, record: dict) -> float:
    own = accounts.account_threshold(record)
    return own if own is not None else float(ctx.settings["autoswitch.threshold"])


def _query(cache: UsageCache, folder: str, env: dict, fetcher, cred_sha: str | None, *, active: bool, threshold: float,
           previous: dict | None, seen: Entry | None = None) -> None:
    """One agy start: claim the account first (cswap #157, #85), log the start and the result (cswap #434, #590), then record the outcome under that claim."""
    claim = cache.claim(folder, seen)
    if claim is None:
        return  # another agyswap is querying this account right now, or just did; its result lands in the cache
    recorded = False
    try:
        usage.log_activity(f"usage query start: folder {folder}, login {cred_sha[:8] if cred_sha else 'none'}")
        started = time.monotonic()
        result = fetcher(env)
        outcome = "ok" if result.usage is not None else (result.error or "parse")
        usage.log_activity(f"usage query end: folder {folder}, result {outcome}, {time.monotonic() - started:.1f}s")
        if result.usage is not None:
            interval = poll_interval(result.usage, active=active, threshold=threshold, previous=previous)
            cache.record_success(folder, result.usage, interval, claim=claim)
        else:
            cache.record_failure(folder, result.error or "parse", relogin=result.relogin, cred_sha=cred_sha, claim=claim)
        recorded = True
    finally:
        if not recorded:  # an exception or Ctrl-C: free the account now rather than after CLAIM_S
            with contextlib.suppress(Exception):
                cache.release(folder, claim)


def collect(ctx: Context, nums: list[str], *, fetcher=usage.fetch, cache: UsageCache | None = None,
            now: float | None = None, fetch: set[str] | None = None, query: bool = True) -> dict[str, Entry]:
    """Usage for each account. Queried one at a time, not in parallel (parallel queries are not verified safe). Nothing is queried while the cache is still fresh.
    With fetch (agyswap auto's own schedule), exactly those accounts are queried unless in backoff, fresh or not, and the others are served from the cache.
    With query=False nothing is queried at all, not even a re-logged account: the dashboard's redraws read the cache only (cswap #496)."""
    cache = cache or UsageCache(clock=time.time if now is None else (lambda: now))
    active = ctx.active_number
    out: dict[str, Entry] = {}
    for num in nums:
        record = ctx.data["accounts"][num]
        folder = record.get("folder") or num
        entry = cache.entry(folder)
        if record.get("kind") == "apikey":
            out[num] = dataclasses.replace(entry, sentinel="api_key")
            continue
        is_active = num == active
        token = paths.token_path(folder)
        # Credential Manager cannot be read in this logon session (it is unavailable over SSH): read the active account's usage through its folder copy, isolated as run does.
        # Nothing is written to Credential Manager or the folder here (the sync above is skipped too), and the row is marked so the unchecked global login is visible.
        via_folder = is_active and bool(ctx.live.error)
        if via_folder and not usable_login(read_token_file(token)):
            out[num] = dataclasses.replace(entry, sentinel="keychain_unavailable")
            continue
        if is_active and ctx.foreign:
            out[num] = dataclasses.replace(entry, sentinel="foreign_credential")
            continue
        folder_blob = read_token_file(token) if not is_active or via_folder else None
        if not is_active and folder_blob is None and not token.is_file():
            out[num] = dataclasses.replace(entry, sentinel="no_credentials")
            continue
        # When the folder copy is identical to the Credential Manager one, query through Credential Manager: one login must not be refreshed from two places (cswap #590)
        via_live = (is_active and not via_folder) or (folder_blob is not None and sha256_hex(folder_blob) == ctx.live.sha)
        cred_sha = ctx.live.sha if via_live else (sha256_hex(folder_blob) if folder_blob else None)
        moment = cache.clock()
        relogged = entry.relogin and cred_sha is not None and cred_sha != entry.relogin_sha
        wanted = entry.due(moment) if fetch is None else (num in fetch and not entry.in_backoff(moment))
        if query and (wanted or relogged):
            env = usage.live_env() if via_live else usage.profile_env(paths.profile_home(folder))
            _query(cache, folder, env, fetcher, cred_sha, active=is_active, threshold=threshold_for(ctx, record),
                   previous=entry.last_good, seen=entry)
            entry = cache.entry(folder)
        if entry.relogin:
            entry = dataclasses.replace(entry, sentinel="relogin_required")
        out[num] = dataclasses.replace(entry, via_folder=True) if via_folder else entry
    return out
