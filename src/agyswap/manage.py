"""Account management: add, login, remove, disable, enable, alias, threshold, priority, swap, move.

The order of checks, what gets refused and the wording of the messages follow cswap 1.0.0's switcher.py add_account, remove_account,
set_account_disabled, set_alias, set_account_threshold, set_account_priority, swap_accounts and
move_account (MIT License, claude-swap). The roster is always read and changed under .lock; the text to print is returned only after every
state-changing step is done (cswap #113), each item being (style, text) with style ok, dim or warn.
"""
from __future__ import annotations

import contextlib
import os
import re
import shutil
import stat
import subprocess
import sys
import time

from agyswap import accounts, migrate, paths, settings, usage
from agyswap.credman import WinCred
from agyswap.errors import AgyswapError, ConfigError
from agyswap.fsutil import atomic_write_bytes, check_owner, current_user_sid, ensure_private_dir, file_lock, is_reparse_point, \
    replace_with_retry
from agyswap.login import login_email, read_live_login, read_token_file, usable_login
from agyswap.state import NOT_SAVED, store_login
from agyswap.switch import NO_ACCOUNTS, label, read_live, skip_reason
from agyswap.usage_store import UsageCache

LOGOUT_NOTE = ("Do not /logout in agy and then log in as another account (it is not known whether the token on the server is revoked too); "
               "always add accounts with agyswap login")
SLOT_CAP = 99
CARRIED = ("alias", "disabled", "threshold", "priority")


def _prepare(store=None) -> None:
    """Migration runs before the lock is taken: it takes .lock itself, so calling it while holding the lock would deadlock. Credential Manager is read only when the migration really needs it."""
    for note in migrate.migrate(lambda: read_live_login(store)):
        print(f"agyswap: {note}", file=sys.stderr)


@contextlib.contextmanager
def _roster(allow_empty: bool = False):
    with file_lock(paths.lock_file()):
        data = accounts.load_sequence()
        if not data or not data["accounts"]:
            if not allow_empty:
                raise ConfigError(NO_ACCOUNTS)
            data = data or accounts.new_sequence()
        yield data


def _alias(name: str | None) -> str | None:
    try:
        return accounts.normalize_alias(name) if name is not None else None
    except ValueError as e:
        raise ConfigError(str(e)) from None


def _alias_owner(data: dict, alias: str, exclude: tuple[str, ...] = ()) -> str | None:
    return next((n for n in accounts.numbers(data) if n not in exclude
                 and (data["accounts"][n].get("alias") or "").lower() == alias), None)


def _next_number(data: dict) -> str:
    return str(max((int(n) for n in data["accounts"]), default=0) + 1)


def _new_folder(data: dict, alias: str | None, num: str) -> str:
    """The name of a new account folder: the alias or slot<N>; a suffix is added when the roster already uses the name or the folder exists."""
    used = {(r.get("folder") or n).lower() for n, r in data["accounts"].items()}
    base = alias or f"slot{num}"
    for i in range(1, 1000):
        name = base if i == 1 else f"{base}-{i}"
        if name.lower() not in used and not paths.profile_home(name).exists():
            return name
    raise AgyswapError(f"Could not find a free account folder name ({base})")


def _same_email(record: dict, email: str) -> bool:
    return record.get("kind") != "apikey" and (record.get("email") or "").lower() == email.lower()


def remove_tree(path) -> None:
    """Delete an account folder. On Windows shutil.rmtree removes only the link itself for junctions inside the tree and does not follow them (cswap #292);
    for a link it refuses to enter (the path itself, or a directory symlink inside) it reports os.path.islink, and only the link is removed.
    Read-only files lose the read-only flag first and are then deleted."""
    def retry(func, p, _exc):
        if func is os.path.islink:
            os.rmdir(p)  # removes the link, never its target; before this a link at the top was silently left in place
            return
        os.chmod(p, stat.S_IWRITE)
        func(p)
    shutil.rmtree(path, onerror=retry)


def _ask(prompt: str) -> bool:
    """When stdin is not a terminal (for example when called from Claude's Bash) the answer is always N, so nothing is deleted by mistake."""
    if not sys.stdin.isatty():
        print("Not running in a terminal; answering N", file=sys.stderr)
        return False
    try:
        return input(prompt).strip().lower() in ("y", "yes")
    except EOFError:
        return False


