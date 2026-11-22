"""The model ids each account offers, from agy models run in the account's own folder, cached per account folder for a day.

agy prints "Fetching available models..." and then one line per model: id, a tab, a label. Accounts differ (the personal Pro account has Gemini 4 and
Claude 5.5 models, a Workspace account does not), so run --auto uses the list to skip an account that lacks the model the call asks for.
"""
from __future__ import annotations

import time
from pathlib import Path

from agyswap import paths, usage
from agyswap.fsutil import atomic_write_json, file_lock, read_json

SCHEMA_VERSION = 1
TTL_S = 86400.0
TIMEOUT_S = 30.0


def parse(text: str) -> list[str]:
    """The model ids in agy models' output. A line without a tab (the header, a sign-in notice) is not a model."""
    ids = []
    for line in (text or "").split("\n"):
        fields = [usage._CONTROL_RE.sub("", x).strip() for x in line.split("\t")]
        if len(fields) >= 2 and fields[0] and not any(c.isspace() for c in fields[0]):
            ids.append(fields[0])
    return ids


def fetch(env: dict, agy: Path | None = None, timeout: float = TIMEOUT_S) -> list[str] | None:
    """The account's model ids, or None when agy could not be run, timed out or printed no model (unknown, never "offers nothing")."""
    captured = usage.capture(env, ["models"], agy, timeout)
    if captured is None or captured[1]:
        return None
    return parse(captured[0]) or None


class ModelCache:
    def __init__(self, path: Path | None = None, clock=time.time):
        self.path = path or paths.models_cache_file()
        self.clock = clock

    def _rows(self) -> dict:
        data = read_json(self.path, {})
        rows = data.get("accounts") if isinstance(data, dict) else None
        return rows if isinstance(rows, dict) else {}

    def get(self, folder: str) -> list[str] | None:
        """The cached ids while younger than TTL_S, else None."""
        row = self._rows().get(folder)
        if not isinstance(row, dict):
            return None
        at, ids = row.get("fetchedAt"), row.get("models")
        if isinstance(at, bool) or not isinstance(at, (int, float)) or not 0 <= self.clock() - at <= TTL_S:
            return None
        clean = [i for i in ids if isinstance(i, str)] if isinstance(ids, list) else []
        return clean or None

    def put(self, folder: str, ids: list[str]) -> None:
        with file_lock(self.path.with_name(".models.lock")):
            rows = self._rows()
            rows[folder] = {"fetchedAt": self.clock(), "models": ids}
            atomic_write_json(self.path, {"schemaVersion": SCHEMA_VERSION, "accounts": rows})


def known(folder: str, *, fetcher=fetch, cache: ModelCache | None = None) -> list[str] | None:
    """The model ids of the account in this folder: from the cache, else from agy (cached when it printed some), else None (unknown).
    shortcut: a failed fetch is not remembered, so each run --auto asks agy again; add a short negative cache if that proves slow."""
    cache = cache or ModelCache()
    ids = cache.get(folder)
    if ids is None:
        ids = fetcher(usage.profile_env(paths.profile_home(folder)))
        if ids:
            cache.put(folder, ids)
    return ids or None
