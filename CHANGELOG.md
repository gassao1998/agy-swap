# Changelog

## Unreleased

### Added

- `agyswap` package (`src/agyswap/`) following the command set, flags and `--json` fields of
  [claude-swap](https://github.com/realiti4/claude-swap) 1.0.0, adapted to Antigravity CLI (`agy`) on Windows.
- `list` and `status`, with cswap-compatible `--json` output. Usage comes from `agy -p /usage`, cached and
  paced; the weekly and five-hour windows map to `sevenDay` and `fiveHour`, and per-model limits such as
  Gemini 4 Argon's weekly limit go under `scoped`.
- `switch` (rotate, by account, `--strategy`, `--force`): backs up the live `gemini:antigravity` entry in Windows
  Credential Manager and reads the backup back before writing anything, saves the outgoing account's refreshed
  login to its own folder only after checking whose login it is, writes the target login, reads it back, and
  restores the backup on any mismatch. An unfinished switch leaves `pending_switch.json` and every later command
  warns about it.
- `add`, `login`, `remove`, `disable`, `enable`, `alias`, `threshold`, `priority`, `swap`, `move`.
- `run <num|email|alias> [-- agy args]`: runs `agy` as that account in this terminal only. `agy` always runs in
  the account folder with the SSH variables set, the active account included, so a later `switch` never takes
  the login away from a running `agy` and each account keeps one chat history. Bare `run` uses the nearest mapped
  ancestor directory and, as in cswap, the active account when nothing is mapped. agyswap's own notices go to
  stderr, so stdout carries only `agy`'s output, and `agy`'s exit code is passed through. A second `run` of the
  same account warns (`.run.pid` holds the PID and the process creation time, written under `.lock`).
  `--share-history` and `--no-share` are accepted, but nothing is shared yet.
- `run --auto` (agyswap extension, used by `gm.sh`): starts from the given, mapped or active account and, when the
  model class of this call (`--model` starting with `claude` or `gpt` means the Claude/GPT class,
  `gemini-4-argon-*` also checks the Argon limit, anything else the Gemini class) has no quota left, runs with the
  next account that has some. It reads the shared usage cache and queries only accounts that are due, one at a
  time; it never switches globally.
- `map <num|email|alias> [PATH]`, `map` (list) and `unmap [PATH]`.
- `auto` (`--once`, `--json`, `--dry-run`, `--interval`, `--threshold`, `--weekly-threshold`, `--cooldown`,
  `--model`, `--strategy`, `--include-api-key-accounts`), following cswap 1.0.0's auto-switch engine: each window
  is judged against its own switch line (per-account thresholds govern both leaving and landing), a target must
  beat the active account by `hysteresisPct` points of line headroom, `cooldownSeconds` bounds proactive switches
  and an account at its limit bypasses it, `unhealthyTicks` polls without readable usage fail over, and when every
  account is over its lines the engine moves only by cswap's ratio margin and never undoes its own last move until
  the account it left has recovered. Strategies: `best`, `weekly-first`, `consume-first`,
  `priority-consume-first`, and `next-available` (agyswap extension: the next qualifying account in sequence
  order). Events and fields are cswap's (`poll`, `switch`, `no-switch`, `account-quarantined`,
  `account-unquarantined`, `all-exhausted`, `sleep`, `error`, `config-warning`); `--once` exits 0 switched, 1
  error, 2 no action, 3 blocked. Every switch goes through `switch`, so a running agy or App is warned about,
  not refused.
- `auto` keeps cooldown, failover counters, the last switch and quarantines in `autoswitch_state.json` under its
  own lock, so separate `--once` runs behave like the loop. `--dry-run` writes no state, no Credential Manager
  entry and no roster change (the shared usage cache still updates, as with `list`).