# ---------- add ----------
def add(*, slot: int | None = None, alias: str | None = None, email: str | None = None, store=None) -> list:
    """Take the current Credential Manager login in as an account; when the same email is already managed, update its login (same as cswap)."""
    store = store or WinCred()
    alias = _alias(alias)
    _prepare(store)
    msgs: list = []
    with _roster(allow_empty=True) as data:
        first = read_live(store)
        if not first.present:
            raise ConfigError(f"Credential Manager holds no agy login; add a new account with agyswap login. {LOGOUT_NOTE}")
        blob = first.cred.blob
        if not usable_login(blob):
            raise AgyswapError("The Credential Manager login lacks an email or tokens and cannot be taken in; log in again in agy and retry")
        holder = login_email(blob)
        if email and email.lower() != holder.lower():
            msgs.append(("warn", f"The email in the login is {holder}, which takes precedence (--email {email} is ignored)"))
        same = [n for n in accounts.numbers(data) if _same_email(data["accounts"][n], holder)]
        if slot is not None:
            target = str(slot)
            occupant = data["accounts"].get(target)
            if occupant is not None and target not in same:
                raise ConfigError(f"Slot {target} is already occupied by {label(data, target)}; run agyswap remove {target} or move it away first")
            moved_from = next((n for n in same if n != target), None)
        else:
            if len(same) > 1:
                raise ConfigError(f"{holder} is registered under several accounts (Account-{', Account-'.join(same)}); remove the extra ones first")
            target, moved_from = (same[0] if same else _next_number(data)), None
        prior = data["accounts"].get(target) or (data["accounts"][moved_from] if moved_from else None)
        carried = {k: prior[k] for k in CARRIED if prior and k in prior}  # cswap PR #358
        if moved_from and target in data["accounts"]:
            carried.update({k: data["accounts"][moved_from][k] for k in CARRIED if k in data["accounts"][moved_from]})
        if alias:
            owner = _alias_owner(data, alias, exclude=(target, moved_from or ""))
            if owner:
                raise ConfigError(f"Alias '{alias}' is already used by account {owner}")
            carried["alias"] = alias
        folder = (prior.get("folder") if prior else None) or _new_folder(data, carried.get("alias"), target)
        if "alias" not in carried and not prior:  # without an alias, use the folder name
            with contextlib.suppress(ConfigError):
                default = _alias(folder)
                if not _alias_owner(data, default):
                    carried["alias"] = default

        home, token = paths.profile_home(folder), paths.token_path(folder)
        created, previous = not home.exists(), read_token_file(token)

        def rollback():
            if created:
                remove_tree(home)
            elif previous is None:
                token.unlink(missing_ok=True)
            elif read_token_file(token) != previous:
                atomic_write_bytes(token, previous)

        try:
            if created:
                ensure_private_dir(home)  # store_login never creates a folder
            result = store_login(folder, blob)
        except (AgyswapError, OSError) as e:
            with contextlib.suppress(OSError):
                rollback()
            raise AgyswapError(f"Saving the login into {home} failed ({e})") from e
        if result == "unreadable":
            raise AgyswapError(f"Cannot read {token} right now, and without knowing what is inside it is not overwritten; try again shortly")
        if result in NOT_SAVED:
            with contextlib.suppress(OSError):
                rollback()
            raise AgyswapError(f"Not saving the login into {home}: {NOT_SAVED[result]}; log the account in again with agyswap login --slot {target}")
        try:
            second = read_live(store)  # cswap #117, #565: if it changed between reading and writing, do not take it in
            if not second.present or second.sha != first.sha:
                raise AgyswapError("The Credential Manager login was replaced while it was being taken in; changes were rolled back and nothing was added; run again")
            if moved_from:
                del data["accounts"][moved_from]
            record = {"email": holder, "folder": folder, "added": (prior or {}).get("added") or accounts.timestamp()}
            record.update(carried)
            data["accounts"][target] = record
            data["activeAccountNumber"] = int(target)
            accounts.save_sequence(data)
        except BaseException:
            with contextlib.suppress(OSError):
                rollback()
            raise
    if moved_from:
        msgs.append(("dim", f"Moved from slot {moved_from} → {target}"))
    if prior and not moved_from:
        msgs.append(("ok", f"Updated the login of Account {target}: {holder}"))
    else:
        msgs.append(("ok", f"Added Account {target}: {holder}"))
    if result == "folder-newer":
        msgs.append(("dim", "The login in the account folder is newer than the Credential Manager one; keeping the folder copy"))
    return msgs


