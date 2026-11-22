"""Querying and parsing agy -p /usage, converted to cswap's usage format.

agy prints tab-separated rows: model category, limit name, remaining NN%, reset time (UTC ISO). cswap's pct is the used share, so pct = 100 - remaining.
The weekly and 5-hour limits of the Gemini category go in seven_day and five_hour; each limit of each category also gets one entry in scoped.
"""
from __future__ import annotations

import os
import re
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from agyswap import paths
from agyswap.errors import AgyswapError
from agyswap.fsutil import ensure_data_root
from agyswap.paths import real_home

TIMEOUT_S = 90.0
AUTH_MARKERS = ("Authentication required",)
_PCT_RE = re.compile(r"(\d+(?:\.\d+)?)%")
# C0 and C1 control characters: text from the remote service must not reach the terminal as escape sequences (window title, OSC 52 clipboard)
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f-\x9f]")


def window_of(limit_name: str) -> str | None:
    """Limit name to 7d or 5h. The name of Pro's 5-hour limit has not been seen yet, so anything containing hour is accepted for now."""
    low = limit_name.lower()
    if "week" in low:
        return "7d"
    if "hour" in low or re.search(r"\b5\s*h\b", low):
        return "5h"
    return None


def parse_lines(text: str) -> list[dict]:
    rows = []
    for line in (text or "").split("\n"):  # not splitlines: it also breaks at control characters such as \x85, which are dropped below
        f = [_CONTROL_RE.sub("", x).strip() for x in line.split("\t")]
        if len(f) < 3:
            continue
        m = _PCT_RE.fullmatch(f[2])
        window = window_of(f[1])
        if not m or not window or not f[0]:
            continue
        remaining = float(m.group(1))
        row = {"name": f[0], "window": window, "pct": max(0.0, min(100.0, 100.0 - remaining))}
        if len(f) > 3 and f[3]:
            row["resets_at"] = f[3]
        rows.append(row)
    return rows


def is_class(category: str) -> bool:
    """A model category (Gemini Models, Claude and GPT models); others such as Gemini 4 Argon are limits of a single model."""
    return "models" in category.lower()


def is_gemini_class(category: str) -> bool:
    return is_class(category) and category.lower().startswith("gemini")


def to_usage(rows: list[dict]) -> dict | None:
    """Parsed rows to {"five_hour","seven_day","scoped"}; no rows at all returns None (it must not read as 0%).
    A single model's limit goes only in scoped and is not merged into the Gemini category."""
    if not rows:
        return None
    usage: dict = {"scoped": [dict(r) for r in rows]}
    for r in rows:
        if is_gemini_class(r["name"]):
            key = "seven_day" if r["window"] == "7d" else "five_hour"
            usage.setdefault(key, {k: v for k, v in r.items() if k in ("pct", "resets_at")})
    return usage


def parse_usage(text: str) -> dict | None:
    return to_usage(parse_lines(text))


def needs_login(text: str) -> bool:
    return any(m in (text or "") for m in AUTH_MARKERS)


def model_id(agy_args: list[str]) -> str | None:
    """The value of --model in agy's arguments (same forms as the single-file version: --model X or --model=X)."""
    for i, a in enumerate(agy_args):
        if a == "--model" and i + 1 < len(agy_args):
            return agy_args[i + 1]
        if a.startswith("--model="):
            return a.split("=", 1)[1]
    return None


def limits_for_model(model: str | None, usage: dict | None) -> list[dict]:
    """The scoped windows this call should look at: the limits of the model's category, plus any single-model limit whose name matches
    (Gemini 4 Argon matches gemini-4-argon-*). With no model given, agy's default Gemini category applies."""
    fam = "claude-gpt" if (model or "").lower().startswith(("claude", "gpt")) else "gemini"
    slug = (model or "").lower()
    out = []
    for w in (usage or {}).get("scoped") or []:
        name = w["name"]
        if is_class(name):
            low = name.lower()
            if (fam == "gemini" and is_gemini_class(name)) or (fam == "claude-gpt" and ("claude" in low or "gpt" in low)):
                out.append(w)
        elif slug:
            prefix = re.sub(r"\s+", "-", name.strip().lower())
            if slug == prefix or slug.startswith(prefix + "-"):
                out.append(w)
    return out


# ---------- querying ----------
def log_activity(message: str) -> None:
    """One INFO line in agyswap.log, without --debug (cswap #434, #590: every agy start for usage is traceable). Never pass token content here.
    shortcut: the file is appended to without rotation; add a size cap if it ever grows large."""
    try:
        ensure_data_root()
        stamp, path = time.strftime("%Y-%m-%d %H:%M:%S"), paths.log_file()
        with open(path, "a", encoding="utf-8") as f:
            f.write(f"{stamp} INFO {message}\n")
    except (OSError, AgyswapError):
        pass



