"""export, import and import-usage.

Follows cswap 1.0.0's transfer.py (MIT License, claude-swap): a portable JSON envelope with no built-in encryption (compose your
own, for example agyswap export - | gpg -c > accounts.gpg), every account validated before anything is written, and
import-usage reading another machine's list --json. Where agyswap differs: the login travels as base64 of the account folder's
login file, the envelope is recognised by its formatVersion, API key accounts are not exported, an export file is created
restricted to the current Windows user before any content goes in and never replaces an existing file without --force,
logins are not printed to a terminal, and import never changes the active account or the global login.
"""
from __future__ import annotations

import base64
import binascii
import contextlib
import json
import os
import re
import sys
from pathlib import Path

from agyswap import __version__, accounts, fsutil, manage, pace, paths, usage
from agyswap.credman import CRED_MAX_CREDENTIAL_BLOB_SIZE
from agyswap.errors import TransferError
from agyswap.fsutil import atomic_write_bytes, ensure_private_dir, read_bytes
from agyswap.login import login_email, read_token_file, usable_login
from agyswap.state import NOT_SAVED, load_context, store_login, sync_active_login
from agyswap.switch import label, skip_reason
from agyswap.usage_store import HOLD_CAP_S, UsageCache

FORMAT_VERSION = 1
SETTINGS_REL = Path(".gemini") / "antigravity-cli" / "settings.json"
CARRIED = ("disabled", "threshold", "priority")
_EMAIL_RE = re.compile(r"[^@\s]+@[^@\s]+")
# Bounds on untrusted input. An export --full of a few dozen accounts with settings at the cap still fits MAX_INPUT_BYTES.
MAX_INPUT_BYTES = 64 * 1024 * 1024
MAX_SETTINGS_BYTES = 1024 * 1024
MAX_ROWS = 1000
MAX_SLOT = 9999
MAX_TEXT = 254  # emails and timestamps
MAX_ALIAS = 64  # an alias doubles as the account folder name
MAX_WINDOWS = 100
MAX_USAGE_AGE_S = 30 * 86400
PLAINTEXT_NOTE = ("The export holds full logins in plain text. Keep it private and delete it once imported; two machines using the same "
                  "login both refresh it, so prefer moving an account over copying it")


def _eprint(msg: str) -> None:
    """Notices go to stderr, so stdout carries only the export itself (with -)."""
    print(msg, file=sys.stderr)


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def settings_path(folder: str) -> Path:
    return paths.profile_home(folder) / SETTINGS_REL


# ---------- export ----------
def _exportable(record: dict, num: str) -> tuple[str | None, bytes | None]:
    """(why not, login): only a usable login that belongs to the recorded email leaves this machine."""
    if record.get("kind") == "apikey":
        return "API key accounts are not exported (add the key again on the other machine with agyswap add-token)", None
    blob = read_token_file(paths.token_path(record.get("folder") or num))
    if blob is None:
        return "no login file", None
    if not usable_login(blob):
        return "its login file lacks an email or tokens", None
    holder = login_email(blob)
    if not accounts.is_placeholder(record.get("email")) and holder.lower() != record["email"].lower():
        return f"its login file belongs to {holder}, not {record['email']}", None
    return None, blob


def _write_private(path: Path, data: bytes, force: bool) -> None:
    """Write the logins into a new file next to path that is readable only by the current user from the moment it exists, then rename it
    into place (a rename within the folder keeps its permissions). Without --force the rename fails when the destination exists, so an
    existing file is never replaced."""
    try:
        tmp = fsutil.create_private_file(path.parent, f".{path.name}.", data)
    except OSError as e:
        raise TransferError(f"Could not create a file readable only by you next to {path} ({e.strerror or e}); nothing was exported") from e
    try:
        if force:
            fsutil.replace_with_retry(tmp, path)
        else:
            os.rename(tmp, path)
    except FileExistsError:
        raise TransferError(f"{path} already exists; pass --force to overwrite it") from None
    except OSError as e:
        raise TransferError(f"Could not write {path}: {e.strerror or e}") from e
    finally:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            _eprint(f"Warning: could not delete {tmp}, which holds the exported logins; delete it by hand")


_PTY_PIPE_RE = re.compile(r"\\(?:msys|cygwin)-[0-9a-f]+-pty\d+-", re.IGNORECASE)