# ---------- add-token (cswap 1.0.0 switcher.add_token: "-" reads one line from stdin, no value prompts with getpass) ----------
APIKEY_NOTE = ("Experimental: agyswap run passes the key to agy as GEMINI_API_KEY, and whether agy honours it is not verified yet. "
               "An API key account can only be run, never switched to")
_EMAIL_RE = re.compile(r"[^@\s]+@[^@\s]+")


def apikey_placeholder(number: str) -> str:
    return f"api-key-{number}@{accounts.PLACEHOLDER_DOMAIN}"


def read_api_key(token: str) -> str:
    if token == "-":
        token = sys.stdin.readline()
    elif not token:
        if not sys.stdin.isatty():  # getpass reads the console directly and would wait unseen
            raise ConfigError("No API key given and not running in a terminal; pass it on stdin with agyswap add-token -")
        import getpass

        token = getpass.getpass("Gemini API key: ")
    key = token.strip()
    if not key:
        raise ConfigError("API key cannot be empty")
    if not (key.isascii() and key.isprintable()) or " " in key:
        raise ConfigError("An API key is one word of printable ASCII characters")
    return key


def add_token(token: str, *, slot: int | None = None, email: str | None = None, store=None) -> list:
    """Store a Gemini API key in a Credential Manager entry of the account's own (agyswap:apikey:<folder>) and register an API key account.
    Re-adding to the same account replaces the key and keeps its alias and other settings."""
    key = read_api_key(token)
    email = email.strip() if email else None
    if email is not None and not _EMAIL_RE.fullmatch(email):
        raise ConfigError(f"Invalid email format: {email}")
    store = store or WinCred()
    _prepare(store)
    with _roster(allow_empty=True) as data:
        target = str(slot) if slot is not None else None
        if target is None and email:
            target = next((n for n in accounts.numbers(data)
                           if (data["accounts"][n].get("email") or "").lower() == email.lower()), None)
        target = target or _next_number(data)
        existing = data["accounts"].get(target)
        if existing is not None and existing.get("kind") != "apikey":
            raise ConfigError(f"{label(data, target)} is a login account, not an API key account; pick another slot or email")
        email = email or (existing or {}).get("email") or apikey_placeholder(target)
        clash = next((n for n in accounts.numbers(data) if n != target
                      and (data["accounts"][n].get("email") or "").lower() == email.lower()), None)
        if clash:
            raise ConfigError(f"{email} is already {label(data, clash)}")
        record = dict(existing) if existing else {"folder": _new_folder(data, None, target), "kind": "apikey",
                                                  "added": accounts.timestamp()}
        if not existing:
            with contextlib.suppress(ConfigError):  # without an alias, use the folder name
                default = _alias(record["folder"])
                if not _alias_owner(data, default):
                    record["alias"] = default
        record["email"] = email
        folder, entry = record["folder"], paths.apikey_target(record["folder"])
        home = paths.profile_home(folder)
        previous, created = store.read(entry), not home.exists()
        try:
            ensure_private_dir(home)  # agy's home for this account under run
            store.write(entry, key.encode("utf-8"), username=paths.APIKEY_USERNAME)
            back = store.read(entry)
            if back is None or back.blob != key.encode("utf-8"):
                raise AgyswapError("The stored API key did not match when read back; nothing was added")
            data["accounts"][target] = record
            accounts.save_sequence(data)
        except BaseException:
            with contextlib.suppress(AgyswapError, OSError):
                if previous is None:
                    store.delete(entry)
                else:
                    store.write(entry, previous.blob, username=previous.username or paths.APIKEY_USERNAME)
            if created:
                with contextlib.suppress(OSError):
                    remove_tree(home)
            raise
    verb = "Updated API key for" if existing else "Added API key"
    return [("ok", f"{verb} Account {target}: {email}"), ("dim", APIKEY_NOTE)]


# ---------- login ----------
def run_agy_login(home) -> int:
    env = usage.profile_env(home)
    try:
        return subprocess.run([str(paths.agy_bin())], env=env, cwd=paths.base()).returncode
    except OSError as e:
        raise AgyswapError(f"Could not start agy ({paths.agy_bin()}): {e.strerror or e}") from e


