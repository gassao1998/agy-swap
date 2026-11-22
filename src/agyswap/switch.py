"""Global switch: write the target account folder's login into gemini:antigravity in Credential Manager.

Order before writing: identify who the current login is, back it up to backup\\last-live.bin and read it back to compare, save it back to the original account's folder,
then write pending_switch.json. After that, write, read back and compare; on a mismatch restore from the backup; only on success update the roster and delete pending.
If the backup or the save-back did not succeed nothing is written; --force can skip the save-back and the identity check but never the backup.

The rules for choosing the target (where rotation starts, which accounts are skipped, best and next-available, the reason values) follow cswap 1.0.0's
switcher.py switch, switch_to and _select_best_switchable (MIT License, claude-swap).
"""
from __future__ import annotations

import contextlib
import json
import re
from datetime import datetime, timezone

from agyswap import accounts, paths, procs
from agyswap.credman import WinCred
from agyswap.errors import AgyswapError, ConfigError, CredentialError
from agyswap.fsutil import atomic_write_bytes, atomic_write_json, ensure_private_dir, file_lock, read_bytes, \
    sha256_hex
from agyswap.login import LiveLogin, login_email, read_live_login, read_token_file, usable_login
from agyswap.output import SCHEMA_VERSION
from agyswap.state import NOT_SAVED, UNKNOWN_EMAIL, Context, collect, load_context, store_login
from agyswap.usage_store import UsageCache

NO_ACCOUNTS = "No accounts are managed yet; add one with agyswap login or add"


# ---------- shared ----------
def label(data: dict, num: str) -> str:
    record = data["accounts"][num]
    return f"Account-{num} ({record.get('alias') or record.get('email', '')})"


def ref(data: dict, num: str | None) -> dict | None:
    if num is None:
        return None
    return {"number": int(num), "email": data["accounts"][num].get("email", "")}


def read_live(store) -> LiveLogin:
    """Re-read once while holding the lock. If it cannot be read (as opposed to absent) stop: per section 11.2 point 5 writing does not continue."""
    return LiveLogin(cred=store.read(paths.KEYRING_TARGET))


def fresh_roster() -> dict:
    data = accounts.load_sequence()
    if not data or not data["accounts"]:
        raise ConfigError(NO_ACCOUNTS)
    return data


def skip_reason(data: dict, num: str, cache: UsageCache | None = None) -> str | None:
    """Why an account cannot be a switch target, or None when it can (cswap #41: skipped during rotation rather than failing the whole batch)."""
    record = data["accounts"][num]
    if record.get("kind") == "apikey":
        return "API key account, run only"
    folder = record.get("folder") or num
    blob = read_token_file(paths.token_path(folder))
    if blob is None:
        return "no login file"
    if not usable_login(blob):
        return "login file lacks an email or tokens"
    entry = (cache or UsageCache()).entry(folder)
    if entry.relogin and entry.relogin_sha in (None, sha256_hex(blob)):
        return "needs a new login"
    return None


def _top_keys(blob: bytes | None) -> set[str]:
    try:
        data = json.loads(blob.decode("utf-8")) if blob else None
    except (UnicodeDecodeError, ValueError):
        return set()
    return set(data) if isinstance(data, dict) else set()


# ---------- usage comparison (--strategy) ----------
def parse_models(text: str | None) -> tuple[str, ...]:
    return tuple(m.strip() for m in (text or "").split(",") if m.strip())


def model_matches(category: str, name: str) -> bool:
    """Case-insensitive, matching the start of the category string or any word in it (gemini, claude, gpt)."""
    low, n = category.lower(), name.lower()
    return n == "all" or low.startswith(n) or n in re.split(r"\W+", low)


def headroom(usage: dict | str | None, models: tuple[str, ...] = ()) -> float | None:
    """100 minus the highest used percentage among the relevant windows (cswap oauth.account_headroom); None when unknown."""
    if not isinstance(usage, dict):
        return None
    pcts = [w["pct"] for w in (usage.get("five_hour"), usage.get("seven_day"))
            if isinstance(w, dict) and isinstance(w.get("pct"), (int, float))]
    if models:
        pcts += [s["pct"] for s in usage.get("scoped") or []
                 if any(model_matches(s.get("name", ""), m) for m in models)]
    return 100.0 - max(pcts) if pcts else None


