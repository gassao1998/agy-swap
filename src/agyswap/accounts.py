"""Reading and writing sequence.json (the account roster) and mappings.json (directory-to-account mappings), and resolving an account identifier.

sequence.json has cswap's shape: {"activeAccountNumber","lastUpdated","sequence":[1,2],"accounts":{"1":{...}}}.
Each account has an extra folder (the account folder name, fixed at creation and not tied to the number, so swap and move never relocate folders).
"""
from __future__ import annotations

import math
import os
import re
from datetime import datetime, timezone
from pathlib import Path

from agyswap import paths
from agyswap.errors import AccountNotFoundError, ConfigError
from agyswap.fsutil import atomic_write_json, file_lock, read_json

PLACEHOLDER_DOMAIN = "agyswap.local"
THRESHOLD_MIN, THRESHOLD_MAX = 50.0, 99.9
PRIORITY_MAX = 1000
_ALIAS_RE = re.compile(r"^[a-z0-9_.-]+$")
_RESERVED = {"con", "prn", "aux", "nul", *(f"com{i}" for i in range(10)), *(f"lpt{i}" for i in range(10))}


def timestamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def placeholder_email(number: int) -> str:
    return f"slot-{number}@{PLACEHOLDER_DOMAIN}"


def is_placeholder(email: str | None) -> bool:
    return not email or email.endswith("@" + PLACEHOLDER_DOMAIN)


# ---------- sequence.json ----------
def new_sequence() -> dict:
    return {"activeAccountNumber": None, "lastUpdated": timestamp(), "sequence": [], "accounts": {}}


def _number_key(key: object) -> bool:
    return isinstance(key, str) and key.isascii() and key.isdecimal() and key == str(int(key)) and int(key) >= 1


def load_sequence() -> dict | None:
    """Return None when there is no roster; a damaged roster raises, so the next write cannot treat it as empty and overwrite it."""
    data = read_json(paths.sequence_file(), None, strict=True)
    if data is None:
        return None
    broken = ConfigError(f"{paths.sequence_file()} has an invalid format; fix or delete it and try again")
    if not isinstance(data, dict) or not isinstance(data.get("accounts"), dict):
        raise broken
    if not all(_number_key(k) and isinstance(v, dict) for k, v in data["accounts"].items()):
        raise broken
    active = data.get("activeAccountNumber")
    if active is not None and (isinstance(active, bool) or not isinstance(active, int)):
        raise broken
    owners: dict[str, str] = {}
    for num, record in data["accounts"].items():
        for key in ("email", "alias", "folder"):
            if key in record and not isinstance(record[key], str):
                raise ConfigError(f"{paths.sequence_file()}: the {key} of account {num} is not text; fix or delete it and try again")
        # A record without a folder would otherwise follow its number through swap and move, onto another account's login
        record["folder"] = record.get("folder") or num
        other = owners.setdefault(record["folder"].casefold(), num)
        if other != num:
            raise ConfigError(f"{paths.sequence_file()}: accounts {other} and {num} share the account folder {record['folder']!r}; "
                              "give each account its own folder and try again")
    data.setdefault("sequence", sorted(int(n) for n in data["accounts"]))
    data.setdefault("activeAccountNumber", None)
    return data


def save_sequence(data: dict) -> None:
    data["lastUpdated"] = timestamp()
    data["sequence"] = sorted(int(n) for n in data["accounts"])
    atomic_write_json(paths.sequence_file(), data)


def numbers(data: dict) -> list[str]:
    return [str(n) for n in sorted(int(k) for k in data.get("accounts", {}))]


def resolve(data: dict | None, identifier: str) -> str:
    """Number, alias or email to the account number (a string). Aliases and emails are case-insensitive."""
    accounts = (data or {}).get("accounts", {})
    ident = (identifier or "").strip()
    if ident.isascii() and ident.isdecimal():
        if str(int(ident)) in accounts:
            return str(int(ident))
        raise AccountNotFoundError(f"Account-{ident} does not exist")
    low = ident.lower()
    for num in numbers(data or {}):
        if (accounts[num].get("alias") or "").lower() == low:
            return num
    hits = [n for n in numbers(data or {}) if (accounts[n].get("email") or "").lower() == low]
    if len(hits) > 1:
        raise ConfigError(f"Email '{ident}' is ambiguous — matches accounts: {', '.join(hits)}. "
                          "Use account number instead (e.g., agyswap switch 1).")
    if hits:
        return hits[0]
    raise AccountNotFoundError(f"No account found with identifier: {ident}")


