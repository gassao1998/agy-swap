"""agyswap run: launch agy as one account in this terminal only.

The child always runs isolated, the active account included: USERPROFILE and HOME point at the account folder, and the SSH variables make agy
read that folder's login file instead of Credential Manager, so a later switch never pulls the login out from under a running agy.
Resolving the account (the argument, else the nearest mapped ancestor directory, else the active account), keeping agyswap's own notices on
stderr so stdout carries only agy's output, and mirroring agy's exit code follow cswap 1.2's cli._run_command and session.SessionManager.run
(MIT License, claude-swap). run --auto is an agyswap extension: it picks the account for this one call and never switches globally.
"""
from __future__ import annotations

import os
import subprocess
import sys

from agyswap import accounts, models, paths, procs, usage
from agyswap.credman import WinCred
from agyswap.errors import AgyswapError, ConfigError
from agyswap.fsutil import atomic_write_json, ensure_private_dir, file_lock, read_json
from agyswap.login import login_email, read_live_login, read_token_file, usable_login
from agyswap.printer import warning
from agyswap.state import Context, collect, load_context, sync_active_login
from agyswap.switch import NO_ACCOUNTS, label, skip_reason


def _info(text: str) -> None:
    print(f"agyswap: {text}", file=sys.stderr)


def _warn(text: str) -> None:
    warning(f"agyswap: {text}", file=sys.stderr)


# ---------- which account ----------
def resolve_target(ctx: Context, identifier: str | None, cwd: str, *, quiet: bool = False) -> str:
    data = ctx.data
    if not data or not data["accounts"]:
        raise ConfigError(NO_ACCOUNTS)
    if identifier is not None:
        return accounts.resolve(data, identifier)
    found = accounts.resolve_mapping(cwd)
    if found:
        path, entry = found
        entry = entry if isinstance(entry, dict) else {}
        num = accounts.number_for_folder(data, entry.get("folder") or "")
        if num is not None:
            return num
        _warn(f"the account mapped to {path} ({entry.get('email', '')}) no longer exists; using the active account")
    elif not quiet:
        _info(f"no account mapped for {cwd}; using the active account")
    active = ctx.active_number
    if active is None:
        raise ConfigError("No account given, none is mapped to this directory, and there is no active account; "
                          "run agyswap run <num|email|alias>")
    return active


def api_key(store, folder: str) -> str | None:
    cred = store.read(paths.apikey_target(folder))
    key = cred.blob.decode("utf-8", "replace").strip() if cred is not None else ""
    return key or None


# ---------- run --auto ----------
def class_label(model: str | None) -> str:
    if (model or "").lower().startswith(("claude", "gpt")):
        return "Claude and GPT"
    return f"Gemini ({model})" if model else "Gemini"


def class_state(value, model: str | None) -> str:
    """For the limits this call's model uses: skip (the account cannot run), exhausted, ok, or unknown (no data, so it is not ruled out)."""
    if value in ("no_credentials", "relogin_required"):
        return "skip"
    if not isinstance(value, dict):
        return "unknown"
    limits = usage.limits_for_model(model, value)
    if not limits:
        return "unknown"
    return "exhausted" if any(w["pct"] >= 100 for w in limits) else "ok"


def lacks_model(record: dict, model: str | None, models_fetcher=None) -> bool:
    """True only when the account's model list is known and has no id equal to model (case-insensitive, as agy matches). Unknown never rules out:
    no model named, an API key account, no login file, or a list that could not be fetched."""
    if not model or record.get("kind") == "apikey" or not paths.token_path(record["folder"]).is_file():
        return False
    ids = models.known(record["folder"], **({"fetcher": models_fetcher} if models_fetcher else {}))
    return ids is not None and model.lower() not in {i.lower() for i in ids}


def pick_auto(ctx: Context, start: str, model: str | None, *, store, fetcher=None, models_fetcher=None) -> str:
    """Start from start and walk the sequence; the first account whose model class has headroom wins. Usage comes from the shared cache and is
    queried only when due, one account at a time and only as far as needed. When the call names a model, an account that does not offer it is
    skipped (the lists come from agy models, cached). When no account is left, start runs anyway."""
    data = ctx.data
    nums = accounts.numbers(data)
    i = nums.index(start)
    include_keys = bool(ctx.settings.get("autoswitch.includeApiKeyAccounts"))
    kwargs = {"fetcher": fetcher} if fetcher else {}
    missing = start_lacks = False
    for num in nums[i:] + nums[:i]:
        record = data["accounts"][num]
        if num != start:
            if record.get("disabled"):
                continue
            if record.get("kind") == "apikey":
                if not include_keys or api_key(store, record["folder"]) is None:
                    continue
            elif skip_reason(data, num) is not None:
                continue
        if lacks_model(record, model, models_fetcher):
            _info(f"{label(data, num)} does not offer {model}; skipping it")
            missing = True
            start_lacks = start_lacks or num == start
            continue
        state = class_state(collect(ctx, [num], **kwargs)[num].decision_value(), model)
        if state in ("ok", "unknown"):
            if num != start:
                why = "does not offer it" if start_lacks else f"has no {class_label(model)} quota left"
                _info(f"{label(data, start)} {why}; this run uses {label(data, num)}")
            return num
    if missing:
        _warn(f"no account that offers {model} has quota left; running {label(data, start)} anyway")
    else:
        _warn(f"no account has {class_label(model)} quota left; running {label(data, start)} anyway")
    return start