def _pipe_name(stream) -> str | None:
    """The name of the named pipe behind stream, or None when it is not a pipe or the name cannot be read."""
    import ctypes
    import msvcrt
    from ctypes import wintypes

    try:
        handle = msvcrt.get_osfhandle(stream.fileno())
    except (AttributeError, OSError, ValueError):
        return None
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.GetFileType.argtypes = [wintypes.HANDLE]
    k32.GetFileType.restype = wintypes.DWORD
    k32.GetFileInformationByHandleEx.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
    k32.GetFileInformationByHandleEx.restype = wintypes.BOOL
    if k32.GetFileType(handle) != 3:  # FILE_TYPE_PIPE
        return None
    buf = ctypes.create_string_buffer(4 + 2 * 1024)  # FILE_NAME_INFO: the name's length in bytes, then the name
    if not k32.GetFileInformationByHandleEx(handle, 2, buf, ctypes.sizeof(buf)):  # FileNameInfo
        return None
    size = min(int.from_bytes(buf.raw[:4], "little"), len(buf) - 4)
    return buf.raw[4:4 + size].decode("utf-16-le", "replace")


def _is_terminal(stream) -> bool:
    """A console, or the pty pipe of mintty (Git Bash, MSYS2, Cygwin), where isatty() is False although the output is on screen."""
    return stream.isatty() or bool(_PTY_PIPE_RE.search(_pipe_name(stream) or ""))


def export_accounts(destination: str, *, account: str | None = None, full: bool = False, force: bool = False) -> None:
    """Export to a file or to stdout ("-"). By default each account carries its login and roster settings; --full adds the account folder's
    agy settings.json. With --account only that account is exported, and a problem with it is an error instead of a skipped row."""
    to_stdout = destination == "-"
    out_path = None if to_stdout else Path(destination).expanduser()
    if to_stdout and _is_terminal(sys.stdout):
        raise TransferError("Refusing to print logins to the terminal; redirect or pipe the export, e.g. agyswap export - | gpg -c > accounts.gpg")
    if out_path is not None and out_path.is_dir():
        raise TransferError(f"export destination must be a file path, not a directory: {out_path}")
    if out_path is not None and out_path.exists() and not force:
        raise TransferError(f"{out_path} already exists; pass --force to overwrite it")
    ctx = load_context()
    data = ctx.data
    if not data or not data["accounts"]:
        raise TransferError("no accounts to export — add one with agyswap login or add first")
    note = sync_active_login(ctx)  # cswap #154, #265: the active account's folder copy is brought up to date first
    if note:
        _eprint(f"agyswap: {note}")
    nums = [accounts.resolve(data, account)] if account is not None else accounts.numbers(data)
    rows = []
    for num in nums:
        record = data["accounts"][num]
        problem, blob = _exportable(record, num)
        if problem:
            if account is not None:
                raise TransferError(f"{label(data, num)} cannot be exported: {problem}")
            _eprint(f"Skipping {label(data, num)}: {problem}")
            continue
        row = {"number": int(num), "email": login_email(blob), "added": record.get("added") or "", "login": _b64(blob)}
        if record.get("alias"):
            row["alias"] = record["alias"]
        row.update({k: record[k] for k in CARRIED if k in record})
        if full:
            agy_settings = read_token_file(settings_path(record.get("folder") or num))
            if agy_settings is not None and len(agy_settings) > MAX_SETTINGS_BYTES:
                _eprint(f"Leaving out the agy settings of {label(data, num)}: larger than import accepts ({MAX_SETTINGS_BYTES} bytes)")
            elif agy_settings is not None:
                row["settings"] = _b64(agy_settings)
        rows.append(row)
    if not rows:
        raise TransferError("no exportable accounts — every account lacks a usable login file; log them in again with agyswap login --slot N")
    active = data.get("activeAccountNumber")
    envelope = {"formatVersion": FORMAT_VERSION, "exportedAt": accounts.timestamp(), "agyswapVersion": __version__,
                "encrypted": False, "full": full,
                "activeAccountNumber": active if active in {r["number"] for r in rows} else None, "accounts": rows}
    text = json.dumps(envelope, ensure_ascii=False, indent=2) + "\n"
    if to_stdout:
        _eprint(f"Warning: {PLAINTEXT_NOTE}.")
        sys.stdout.write(text)
        sys.stdout.flush()
        return
    _write_private(out_path, text.encode("utf-8"), force)
    _eprint(f"Exported {len(rows)} account(s) to {out_path} (readable only by your Windows user)")
    _eprint(PLAINTEXT_NOTE)