def login(*, slot: int | None = None, alias: str | None = None, email: str | None = None,
          runner=None, store=None) -> list:
    """Open an interactive agy with the SSH variables inside the account folder to log in, never touching Credential Manager. --slot with an existing account logs that account in again."""
    runner = runner or run_agy_login
    alias = _alias(alias)
    _prepare(store)
    with _roster(allow_empty=True) as data:
        target = str(slot) if slot is not None else _next_number(data)
        existing = data["accounts"].get(target)
        if existing is not None and existing.get("kind") == "apikey":
            raise ConfigError(f"{label(data, target)} is an API key account and cannot use login")
        if alias and _alias_owner(data, alias, exclude=(target,)):
            raise ConfigError(f"Alias '{alias}' is already used by account {_alias_owner(data, alias, exclude=(target,))}")
        folder = (existing.get("folder") or target) if existing else _new_folder(data, alias, target)
        expected = None if existing is None or accounts.is_placeholder(existing.get("email")) else existing["email"]
    home, token = paths.profile_home(folder), paths.token_path(folder)
    created = not home.exists()
    ensure_private_dir(home)
    previous = read_token_file(token)

    def rollback():
        if created:
            remove_tree(home)
        elif previous is None:
            token.unlink(missing_ok=True)
        elif read_token_file(token) != previous:
            atomic_write_bytes(token, previous)

    print(f"Opening an interactive agy in {home}: sign in to the account you want to add with the URL it prints, paste the authorization code back, and leave agy once you are signed in.\n"
          f"{LOGOUT_NOTE}.", file=sys.stderr)
    try:
        runner(home)
        after = read_token_file(token)
        if after is None:
            raise AgyswapError("The account folder has no login file; the login did not succeed")
        if not usable_login(after):
            raise AgyswapError("The login file lacks an email or tokens; the login did not succeed")
        holder = login_email(after)
        if expected and holder.lower() != expected.lower():
            raise AgyswapError(f"Logged in as {holder}, not {expected} as recorded for Account-{target}; the original login was restored")
        if existing is not None and after == previous:
            return [("dim", f"The login file of Account-{target} did not change")]
        with _roster(allow_empty=True) as data:
            dup = next((n for n in accounts.numbers(data) if n != target and _same_email(data["accounts"][n], holder)),
                       None)
            if dup:
                raise ConfigError(f"{holder} is already Account-{dup}; to log it in again use agyswap login --slot {dup}")
            record = data["accounts"].get(target)
            if existing is None and record is not None:
                raise AgyswapError(f"Account-{target} was taken by another command during the login; run again")
            if existing is not None and (record is None or (record.get("folder") or target) != folder):
                raise AgyswapError(f"Account-{target} was removed or moved during the login; run again")
            if record is None:
                record = {"folder": folder, "added": accounts.timestamp()}
                if not alias:  # without an alias, use the folder name
                    with contextlib.suppress(ConfigError):
                        default = _alias(folder)
                        if not _alias_owner(data, default):
                            record["alias"] = default
                data["accounts"][target] = record
            record["email"] = holder
            if alias:
                record["alias"] = alias
            accounts.save_sequence(data)
    except BaseException:
        with contextlib.suppress(OSError):
            rollback()
        raise
    msgs = []
    if email and email.lower() != holder.lower():
        msgs.append(("warn", f"The email in the login is {holder}, which takes precedence (--email {email} is ignored)"))
    verb = "Logged in again as" if existing is not None else "Added"
    msgs.append(("ok", f"{verb} Account {target}: {holder} (account folder {folder})"))
    msgs.append(("dim", "The global login is untouched; to switch to it run agyswap switch " + target))
    return msgs


