"""upgrade and the daily update check (cswap PR #48, #480, #491).

agyswap runs from its own git checkout (uv tool install --editable), so where cswap 1.0.0's update_check.py (MIT License, claude-swap) asks
PyPI, this asks the checkout's origin with git ls-remote at most once a day, and upgrade runs git pull --ff-only. git is started only by an
absolute path found without looking in the current directory, always inside the checkout, and never through a shell. The check stays silent
whenever anything fails.
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
import time
from pathlib import Path

from agyswap import paths, settings
from agyswap.fsutil import atomic_write_json, is_reparse_point, read_json, sha256_hex
from agyswap.printer import accent, error, warning

CACHE_TTL = 24 * 3600  # a successful lookup is kept for a day
FAILURE_TTL = 3600  # a failed one is retried after an hour
_SHA_RE = re.compile(r"[0-9a-f]{40}")
_NAME_RE = re.compile(r'(?m)^name\s*=\s*"agyswap"\s*$')


def checkout() -> Path | None:
    """The git checkout this agyswap runs from, or None when it is not one (then upgrade cannot help)."""
    root = Path(__file__).resolve().parents[2]
    try:
        project = (root / "pyproject.toml").read_text(encoding="utf-8")
    except OSError:
        return None
    # a real .git folder: an empty or broken one would let git climb to a parent repository, and a .git file or link points elsewhere
    git_dir = root / ".git"
    real = git_dir.is_dir() and not is_reparse_point(git_dir) and (git_dir / "HEAD").is_file()
    return root if real and _NAME_RE.search(project) else None


def _local_dir(d: str) -> bool:
    """An absolute path on a drive letter: not relative (the current directory) and not a UNC share another machine or user controls."""
    drive = os.path.splitdrive(d)[0]
    return len(drive) == 2 and drive[1] == ":" and drive[0].isalpha() and os.path.isabs(d)


def find_git() -> str | None:
    """git.exe from the Git for Windows folders, else from a local, drive-qualified PATH entry. The install folders come first, since an
    earlier PATH entry may be a folder other users can write to. shutil.which is not used: on Windows it searches the current directory
    first even when given a PATH, which would run a planted git.exe."""
    dirs = [os.path.join(base, "Git", "cmd") for base in (os.environ.get("ProgramW6432"), os.environ.get("ProgramFiles"),
                                                           os.path.join(paths.local_appdata(), "Programs")) if base]
    dirs += [d.strip().strip('"') for d in os.environ.get("PATH", "").split(os.pathsep)]
    for d in dirs:
        candidate = os.path.join(d, "git.exe")
        if _local_dir(d) and os.path.isfile(candidate):
            return candidate
    return None


def _git(git: str, root: Path, *args: str, timeout: float = 30) -> subprocess.CompletedProcess:
    # inherited GIT_* variables (GIT_DIR, GIT_CONFIG_*, GIT_SSH_COMMAND...) would point git at another repository or run other programs
    env = {k: v for k, v in os.environ.items() if not k.upper().startswith("GIT_") and k.upper() != "SSH_ASKPASS"}
    # no prompt or sign-in window: the background check must stay silent; an upgrade that needs a sign-in fails and git says why
    env.update(GIT_TERMINAL_PROMPT="0", GCM_INTERACTIVE="never", GIT_CEILING_DIRECTORIES=str(root.parent))
    return subprocess.run([git, "-C", str(root), *args], cwd=str(root), env=env, stdin=subprocess.DEVNULL,
                          capture_output=True, encoding="utf-8", errors="replace", timeout=timeout)


def _head(git: str, root: Path) -> str | None:
    result = _git(git, root, "rev-parse", "HEAD")
    sha = result.stdout.strip()
    return sha if result.returncode == 0 and _SHA_RE.fullmatch(sha) else None


def _remote_head(git: str, root: Path) -> str | None:
    result = _git(git, root, "ls-remote", "origin", "HEAD", timeout=10)
    sha = (result.stdout.split() or [""])[0]
    return sha if result.returncode == 0 and _SHA_RE.fullmatch(sha) else None


def check_for_update() -> str | None:
    """A one-line notice when origin has commits this checkout lacks, else None. With updates.check off, git is never started (cswap #480)."""
    try:
        if not settings.load()["updates.check"]:
            return None
        root, git = checkout(), find_git()
        if root is None or git is None:
            return None
        cache, now = read_json(paths.update_check_file(), {}), time.time()
        cache = cache if isinstance(cache, dict) else {}
        checked, remote = cache.get("checkedAt"), cache.get("remote")
        ttl = CACHE_TTL if remote else FAILURE_TTL
        if isinstance(checked, bool) or not isinstance(checked, (int, float)) or not 0 <= now - checked < ttl:
            remote = _remote_head(git, root)
            atomic_write_json(paths.update_check_file(), {"checkedAt": now, "remote": remote})
        if not isinstance(remote, str) or not _SHA_RE.fullmatch(remote):
            return None
        local = _head(git, root)
        # a commit origin has that this checkout already contains (or a local commit ahead of origin) is not an update
        if local is None or local == remote or _git(git, root, "merge-base", "--is-ancestor", remote, "HEAD").returncode == 0:
            return None
        return (f"A newer agyswap is available on origin ({remote[:7]}; this checkout is at {local[:7]}). "
                "Run `agyswap upgrade` to update.")
    except Exception:
        return None


def _file_sha(path: Path) -> str | None:
    try:
        return sha256_hex(path.read_bytes())
    except OSError:
        return None


def reinstall_command(root: Path) -> str:
    parts = [p.lower() for p in Path(sys.prefix).parts]
    if ("uv", "tools") in zip(parts, parts[1:]):
        return subprocess.list2cmdline(["uv", "tool", "install", "--editable", str(root), "--force"])
    return subprocess.list2cmdline([sys.executable, "-m", "pip", "install", "-e", str(root)])


def run_upgrade() -> int:
    """git pull --ff-only in the checkout, then report before -> after (a pull can succeed without changing anything, cswap #491).
    When pyproject.toml changed, the installed tool may need a reinstall: the running agyswap.exe is locked on Windows, so the command
    is printed for a new terminal instead of run here (cswap PR #48)."""
    root = checkout()
    if root is None:
        error("agyswap: this agyswap is not running from its git checkout, so upgrade cannot update it; reinstall it by hand")
        return 1
    git = find_git()
    if git is None:
        error("agyswap: git was not found on PATH or in the Git for Windows folders; install Git or run git pull in the checkout by hand")
        return 1
    project = root / "pyproject.toml"
    try:
        before, project_before = _head(git, root), _file_sha(project)
        result = _git(git, root, "pull", "--ff-only", timeout=300)
    except (OSError, subprocess.TimeoutExpired) as e:
        error(f"agyswap: git pull did not finish ({e})")
        return 1
    if result.stdout.strip():
        print(result.stdout.rstrip())
    if result.stderr.strip():
        print(result.stderr.rstrip(), file=sys.stderr)
    if result.returncode != 0:
        error(f"agyswap: git pull --ff-only failed in {root}; settle it there with git, then run agyswap upgrade again")
        return result.returncode
    after = _head(git, root)
    try:
        paths.update_check_file().unlink(missing_ok=True)  # the next check asks origin afresh
    except Exception:
        pass
    if before and after and before != after:
        print(accent(f"agyswap upgraded: {before[:7]} -> {after[:7]}"))
    elif after:
        print(f"agyswap is already up to date ({after[:7]}).")
    if _file_sha(project) != project_before:
        warning("pyproject.toml changed, so the installed tool may need a reinstall. Run this in a new terminal "
                f"(the running agyswap.exe is locked):\n  {reinstall_command(root)}")
    return 0
