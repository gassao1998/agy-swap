"""Migration from agyswap.py (the single-file version) to v1. Each step is recorded in .migrations.json, so any number of runs gives the same result.

The login file for default is filled in only once Credential Manager is known to hold the personal account: id_token_email belongs to no other account, every other account's email
is known (one that cannot be read cannot be ruled out), and the roster records no other account as active. When the conditions are not all met, only a note is recorded and the next run tries again; nothing is written.
"""
from __future__ import annotations

import re
from typing import Callable

from agyswap import accounts, paths
from agyswap.errors import AgyswapError
from agyswap.fsutil import atomic_write_bytes, atomic_write_json, ensure_private_dir, file_lock, read_bytes, \
    read_json, restrict_to_current_user, sha256_hex
from agyswap.login import LiveLogin, login_email, read_token_file, usable_login

OLD_NAME_RE = re.compile(r"[A-Za-z0-9_-]{1,32}")
DEFAULT = "default"
STEPS = ("v1-old-cache", "v1-old-active", "v1-default-login", "v1-acl", "v1-root-acl")


def _old_layout_present() -> bool:
    b = paths.base()
    return paths.profiles_dir().is_dir() or (b / "usage.json").exists() or (b / "active").exists()


def _old_folders() -> list[str]:
    found = []
    if paths.profiles_dir().is_dir():
        # NTFS is case-insensitive: Default and default are the same folder
        found = sorted(p.name for p in paths.profiles_dir().iterdir()
                       if p.is_dir() and OLD_NAME_RE.fullmatch(p.name) and p.name.lower() != DEFAULT)
    return [DEFAULT] + found


def build_roster(live_email: str | None) -> dict:
    data = accounts.new_sequence()
    used_aliases = set()
    for i, folder in enumerate(_old_folders(), start=1):
        email = login_email(read_token_file(paths.token_path(folder))) or accounts.placeholder_email(i)
        record = {"email": email, "folder": folder, "added": accounts.timestamp()}
        try:
            alias = accounts.normalize_alias(folder)
            if alias not in used_aliases:
                record["alias"] = alias
                used_aliases.add(alias)
        except ValueError:
            pass
        data["accounts"][str(i)] = record
        if live_email and email.lower() == live_email.lower():
            data["activeAccountNumber"] = i
    return data


def _adopt_default_login(data: dict, live: LiveLogin) -> tuple[bool, str | None]:
    """Return (whether this step is finished, a note for the user)."""
    num = accounts.number_for_folder(data, DEFAULT)
    if num is None or paths.token_path(DEFAULT).exists():
        return True, None
    if not live.present or live.email is None:
        return False, None
    if not usable_login(live.cred.blob):
        return False, "The Credential Manager login lacks an email or tokens; not filling in the default login file yet"
    oauth = {n: r for n, r in data["accounts"].items() if r.get("kind") != "apikey"}
    holders = [n for n, r in oauth.items() if (r.get("email") or "").lower() == live.email.lower()]
    if holders and holders != [num]:
        other = data["accounts"][holders[0]]
        label = other.get("alias") or other.get("email")
        return False, (f"Credential Manager currently holds the login of Account-{holders[0]} ({label}); "
                       "the default login file will be filled in once the personal account is logged back in")
    unknown = [n for n, r in oauth.items() if n != num and accounts.is_placeholder(r.get("email"))]
    if unknown:
        return False, (f"The email of Account-{', Account-'.join(unknown)} cannot be read, so it cannot be ruled out as the owner of the Credential Manager login; "
                       "not filling in the default login file yet")
    active = data.get("activeAccountNumber")
    if active is not None and str(active) != num:
        return False, f"The roster records Account-{active} as active; not filling in the default login file yet"
    record = data["accounts"][num]
    if not accounts.is_placeholder(record.get("email")) and record["email"].lower() != live.email.lower():
        return False, f"The Credential Manager login ({live.email}) is not the account recorded for default; not filling in the default login file yet"
    try:
        ensure_private_dir(paths.profile_home(DEFAULT))
    except AgyswapError as e:
        return False, str(e)
    token = paths.token_path(DEFAULT)
    atomic_write_bytes(token, live.cred.blob)
    if sha256_hex(read_bytes(token)) != live.sha:
        token.unlink(missing_ok=True)
        return False, "The default login file did not match when read back after writing; it was deleted and the next run tries again"
    record["email"] = live.email
    if active is None:
        data["activeAccountNumber"] = int(num)
    accounts.save_sequence(data)
    return True, f"Saved the Credential Manager login ({live.email}) as the default login file"


def _all_done() -> bool:
    if not paths.sequence_file().exists():
        return not _old_layout_present()
    done = read_json(paths.migrations_file(), {})
    return isinstance(done, dict) and all(step in done for step in STEPS)


def migrate(read_live: Callable[[], LiveLogin]) -> list[str]:
    """Return the notes to print to stderr. read_live is called only when really needed (CredReadW reads only on demand).
    When everything is done no lock is taken; while steps remain they run under .lock, so two agyswap processes starting for the first time at once cannot overwrite each other."""
    if _all_done():
        return []
    with file_lock(paths.lock_file()):
        return _migrate_locked(read_live)


def _migrate_locked(read_live: Callable[[], LiveLogin]) -> list[str]:
    notes: list[str] = []
    done = read_json(paths.migrations_file(), {})
    done = done if isinstance(done, dict) else {}
    before = dict(done)
    b = paths.base()

    if not paths.sequence_file().exists():
        if not _old_layout_present():
            return notes
        data = build_roster(read_live().email)
        accounts.save_sequence(data)
        done["v1-roster"] = accounts.timestamp()
        names = ", ".join(f"{n} {r.get('alias') or r['folder']}" for n, r in data["accounts"].items())
        notes.append(f"Built the account roster from the old version: {names}")
    data = accounts.load_sequence()
    if data is None:
        return notes

    if "v1-old-cache" not in done:
        (b / "usage.json").unlink(missing_ok=True)
        done["v1-old-cache"] = accounts.timestamp()
    if "v1-old-active" not in done:
        try:
            (b / "active").unlink()
            notes.append("Deleted the old version's active file: it recorded run's default account, a notion the new version does not have")
        except FileNotFoundError:
            pass
        done["v1-old-active"] = accounts.timestamp()
    if "v1-default-login" not in done:
        finished, note = _adopt_default_login(data, read_live())
        if finished:
            done["v1-default-login"] = accounts.timestamp()
            if note:
                notes.append(note)
        elif note and "v1-default-login-noted" not in done:
            done["v1-default-login-noted"] = accounts.timestamp()
            notes.append(note)
    if "v1-acl" not in done:
        homes = [paths.profile_home(r["folder"]) for r in data["accounts"].values() if r.get("folder")]
        if all(restrict_to_current_user(h) for h in homes if h.is_dir()):
            done["v1-acl"] = accounts.timestamp()
    # Data folders made by older versions kept the parent's permissions. Children keep theirs: account folders are locked down on their own,
    # and the rest inherits the new permissions. A data folder owned by another account raises here, so every command stops.
    if "v1-root-acl" not in done and restrict_to_current_user(b, children=False):
        done["v1-root-acl"] = accounts.timestamp()

    if done != before:
        atomic_write_json(paths.migrations_file(), done)
    return notes