# ---------- remove ----------
def remove(identifier: str, *, confirm=None, store=None) -> list:
    confirm = confirm or _ask
    _prepare()
    data = accounts.load_sequence()
    if not data or not data["accounts"]:
        raise ConfigError(NO_ACCOUNTS)
    num = accounts.resolve(data, identifier)
    record = data["accounts"][num]
    folder = record.get("folder") or num
    is_active = str(data.get("activeAccountNumber")) == num
    if is_active:
        print(f"Warning: {label(data, num)} is currently active", file=sys.stderr)
    if not confirm(f"Are you sure you want to permanently remove {label(data, num)} and its account folder (login file, chat history)? [y/N] "):
        return [("dim", "Cancelled")]
    home = paths.profile_home(folder)
    trash = paths.profiles_dir() / f".removing-{folder}-{int(time.time())}"
    with _roster() as data:
        if data["accounts"].get(num, {}).get("folder", num) != record.get("folder", num):
            raise AgyswapError("The roster changed while waiting for confirmation; run again")
        moved = home.exists()
        if moved:
            # Rename first, then change the roster: if the roster write fails the rename is undone, and a half-deleted folder never stays behind with the roster pointing at it (cswap #270)
            try:
                replace_with_retry(home, trash)
            except OSError as e:
                raise AgyswapError(f"Could not move {home} ({e.strerror or e}); if an agy is using this account, close it first") from e
        try:
            del data["accounts"][num]
            if is_active:
                data["activeAccountNumber"] = None
            accounts.save_sequence(data)
        except BaseException:
            if moved:
                replace_with_retry(trash, home)
            raise
        # The pre-switch backup may still hold this account's working login
        holder = login_email(read_token_file(paths.last_live_file()))
        if holder and _same_email(record, holder):
            with contextlib.suppress(OSError):
                paths.last_live_file().unlink()
    msgs = [("ok", f"Removed Account-{num} ({record.get('email', '')})")]
    if moved:
        try:
            remove_tree(trash)
        except OSError as e:
            msgs.append(("warn", f"The account folder was not fully deleted ({e.strerror or e}); delete {trash} by hand"))
    for path, entry in accounts.load_mappings().items():
        if entry.get("folder") == folder:
            accounts.remove_mapping(path)
    UsageCache().forget(folder)
    if record.get("kind") == "apikey":
        try:
            (store or WinCred()).delete(paths.apikey_target(folder))
        except (AgyswapError, OSError) as e:
            msgs.append(("warn", f"The stored API key was not deleted ({e}); delete {paths.apikey_target(folder)} in Credential Manager by hand"))
    if is_active:
        msgs.append(("warn", "It was the active account: the Credential Manager login is untouched and agy keeps using it until you switch to another account"))
    return msgs


# ---------- purge (cswap 1.0.0 switcher.purge: list what goes, ask [y/N], leave the current login alone) ----------
# What agyswap creates in its data folder, lock files included (cache\.usage.lock goes with cache). Only these are deleted, so a data folder
# pointed at a shared place by AGYSWAP_HOME keeps everything else. .lock is held during the purge and removed last.
OWN_ENTRIES = ("profiles", "cache", "backup", "sequence.json", "mappings.json", "settings.json", "autoswitch_state.json",
               "pending_switch.json", ".migrations.json", "agyswap.log", "usage.json", "active",
               ".settings.lock", ".mappings.lock", ".autoswitch_state.lock", ".auto.lock", ".lock")
# One of these must be present before anything is deleted: the generic names above alone do not show the folder is agyswap's
MARKERS = ("sequence.json", ".migrations.json")


def _purge_blockers() -> None:
    """Raise when deleting now could lose a login or pull data from under a running agyswap. Called before the prompt and again under .lock."""
    from agyswap import autoswitch, run

    if paths.pending_switch_file().exists():
        raise AgyswapError("The last switch did not finish, and backup\\last-live.bin may hold the only copy of a login; "
                           "finish the switch first (see the warning above), then purge")
    # every account folder is scanned, not only the roster's, so a damaged sequence.json cannot hide a live run
    homes = []
    if paths.profiles_dir().is_dir():
        homes = [p for p in paths.profiles_dir().iterdir() if p.is_dir() and not is_reparse_point(p)]
    live = [p.name for p in homes if run._runner_alive(p / ".run.pid") is not None]
    if live:
        raise AgyswapError(f"agyswap run is still running for: {', '.join(live)}. Exit it first, then purge")
    if autoswitch.loop_running():
        raise AgyswapError("agyswap auto is running; stop it first, then purge")