# ---------- reading an input ----------
def _read_source(source: str, what: str) -> str:
    hint = "if it is an encrypted backup, decrypt it first (e.g. gpg -d accounts.gpg | agyswap import -)"
    too_big = TransferError(f"{what} is larger than {MAX_INPUT_BYTES // (1024 * 1024)} MB; not reading it")
    if source == "-":
        stream = getattr(sys.stdin, "buffer", None)
        if stream is None:
            text = sys.stdin.read(MAX_INPUT_BYTES + 1)
            if len(text) > MAX_INPUT_BYTES:
                raise too_big
            return text
        raw, where = stream.read(MAX_INPUT_BYTES + 1), "piped input"
    else:
        path = Path(source).expanduser()
        if not path.exists():
            raise TransferError(f"{what} not found: {path}")
        if path.is_dir():
            raise TransferError(f"{what} must be a file path, not a directory: {path}")
        try:
            if path.stat().st_size > MAX_INPUT_BYTES:
                raise too_big
            raw, where = read_bytes(path), str(path)
        except OSError as e:
            raise TransferError(f"could not read {path}: {e.strerror or e}") from e
    if len(raw) > MAX_INPUT_BYTES:
        raise too_big
    try:
        return raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise TransferError(f"{where} is not UTF-8 text; {hint}") from None


def _parse_object(text: str, label_: str) -> dict:
    try:
        parsed = json.loads(text)
    except RecursionError:
        raise TransferError(f"{label_} is nested too deeply") from None
    except ValueError as e:
        raise TransferError(f"{label_} is not valid JSON: {e}") from None
    if not isinstance(parsed, dict):
        raise TransferError(f"{label_} must be a JSON object")
    return parsed


def _decode(value: object, what: str) -> bytes:
    if not isinstance(value, str):
        raise TransferError(f"{what} must be base64 text")
    try:
        return base64.b64decode(value, validate=True)
    except (binascii.Error, ValueError):
        raise TransferError(f"{what} is not valid base64") from None


def _email(value: object, where: str) -> str:
    """An email from an untrusted file: bounded, and without control characters that would reach the terminal and the roster."""
    if not isinstance(value, str) or len(value) > MAX_TEXT or usage._CONTROL_RE.search(value) or not _EMAIL_RE.fullmatch(value.strip()):
        shown = value[:80] if isinstance(value, str) else value
        raise TransferError(f"invalid or missing email in {where}: {shown!r}")
    return value.strip()


