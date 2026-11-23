"""Language checks for this repository, run by the git hooks in this folder.

    python .githooks/check_english.py msg <commit-message-file>    # commit-msg hook
    python .githooks/check_english.py push < <pre-push stdin>       # pre-push hook
    python .githooks/check_english.py selftest

Rules:
- Commit messages are written in English: no CJK characters.
- Lines added to the English-only files below contain no CJK characters.
- README.md (English) and README.zh-TW.md (Chinese) change together: a push that touches one must touch
  the other, so the two stay in sync.
"""
import os
import subprocess
import sys
import tempfile

ENGLISH_ONLY = ("README.md", "CHANGELOG.md", "LICENSE", "THIRD_PARTY_NOTICES.md", "pyproject.toml", ".gitignore")
ENGLISH_ONLY_PREFIXES = (".githooks/", "src/", "tests/")
README_PAIR = ("README.md", "README.zh-TW.md")
ZERO = "0" * 40
# CJK radicals, punctuation, kana, bopomofo, ideographs, hangul, compatibility ideographs and full-width forms.
# Code points instead of literal characters keep this English-only file plain ASCII.
CJK_RANGES = ((0x2E80, 0x2FDF), (0x3000, 0x30FF), (0x3100, 0x31FF), (0x3400, 0x4DBF), (0x4E00, 0x9FFF),
              (0xAC00, 0xD7AF), (0xF900, 0xFAFF), (0xFE30, 0xFE4F), (0xFF00, 0xFFEF))
# The one Chinese label README.md may carry: the language switch at the top, linking to README.zh-TW.md.
ZH_LABEL = "".join(map(chr, (0x7E41, 0x9AD4, 0x4E2D, 0x6587)))  # Traditional Chinese, written in Chinese
LANGUAGE_SWITCH = f"**English** | [{ZH_LABEL}](README.zh-TW.md)"


def has_cjk(text):
    return any(lo <= ord(c) <= hi for c in text for lo, hi in CJK_RANGES)


def allowed_line(path, line):
    """The language switch at the top of README.md is the only CJK an English-only file may contain."""
    return path == "README.md" and line.strip() == LANGUAGE_SWITCH


def english_only(path):
    return path in ENGLISH_ONLY or path.startswith(ENGLISH_ONLY_PREFIXES)


def git(*args, cwd=None):
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, encoding="utf-8",
                          errors="replace", check=True).stdout


def message_problems(text, label="commit message"):
    lines = [l for l in text.splitlines() if not l.startswith("#")]
    bad = [l for l in lines if has_cjk(l)]
    return [f"{label} is not in English: {bad[0].strip()[:80]}"] if bad else []


def added_cjk_lines(commit, cwd=None):
    """(path, line) pairs for CJK text added to English-only files by one commit."""
    out = git("show", "--format=", "--unified=0", "--no-color", "--no-renames", commit, cwd=cwd)
    hits, path = [], None
    for line in out.splitlines():
        if line.startswith("+++ "):
            p = line[4:]
            path = p[2:] if p.startswith("b/") else None
        elif line.startswith("+") and path and english_only(path) and has_cjk(line[1:]) and not allowed_line(path, line[1:]):
            hits.append((path, line[1:].strip()[:80]))
    return hits


def changed_files(commit, cwd=None):
    return set(git("show", "--format=", "--name-only", "--no-renames", commit, cwd=cwd).split())


def readme_sync_problem(changed):
    a, b = README_PAIR
    if (a in changed) != (b in changed):
        done, missing = (a, b) if a in changed else (b, a)
        return f"{done} changed but {missing} did not; update both READMEs in the same push"
    return None


def pushed_commits(local_sha, remote_sha, cwd=None):
    if local_sha == ZERO:
        return []  # deleting a remote branch
    if remote_sha == ZERO:
        out = git("rev-list", local_sha, "--not", "--remotes", cwd=cwd)
    else:
        out = git("rev-list", f"{remote_sha}..{local_sha}", cwd=cwd)
    return out.split()