def _purge_survey(root) -> tuple[list, list, bool]:
    """(the API key entries to delete, the entries present, whether the roster could be read). Links are refused: rmtree would not follow them,
    but a linked profiles or backup folder means the data is not where agyswap put it."""
    try:
        data, readable = accounts.load_sequence() or accounts.new_sequence(), True
    except ConfigError:
        data, readable = accounts.new_sequence(), False  # a damaged roster must not stop a purge; its API key entries cannot be named then
    keys = [paths.apikey_target(r["folder"]) for r in data["accounts"].values() if r.get("kind") == "apikey"]
    present = [root / name for name in OWN_ENTRIES if os.path.lexists(root / name)]
    links = [p.name for p in present if is_reparse_point(p)]
    if links:
        raise AgyswapError(f"{', '.join(links)} in {root} is a junction or symbolic link; remove the link by hand, then purge")
    return keys, present, readable


def purge(*, confirm=None, store=None) -> list:
    """Delete agyswap's own data: the entries above and the agyswap:apikey:* Credential Manager entries of API key accounts.
    gemini:antigravity is never touched, so agy keeps using the account it was last switched to."""
    confirm = confirm or _ask
    root = paths.base()
    if not os.path.lexists(root):
        return [("dim", f"Nothing to purge: {root} does not exist")]
    if is_reparse_point(root) or not root.is_dir():
        raise AgyswapError(f"{root} is a junction, symbolic link or file, not agyswap's data folder; nothing was removed")
    sid = current_user_sid()
    if not sid or not check_owner(root, sid):
        raise AgyswapError(f"Could not confirm that {root} belongs to you; nothing was removed")
    if not any((root / m).is_file() for m in MARKERS):
        raise AgyswapError(f"{root} has neither {' nor '.join(MARKERS)}, so it does not look like agyswap's data folder; nothing was removed")
    _purge_blockers()
    keys, present, readable = _purge_survey(root)
    print(f"This will remove ALL agyswap data from {root}, including every account folder (login files, chat history) and the login backups:",
          file=sys.stderr)
    for path in present:
        print(f"  - {path.name}{os.sep if path.is_dir() else ''}", file=sys.stderr)
    for key in keys:
        print(f"  - Credential Manager entry {key}", file=sys.stderr)
    if not readable:
        print("Warning: sequence.json cannot be read, so the agyswap:apikey:* Credential Manager entries of API key accounts cannot be named; "
              "delete any that remain by hand in Credential Manager", file=sys.stderr)
    print("Note: the Credential Manager login gemini:antigravity is NOT touched; agy keeps using the account it was last switched to.",
          file=sys.stderr)
    if not confirm("Are you sure you want to purge all agyswap data? [y/N] "):
        return [("dim", "Cancelled")]
    msgs, left = [], []
    with file_lock(paths.lock_file()):
        # the prompt may have waited for minutes: a switch, run or auto loop started meanwhile stops the purge before anything is deleted
        _purge_blockers()
        keys, present, _ = _purge_survey(root)
        for key in keys:
            try:
                (store or WinCred()).delete(key)
            except (AgyswapError, OSError) as e:
                msgs.append(("warn", f"Could not delete {key} ({e}); delete it in Credential Manager by hand"))
        for path in present:
            if path.name == ".lock":
                continue
            try:
                if path.is_dir():
                    remove_tree(path)
                else:
                    path.unlink()
            except OSError as e:
                left.append(f"{path} ({e.strerror or e})")
    with contextlib.suppress(OSError):
        paths.lock_file().unlink()
    with contextlib.suppress(OSError):
        root.rmdir()  # only when nothing else is in it
    msgs.insert(0, ("ok", f"Removed agyswap data from {root}"))
    msgs += [("warn", f"Not removed: {item}") for item in left]
    if root.exists() and not left:
        msgs.append(("dim", f"{root} still holds files agyswap did not create; they were left alone"))
    return msgs


# ---------- disable, enable ----------
def set_disabled(identifier: str, disabled: bool) -> list:
    _prepare()
    with _roster() as data:
        num = accounts.resolve(data, identifier)
        record = data["accounts"][num]
        word = "disabled" if disabled else "enabled"
        if bool(record.get("disabled")) == disabled:
            return [("dim", f"{label(data, num)} is already {word}.")]
        if disabled:
            record["disabled"] = True
        else:
            record.pop("disabled", None)
        accounts.save_sequence(data)
        left = [n for n in accounts.numbers(data) if not data["accounts"][n].get("disabled")
                and skip_reason(data, n) is None]
    msgs = [("ok", f"{word.capitalize()} {label(data, num)}.")]
    if not disabled:
        return msgs + [("dim", "  It is back in the rotation.")]
    if str(data.get("activeAccountNumber")) == num:
        msgs.append(("dim", "  It is the active account — it stays live until you switch away; it just won't be an automatic switch target."))
    if not left:
        msgs.append(("warn", "  No accounts remain in rotation — auto-switch and bare switch have nothing to pick. Re-enable one with agyswap enable <num|email|alias>."))
    return msgs