def inert_models(values: dict, models: tuple[str, ...]) -> list[str]:
    """Model names that match no account's usage category (misspelled). No conclusion is drawn while any account's usage is unreadable."""
    wanted = [m for m in models if m.lower() != "all"]
    if not wanted or not values or any(not isinstance(v, dict) for v in values.values()):
        return []
    names = [s.get("name", "") for v in values.values() for s in v.get("scoped") or []]
    return [m for m in wanted if not any(model_matches(n, m) for n in names)]


def select_best(current: str, others: list[str], values: dict, models: tuple[str, ...]) -> tuple[str | None, str]:
    """Switch only when another account is shown to have more headroom; ties stay put. The note values are the same as cswap's."""
    if not others:
        return None, "none"
    mine = headroom(values.get(current), models)
    if mine is None:
        return None, "current-unavailable"
    scored = [(headroom(values.get(n), models), n) for n in others]
    known = [(h, n) for h, n in scored if h is not None]
    if not known:
        return None, "no-comparison"
    best_h, best_n = max(known, key=lambda t: t[0])
    if best_h > mine:
        return best_n, ""
    if any(h is None for h, _ in scored):
        return None, "incomplete-comparison"
    return (None, "exhausted") if mine <= 0 else (None, "stay")


# ---------- writing to Credential Manager ----------
def _unsaved_backup_path():
    """A new backup\\unsaved-<utc>.bin. Called under .lock, so no other agyswap can pick the same name between the check and the write."""
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    path, n = paths.backup_dir() / f"unsaved-{stamp}.bin", 1
    while path.exists():
        path, n = paths.backup_dir() / f"unsaved-{stamp}-{n}.bin", n + 1
    return path


def _backup(blob: bytes, *, keep: bool = False) -> str:
    """Back up the Credential Manager login and read it back. With keep (the login is not being saved anywhere else), it goes to a new file that no later switch overwrites;
    otherwise to backup\\last-live.bin, which the next switch replaces."""
    path = _unsaved_backup_path() if keep else paths.last_live_file()
    try:
        ensure_private_dir(path.parent)
        atomic_write_bytes(path, blob)
        ok = sha256_hex(read_bytes(path)) == sha256_hex(blob)
    except (AgyswapError, OSError) as e:
        raise AgyswapError(f"Backing up the current login failed ({e}); not switching") from e
    if not ok:
        raise AgyswapError(f"The backup {path} did not match when read back; not switching")
    return str(path)


def _save_back(data: dict, owner: str, blob: bytes, backup: str) -> str | None:
    """Save the login back to the original account's folder. Returns None, or a one-line note when it succeeded, was already identical, or the folder holds a newer copy of the same login; anything else raises."""
    who = label(data, owner)
    if not usable_login(blob):
        raise AgyswapError(f"The Credential Manager login of {who} lacks an email or tokens and cannot be saved back to its folder; not switching. "
                           f"Add --force to discard this login (it is backed up at {backup})")
    try:
        result = store_login(data["accounts"][owner].get("folder") or owner, blob)
    except (AgyswapError, OSError) as e:
        raise AgyswapError(f"Saving the current login back to {who} failed ({e}); not switching") from e
    if result == "unreadable":
        raise AgyswapError(f"The login file of {who} cannot be read right now, so the save-back cannot be confirmed; not switching, try again shortly")
    if result in NOT_SAVED:
        raise AgyswapError(f"The current login was not saved back to {who}: {NOT_SAVED[result]}; not switching. "
                           "Check the roster with agyswap list, or add --force to switch without saving it back")
    return f"The folder of {who} holds a newer login; keeping that one" if result == "folder-newer" else None


def _restore(store, original) -> bool:
    try:
        if original is None:
            store.delete(paths.KEYRING_TARGET)
            return store.read(paths.KEYRING_TARGET) is None
        store.write(paths.KEYRING_TARGET, original.blob, username=original.username or paths.KEYRING_USERNAME,
                    persist=original.persist)
        back = store.read(paths.KEYRING_TARGET)
        return back is not None and back.blob == original.blob
    except CredentialError:
        return False


