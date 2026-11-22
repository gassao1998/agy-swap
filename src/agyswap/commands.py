"""Implementation of list and status (text and --json)."""
from __future__ import annotations

import sys
import time
from datetime import datetime, timezone

from agyswap import accounts, output, paths, procs
from agyswap.errors import CredentialError
from agyswap.fsutil import sha256_hex
from agyswap.login import login_email, read_token_file, read_token_file_stat
from agyswap.output import SCHEMA_VERSION
from agyswap.printer import accent, bold_accent, bolded, dimmed, muted, warning
from agyswap.pace import parse_ts, unstarted
from agyswap.state import UNKNOWN_EMAIL, Context, collect, sync_active_login
from agyswap.usage_store import Entry

LOCKSTEP_TOLERANCE_S = 5.0


def _local(epoch: float | None) -> str:
    try:
        return datetime.fromtimestamp(epoch, tz=timezone.utc).astimezone().strftime("%m-%d %H:%M") if epoch else "?"
    except (OSError, OverflowError, ValueError):
        return "?"


def _label(num: str, record: dict) -> str:
    alias = record.get("alias")
    return f"Account-{num} ({alias})" if alias else f"Account-{num}"


# ---------- contamination detection ----------
def duplicate_warnings(data: dict) -> list[str]:
    """Signs of slot contamination: two login files that are identical, a login file whose email differs from the roster's, two login files that belong to the same email
    (a refreshed token has a different hash, so comparing hashes alone would miss it), and two roster accounts registered under the same email."""
    out, by_sha, by_email, by_file_email = [], {}, {}, {}
    for num in accounts.numbers(data):
        record = data["accounts"][num]
        blob = read_token_file(paths.token_path(record.get("folder") or num))
        holder = login_email(blob)
        if blob:
            sha = sha256_hex(blob)
            if sha in by_sha:
                out.append(f"{_label(by_sha[sha], data['accounts'][by_sha[sha]])} and {_label(num, record)} hold the same "
                           "login file — one account's login was overwritten. Log in with the missing account and "
                           "re-add it: agyswap login --slot N")
            else:
                by_sha[sha] = num
                if holder and holder.lower() in by_file_email:
                    first = by_file_email[holder.lower()]
                    out.append(f"{_label(first, data['accounts'][first])} and {_label(num, record)} have login files "
                               f"that both belong to {holder} — one account's login was overwritten. Log in with the "
                               "missing account and re-add it: agyswap login --slot N")
        if holder:
            by_file_email.setdefault(holder.lower(), num)
            if not accounts.is_placeholder(record.get("email")) and holder.lower() != record["email"].lower():
                out.append(f"{_label(num, record)} is recorded as {record['email']}, but its login file belongs to {holder}; "
                           "log in with the right account and re-add it: agyswap login --slot N")
        email = (record.get("email") or "").lower()
        if email and not accounts.is_placeholder(email):
            if email in by_email:
                out.append(f"{_label(by_email[email], data['accounts'][by_email[email]])} and {_label(num, record)}"
                           f" are both registered as {record['email']} — remove or re-login one of them.")
            else:
                by_email[email] = num
    return out


def lockstep_warnings(data: dict, entries: dict[str, Entry]) -> list[str]:
    rows = []
    for num in accounts.numbers(data):
        value = entries[num].decision_value() if num in entries else None
        if not isinstance(value, dict):
            continue
        windows = {}
        for w in value.get("scoped") or []:
            ts = parse_ts(w.get("resets_at"))
            if ts is None:
                windows = None
                break
            # An unused window's reset time is "fetch time + period", so two idle accounts queried in turn differ by only a few seconds, and comparing them would only raise false alarms
            if not unstarted(w, entries[num].fetched_at):
                windows[(w["name"], w.get("window"))] = (w["pct"], ts)
        if windows:
            rows.append((num, windows))
    out = []
    for i, (num, mine) in enumerate(rows):
        for other, theirs in rows[:i]:
            if mine.keys() == theirs.keys() and all(
                    mine[k][0] == theirs[k][0] and abs(mine[k][1] - theirs[k][1]) <= LOCKSTEP_TOLERANCE_S
                    for k in mine):
                out.append(f"{_label(other, data['accounts'][other])} and {_label(num, data['accounts'][num])}"
                           " report identical usage and matching reset times — they may be the same account. "
                           "If it persists, log in with the missing account and re-add it: agyswap login --slot N")
                break
    return out