def normalize_alias(name: str) -> str:
    alias = (name or "").strip().lower()
    if not alias:
        raise ValueError("alias cannot be empty")
    if alias.isdigit():
        raise ValueError(f"alias '{name}' cannot be purely numeric (reserved for slot numbers)")
    if alias.startswith("-"):
        raise ValueError(f"alias '{name}' cannot start with '-' (would be read as a command flag)")
    if not _ALIAS_RE.match(alias):
        raise ValueError(f"alias '{name}' may only contain letters, digits, '-', '_', and '.'")
    # The alias also serves as the account folder name: Windows strips trailing dots and cannot create folders named after reserved devices, and . or .. would escape profiles
    if alias.startswith(".") or alias.endswith(".") or alias.split(".")[0] in _RESERVED:
        raise ValueError(f"alias '{name}' cannot start or end with '.' or be a reserved Windows name")
    return alias


def normalize_threshold(value: object) -> float:
    text = f"threshold must be between {THRESHOLD_MIN:g} and {THRESHOLD_MAX:g}"
    if isinstance(value, bool):
        raise ValueError(text)
    try:
        num = float(value)
    except (TypeError, ValueError):
        raise ValueError(f"{text}, got {value!r}") from None
    if not math.isfinite(num) or not THRESHOLD_MIN <= num <= THRESHOLD_MAX:
        raise ValueError(f"{text}, got {value!r}")
    return num


def normalize_priority(value: object) -> int:
    text = f"priority must be an integer between 0 and {PRIORITY_MAX}"
    if isinstance(value, str):
        value = value.strip()
        if not value.isascii() or not value.isdecimal() or len(value.lstrip("0")) > len(str(PRIORITY_MAX)):
            raise ValueError(text)
        value = int(value)
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= PRIORITY_MAX:
        raise ValueError(text)
    return value


def account_threshold(record: dict) -> float | None:
    """Lenient on read: a hand-damaged value counts as unset."""
    try:
        return normalize_threshold(record["threshold"]) if "threshold" in record else None
    except ValueError:
        return None


# ---------- mappings.json ----------
def normalize_path(p: str | Path) -> str:
    return os.path.normcase(str(Path(p).expanduser().resolve()))


def load_mappings(strict: bool = False) -> dict[str, dict]:
    """Lenient on read; before a write use strict, so a damaged file is never treated as empty and overwritten whole."""
    data = read_json(paths.mappings_file(), {}, strict=strict)
    mappings = data.get("mappings") if isinstance(data, dict) else None
    if strict and not isinstance(mappings, (dict, type(None))):
        raise ConfigError(f"{paths.mappings_file()} has an invalid format; fix or delete it before changing mappings")
    return mappings if isinstance(mappings, dict) else {}


def save_mappings(mappings: dict[str, dict]) -> None:
    atomic_write_json(paths.mappings_file(), {"schemaVersion": 1, "mappings": mappings})


def _mappings_lock():
    return file_lock(paths.mappings_file().with_name(".mappings.lock"))


def set_mapping(path: str | Path, folder: str, email: str) -> None:
    with _mappings_lock():
        mappings = load_mappings(strict=True)
        mappings[normalize_path(path)] = {"folder": folder, "email": email, "added": timestamp()}
        save_mappings(mappings)


def remove_mapping(path: str | Path) -> bool:
    with _mappings_lock():
        mappings = load_mappings(strict=True)
        if mappings.pop(normalize_path(path), None) is None:
            return False
        save_mappings(mappings)
        return True


def resolve_mapping(cwd: str | Path, mappings: dict[str, dict] | None = None) -> tuple[str, dict] | None:
    """The mapping for cwd itself or the nearest ancestor; every candidate lies on the single chain from the root to cwd, so the longest key is the nearest."""
    target = Path(normalize_path(cwd))
    best = None
    for key, entry in (load_mappings() if mappings is None else mappings).items():
        candidate = Path(key)
        if (candidate == target or candidate in target.parents) and (best is None or len(key) > len(best[0])):
            best = (key, entry)
    return best


def number_for_folder(data: dict, folder: str) -> str | None:
    for num, record in data.get("accounts", {}).items():
        if record.get("folder") == folder:
            return num
    return None