def perform(store, data: dict, live: LiveLogin, target: str, *, force: bool) -> tuple[dict | None, dict, list[str]]:
    """Call while holding .lock. Returns (from, to, warnings)."""
    record = data["accounts"][target]
    who = label(data, target)
    if record.get("kind") == "apikey":
        raise ConfigError(f"{who} is an API key account and cannot be switched globally; use agyswap run {target}")
    blob = read_token_file(paths.token_path(record.get("folder") or target))
    if blob is None:
        raise AgyswapError(f"{who} has no login file (or it cannot be read right now); run agyswap login --slot {target} to log in again")
    if not usable_login(blob):
        raise AgyswapError(f"The login file of {who} lacks an email or tokens, so it is not written to Credential Manager; run agyswap login --slot {target} to log in again")
    holder = login_email(blob)
    placeholder = accounts.is_placeholder(record.get("email"))
    if not placeholder and holder.lower() != record["email"].lower():
        raise AgyswapError(f"The login file of {who} belongs to {holder}, not {record['email']} as recorded in the roster; log in again with the right account")
    if placeholder:
        # The real email is recorded below; one that already belongs to another account would leave two accounts holding one login
        taken = next((n for n in accounts.numbers(data) if n != target and data["accounts"][n].get("kind") != "apikey"
                      and (data["accounts"][n].get("email") or "").lower() == holder.lower()), None)
        if taken:
            raise AgyswapError(f"The login file of {who} belongs to {holder}, which is already {label(data, taken)}; "
                               f"log {who} in again with its own account (agyswap login --slot {target})")

    warnings: list[str] = []
    original = live.cred
    owner = Context(data=data, live=live, settings={}).live_owner
    from_ref = None
    if original is not None:
        extra = _top_keys(original.blob) - _top_keys(blob)
        if extra and not force:
            raise AgyswapError(f"The Credential Manager login has {', '.join(sorted(extra))}, which the login file of {who} lacks (a field a newer agy may have added); "
                               "writing the whole file would lose it. Use --force if you are sure")
        if owner is None and not force:
            raise AgyswapError(f"Cannot tell which account the Credential Manager login belongs to ({live.email or 'email unreadable'}); not switching. "
                               "Use agyswap add to take it in, or --force to write anyway")
        kept = owner is None or force  # the original is not saved back, so the backup is its only copy
        backup = _backup(original.blob, keep=kept)
        if extra:
            warnings.append(f"--force: {', '.join(sorted(extra))} from the Credential Manager login was not carried over; the original is at {backup}")
        if owner is None:
            warnings.append(f"--force: could not tell which account the original login belonged to, so it was not saved back; the original is at {backup}")
        elif force:
            warnings.append(f"--force: the original login was not saved back to {label(data, owner)}; the original is at {backup}")
        else:
            note = _save_back(data, owner, original.blob, backup)
            if note:
                warnings.append(note)
        from_ref = ref(data, owner) if owner else {"number": None, "email": live.email or UNKNOWN_EMAIL}

    pending = paths.pending_switch_file()
    atomic_write_json(pending, {"from": int(owner) if owner else None, "to": int(target), "at": accounts.timestamp()})
    try:
        store.write(paths.KEYRING_TARGET, blob, username=paths.KEYRING_USERNAME)
        after = store.read(paths.KEYRING_TARGET)
        written = after is not None and sha256_hex(after.blob) == sha256_hex(blob)
        problem = "the written login did not match when read back"
    except CredentialError as e:
        written, problem = False, str(e)
    if not written:
        if _restore(store, original):
            pending.unlink(missing_ok=True)
            raise AgyswapError(f"Switch to {who} failed ({problem}); the original login was restored from the backup")
        where = ""
        if original is not None:
            if not kept:  # last-live.bin would be replaced by the next switch, which is what the pending warning suggests
                with contextlib.suppress(AgyswapError):
                    backup = _backup(original.blob, keep=True)
            where = f"; the original login is kept at {backup}"
        raise AgyswapError(f"Switch to {who} failed ({problem}) and the restore failed too{where}; follow the manual restore steps in the README")
    data["activeAccountNumber"] = int(target)
    if placeholder:
        record["email"] = holder
    accounts.save_sequence(data)
    pending.unlink(missing_ok=True)
    return from_ref, ref(data, target), warnings