# ---------- list ----------
def list_payload(ctx: Context, entries: dict[str, Entry], now: float) -> dict:
    active = ctx.active_number
    payload = {
        "schemaVersion": SCHEMA_VERSION,
        "activeAccountNumber": int(active) if active else None,
        "accounts": [output.account_row(int(n), ctx.data["accounts"][n], n == active, entries[n], now)
                     for n in accounts.numbers(ctx.data)],
    }
    dup = duplicate_warnings(ctx.data)
    if dup:
        payload["duplicateAccountWarnings"] = dup
    lock = lockstep_warnings(ctx.data, entries)
    if lock:
        payload["lockstepUsageWarnings"] = lock
    return payload


def token_status_lines(ctx: Context, num: str, record: dict, is_active: bool) -> list[str]:
    lines = []
    if is_active and ctx.live.present:
        c = ctx.live.cred
        lines.append(f"Credential Manager: present, {len(c.blob)} bytes, last written {_local(c.last_written)}, sha256 {ctx.live.sha[:8]}")
    path = paths.token_path(record.get("folder") or num)
    found = read_token_file_stat(path)
    if found is None:
        lines.append("account folder login file: unreadable" if path.exists() else "account folder login file: missing")
    else:
        blob, mtime = found
        holder = login_email(blob)
        lines.append(f"account folder login file: present, {len(blob)} bytes, modified {_local(mtime)}, "
                     f"sha256 {sha256_hex(blob)[:8]}" + (f", belongs to {holder}" if holder else ", email unreadable"))
    return lines


def _prepare(ctx: Context) -> None:
    """The preparation list and status share: save the active account's login back to its folder, then print warnings. Messages always go to stderr."""
    note = sync_active_login(ctx)
    if note:
        warning(f"agyswap: {note}", file=sys.stderr)


def _common_warnings(ctx: Context) -> None:
    if ctx.live.error:
        warning(f"agyswap: Cannot read Credential Manager ({ctx.live.error})", file=sys.stderr)
    elif ctx.foreign:
        warning(f"agyswap: Credential Manager holds the login of Account-{ctx.live_owner}, not Account-{ctx.active_number} as recorded "
                f"(foreign_credential); run agyswap switch {ctx.live_owner} to settle it, or switch {ctx.active_number} to switch back",
                file=sys.stderr)
        return
    elif ctx.unmanaged_live:
        warning(f"agyswap: The Credential Manager login ({ctx.live.email or 'email unreadable'}) is not managed; run agyswap add to take it in",
                file=sys.stderr)
    mismatch = ctx.recorded_mismatch()
    if mismatch:
        warning(f"agyswap: {mismatch}", file=sys.stderr)