- `auto` differs from cswap where agy's slow `/usage` calls for it: it queries the active account when its poll
  plan is due and one stale candidate per tick, and every candidate only when a switch is near; a failed latest
  query makes an account unknown even if the cache still holds a value, and ticks spent in the query backoff do
  not count towards failover again. An escape (at-limit, failover) lands below the lines when it can and ranks by
  headroom, never by reset time; a manual switch is not undone by `consume-first` or `priority-consume-first`
  until the chosen account reaches a line; with `--model`, an account out of Gemini quota falls back to choosing
  on Gemini alone. A target with a live `agyswap run` is skipped, rechecked under `.lock`. Accounts whose query
  asks for a login are quarantined until their login file changes.
- Usage queries claim their account in the cache first (150 s), so `list`, `status` and `auto` never start agy
  twice for one account, and a late result never overwrites a newer one. Every agy start for usage writes a start
  and an end line to `agyswap.log` (folder, first 8 hex digits of the login hash, outcome, duration; no token
  content), without `--debug`.
- `add-token [KEY|-]` (experimental): stores a Gemini API key in a Credential Manager entry of its own
  (`agyswap:apikey:<folder>`) and registers an API key account (`--slot`, `--email`; the default email is
  `api-key-<N>@agyswap.local`). `run` passes the key to `agy` as `GEMINI_API_KEY`, together with the account
  folder and the SSH variables; whether `agy` honours `GEMINI_API_KEY` this way is not verified yet. API key
  accounts are never switch targets, and `remove` deletes the stored key.