def check_push(lines, cwd=None):
    problems = []
    for line in lines:
        parts = line.split()
        if len(parts) != 4:
            continue
        _, local_sha, _, remote_sha = parts
        commits = pushed_commits(local_sha, remote_sha, cwd=cwd)
        changed = set()
        for c in commits:
            short = c[:7]
            problems += message_problems(git("log", "-1", "--format=%B", c, cwd=cwd), f"commit {short} message")
            problems += [f"commit {short} adds non-English text to {p}: {t}" for p, t in added_cjk_lines(c, cwd=cwd)]
            changed |= changed_files(c, cwd=cwd)
        sync = readme_sync_problem(changed)
        if sync:
            problems.append(sync)
    return problems


def report(problems):
    for p in problems:
        print(f"check_english: {p}", file=sys.stderr)
    return 1 if problems else 0


def selftest():
    zh = chr(0x4E2D) + chr(0x6587)
    assert has_cjk(zh) and has_cjk(chr(0xFF08)) and not has_cjk("Add switch -- with backup (v1)")
    assert message_problems(f"feat: add switch\n\n# {zh}\n") == []
    assert message_problems(f"feat: {zh}")
    assert readme_sync_problem({"README.md"}) and readme_sync_problem({"README.zh-TW.md", "src/x.py"})
    assert readme_sync_problem({"README.md", "README.zh-TW.md"}) is None and readme_sync_problem({"src/x.py"}) is None
    assert english_only("CHANGELOG.md") and english_only(".githooks/pre-push")
    assert english_only("src/agyswap/cli.py") and english_only("tests/test_cli.py") and not english_only("agyswap.py")
    assert allowed_line("README.md", LANGUAGE_SWITCH) and allowed_line("README.md", LANGUAGE_SWITCH + "  ")
    assert not allowed_line("README.md", f"{LANGUAGE_SWITCH} {zh}") and not allowed_line("CHANGELOG.md", LANGUAGE_SWITCH)
    with tempfile.TemporaryDirectory() as d:
        def g(*a):
            return git(*a, cwd=d)

        g("init", "-q", "-b", "main")
        g("config", "user.email", "test@example.invalid")
        g("config", "user.name", "test")
        g("config", "core.hooksPath", os.devnull)

        def commit(files, msg):
            for name, text in files.items():
                with open(os.path.join(d, name), "w", encoding="utf-8") as f:
                    f.write(text)
            g("add", *files)
            g("commit", "-q", "-m", msg)
            return g("rev-parse", "HEAD").strip()

        base = commit({"CHANGELOG.md": "# Changelog\n"}, "chore: start")
        good = commit({"README.md": f"{LANGUAGE_SWITCH}\n\nhello\n", "README.zh-TW.md": f"{zh}\n", "src.py": f"x = '{zh}'\n"},
                      "docs: add both readmes")
        assert check_push([f"refs/heads/main {good} refs/heads/main {base}"], cwd=d) == []
        bad = commit({"CHANGELOG.md": f"# Changelog\n\n- {zh}\n", "README.md": "hello again\n"}, f"feat: {zh}")
        problems = check_push([f"refs/heads/main {bad} refs/heads/main {good}"], cwd=d)
        assert any("message is not in English" in p for p in problems), problems
        assert any("non-English text to CHANGELOG.md" in p for p in problems), problems
        assert any("README.zh-TW.md did not" in p for p in problems), problems
        assert check_push([f"(delete) {ZERO} refs/heads/old {good}"], cwd=d) == []
    print("selftest ok")


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "msg" and len(sys.argv) > 2:
        with open(sys.argv[2], encoding="utf-8", errors="replace") as f:
            sys.exit(report(message_problems(f.read())))
    elif cmd == "push":
        sys.exit(report(check_push(sys.stdin.read().splitlines())))
    elif cmd == "selftest":
        selftest()
    else:
        sys.exit(__doc__)