def process_warnings() -> list[str]:
    running = procs.running()
    if not running:
        return []
    names = ", ".join(f"{image}×{count}" for image, count in running.items())
    return [f"agy or the App is running ({names}): they hold the original account's token in memory, and a refresh may write the original account back to Credential Manager"]


# ---------- JSON ----------
def result(strategy: str, reason: str, message: str, from_ref, to_ref, warnings: list[str]) -> dict:
    return {"schemaVersion": SCHEMA_VERSION, "switched": from_ref != to_ref,
            "from": from_ref, "to": to_ref, "strategy": strategy, "reason": reason, "message": message,
            "warnings": warnings}


def _done(strategy: str, from_ref, to_ref, warnings: list[str]) -> dict:
    if from_ref != to_ref:
        return result(strategy, "switched", f"Switched to Account-{to_ref['number']} ({to_ref['email']})", from_ref, to_ref,
                      warnings)
    return result(strategy, "already-active", f"Already on Account-{to_ref['number']} ({to_ref['email']})", to_ref, to_ref,
                  warnings)


def _noop(strategy: str, reason: str, message: str, at, warnings: list[str]) -> dict:
    return result(strategy, reason, message, at, at, warnings)


# ---------- commands ----------
def switch_to(identifier: str, *, force: bool = False, store=None, check=None) -> dict:
    """check(ctx, target), when given, runs under .lock before anything is written and may raise to call the switch off (auto's rechecks:
    the live owner and the target as the tick saw them, and a live run, cswap #508)."""
    store = store or WinCred()
    load_context(read_live=lambda: read_live_login(store))
    warnings = process_warnings()
    with file_lock(paths.lock_file()):
        data = fresh_roster()
        target = accounts.resolve(data, identifier)
        live = read_live(store)
        ctx = Context(data=data, live=live, settings={})
        if check is not None:
            check(ctx, target)
        if not force and ctx.live_owner == target:
            # cswap #79: no save-back and no write. The roster is changed only when it was wrong (settling a foreign credential)
            message = f"Already on {label(data, target)}"
            if ctx.recorded() != target:
                data["activeAccountNumber"] = int(target)
                accounts.save_sequence(data)
                message += f"; the roster now records Account-{target} as active"
            else:
                message += f". Run agyswap switch {target} --force to rewrite the global login from the account folder"
            paths.pending_switch_file().unlink(missing_ok=True)  # settles an unfinished switch, as the pending warning tells the user to do
            return _noop("direct", "already-active", message, ref(data, target), warnings)
        from_ref, to_ref, more = perform(store, data, live, target, force=force)
    payload = _done("direct", from_ref, to_ref, warnings + more)
    if force and not payload["switched"]:
        payload["reason"] = "activated"
        payload["message"] = f"Activated Account-{to_ref['number']} ({to_ref['email']}) from its account folder"
    return payload