# ---------- alias ----------
def list_aliases() -> list:
    _prepare()
    data = accounts.load_sequence() or accounts.new_sequence()
    rows = [(n, data["accounts"][n]) for n in accounts.numbers(data) if data["accounts"][n].get("alias")]
    if not rows:
        return [("dim", "No aliases set")]
    return [("ok", "Aliases:")] + [("ok", f"  {n}: {r['alias']} ({r.get('email', '')})") for n, r in rows]


def set_alias(identifier: str, name: str | None) -> list:
    """A name of None means clear it. An alias never changes the account folder name (fixed when the folder was created)."""
    alias = _alias(name)
    _prepare()
    with _roster() as data:
        num = accounts.resolve(data, identifier)
        record = data["accounts"][num]
        if alias is None:
            if "alias" in record:
                del record["alias"]
                accounts.save_sequence(data)
            return [("ok", f"Removed alias for Account {num}")]
        owner = _alias_owner(data, alias, exclude=(num,))
        if owner:
            raise ConfigError(f"Alias '{alias}' is already used by account {owner}")
        record["alias"] = alias
        accounts.save_sequence(data)
    return [("ok", f"Set alias '{alias}' for Account {num}")]


# ---------- threshold, priority ----------
def list_thresholds() -> list:
    _prepare()
    data = accounts.load_sequence() or accounts.new_sequence()
    msgs = [("ok", "Switch-away thresholds:"), ("ok", f"  global default  {settings.load()['autoswitch.threshold']:g}%")]
    rows = [(n, accounts.account_threshold(data["accounts"][n])) for n in accounts.numbers(data)]
    rows = [(n, v) for n, v in rows if v is not None]
    if not rows:
        return msgs + [("dim", "  No per-account overrides set")]
    return msgs + [("ok", f"  Account-{n}  {v:g}%") for n, v in rows]


def set_threshold(identifier: str, value: str | None) -> list:
    try:
        normalized = None if value is None else accounts.normalize_threshold(value)
    except ValueError as e:
        raise ConfigError(str(e)) from None
    _prepare()
    with _roster() as data:
        num = accounts.resolve(data, identifier)
        record, who = data["accounts"][num], label(data, num)
        if normalized is None and "threshold" not in record:
            return [("dim", f"{who} has no threshold override — already using the global default.")]
        if normalized is not None and record.get("threshold") == normalized:
            return [("dim", f"{who} is already at {normalized:g}%.")]
        if normalized is None:
            record.pop("threshold", None)
        else:
            record["threshold"] = normalized
        accounts.save_sequence(data)
    if normalized is None:
        return [("ok", f"Cleared threshold for {who} — now using the global default.")]
    return [("ok", f"Set {who} threshold to {normalized:g}%.")]


def list_priorities() -> list:
    _prepare()
    data = accounts.load_sequence() or accounts.new_sequence()
    rows = [("ok", f"  Account-{n}  {data['accounts'][n].get('email', '')}  {data['accounts'][n].get('priority', 0)}")
            for n in accounts.numbers(data)]
    return [("ok", "Account priorities (higher first; default 0):")] + rows + [
        ("dim", "Used only by: agyswap auto --strategy priority-consume-first")]


def set_priority(identifier: str, value: str | None) -> list:
    try:
        normalized = 0 if value is None else accounts.normalize_priority(value)
    except ValueError as e:
        raise ConfigError(str(e)) from None
    _prepare()
    with _roster() as data:
        num = accounts.resolve(data, identifier)
        record = data["accounts"][num]
        changed = record.get("priority", 0) != normalized
        if normalized:
            record["priority"] = normalized
        else:
            changed = "priority" in record
            record.pop("priority", None)
        if changed:
            accounts.save_sequence(data)
        who = label(data, num)
    return [("ok", f"{who} priority: {normalized} (used by priority-consume-first).")]


