# agyswap

agyswap keeps several Google accounts logged in to the Antigravity CLI (`agy`) on Windows and moves between them. Each account has its own quota. When one runs low, you can switch the global login, run one terminal as another account, or let `agyswap auto` switch before the active account hits its limit.

The commands, flags, `--json` fields and settings keys follow [claude-swap](https://github.com/realiti4/claude-swap) (`cswap`) 1.0.0, a multi-account switcher for Claude Code, so cswap users will find their way around. agyswap is not affiliated with Google.

[Traditional Chinese version](README.zh-TW.md)

## Status

Alpha (1.0.0a1), Windows only. The switching code is covered by offline tests that never touch the real Credential Manager. Global switches have also been run against the real one.

Two things are still unverified. Nobody has checked whether the Antigravity App picks up a switch. `add-token` passes a Gemini API key to `agy` as `GEMINI_API_KEY`, and it is not known yet whether `agy` honours it; a forum reply says agy also needs `modelProvider` set to `gemini`, which hasn't been tested either.

## Google's terms

Read this before you use agyswap. Google's Antigravity FAQ says: "Antigravity CLI headless mode (agy -p) is intended for direct scripting and CI workflows. Wrapping agy inside third-party coding agents or applications, or automating account rotation, is not supported under consumer plans." ([FAQ](https://antigravity.google/docs/faq)) The Antigravity terms add that using third-party software, tools or services to access the service is a breach, which "may be grounds for suspension or termination of your Antigravity and/or Gemini CLI accounts." ([terms](https://antigravity.google/terms))

Where agyswap stands:

- `auto` and `run --auto` automate account rotation.
- `run` wraps agy by definition, and so does any coding agent that calls agyswap.
- agyswap reads the OAuth login that agy caches, checks its `id_token_email`, keeps a copy in the account folder and hands it back to the official agy. It never calls Google's servers itself. A Google staff answer from September allows scripts to call the unmodified agy, but rules out extracting, proxying or reusing its cached credentials ([forum](https://discuss.ai.google.dev/t/185992)), and agyswap does read and move them. A reply from November says another vendor's coding agent launching `agy -p` is not supported under a personal plan ([forum](https://discuss.ai.google.dev/t/201688)).
- Switching by hand between accounts you own and pay for was called fine in a July staff answer ([forum](https://discuss.ai.google.dev/t/174378)) and in an October reply ([forum](https://discuss.ai.google.dev/t/190436)).

Accounts have been banned before. After the February 2026 wave, Google added a form that lifts a first ban; a second violation means a permanent ban ([forum](https://discuss.ai.google.dev/t/131424)). Google's general terms also allow it to suspend your access or delete your Google Account if you materially or repeatedly breach service-specific terms ([terms](https://policies.google.com/terms)).

The supported route for scripted use is an API key (the November reply says to set `modelProvider` to `gemini` together with `GEMINI_API_KEY`) or Pay As You Go on Google Cloud. agyswap's `add-token` only sets `GEMINI_API_KEY`, and none of this has been tested here.

agyswap doesn't change what Google allows. If you use it, the risk falls on your own accounts.

## Requirements

- Windows 10 or 11
- Python 3.11 or newer, and [uv](https://docs.astral.sh/uv/)
- The Antigravity CLI (`agy`), installed and logged in once
- Git, for installing from GitHub and for `agyswap upgrade`

## Install

From a clone, so that `agyswap upgrade` can update it later:

```powershell
git clone https://github.com/gassao1998/agy-swap
cd agy-swap
uv tool install --editable . --with "textual>=8.2.8,<9"
```

Or straight from GitHub:

```powershell
uv tool install "agyswap[tui] @ git+https://github.com/gassao1998/agy-swap"
```

To update that kind of install, run the same command again with `--force --refresh`.

textual is only needed for the dashboard. Without it (drop `--with ...` or `[tui]`), every other command works, and `agyswap tui` prints an install hint.

## Quick start

Register the account `agy` is logged in with now:

```powershell
agyswap add
```

Log in a second account. This runs agy's login inside a new account folder and leaves the global login alone:

```powershell
agyswap login
```

Don't add accounts by running `/logout` in `agy` and logging in again. It is not known whether `/logout` also revokes the old login on Google's side.

After that:

```powershell
agyswap list                       # every account and its quota
agyswap switch                     # move the global login to the next account
agyswap switch 2                   # or to a slot number, email or alias
agyswap run 2 -- -p "hello"        # run agy as account 2, in this terminal only
agyswap run --auto -- -p "hello"   # use an account that still has quota for this call
agyswap auto                       # switch before the active account runs out
```

In Git Bash, an argument that starts with `/` (such as `/usage`) is rewritten into a Windows path before agy sees it. Set `MSYS_NO_PATHCONV=1` for those calls.

## Dashboard

`agyswap tui`, or plain `agyswap` in a terminal, opens a textual dashboard. The active account's windows are drawn as bars with the switch line and a reset countdown, and every other account gets one line. `agyswap watch` opens it on the live page.

Keys: `s` switch (`b` picks the best account), `w` watch, `g` auto-switch page, `f` query every account now, `ctrl+t` theme, `q` quit. Switching, removing and adding ask first, with Cancel selected. The auto-switch page starts as a dry run and switches for real only after you press `l` and confirm. `--no-mouse` leaves the terminal's own text selection working.

## How it works

### Global switch

`agy` keeps its login in Windows Credential Manager, in the generic credential `gemini:antigravity`. `agyswap switch` replaces that entry, so every `agy` started afterwards uses the new account. The Antigravity App is expected to read the same entry, but that is untested.

Before writing, the switch backs up the current entry and reads the backup back. The outgoing login, which agy may have refreshed, is saved to its account folder only when its `id_token_email` matches that account. Then the new login is written and read back, and the backup is restored if anything doesn't match. An unfinished switch leaves `pending_switch.json`, and later commands warn about it. When the outgoing login can't be saved (an unmanaged login, `--force`, a failed restore), its backup goes to `backup\unsaved-<utc>.bin`, which later switches don't overwrite.

### Per-terminal runs

When `SSH_CONNECTION` is set, agy ignores Credential Manager and reads `.gemini\antigravity-cli\antigravity-oauth-token` under its home folder instead. Both places hold the same JSON. `agyswap run` gives each account its own home folder and starts agy there with `USERPROFILE`, `HOME` and the SSH variables set. A later global switch can't pull the login out from under a running agy, and each account keeps its own chat history. agyswap writes its notices to stderr and passes agy's exit code through.

### Usage

Quota comes from agy's own `/usage`, which takes a couple of seconds per account, so results are cached. The five-hour and weekly windows map to cswap's `fiveHour` and `sevenDay` fields, and per-model limits such as Gemini 4 Argon's weekly limit go under `scoped`. A query claims its account in the shared cache before starting agy, so `list`, `status`, `auto` and the dashboard running at the same time don't query one account twice.

Over SSH, Credential Manager can't be read from that logon session. `list` and `status` then read the active account's usage through its account folder and mark the row as unchecked.

### Automatic switching

`agyswap auto` is cswap 1.0.0's auto-switch loop, adapted to agy. It has the same strategies (`best`, `weekly-first`, `consume-first`, `priority-consume-first`, `next-available`), per-account and global thresholds, hysteresis, a cooldown between switches, and failover after several polls that can't read an account. Its state is kept on disk, so `agyswap auto --once` run from a scheduler behaves like the loop. It never switches to an account that a live `agyswap run` is using.

## Commands

Short forms: `ls` for `list`, `rm` for `remove`, `update` for `upgrade`. `<account>` is a slot number, email or alias.

Accounts:

| Command | What it does |
| --- | --- |
| `agyswap add` | Register the current Credential Manager login. Takes `--slot`, `--email`, `--alias`. |
| `agyswap login` | Log in a new account inside its own folder; the global login is left alone. |
| `agyswap remove <account>` | Remove an account and its folder. |
| `agyswap alias <account> <name>` | Set a short name (`--unset` removes it; no arguments lists them). |
| `agyswap swap <a> <b>` | Exchange two accounts' slot numbers. |
| `agyswap move <account> <slot>` | Put an account in a slot, swapping if it is taken. |
| `agyswap add-token [KEY\|-]` | Experimental: store a Gemini API key for `run` only. |

Status and switching:

| Command | What it does |
| --- | --- |
| `agyswap list` | Every account and its quota. `--json`, `--token-status`. |
| `agyswap status` | The active account and its quota. `--json`. |
| `agyswap switch [<account>]` | Move the global login to the next account, or to the given one. `--strategy best\|next-available`, `--model`, `--json`. |
| `agyswap run [<account>] [-- args]` | Run agy as an account in this terminal only. Without an account, the folder's mapped account or the active one. |
| `agyswap run --auto [-- args]` | Run agy as an account that has quota for this call's model. |
| `agyswap map <account> [path]` | Map a folder to an account for `run` (no arguments lists mappings). |
| `agyswap unmap [path]` | Remove a mapping. |
| `agyswap tui`, `agyswap watch` | The dashboard. `--no-mouse`. |

Auto-switching:

| Command | What it does |
| --- | --- |
| `agyswap auto` | Switch before the active account runs out. `--once`, `--dry-run`, `--json`, `--interval`, `--threshold`, `--weekly-threshold`, `--cooldown`, `--model`, `--strategy`, `--include-api-key-accounts`. With `--once`, it exits 0 after a switch, 1 on an error, 2 when no switch was needed and 3 when no account could take over. |
| `agyswap disable <account>` | Keep an account out of rotation (`enable` brings it back). |
| `agyswap threshold [<account> <pct>]` | Set one account's switch line (`--unset` clears it; no arguments lists them). |
| `agyswap priority [<account> <tier>]` | Set an account's priority, higher first (no arguments lists them). |

Settings, transfer and upkeep:

| Command | What it does |
| --- | --- |
| `agyswap config` | `list`, `get`, `set`, `unset` or `path` for `settings.json`. |
| `agyswap export <path\|->` | Export accounts with their logins. `--account`, `--full` (adds each folder's agy `settings.json`), `--force`. |
| `agyswap import <path\|->` | Import an export. `--force`, `--include-settings`. |
| `agyswap import-usage <path\|->` | Adopt usage another machine read (`list --json`). `--hold SECONDS`. |
| `agyswap upgrade` | `git pull --ff-only` in the agyswap clone. |
| `agyswap purge` | Delete all of agyswap's own data, after asking. |
| `agyswap selftest` | Run the built-in tests. |

`--debug` writes debug logs to stderr and to `agyswap.log`.

## Settings

`agyswap config set <key> <value>` changes these. A misspelt key gets a suggestion.

| Key | Default | Meaning |
| --- | --- | --- |
| `autoswitch.threshold` | 90 | Switch when the 5-hour window (or any window, if no weekly line is set) reaches this percent. |
| `autoswitch.weeklyThreshold` | unset | Switch line for the weekly and per-model windows; unset means `threshold`. |
| `autoswitch.intervalSeconds` | 60 | How often the `auto` loop polls. |
| `autoswitch.cooldownSeconds` | 300 | Minimum time between switches made ahead of a limit. |
| `autoswitch.hysteresisPct` | 10 | A target needs this many points more headroom than the active account. |
| `autoswitch.strategy` | best | How `auto` picks the next account. |
| `autoswitch.includeApiKeyAccounts` | false | Let `auto` rotate onto API key accounts. |
| `autoswitch.unhealthyTicks` | 3 | Failed polls in a row before an account counts as unhealthy. |
| `autoswitch.model` | unset | Also switch on these models' weekly limits (`gemini`, `claude` or `all`). |
| `ui.theme` | auto | Dashboard colours: `dark`, `light`, or `auto` to follow the terminal. |
| `run.shareHistory` | false | Share one chat history across accounts in `run`. Off until that is known to be safe. |
| `updates.check` | true | Check GitHub once a day (`git ls-remote`) and mention a newer release. |

## Where things are stored

Everything lives in `%LOCALAPPDATA%\agyswap`, or in `AGYSWAP_HOME` if you set it. Either has to be inside your own Windows profile folder. `profiles\` holds one home folder per account, with its login file and agy's settings and chat history. `sequence.json` is the account list, `settings.json` the settings, `cache\` the usage cache, `backup\` the Credential Manager backups taken before switches, and `agyswap.log` a record of each agy start for usage (a short hash of the login, never the login itself).

Outside that folder, agyswap touches two kinds of Credential Manager entries: `gemini:antigravity`, the login agy uses, which only a switch changes, and `agyswap:apikey:<folder>` for keys stored by `add-token`.

## Security

An export file contains the logins themselves. Treat it like a password, and never share it or commit it. agyswap creates it so that only your Windows user can open it, from the moment the file exists. `agyswap export -` writes to stdout and refuses to print to a terminal, including Git Bash's.

`agyswap import` skips the agy `settings.json` files that `export --full` carries, unless you add `--include-settings`. agy settings can start other programs (MCP servers, hooks), so use that flag only with exports you made yourself.

The data folder and every account folder are locked to your Windows user before a login is written into them. agyswap refuses junctions, symbolic links, and folders owned by another Windows account.

`agyswap purge` lists what it will delete and asks first. It removes only what agyswap created, plus its own `agyswap:apikey:*` entries, and leaves `gemini:antigravity` alone. It refuses while a switch is unfinished or while `agyswap run` or `agyswap auto` is running.

## Credits

The command set, flags, `--json` field names, settings keys and several algorithms come from [claude-swap](https://github.com/realiti4/claude-swap) 1.0.0, under the MIT License. Modules that adapt its code say so in their docstrings. See [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).

## License

MIT. See [LICENSE](LICENSE).