# ---------- import ----------
def _validate(envelope: dict) -> list[dict]:
    """Every account is checked before anything is written: a bad account late in the file must not leave earlier ones half imported."""
    version = envelope.get("formatVersion")
    if version != FORMAT_VERSION:
        hint = " (this looks like a claude-swap export; only agyswap exports can be imported)" if "version" in envelope else ""
        raise TransferError(f"unsupported export formatVersion: {version!r} (expected {FORMAT_VERSION}){hint}")
    if envelope.get("encrypted") is True:
        raise TransferError("encrypted exports are not supported — decrypt before importing (e.g. gpg -d accounts.gpg | agyswap import -)")
    rows = envelope.get("accounts")
    if not isinstance(rows, list) or not rows:
        raise TransferError("export file has no accounts to import")
    if len(rows) > MAX_ROWS:
        raise TransferError(f"export file has more than {MAX_ROWS} accounts")
    out, emails, aliases, numbers = [], set(), set(), set()
    for raw in rows:
        if not isinstance(raw, dict):
            raise TransferError("account entry must be a JSON object")
        email = _email(raw.get("email"), "imported account")
        number = raw.get("number")
        if isinstance(number, bool) or not isinstance(number, int) or not 1 <= number <= MAX_SLOT:
            raise TransferError(f"invalid slot number in imported account ({email}): must be 1 to {MAX_SLOT}")
        if raw.get("kind") not in (None, "oauth"):
            raise TransferError(f"{email}: only login accounts can be imported, not {raw.get('kind')!r}")
        blob = _decode(raw.get("login"), f"the login of {email}")
        if not usable_login(blob):
            raise TransferError(f"the login of {email} lacks an email or tokens; nothing was imported")
        if login_email(blob).lower() != email.lower():
            raise TransferError(f"the login recorded as {email} belongs to {login_email(blob)!r}; nothing was imported")
        if len(blob) > CRED_MAX_CREDENTIAL_BLOB_SIZE:
            raise TransferError(f"the login of {email} is larger than Credential Manager allows ({CRED_MAX_CREDENTIAL_BLOB_SIZE} bytes)")
        entry = {"email": email, "number": number, "login": blob, "added": raw.get("added") or accounts.timestamp()}
        if not isinstance(entry["added"], str) or len(entry["added"]) > MAX_TEXT or usage._CONTROL_RE.search(entry["added"]):
            raise TransferError(f"added for {email} must be a timestamp")
        try:
            if raw.get("alias") is not None:
                if not isinstance(raw["alias"], str) or len(raw["alias"]) > MAX_ALIAS:
                    raise ValueError(f"alias must be a string of at most {MAX_ALIAS} characters")
                entry["alias"] = accounts.normalize_alias(raw["alias"])
            if "disabled" in raw:
                if not isinstance(raw["disabled"], bool):
                    raise ValueError("disabled must be true or false")
                if raw["disabled"]:
                    entry["disabled"] = True
            if "threshold" in raw:
                entry["threshold"] = accounts.normalize_threshold(raw["threshold"])
            if "priority" in raw:
                entry["priority"] = accounts.normalize_priority(raw["priority"])
        except (ValueError, OverflowError) as e:
            raise TransferError(f"invalid setting for {email}: {e}") from None
        if raw.get("settings") is not None:
            agy_settings = _decode(raw["settings"], f"the agy settings of {email}")
            if len(agy_settings) > MAX_SETTINGS_BYTES:
                raise TransferError(f"the agy settings of {email} are larger than {MAX_SETTINGS_BYTES} bytes")
            try:
                parsed = json.loads(agy_settings.decode("utf-8-sig"))
            except (UnicodeDecodeError, ValueError, RecursionError):
                parsed = None
            if not isinstance(parsed, dict):
                raise TransferError(f"the agy settings of {email} are not a JSON object")
            entry["settings"] = agy_settings
        for seen, key, what in ((emails, email.lower(), "account"), (numbers, number, "slot number"),
                                (aliases, entry.get("alias"), "alias")):
            if key is None:
                continue
            if key in seen:
                raise TransferError(f"duplicate {what} in export: {key}")
            seen.add(key)
        out.append(entry)
    return out


class _Undo:
    """What import wrote, so a failure later on puts every account folder back as it was (cswap #270)."""

    def __init__(self):
        self.steps = []

    def run(self) -> None:
        for step in reversed(self.steps):
            with contextlib.suppress(OSError):
                step()


def _write_account(folder: str, entry: dict, undo: _Undo) -> str:
    """Write the login through store_login (it keeps a .bak, reads back, and never overwrites another email's or a newer login), then
    the agy settings.json when the folder has none. Returns store_login's result."""
    home, token = paths.profile_home(folder), paths.token_path(folder)
    created = not home.exists()
    previous = None if created else read_token_file(token)

    def restore():
        if created:
            manage.remove_tree(home)
        elif previous is None:
            token.unlink(missing_ok=True)
        elif read_token_file(token) != previous:
            atomic_write_bytes(token, previous)

    if created:
        ensure_private_dir(home)  # store_login never creates a folder
    undo.steps.append(restore)
    result = store_login(folder, entry["login"])
    agy_settings = settings_path(folder)
    # only alongside a login that was written: a kept newer login keeps its folder as it is
    if entry.get("settings") is not None and result in ("saved", "same") and not agy_settings.exists():
        atomic_write_bytes(agy_settings, entry["settings"])
        undo.steps.append(lambda: agy_settings.unlink(missing_ok=True))
    return result