# ---------- map, unmap (cswap 1.0.0 cli._map_command, _unmap_command, switcher.list_mappings) ----------
def map_directory(identifier: str, path: str | None) -> list:
    _prepare()
    data = accounts.load_sequence()
    if not data or not data["accounts"]:
        raise ConfigError(NO_ACCOUNTS)
    num = accounts.resolve(data, identifier)
    record = data["accounts"][num]
    target = path or os.getcwd()
    msgs = []
    if not os.path.isdir(target):
        msgs.append(("warn", f"Warning: {target} is not an existing directory (mapping it anyway)"))
    shown = accounts.normalize_path(target)
    previous = accounts.load_mappings().get(shown)
    accounts.set_mapping(target, record["folder"], record.get("email", ""))
    was = ""
    if isinstance(previous, dict) and previous.get("folder") != record["folder"]:
        was = f" (was {previous.get('email', '')})"
    return msgs + [("ok", f"Mapped {shown} → Account-{num} ({record.get('email', '')}){was}")]


def list_mappings() -> list:
    _prepare()
    data = accounts.load_sequence() or accounts.new_sequence()
    mappings = accounts.load_mappings()
    if not mappings:
        return [("dim", "No directory mappings yet."), ("dim", "Map one with: agyswap map <num|email|alias> [PATH]")]
    msgs = [("ok", "Directory mappings:")]
    for path in sorted(mappings):
        entry = mappings[path] if isinstance(mappings[path], dict) else {}
        num = accounts.number_for_folder(data, entry.get("folder") or "")
        if num:
            msgs.append(("ok", f"  {path} → {num}: {data['accounts'][num].get('email', '')}"))
        else:
            msgs.append(("ok", f"  {path} → {entry.get('email', '')} (account removed)"))
    return msgs


def unmap_directory(path: str | None) -> list:
    target = path or os.getcwd()
    shown = accounts.normalize_path(target)
    if accounts.remove_mapping(target):
        return [("ok", f"Unmapped {shown}")]
    return [("dim", f"No mapping for {shown}")]


# ---------- swap, move ----------
def _swap_locked(data: dict, a: str, b: str) -> None:
    """Account folders, directory mappings and the usage cache all follow the folder name, so only the roster numbers need exchanging."""
    acc = data["accounts"]
    acc[a], acc[b] = acc[b], acc[a]
    active = data.get("activeAccountNumber")
    if active is not None and str(active) in (a, b):
        data["activeAccountNumber"] = int(b if str(active) == a else a)
    accounts.save_sequence(data)


def _pair_lines(data: dict, a: str, b: str) -> list:
    return [("ok", f"Swapped Account {a} and Account {b}:")] + [
        ("ok", f"  {n}: {data['accounts'][n].get('email', '')}") for n in sorted((a, b), key=int)]


def swap(first: str, second: str) -> list:
    _prepare()
    with _roster() as data:
        a, b = accounts.resolve(data, first), accounts.resolve(data, second)
        if a == b:
            raise ConfigError("Cannot swap an account with itself")
        _swap_locked(data, a, b)
    return _pair_lines(data, a, b)


def move(identifier: str, slot: str) -> list:
    text = (slot or "").strip()
    if not (text.isascii() and text.isdecimal()) or int(text) < 1:
        raise ConfigError(f"Target slot must be a positive slot number, got: {slot!r} (use `swap` to trade two accounts by identifier)")
    target = str(int(text))
    _prepare()
    with _roster() as data:
        src = accounts.resolve(data, identifier)
        cap = max(SLOT_CAP, *(int(n) for n in data["accounts"]))
        if int(target) > cap:
            raise ConfigError(f"Target slot {target} is out of range (1-{cap}): new accounts are numbered from the highest slot, so a large target would inflate future account numbers")
        if src == target:
            return [("dim", f"Already in slot {target}: {data['accounts'][src].get('email', '')}")]
        if target in data["accounts"]:
            _swap_locked(data, src, target)
            return _pair_lines(data, src, target)
        data["accounts"][target] = data["accounts"].pop(src)
        if str(data.get("activeAccountNumber")) == src:
            data["activeAccountNumber"] = int(target)
        accounts.save_sequence(data)
    return [("ok", f"Moved {data['accounts'][target].get('email', '')} to slot {target}")]