# ---------- .run.pid ----------
def _runner_alive(path) -> int | None:
    data = read_json(path, None)
    if not isinstance(data, dict):
        return None
    pid, started = data.get("pid"), data.get("started")
    if not isinstance(started, int) or isinstance(started, bool):
        return None
    return pid if procs.process_started(pid) == started else None


def claim(folder: str) -> tuple[bool, int | None]:
    """Write .run.pid under .lock (cswap #508). Returns (whether this run owns the file, the PID of another live run of this account)."""
    path = paths.run_pid_file(folder)
    with file_lock(paths.lock_file()):
        other = _runner_alive(path)
        if other is not None and other != os.getpid():
            return False, other
        atomic_write_json(path, {"pid": os.getpid(), "started": procs.process_started(os.getpid()),
                                 "at": accounts.timestamp()})
    return True, None


def release(folder: str) -> None:
    path = paths.run_pid_file(folder)
    try:
        with file_lock(paths.lock_file()):
            data = read_json(path, None)
            if isinstance(data, dict) and data.get("pid") == os.getpid():
                path.unlink(missing_ok=True)
    except (AgyswapError, OSError):
        pass  # a stale file is harmless: the next run sees its process is gone


# ---------- launching ----------
def launch(argv: list[str], env: dict) -> int:
    """stdout and stderr are inherited, so agy's output reaches the caller untouched (cswap #337)."""
    try:
        proc = subprocess.Popen(argv, env=env)
    except OSError as e:
        raise AgyswapError(f"Could not start agy ({argv[0]}): {e.strerror or e}") from e
    while True:
        try:
            return proc.wait()
        except KeyboardInterrupt:
            # Ctrl+C reaches agy too (same console) and agy decides whether to exit; killing it mid-refresh could lose the token (cswap #590)
            continue


def child_env(store, record: dict, num: str, data: dict) -> dict:
    folder, who = record["folder"], label(data, num)
    home = paths.profile_home(folder)
    if record.get("kind") == "apikey":
        key = api_key(store, folder)
        if key is None:
            raise ConfigError(f"{who} has no stored API key; run agyswap add-token --slot {num}")
        if not home.is_dir():
            ensure_private_dir(home)
        env = usage.profile_env(home)
        env["GEMINI_API_KEY"] = key  # experimental: whether agy honours it is not verified yet
        return env
    blob = read_token_file(paths.token_path(folder))
    if blob is None:
        raise AgyswapError(f"{who} has no login file (or it cannot be read right now); run agyswap login --slot {num} to log in again")
    if not usable_login(blob):
        raise AgyswapError(f"The login file of {who} lacks an email or tokens; run agyswap login --slot {num} to log in again")
    holder = login_email(blob)
    if not accounts.is_placeholder(record.get("email")) and holder.lower() != record["email"].lower():
        raise AgyswapError(f"The login file of {who} belongs to {holder}, not {record['email']} as recorded in the roster; "
                           f"log in again with the right account (agyswap login --slot {num})")
    return usage.profile_env(home)


def run(identifier: str | None, agy_args: list[str], *, auto: bool = False, require_session: bool = False,
        share_history: bool | None = None, cwd: str | None = None, store=None, fetcher=None, launcher=None, models_fetcher=None) -> int:
    """Returns agy's exit code. Every notice goes to stderr."""
    store = store or WinCred()
    # Who is active comes from Credential Manager and the roster, never from USERPROFILE or HOME, so a run inside a run terminal resolves the same way (cswap #381, #515, #206)
    ctx = load_context(read_live=lambda: read_live_login(store))
    note = sync_active_login(ctx)  # the active account's folder gets the latest login before agy uses it
    if note:
        _warn(note)
    start = resolve_target(ctx, identifier, cwd or os.getcwd(), quiet=auto)
    num = pick_auto(ctx, start, usage.model_id(agy_args), store=store, fetcher=fetcher, models_fetcher=models_fetcher) if auto else start
    data = ctx.data
    if require_session and num == ctx.active_number:
        raise ConfigError(f"{label(data, num)} is the active account (the global login); --require-session refuses to run it. "
                          "Switch the global login to another account first, or run agy directly")
    record = data["accounts"][num]
    env = child_env(store, record, num, data)
    wanted = share_history if share_history is not None else ctx.settings.get("run.shareHistory")
    if wanted:
        _info("sharing settings and chat history is not implemented yet (not verified safe yet); this run uses the account's own folder")
    try:
        owned, other = claim(record["folder"])
    except AgyswapError as e:
        owned, other = False, None
        _warn(f"could not check for another run of this account ({e})")
    if other is not None:
        _warn(f"{label(data, num)} already has an agyswap run in progress (PID {other}); both agy processes refresh the same login file")
    _info(f"running {label(data, num)} in its account folder (this terminal only)")
    try:
        return (launcher or launch)([str(paths.agy_bin()), *agy_args], env)
    finally:
        if owned:
            release(record["folder"])