def import_accounts(source: str, *, force: bool = False, include_settings: bool = False) -> None:
    """Import from a file or stdin ("-"). New accounts are added; an existing account (same email) is replaced when it has no usable login or
    needs a new login here, and skipped when healthy unless --force (cswap #136, #138). The active account and Credential Manager are never changed.
    agy settings from an export --full are written only with include_settings: they can make agy start programs."""
    entries = _validate(_parse_object(_read_source(source, "import file"), "export file"))
    carrying = [e for e in entries if e.get("settings") is not None]
    if carrying and not include_settings:
        for entry in carrying:
            del entry["settings"]
        _eprint(f"Ignoring the agy settings of {len(carrying)} account(s); they can make agy start programs. "
                "To import them from an export you made yourself, pass --include-settings")
    elif carrying:
        _eprint("Warning: agy settings can start programs (MCP servers, hooks); they are written into account folders that have none:")
        for entry in carrying:
            keys = json.loads(entry["settings"].decode("utf-8-sig"))
            _eprint(f"  {entry['email']}: {', '.join(json.dumps(k) for k in keys) or '(empty)'}")
    manage._prepare()
    msgs, written, undo = [], [], _Undo()
    counts = {"imported": 0, "overwritten": 0, "replaced": 0, "skipped": 0, "kept": 0}
    with manage._roster(allow_empty=True) as data:
        try:
            for entry in entries:
                email = entry["email"]
                same = [n for n in accounts.numbers(data) if (data["accounts"][n].get("email") or "").lower() == email.lower()]
                if len(same) > 1:
                    raise TransferError(f"{email} is registered under several accounts here (Account-{', Account-'.join(same)}); remove the extra ones first")
                if same and data["accounts"][same[0]].get("kind") == "apikey":
                    raise TransferError(f"{email} is an API key account here; pick another email or remove it first")
                alias = entry.get("alias")
                owner = manage._alias_owner(data, alias, exclude=tuple(same)) if alias else None
                if owner:
                    msgs.append(f"Warning: alias '{alias}' for {email} is already used by Account-{owner}; dropping the imported alias")
                    alias = None
                if same:
                    num = same[0]
                    record = dict(data["accounts"][num])  # a copy: it replaces the roster's record only once the login is written
                    if force:
                        outcome = "overwritten"
                    elif skip_reason(data, num) is not None:
                        outcome = "replaced"
                    else:
                        msgs.append(f"Skipped {email} (already exists, use --force)")
                        counts["skipped"] += 1
                        continue
                    for key, value in (("alias", alias), *((k, entry.get(k)) for k in CARRIED)):
                        if value is not None and key not in record:
                            record[key] = value
                else:
                    num = str(entry["number"]) if str(entry["number"]) not in data["accounts"] else manage._next_number(data)
                    record = {"email": email, "folder": manage._new_folder(data, alias, num), "added": entry["added"]}
                    record.update({k: v for k, v in (("alias", alias), *((k, entry.get(k)) for k in CARRIED)) if v is not None})
                    outcome = "imported"
                folder = record["folder"]
                result = _write_account(folder, entry, undo)
                if result in NOT_SAVED:
                    raise TransferError(f"Not importing {email} into {paths.profile_home(folder)}: {NOT_SAVED[result]}; nothing was imported")
                if result == "folder-newer":
                    msgs.append(f"Kept the login of {email} in slot {num}: the one already here is newer")
                    counts["kept"] += 1
                    continue
                data["accounts"][num] = record
                written.append((num, folder))
                counts[outcome] += 1
                msgs.append({"imported": f"Imported {email} → slot {num}", "overwritten": f"Overwrote {email} (slot {num})",
                             "replaced": f"Replaced {email} (slot {num} had no usable login or needed a new login)"}[outcome])
            if written:
                accounts.save_sequence(data)
        except BaseException:
            undo.run()
            raise
        active = data.get("activeAccountNumber")
    cache = UsageCache()
    for _, folder in written:
        cache.forget(folder)  # cswap #136, #138: the new login is queried afresh, without the old relogin verdict, backoff or error
    for msg in msgs:
        _eprint(msg)
    summary = f"Done: {counts['imported']} imported, {counts['overwritten']} overwritten, {counts['skipped']} skipped"
    for key in ("replaced", "kept"):
        if counts[key]:
            summary += f", {counts[key]} {key}"
    _eprint(summary)
    if active is not None and str(active) in {n for n, _ in written}:
        _eprint(f"Note: Account-{active} is the active account; its imported login is only in its account folder. "
                f"Put it into Credential Manager with: agyswap switch {active} --force")