def cmd_list(ctx: Context, *, json_output: bool = False, token_status: bool = False, fetcher=None,
             now: float | None = None) -> dict | None:
    if ctx.data is None or not ctx.data["accounts"]:
        if json_output:
            return {"schemaVersion": SCHEMA_VERSION, "activeAccountNumber": None, "accounts": []}
        print(dimmed("No accounts are managed yet. Add one with agyswap login or add."))
        return None
    nums = accounts.numbers(ctx.data)
    kwargs = {"fetcher": fetcher} if fetcher else {}
    _prepare(ctx)
    entries = collect(ctx, nums, now=now, **kwargs)
    now = time.time() if now is None else now
    _common_warnings(ctx)
    if json_output:
        return list_payload(ctx, entries, now)

    active = ctx.active_number
    print(bolded("Accounts:"))
    for i, num in enumerate(nums):
        record = ctx.data["accounts"][num]
        label = f"{accent(record['alias'])} ({record.get('email', '')})" if record.get("alias") else record.get("email", "")
        markers = f" {bold_accent('(active)')}" if num == active else ""
        if record.get("disabled"):
            markers += f" {muted('(disabled)')}"
        print(f"  {num}: {label}{markers}")
        for line in output.entry_lines(entries[num], now):
            print(f"     {line}")
        if token_status:
            for line in token_status_lines(ctx, num, record, num == active):
                print(f"     {dimmed('•')} {muted(line)}")
        if i < len(nums) - 1:
            print()
    if token_status and ctx.unmanaged_live:
        c = ctx.live.cred
        print(f"\n  {dimmed('•')} {muted(f'Credential Manager: present, {len(c.blob)} bytes, last written {_local(c.last_written)}, sha256 {ctx.live.sha[:8]}, not managed')}")
    warnings = duplicate_warnings(ctx.data) + lockstep_warnings(ctx.data, entries)
    if warnings:
        print()
        for msg in warnings:
            warning(msg)
    running = procs.running()
    if running:
        print()
        print(bolded("Running processes:"))
        for image, count in running.items():
            print(f"  {dimmed('●')} {muted(image)}  {dimmed(f'({count} running)')}")
    return None


# ---------- status ----------
def status_payload(ctx: Context, entry: Entry | None, now: float) -> dict:
    num = ctx.active_number
    if num is None:
        if not ctx.live.present:
            return {"schemaVersion": SCHEMA_VERSION, "active": None}
        return {"schemaVersion": SCHEMA_VERSION, "active": {"email": ctx.live.email or UNKNOWN_EMAIL, "managed": False}}
    record = ctx.data["accounts"][num]
    block = output.usage_block(entry, now)
    active = {
        "number": int(num),
        "email": record.get("email", ""),
        "organizationName": "",
        "organizationUuid": "",
        "isOrganization": False,
        "managed": True,
        "usageStatus": block.pop("usageStatus"),
        "usage": block.pop("usage"),
    }
    if record.get("alias"):
        active["alias"] = record["alias"]
    active.update(block)
    return {"schemaVersion": SCHEMA_VERSION, "active": active, "totalManagedAccounts": len(ctx.data["accounts"])}


def cmd_status(ctx: Context, *, json_output: bool = False, fetcher=None, now: float | None = None) -> dict | None:
    num = ctx.active_number
    if num is None and ctx.live.error:
        # Credential Manager cannot be read and there is no roster to fall back on: saying "no active login" would be wrong
        raise CredentialError(f"Cannot read Credential Manager, so the active account is unknown ({ctx.live.error})")
    kwargs = {"fetcher": fetcher} if fetcher else {}
    if ctx.data is not None:
        _prepare(ctx)
    entry = collect(ctx, [num], now=now, **kwargs)[num] if num else None
    now = time.time() if now is None else now
    _common_warnings(ctx)
    if json_output:
        return status_payload(ctx, entry, now)
    if num is None:
        if not ctx.live.present:
            print(f"{bolded('Status:')} {dimmed('no active agy login')}")
        else:
            print(f"{bolded('Status:')} {ctx.live.email or UNKNOWN_EMAIL} {dimmed('(not managed)')}")
        return None
    record = ctx.data["accounts"][num]
    name = f" ({record['alias']})" if record.get("alias") else ""
    print(f"{bolded('Status:')} {accent(f'Account-{num}')}{name} {record.get('email', '')}")
    total = len(ctx.data["accounts"])
    print(f"  {dimmed(f'Total managed accounts: {total}')}")
    for line in output.entry_lines(entry, now):
        print(f"  {line}")
    return None
