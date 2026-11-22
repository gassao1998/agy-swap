"""settings.json: keys and ranges follow cswap 1.0.0's SETTING_SPECS (MIT); where the defaults differ, the setting's help says so.

Reading is lenient (out-of-range values are clamped, wrong types fall back to the default) and config set is strict (out-of-range values raise); unrecognised keys are kept as they are.
"""
from __future__ import annotations

import difflib
import math
from dataclasses import dataclass

from agyswap import paths
from agyswap.errors import ConfigError
from agyswap.fsutil import atomic_write_json, file_lock, read_json

SCHEMA_VERSION = 1
# next-available is an agyswap extension for auto (cswap's auto has the other four)
STRATEGIES = ("best", "weekly-first", "consume-first", "priority-consume-first", "next-available")
_BOOL_WORDS = {"true": True, "1": True, "yes": True, "false": False, "0": False, "no": False}


@dataclass(frozen=True)
class Spec:
    section: str
    key: str
    kind: str  # float | int | bool | choice | string
    default: object
    lo: float | None = None
    hi: float | None = None
    choices: tuple[str, ...] = ()
    help: str = ""

    @property
    def dotted(self) -> str:
        return f"{self.section}.{self.key}"


SPECS: dict[str, Spec] = {s.dotted: s for s in (
    Spec("autoswitch", "threshold", "float", 90.0, 50.0, 99.9, help="Switch when the 5h window (or any window, if no weekly line) reaches this pct"),
    Spec("autoswitch", "weeklyThreshold", "float", None, 50.0, 99.9, help="Switch line for the 7d and per-model windows (unset: threshold)"),
    Spec("autoswitch", "intervalSeconds", "float", 60.0, 15.0, 3600.0, help="Poll interval for the agyswap auto loop, in seconds"),
    Spec("autoswitch", "cooldownSeconds", "float", 300.0, 0.0, 86400.0, help="Minimum seconds between proactive switches"),
    Spec("autoswitch", "hysteresisPct", "float", 10.0, 0.0, 50.0, help="A target must beat the active account by this many pct of headroom"),
    Spec("autoswitch", "strategy", "choice", "best", choices=STRATEGIES, help="How auto-switch picks the target account"),
    Spec("autoswitch", "includeApiKeyAccounts", "bool", False, help="Allow rotating onto managed API-key accounts"),
    Spec("autoswitch", "unhealthyTicks", "int", 3, 1, 100, help="Consecutive failed polls before an account is unhealthy"),
    Spec("autoswitch", "model", "string", None, help="Also switch on these models' weekly limits (e.g. gemini, claude, or all)"),
    Spec("ui", "theme", "choice", "auto", choices=("dark", "light", "auto"), help="Color theme; auto follows the terminal background"),
    Spec("run", "shareHistory", "bool", False, help="agyswap run shares one chat history across accounts (off until sharing is verified safe)"),
    Spec("updates", "check", "bool", True, help="Run git ls-remote once a day and print a hint when a newer release exists"),
    Spec("usage", "headerProbe", "bool", False, help="Not applicable: this setting is specific to Claude setup-tokens"),
)}
UNSUPPORTED = {"usage.headerProbe": "usage.headerProbe is specific to Claude setup-tokens and does not apply to agyswap"}


def spec(dotted: str) -> Spec:
    if dotted not in SPECS:
        near = difflib.get_close_matches(dotted, list(SPECS), n=1)
        hint = f" (did you mean {near[0]}?)" if near else ""
        raise ConfigError(f"unknown setting '{dotted}'{hint}\nValid keys: {', '.join(SPECS)}")
    return SPECS[dotted]


def _lenient(s: Spec, value: object) -> object:
    if s.kind in ("float", "int"):
        if isinstance(value, bool) or not isinstance(value, (int, float)) or (
                isinstance(value, float) and not math.isfinite(value)):
            return s.default
        clamped = min(max(value, s.lo), s.hi)
        return int(clamped) if s.kind == "int" else float(clamped)
    if s.kind == "bool":
        if isinstance(value, bool):
            return value
        return _BOOL_WORDS.get(value.strip().lower(), s.default) if isinstance(value, str) else s.default
    if s.kind == "string":
        return value if isinstance(value, str) and value else s.default
    return value if value in s.choices else s.default