# ---------- import-usage ----------
def usage_from_json(value: object) -> dict:
    """The usage block of list --json back into the cache's form. Only scoped is read: fiveHour and sevenDay are derived from it, as agyswap
    itself derives them."""
    if not isinstance(value, dict):
        raise ValueError("usage must be a JSON object")
    scoped = value.get("scoped")
    if not isinstance(scoped, list) or not scoped:
        raise ValueError("usage has no scoped windows")
    if len(scoped) > MAX_WINDOWS:
        raise ValueError(f"usage has more than {MAX_WINDOWS} scoped windows")
    rows = []
    for w in scoped:
        if not isinstance(w, dict):
            raise ValueError("each scoped window must be a JSON object")
        name, window, pct, resets_at = w.get("name"), w.get("window"), w.get("pct"), w.get("resetsAt")
        if not isinstance(name, str) or not name.strip() or len(name) > MAX_TEXT or usage._CONTROL_RE.search(name):
            raise ValueError("invalid window name")
        if window not in ("7d", "5h"):
            raise ValueError(f"invalid window {window!r} for {name}")
        # a range check needs no float conversion, which overflows for huge integers; it also rejects NaN and infinity
        if isinstance(pct, bool) or not isinstance(pct, (int, float)) or not 0 <= pct <= 100:
            raise ValueError(f"pct of {name} must be a number from 0 to 100")
        row = {"name": name.strip(), "window": window, "pct": float(pct)}
        if resets_at is not None:
            if not isinstance(resets_at, str) or len(resets_at) > MAX_TEXT or usage._CONTROL_RE.search(resets_at) \
                    or pace.parse_ts(resets_at) is None:
                raise ValueError(f"resetsAt of {name} must be an ISO 8601 timestamp")
            row["resets_at"] = resets_at
        elif w.get("notStarted") is True:
            row["not_started"] = True
        rows.append(row)
    return usage.to_usage(rows)


def import_usage(source: str, *, hold_s: float | None = None) -> None:
    """Adopt readings from another machine's agyswap list --json. Rows with usageStatus ok are matched to local accounts by email; others,
    and accounts not managed here, are skipped. Every row is checked before any is adopted."""
    document = _parse_object(_read_source(source, "usage file"), "usage document")
    if document.get("schemaVersion") != 1:
        raise TransferError(f"unsupported usage document schemaVersion: {document.get('schemaVersion')!r} "
                            "(expected 1, as printed by 'agyswap list --json')")
    rows = document.get("accounts")
    if not isinstance(rows, list):
        raise TransferError("usage document has no accounts list")
    if len(rows) > MAX_ROWS:
        raise TransferError(f"usage document has more than {MAX_ROWS} accounts")
    manage._prepare()
    data = accounts.load_sequence() or accounts.new_sequence()
    readings, who, skipped = {}, {}, 0
    for row in rows:
        if not isinstance(row, dict):
            raise TransferError("each account row must be a JSON object")
        email = _email(row.get("email"), "usage document")
        if row.get("usageStatus") != "ok":
            skipped += 1
            continue
        age = row.get("usageAgeSeconds")
        if isinstance(age, bool) or not isinstance(age, (int, float)) or not 0 <= age <= MAX_USAGE_AGE_S:
            raise TransferError(f"usageAgeSeconds for {email} must be a number from 0 to {MAX_USAGE_AGE_S}")
        try:
            reading = usage_from_json(row.get("usage"))
        except ValueError as e:
            raise TransferError(f"usage for {email}: {e}") from None
        hits = [n for n in accounts.numbers(data) if data["accounts"][n].get("kind") != "apikey"
                and (data["accounts"][n].get("email") or "").lower() == email.strip().lower()]
        if len(hits) != 1:
            if hits:
                raise TransferError(f"{email} is registered under several accounts here (Account-{', Account-'.join(hits)})")
            skipped += 1
            continue
        folder = data["accounts"][hits[0]]["folder"]
        if folder in readings:
            raise TransferError(f"duplicate account in usage document: {email}")
        readings[folder], who[folder] = (reading, float(age)), (email.strip(), hits[0])
    adopted = UsageCache().adopt(readings, hold_s=hold_s)
    for folder, (email, num) in who.items():
        _eprint(f"Adopted usage for {email} → slot {num}" if folder in adopted
                else f"Kept slot {num}'s own reading for {email}: it is newer")
    summary = f"Done: {len(adopted)} adopted, {len(readings) - len(adopted)} kept, {skipped} skipped"
    if hold_s is not None and readings:
        summary += f"; fetching held for up to {min(hold_s, HOLD_CAP_S):.0f}s" if hold_s > 0 else "; holds lifted"
    _eprint(summary)