def _inside_profiles(path: str | None) -> bool:
    if not path:
        return False
    root = os.path.normcase(os.path.abspath(paths.profiles_dir()))
    p = os.path.normcase(os.path.abspath(path))
    return p == root or p.startswith(root + os.sep)


def live_env() -> dict:
    """For the active account: the normal environment, reading Credential Manager. The home directory is always reset to the real one: in a terminal opened by agyswap run,
    USERPROFILE points at another account's folder, and running agy without the SSH variables would move that folder's login into Credential Manager."""
    env = dict(os.environ)
    for key in (*paths.SSH_ENV, "GEMINI_API_KEY"):
        env.pop(key, None)
    home = real_home()
    if home:
        env["USERPROFILE"] = env["HOME"] = home
    elif _inside_profiles(env.get("USERPROFILE")) or _inside_profiles(env.get("HOME")):
        raise AgyswapError("Could not find the real home directory and USERPROFILE points at an account folder; not querying the active account's usage")
    return env


def profile_env(home: Path) -> dict:
    env = dict(os.environ)
    env.pop("GEMINI_API_KEY", None)
    env["USERPROFILE"] = env["HOME"] = str(home)
    env.update(paths.SSH_ENV)
    return env


@dataclass(frozen=True)
class FetchResult:
    usage: dict | None = None
    error: str | None = None  # timeout | parse | spawn | auth
    relogin: bool = False


def classify(text: str, timed_out: bool = False) -> FetchResult:
    """A timeout always counts as a timeout: half-printed output may lack a category, and caching it would leave --model decisions without data."""
    if needs_login(text):
        return FetchResult(error="auth", relogin=True)
    if timed_out:
        return FetchResult(error="timeout")
    usage = parse_usage(text)
    return FetchResult(usage=usage) if usage is not None else FetchResult(error="parse")


class Cancelled(AgyswapError):
    """The caller gave up on the query (the dashboard closed or its auto engine stopped); nothing is recorded for it."""


def fetch(env: dict, agy: Path | None = None, timeout: float = TIMEOUT_S, cancel=None) -> FetchResult:
    captured = capture(env, ["-p", "/usage"], agy, timeout, cancel)
    return FetchResult(error="spawn") if captured is None else classify(*captured)


def capture(env: dict, args: list[str], agy: Path | None = None, timeout: float = TIMEOUT_S, cancel=None) -> tuple[str, bool] | None:
    """Run agy once with args and return (its output, whether it timed out), or None when it could not be started. A sign-in URL ends the child
    process at once instead of waiting out agy's own 60 seconds. cancel() returning True ends it the way Ctrl-C does (a short grace, then the
    process tree) and raises Cancelled."""
    cwd = ensure_data_root()
    try:
        proc = subprocess.Popen([str(agy or paths.agy_bin()), *args], env=env, cwd=cwd,
                                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                encoding="utf-8", errors="replace")
    except OSError:
        return None
    chunks: list[str] = []
    auth = threading.Event()

    def pump():
        for line in proc.stdout:
            chunks.append(line)
            if needs_login(line):
                auth.set()

    reader = threading.Thread(target=pump, daemon=True)
    reader.start()
    deadline = time.monotonic() + timeout
    timed_out = False
    try:
        while proc.poll() is None:
            if auth.is_set():
                break
            if time.monotonic() > deadline:
                timed_out = True
                break
            if cancel is not None and cancel():
                raise Cancelled("the usage query was cancelled")
            time.sleep(0.1)
    except (KeyboardInterrupt, Cancelled):
        # Give agy a few seconds to wind down: it may be writing a refreshed token back, and killing it at that moment loses the token (cswap #590)
        _wait(proc, INTERRUPT_GRACE_S)
        raise
    finally:
        if proc.poll() is None:
            _kill_tree(proc)
        _wait(proc, 10)
        reader.join(timeout=5)
        # Closing the pipe while the reader thread is still stuck in readline fights it for the same lock, so close only after it has finished
        if not reader.is_alive():
            proc.stdout.close()
    return "".join(list(chunks)), timed_out


INTERRUPT_GRACE_S = 5.0


def _wait(proc: subprocess.Popen, seconds: float) -> None:
    try:
        proc.wait(timeout=seconds)
    except subprocess.TimeoutExpired:
        pass


def _kill_tree(proc: subprocess.Popen) -> None:
    """End agy's child processes too: while a child holds the stdout pipe, killing only agy leaves the reader thread waiting forever."""
    try:
        subprocess.run([paths.system_exe("taskkill"), "/F", "/T", "/PID", str(proc.pid)], capture_output=True, timeout=15)
    except (OSError, subprocess.TimeoutExpired):
        pass
    try:
        proc.kill()
    except OSError:  # it just exited by itself: TerminateProcess answers access denied
        pass