def coerce(dotted: str, value: object) -> object:
    """A value from the command line, read as leniently as settings.json is (cswap clamps CLI overrides the same way)."""
    return _lenient(spec(dotted), value)


def _raw() -> dict:
    raw = read_json(paths.settings_file(), {})
    return raw if isinstance(raw, dict) else {}


def load() -> dict[str, object]:
    """Effective values for all keys, {dotted: value}."""
    raw = _raw()
    out = {}
    for dotted, s in SPECS.items():
        section = raw.get(s.section)
        out[dotted] = _lenient(s, section[s.key]) if isinstance(section, dict) and s.key in section else s.default
    return out


def is_set(dotted: str) -> bool:
    section = _raw().get(spec(dotted).section)
    return isinstance(section, dict) and spec(dotted).key in section


def rows() -> list[tuple[Spec, object, bool]]:
    """(spec, effective value, set in the file) for every key, read once (cswap's effective_settings)."""
    raw, values = _raw(), load()
    return [(s, values[d], isinstance(raw.get(s.section), dict) and s.key in raw[s.section]) for d, s in SPECS.items()]


def parse_value(s: Spec, text: str) -> object:
    if s.dotted in UNSUPPORTED:
        raise ConfigError(UNSUPPORTED[s.dotted])
    if s.kind == "bool":
        parsed = _BOOL_WORDS.get(text.strip().lower())
        if parsed is None:
            raise ConfigError(f"{s.dotted} expects true or false (or 1/0, yes/no), got {text!r}")
        return parsed
    if s.kind == "choice":
        if text not in s.choices:
            raise ConfigError(f"{s.dotted} must be one of: {', '.join(s.choices)}")
        return text
    if s.kind == "string":
        if not text.strip():
            raise ConfigError(f"{s.dotted} expects a non-empty value; use 'agyswap config unset {s.dotted}' to clear it")
        return text.strip()
    try:
        value = int(text) if s.kind == "int" else float(text)
    except ValueError:
        raise ConfigError(f"{s.dotted} expects {'an integer' if s.kind == 'int' else 'a number'}, got {text!r}") from None
    if not (math.isfinite(value) and s.lo <= value <= s.hi):
        raise ConfigError(f"{s.dotted} must be between {s.lo:g} and {s.hi:g}")
    return value


def _raw_for_write() -> dict:
    raw = read_json(paths.settings_file(), {}, strict=True)
    if not isinstance(raw, dict):
        raise ConfigError(f"{paths.settings_file()} is not a JSON object; fix or delete it before changing settings")
    return raw


def _lock():
    return file_lock(paths.settings_file().with_name(".settings.lock"))


def set_value(dotted: str, text: str) -> object:
    """Write only this one key, without freezing the other keys' defaults into the file (same reason as cswap: a later change of defaults is still picked up)."""
    s = spec(dotted)
    value = parse_value(s, text)
    with _lock():
        raw = _raw_for_write()
        raw.setdefault("schemaVersion", SCHEMA_VERSION)
        section = raw.get(s.section) if isinstance(raw.get(s.section), dict) else {}
        section[s.key] = value
        raw[s.section] = section
        atomic_write_json(paths.settings_file(), raw)
    return value


def unset_value(dotted: str) -> bool:
    s = spec(dotted)
    with _lock():
        raw = _raw_for_write()
        section = raw.get(s.section)
        if not isinstance(section, dict) or s.key not in section:
            return False
        del section[s.key]
        if not section:
            del raw[s.section]
        atomic_write_json(paths.settings_file(), raw)
    return True


def unknown_keys() -> list[str]:
    """Keys in settings.json that nothing reads, each with the closest valid key name (cswap 1.1.0 PR #581: misspelled keys used to be silently ignored).
    Unrecognised sections are kept whole and not reported; only misspelled keys inside known sections are."""
    out = []
    sections = {s.section for s in SPECS.values()}
    for section, body in _raw().items():
        if section not in sections or not isinstance(body, dict):
            continue
        for key in body:
            dotted = f"{section}.{key}"
            if dotted not in SPECS:
                near = difflib.get_close_matches(dotted, list(SPECS), n=1)
                out.append(f"{dotted} (did you mean {near[0]}?)" if near else dotted)
    return out


def format_value(value: object) -> str:
    if value is None:
        return "(none)"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)