def switch(*, strategy: str | None = None, models: tuple[str, ...] = (), store=None, fetcher=None) -> dict:
    """Switch without an account: rotate through the sequence to the next account that can be switched to; --strategy picks by usage."""
    store = store or WinCred()
    name = strategy or "rotation"
    models = models if strategy else ()
    ctx = load_context(read_live=lambda: read_live_login(store))
    if ctx.live.error:
        raise CredentialError(f"Cannot read Credential Manager; not switching ({ctx.live.error})")
    data = ctx.data
    if not data or not data["accounts"]:
        raise ConfigError(NO_ACCOUNTS)
    warnings = process_warnings()
    nums = accounts.numbers(data)
    cache = UsageCache()

    def usable(num: str) -> bool:
        if data["accounts"][num].get("disabled"):
            warnings.append(f"Skipped Account-{num} (disabled)")
            return False
        why = skip_reason(data, num, cache)
        if why:
            warnings.append(f"Skipped Account-{num} ({why}; run agyswap login --slot {num} to log in again)")
            return False
        return True

    if not ctx.live.present:
        # Credential Manager holds no login (for example right after a purge, or after logging out in agy): activate the roster's account, or the first usable one
        rec = ctx.recorded()
        order = ([rec] if rec else []) + [n for n in nums if n != rec]
        target = next((n for n in order if usable(n)), None)
        if target is None:
            raise ConfigError("No account has a usable login file; log one in again with agyswap login")
        return _locked_perform(store, ctx.live, target, name, warnings)

    owner = ctx.live_owner
    if owner is None:
        at = {"number": None, "email": ctx.live.email or UNKNOWN_EMAIL}
        return _noop(name, "unmanaged-account", "Active account is not managed; run agyswap add", at, warnings)
    current_ref = ref(data, owner)
    if len(nums) < 2:
        return _noop(name, "only-one-account", "Only one account is managed. Add more accounts to switch between.", current_ref, warnings)

    values: dict = {}
    if strategy:
        candidates = [n for n in nums if n != owner and not data["accounts"][n].get("disabled")
                      and skip_reason(data, n, cache) is None]
        entries = collect(ctx, [owner, *candidates], **({"fetcher": fetcher} if fetcher else {}))
        values = {n: e.decision_value() for n, e in entries.items()}
        for m in inert_models(values, models):
            warnings.append(f"Model {m} matches no account's usage category (misspelled?)")
        limits = "usage limits" if models else "Gemini limit"
        if strategy == "best":
            target, note = select_best(owner, candidates, values, models)
            if target is not None:
                return _locked_perform(store, ctx.live, target, name, warnings)
            stay = {
                "current-unavailable": ("usage-unavailable", f"Current account usage is unavailable — staying on Account-{owner}. Run agyswap switch to rotate."),
                "no-comparison": ("usage-unavailable", f"No other account has usage data to compare — staying on Account-{owner}."),
                "incomplete-comparison": ("usage-unavailable",
                                          f"No account with known usage has more remaining quota; some usage is unavailable — staying on Account-{owner}."),
                "stay": ("already-best", f"Already on the account with the most remaining quota (Account-{owner})."),
                "exhausted": ("candidates-exhausted", f"All accounts are at their {limits} — staying on Account-{owner}."),
            }.get(note)
            if stay:
                return _noop(name, stay[0], stay[1], current_ref, warnings)

    rec = ctx.recorded()
    anchor = owner if strategy == "next-available" or rec is None else rec
    start = nums.index(anchor)
    target, exhausted = None, []
    for offset in range(1, len(nums)):
        candidate = nums[(start + offset) % len(nums)]
        if not usable(candidate):
            continue
        if strategy == "next-available":
            h = headroom(values.get(candidate), models)
            if h is not None and h <= 0:
                exhausted.append(candidate)
                warnings.append(f"Skipped Account-{candidate} (at {limits})")
                continue
        target = candidate
        break
    if target is None and exhausted:
        return _noop(name, "candidates-exhausted", f"All other accounts are at their {limits} — staying on Account-{owner}.", current_ref, warnings)
    if target is None:
        return _noop(name, "no-valid-target", "No other accounts have a usable login file.", current_ref, warnings)
    if target == owner:
        return _noop(name, "already-active", f"Already on {label(data, owner)}", current_ref, warnings)
    return _locked_perform(store, ctx.live, target, name, warnings)


def _locked_perform(store, seen: LiveLogin, target: str, strategy: str, warnings: list[str]) -> dict:
    """The lock is not held while the target is chosen (querying usage starts agy and takes seconds); once it is taken, confirm the Credential Manager login has not changed before writing."""
    with file_lock(paths.lock_file()):
        data = fresh_roster()
        live = read_live(store)
        if live.sha != seen.sha:
            raise AgyswapError("The Credential Manager login changed while the target was being chosen (another agyswap or agy touched it); run again")
        if target not in data["accounts"]:
            raise AgyswapError(f"Account-{target} was removed or moved while the target was being chosen; run again")
        from_ref, to_ref, more = perform(store, data, live, target, force=False)
    return _done(strategy, from_ref, to_ref, warnings + more)