- `export <path|->` (`--account`, `--full`, `--force`): a JSON envelope (`formatVersion` 1) with each account's
  login (base64 of its folder's login file) and roster settings; `--full` adds the account folder's agy
  `settings.json` (one over 1 MB is left out). The file is created with a protected DACL for the current Windows
  user and no sharing, written through that handle and renamed into place; a directory is refused and an existing
  file is replaced only with `--force`. `-` writes to stdout with a plain-text warning on stderr and is refused
  when stdout is a terminal, mintty's pty pipe (Git Bash, MSYS2) included. The active account's folder copy is
  synced first. API key accounts are not exported.
- `import <path|->` (`--force`, `--include-settings`): every account is validated before anything is written (a
  usable login whose `id_token_email` matches the recorded email, no duplicate emails, aliases or slots), logins go
  through the same checked save as `switch`, and a failure rolls every folder back. Existing accounts with no
  usable login or a re-login verdict are healed, healthy ones are skipped unless `--force`, a newer local login is
  kept together with its roster record and folder, and the active account and Credential Manager are never
  changed (importing into the active account prints the `switch <num> --force` to run). agy settings from an
  `export --full` are ignored unless `--include-settings` is given, which lists each account's top-level keys.
- `import-usage <path|-> [--hold SECONDS]`: adopts another machine's `list --json` readings when they are newer;
  `--hold` keeps this machine from querying those accounts (at most a day), `0` lifts it.
- `import` and `import-usage` read at most 64 MB, refuse deep nesting, huge numbers, more than 1000 accounts,
  slots outside 1 to 9999, control characters in emails and timestamps, a `usageAgeSeconds` over 30 days and a
  `resetsAt` that is not an ISO 8601 time, with an error instead of a traceback.
- `config [list|get|set|unset|path]` (`--json` with `list` and `get`), as in cswap. An unknown key on the command
  line, or a misspelt key in `settings.json` (shown by `config list`), names the closest valid key. Changing an
  `autoswitch.*` key while no `agyswap auto` loop runs prints a note.
- `purge`: lists every item by name, asks `[y/N]`, then deletes only what agyswap creates in its data folder and
  the `agyswap:apikey:*` entries; `gemini:antigravity` stays. Refused while a switch is unfinished, an
  `agyswap run` is live (every account folder is scanned) or `agyswap auto` runs, checked again under `.lock`
  after the prompt and before anything is deleted. Also refused when the data folder or an entry in it is a
  junction or symbolic link, when the folder belongs to another Windows account, and when it holds neither
  `sequence.json` nor `.migrations.json`.
- `upgrade` (alias `update`): `git pull --ff-only` in the agyswap checkout, with the commit before and after, and
  the uv tool reinstall command to run in a new terminal when `pyproject.toml` changed.
- `updates.check`: at most once a day `git ls-remote origin HEAD`; a notice on stderr when origin has commits the
  checkout lacks, silent on any failure. git is started by an absolute path that never comes from the current
  directory or a UNC share (the Git for Windows folders before PATH), inside a checkout with a real `.git` folder,
  without a shell, without inherited `GIT_*` variables or `SSH_ASKPASS`, and with `GIT_CEILING_DIRECTORIES` set to
  the checkout's parent.
- `tui` and `watch` (`--no-mouse`), and a bare `agyswap` in a terminal: a textual dashboard adapted from cswap
  1.0.0's, needing the optional `tui` extra (`textual>=8.2.8,<9`). Without textual, `tui` and `watch` print an
  install hint and exit 1, and a bare `agyswap` keeps its usage error. The dashboard shows the active account's
  windows as bars with switch-line ticks and reset countdowns, the other accounts on one line each, and a menu
  (switch, watch, auto-switch view, add the current login, disable or enable, remove, theme); keys `s` switch
  (`b` best pick), `w` watch, `g` auto view, `f` query every account now, `q` quit, `ctrl+t` theme. Switching,
  removing and adding ask for confirmation first, with Cancel focused, because a switch is global and the
  Antigravity App follows it. Redraws read only the usage cache every 3 seconds; agy starts in a separate lane
  when an account's cache plan is due, and Credential Manager is read at most once a minute, a failed read
  included. The auto view runs the auto engine in dry-run until `l` is confirmed, shows the strategy, thresholds,
  model categories and each candidate's windows with reset times, and takes a threshold for that view only with
  `t` and the arrow keys. `ui.theme=auto` probes the console before textual starts, flushes the input buffer
  afterwards and falls back to dark.
- Account folders under `%LOCALAPPDATA%\agyswap\profiles\` are restricted to the current Windows user before any
  login file is written to them.
- `list` and `status` group each model category's limits under its name, 5h before 7d, with cswap's tree
  characters, so lines stay short enough for a phone over SSH. The `--json` output is unchanged.
- README.md in English and README.zh-TW.md in Traditional Chinese: install, quick start, the dashboard, how
  switching and runs work, every command, the settings, where data is stored, security notes, and a section
  on Google's terms (the Antigravity FAQ does not support automated account rotation or wrapping `agy` under
  consumer plans, and the terms allow suspension), with links to the sources.
- Offline test suite (326 tests; the 5 dashboard tests that drive textual's headless Pilot skip without it). An audit hook makes any test that reaches the real Credential Manager, the
  user's folders, `agy` or `git` fail at once.
- Git hooks in `.githooks/` (enable with `git config core.hooksPath .githooks`): commit messages must be in
  English, lines added to English-only files (README.md, CHANGELOG.md, LICENSE, THIRD_PARTY_NOTICES.md,
  pyproject.toml, .gitignore, .githooks/, src/, tests/) must not contain CJK text, and README.md and
  README.zh-TW.md must change in the same push. The commit-msg hook also runs the user's global commit-msg
  hook, if any.
- Code, comments, docstrings and CLI messages in `src/` and `tests/` are in English. Messages that have a
  counterpart in claude-swap 1.0.0 (switch, add, remove, alias, threshold, priority, swap, move, list, status and
  the flag errors) reuse its wording and layout.

### Removed

- The legacy single-file `agyswap.py` (`list`, `add`, `login`, `use`, `usage`, `run --profile/--auto`). The
  package replaces it: `run <account>` instead of `run --profile <name>`, `run --auto` as before, `switch` or
  `run <account>` instead of `use`, `list` instead of `usage`; the retired spellings print what to use instead.

### Security

- Windows tools (`tasklist`, `whoami`, `taskkill`) are
  started by their full `%SystemRoot%\System32` path, so a same-named program in the current folder never runs.
- The data folder (default or `AGYSWAP_HOME`) must lie inside the user's real profile folder (junctions are
  resolved). It is restricted to the current user when agyswap creates it, and an existing one is restricted once
  by a migration step. A folder agyswap locks down that is owned by another Windows account, or that is a junction
  or symbolic link, is refused.
- Restricting a folder or file replaces its DACL with a protected one that holds only the current user
  (`SetNamedSecurityInfoW`, `TreeResetNamedSecurityInfoW` for what is inside) instead of calling `icacls`, which
  left other users' explicit entries in place.

### Fixed

- When Credential Manager cannot be read in the current logon session (over SSH), `list` and `status` read the
  active account's usage through its account folder copy, isolated as `run` does, instead of showing none. The
  row carries `globalLoginUnchecked: true` (text output: a note) and nothing is written to Credential Manager or
  the folder.
- Saving a login back never recreates a missing account folder and never overwrites a folder that holds a
  usable login of another email; `switch`, `add`, `list` and `status` report it as not saved. A roster record
  without `folder` gets its account number as the folder (so `swap` and `move` no longer move it onto another
  account's login), and a roster whose folders collide (ignoring case) or whose `email`, `alias` or `folder` is
  not text is refused.
- When a switch does not save the outgoing login back (unmanaged login, `--force`, or a failed restore), the
  backup goes to a new `backup\unsaved-<utc>.bin` that later switches never overwrite, and that path is printed.
  Switching to an account whose email is still a placeholder records the login's real email, and is refused when
  that email already belongs to another account. Switching to the already active account clears
  `pending_switch.json`. `remove` deletes `backup\last-live.bin` when it holds the removed account's login.
- A reset time without a time zone, or one far outside the normal range, no longer makes `list` and `status`
  crash, and C0 and C1 control characters are dropped from every field of agy's `/usage` output before it is
  cached or printed.
- `auto` calls a switch off under `.lock` when the global login changed hands or the target was disabled while
  usage was being queried (`no-switch` reason `state-changed`), so it never moves off an account just chosen by
  hand and never records anti-flap state that mixes two accounts.
- `auto` failover counters are saved even when a tick raises, are cleared in the same write that records a
  switch, are left alone by a tick without a managed active account, and are merged under the state lock, so a
  loop and a scheduled `--once` add to each other's count instead of overwriting it. An active account whose
  query another process is running does not count as unhealthy, and every candidate is queried for failover only
  when the tick can actually reach it.
- `consume-first` and `priority-consume-first` decide again on the refreshed figures before a below-line move: a
  refreshed active account at or over its line escapes in the same tick, a failed refresh counts towards
  failover, a candidate asking for a login is quarantined, and a move needs a fresh reading of the active
  account (unlike cswap, which keeps the earlier trigger until the next tick).
- With `--model` and `priority-consume-first`, the Gemini-only fallback (C47) now also ranks candidates on Gemini
  alone instead of refusing them on the model's windows.
- Event output that fails (a closed pipe, an unencodable console) and a damaged roster read while planning the
  next sleep no longer stop the `auto` loop. A far-future `lastSwitchAt` no longer freezes proactive switches.
  `--dry-run` skips a target with a live `agyswap run`, as a real tick does.
- Usage claims are released when a query raises or is interrupted, count for at most 150 s from now whatever
  `claimUntil` says, are re-checked under the cache lock (another process that just recorded a result wins), and
  the cache reads the clock after taking its lock.

### How accounts are kept apart

`agy` normally keeps its login in Windows Credential Manager (`gemini:antigravity`). When `SSH_CONNECTION` is set
it ignores Credential Manager and uses `.gemini\antigravity-cli\antigravity-oauth-token` under the home folder.
Overriding `USERPROFILE` and `HOME` alone is not enough, because `agy` then still reads the Credential Manager
entry. agyswap gives every other account its own home folder and runs `agy` there with SSH variables set. Both
places hold the same UTF-8 JSON (`auth_method`, `id_token_email`, `token`), so a login can move between them and
the account is identified by `id_token_email`.
