"""Command parsing. The subcommands, the table that translates them to the legacy flags, and the flag checks follow cswap 1.0.0's cli.py (MIT License, claude-swap):
memorable subcommands are rewritten into the legacy flags and handed to the same parser, so old spellings such as `agyswap --list` keep working;
subcommands that take positional arguments (run, auto, config, map and so on) have their own parsers and are dispatched first.
"""
from __future__ import annotations

import argparse
import json
import logging
import math
import os
import sys
import time
from pathlib import Path

from agyswap import __version__, paths
from agyswap.errors import AgyswapError, error_envelope
from agyswap.fsutil import ensure_data_root, read_json
from agyswap.printer import accent, dimmed, error, muted, warning
from agyswap.settings import STRATEGIES

PROG = "agyswap"

_SUBCOMMAND_FLAGS = {
    "help": "--help", "list": "--list", "ls": "--list", "status": "--status",
    "add": "--add-account", "add-token": "--add-token", "remove": "--remove-account", "rm": "--remove-account",
    "disable": "--disable-account", "enable": "--enable-account", "export": "--export", "import": "--import",
    "import-usage": "--import-usage", "purge": "--purge", "upgrade": "--upgrade", "update": "--upgrade",
    "tui": "--tui", "watch": "--watch",
}

# Commands of the removed single-file agyswap.py: print one line saying what to use instead, and do nothing
_RETIRED = {
    "use": "use was removed: switch globally with agyswap switch <num|email|alias>, or use an account in this terminal only with agyswap run <num|email|alias>",
    "usage": "usage was removed: look at usage with agyswap list instead",
}
_NOT_PLANNED = {
    "unclaimed": "unclaimed is not planned: it is a Claude-specific feature, and agyswap only warns about a login that does not match an account and does not stash it",
    "menubar": "menubar is not planned: it is a macOS-only feature",
}

DESCRIPTION = """Multi-Account Switcher for Antigravity CLI (agy), with cswap's command set

Commands:
  %(prog)s help                       show this help
  %(prog)s list                       list managed accounts and their usage
  %(prog)s status                     show current account and its usage
  %(prog)s switch                     rotate to the next account (global; the App may follow)
  %(prog)s switch <num|email|alias>   switch to a specific account
  %(prog)s add                        add the current Credential Manager login as an account
  %(prog)s add-token [KEY|-]          register a Gemini API key (run only; experimental, whether agy honours GEMINI_API_KEY is not verified)
  %(prog)s login                      log in inside a new account folder, leaving the global login alone
  %(prog)s remove <num|email|alias>   remove an account
  %(prog)s disable <num|email|alias>  hold an account out of auto-rotation
  %(prog)s enable <num|email|alias>   return a disabled account to rotation
  %(prog)s threshold <num|email|alias> <pct>  set one account's switch-away point
  %(prog)s threshold <num|email|alias> --unset  clear it (use the global default)
  %(prog)s threshold                  list the global default and overrides
  %(prog)s priority <num|email|alias> <tier>  set priority (higher first; default 0)
  %(prog)s priority                   list account priorities
  %(prog)s run <num|email|alias> [-- ...]  run agy as an account, this terminal only
  %(prog)s run                        run the current dir's mapped account
  %(prog)s run --auto [-- ...]        run with an account that has quota for this call's model
  %(prog)s map <num|email|alias> [path]  map a directory to an account
  %(prog)s map                        list directory mappings
  %(prog)s unmap [path]               remove a directory mapping
  %(prog)s alias <num|email|alias> <name>  set a short alias for an account
  %(prog)s alias <num|email|alias> --unset  remove an account's alias
  %(prog)s alias                      list all aliases
  %(prog)s swap <a> <b>               exchange two accounts' slot numbers
  %(prog)s move <a> <slot>            assign an account to a slot (swaps if taken)
  %(prog)s auto                       auto-switch when nearing rate limits
  %(prog)s config [set KEY VALUE]     show or change settings (settings.json)
  %(prog)s export <path>              export accounts
  %(prog)s import <path>              import accounts
  %(prog)s import-usage <path>        adopt usage another machine read (list --json)
  %(prog)s tui [--no-mouse]           interactive dashboard (also: bare %(prog)s; needs the tui extra)
  %(prog)s watch [--no-mouse]         dashboard, opened on the live watch page
  %(prog)s upgrade                    update with git pull
  %(prog)s purge                      remove all agyswap data
  %(prog)s selftest                   run the built-in tests

Aliases: ls=list  rm=remove  update=upgrade"""

EPILOG = """Flags combine with subcommands:
  %(prog)s switch --strategy best           # pick the account with most quota left
  %(prog)s list --token-status
  %(prog)s list --json
  %(prog)s run school -- -p "say hi"       # forward args after '--' to agy
  %(prog)s add-token - < key.txt            # experimental: run passes the key as GEMINI_API_KEY, not verified yet
  %(prog)s auto --once
Add accounts with %(prog)s login; do not /logout in agy and log in as another account (it is not known whether the token on the server is revoked too).
From Git Bash, prefix the command with MSYS_NO_PATHCONV=1, or arguments such as /usage are rewritten into paths.
The original flag spellings (%(prog)s --list, %(prog)s --switch, ...) keep working."""


# Options of the main parser that take a value; when looking for the positional argument the value is skipped along with the option
_VALUE_OPTIONS = {"--strategy", "--model", "--slot", "--email", "--account", "--alias", "--hold"}
# Subcommands whose legacy flag itself takes the positional argument: it must move right behind the flag, otherwise options written first (export --full out.json) cannot be parsed
_POSITIONAL_VERBS = {"remove", "rm", "disable", "enable", "export", "import", "import-usage", "add-token"}


def _split_positional(rest: list[str]) -> tuple[str | None, list[str]]:
    i = 0
    while i < len(rest):
        token = rest[i]
        if token == "-" or not token.startswith("-"):
            return token, rest[:i] + rest[i + 1:]
        i += 2 if token in _VALUE_OPTIONS else 1
    return None, rest


def translate_subcommand(argv: list[str]) -> list[str]:
    if not argv:
        return argv
    verb, rest = argv[0], argv[1:]
    if verb == "switch":
        target, others = _split_positional(rest)
        return ["--switch-to", target, *others] if target is not None else ["--switch", *rest]
    flag = _SUBCOMMAND_FLAGS.get(verb)
    if flag and verb in _POSITIONAL_VERBS:
        value, others = _split_positional(rest)
        return [flag, value, *others] if value is not None else [flag, *rest]
    return [flag, *rest] if flag else argv


# ---------- subcommands with positional arguments ----------
def _sub(name: str, description: str) -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog=f"{PROG} {name}", description=description)
    p.add_argument("--debug", action="store_true", help="Enable debug logging (to stderr, and also written to agyswap.log)")
    return p


def _acct(p: argparse.ArgumentParser, name: str = "account", optional: bool = True,
          help: str = "Account (number, email or alias)") -> None:
    p.add_argument(name, nargs="?" if optional else None, metavar="NUM|EMAIL|ALIAS", help=help)


def build_run_parser() -> argparse.ArgumentParser:
    p = _sub("run", "Launch agy as a stored account in this terminal only. agy always runs in the account folder with SSH variables set, "
                    "the active account included, so it reads that folder's login; the global login and other terminals are unaffected. "
                    "agyswap's own notices go to stderr, so stdout carries only agy's output")
    _acct(p, help="Account to run (number, email or alias). Omit to use the current directory's mapping (see `agyswap map`), "
                  "or the active account when nothing is mapped.")
    p.add_argument("--no-share", action="store_true",
                   help="Do not share agy's settings into the account folder (accepted; nothing is shared until sharing is verified safe)")
    p.add_argument("--share-history", action=argparse.BooleanOptionalAction, default=None,
                   help="Share chat history across accounts (accepted; not implemented until sharing is verified safe, default run.shareHistory=false)")
    p.add_argument("--require-session", action="store_true", help="Refuse to launch when the target is the active account (the global login)")
    p.add_argument("--auto", action="store_true",
                   help="Start from the given, mapped or active account; when the model class this call uses (from agy's --model) has no "
                        "quota left there, run with the next account that has some. Never switches globally")
    return p


AUTO_EPILOG = """Exit codes with --once:
  0  switched to another account (with --dry-run: would switch)
  1  error (Credential Manager unreadable, lock contention, ...)
  2  no action needed
  3  blocked: wanted to switch but no viable target / all exhausted

Examples:
  %(prog)s                        # foreground loop, switch at 90%% used
  %(prog)s --threshold 80         # switch earlier
  %(prog)s --weekly-threshold 97  # let the weekly windows run higher before switching
  %(prog)s --model claude         # also switch when the Claude and GPT limit is hit
  %(prog)s --json                 # one JSON event per line (for scripts)
  %(prog)s --once; echo $?        # single tick, outcome in exit code
  %(prog)s --dry-run              # log decisions, never actually switch

A switch is global: new agy processes and the Antigravity App follow it. Usage comes from agy -p /usage, so accounts are
queried on the usage cache's pace, not every interval. Defaults live in settings.json; flags override them."""


def build_auto_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog=f"{PROG} auto", epilog=AUTO_EPILOG, formatter_class=argparse.RawDescriptionHelpFormatter,
                                description="Automatically switch accounts when the active one nears its usage limit. "
                                            "Runs a foreground polling loop; use --once for a single tick")
    p.add_argument("--debug", action="store_true", help="Enable debug logging (to stderr, and also written to agyswap.log)")
    p.add_argument("--once", action="store_true", help="Evaluate once, maybe switch, and exit (exit code = outcome)")
    p.add_argument("--json", action="store_true", help="Emit one machine-readable JSON event per line on stdout")
    p.add_argument("--interval", type=float, metavar="SECONDS", help="Poll interval in loop mode (15-3600; default 60)")
    p.add_argument("--threshold", type=float, metavar="PCT",
                   help="Switch when the active account's 5h window (or any window, without a weekly line) reaches this utilization (50-99.9; default 90)")
    p.add_argument("--weekly-threshold", type=float, metavar="PCT",
                   help="Separate switch line for the 7-day and per-model weekly windows (50-99.9; default: the threshold). "
                        "A flag cannot unset a configured value; use 'agyswap config unset'")
    p.add_argument("--cooldown", type=float, metavar="SECONDS", help="Minimum time between proactive switches (default 300)")
    p.add_argument("--model", metavar="NAMES",
                   help="Also switch on these model categories' limits, not just Gemini's (e.g. claude, gpt, or 'all')")
    p.add_argument("--include-api-key-accounts", action=argparse.BooleanOptionalAction, default=None,
                   help="Accepted for cswap compatibility: API key accounts are never global switch targets, so this only affects run --auto")
    p.add_argument("--strategy", choices=STRATEGIES, default=None,
                   help="Target selection: 'best' (most quota left; default), 'weekly-first' (switch at the line, onto the account whose weekly window "
                        "resets soonest), 'consume-first' (proactively use the account whose weekly window resets soonest), 'priority-consume-first' "
                        "(highest account priority, then soonest weekly reset; only targets below all switch lines), or 'next-available' "
                        "(agyswap extension: switch at the line, onto the next qualifying account in sequence order)")
    p.add_argument("--dry-run", action="store_true", help="Evaluate and report, but never switch or write state (the shared usage cache and agyswap.log still update)")
    return p


def _auto_command(argv: list[str]) -> int:
    """`auto [--once] [--json] [...]`: the engine reports through events, rendered here as JSON lines or timestamped text (same split as cswap)."""
    args = build_auto_parser().parse_args(argv)
    _setup_debug(args.debug)
    from agyswap import autoswitch

    def jsonl(ev: dict) -> None:
        print(json.dumps(ev, ensure_ascii=False), flush=True)

    def text(ev: dict) -> None:
        line = autoswitch.human(ev)
        if ev["event"] == "switch":
            line = accent(line)
        elif ev["event"] in ("poll", "no-switch", "sleep"):
            line = dimmed(line)
        stamp = time.strftime("%H:%M:%S")
        if ev["event"] in ("error", "account-quarantined", "config-warning"):
            warning(f"{stamp}  {line}")
        else:
            print(f"{stamp}  {line}", flush=True)
        for w in ev.get("warnings", []):
            warning(f"          {w}")

    try:
        cfg = autoswitch.AutoSettings.load({
            "autoswitch.threshold": args.threshold, "autoswitch.weeklyThreshold": args.weekly_threshold,
            "autoswitch.intervalSeconds": args.interval, "autoswitch.cooldownSeconds": args.cooldown,
            "autoswitch.includeApiKeyAccounts": args.include_api_key_accounts, "autoswitch.model": args.model,
            "autoswitch.strategy": args.strategy})
        engine = autoswitch.Engine(cfg, jsonl if args.json else text, dry_run=args.dry_run)
        if args.once:
            return int(engine.tick())
        if not args.json:
            weekly = f", weekly {autoswitch.pct_label(cfg.weekly_threshold)}%" if cfg.weekly_threshold is not None else ""
            print(dimmed(f"Auto-switch running: threshold {autoswitch.pct_label(cfg.threshold)}%{weekly}, strategy {cfg.strategy}, "
                         f"every {cfg.interval_seconds:.0f}s{' (dry-run)' if args.dry_run else ''} - Ctrl-C to stop"))
        return engine.run_loop()
    except AgyswapError as e:
        if args.json:
            print(json.dumps(error_envelope(e), ensure_ascii=False))
        else:
            error(f"agyswap: {e}")
        return 1
    except KeyboardInterrupt:
        print(f"\n{dimmed('Auto-switch stopped')}", file=sys.stderr if args.json else sys.stdout)
        return 130


CONFIG_EXAMPLES = """
Examples:
  agyswap config                              # list effective settings
  agyswap config get autoswitch.threshold
  agyswap config set autoswitch.threshold 80
  agyswap config set autoswitch.weeklyThreshold 97  # own line for the 7d/per-model windows
  agyswap config unset autoswitch.threshold   # back to the default
  agyswap config path                         # where settings.json lives"""


def build_config_parser() -> argparse.ArgumentParser:
    from agyswap import settings

    keys = "\n".join(f"  {s.dotted:<34}{s.help} (default {settings.format_value(s.default)})" for s in settings.SPECS.values())
    p = argparse.ArgumentParser(prog=f"{PROG} config", description="Read and edit agyswap settings (settings.json)",
                                formatter_class=argparse.RawDescriptionHelpFormatter, epilog=f"Keys:\n{keys}\n{CONFIG_EXAMPLES}")
    p.add_argument("--debug", action="store_true", help="Enable debug logging (to stderr, and also written to agyswap.log)")
    p.add_argument("--json", action="store_true", help="Emit machine-readable JSON to stdout (with list or get)")
    # The subparsers also accept --json and --debug (as in `config list --json`); SUPPRESS keeps an omitted flag from overwriting the parent's value
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--json", action="store_true", default=argparse.SUPPRESS, help=argparse.SUPPRESS)
    common.add_argument("--debug", action="store_true", default=argparse.SUPPRESS, help=argparse.SUPPRESS)
    sub = p.add_subparsers(dest="action")
    sub.add_parser("list", help="Show all effective settings (the default)", parents=[common])
    sub.add_parser("get", help="Print one setting's effective value", parents=[common]).add_argument("key", metavar="KEY")
    sub.add_parser("path", help="Print the settings.json location", parents=[common])
    p_set = sub.add_parser("set", help="Validate and persist one setting", parents=[common])
    p_set.add_argument("key", metavar="KEY")
    p_set.add_argument("value", metavar="VALUE")
    sub.add_parser("unset", help="Remove one setting (revert to the default)", parents=[common]).add_argument("key", metavar="KEY")
    return p


def _config_command(argv: list[str]) -> int:
    """`config [list|get KEY|set KEY VALUE|unset KEY|path]`, following cswap 1.0.0's cli._config_command (MIT): set is strict,
    and a misspelt key in settings.json or on the command line names the closest valid key (cswap 1.1.0 PR #581)."""
    from agyswap import settings

    p = build_config_parser()
    args = p.parse_args(argv)
    action = args.action or "list"
    if args.json and action not in ("list", "get"):
        p.error("--json can only be used with 'list' or 'get'")
    _setup_debug(args.debug)
    try:
        if action == "path":
            print(paths.settings_file())
        elif action == "list":
            for key in settings.unknown_keys():
                warning(f"agyswap: settings.json has an unknown setting {key}; it is ignored", file=sys.stderr)
            rows = settings.rows()
            if args.json:
                print(json.dumps({"schemaVersion": 1, "path": str(paths.settings_file()),
                                  "settings": [{"key": s.dotted, "value": v, "isSet": is_set} for s, v, is_set in rows]},
                                 ensure_ascii=False, indent=2))
            else:
                key_w = max(len(s.dotted) for s, _, _ in rows)
                val_w = max(len(settings.format_value(v)) for _, v, _ in rows)
                for s, v, is_set in rows:
                    line = f"{s.dotted:<{key_w}}  {settings.format_value(v):<{val_w}}"
                    print(line if is_set else f"{line}  {dimmed('(default)')}")
        elif action == "get":
            s = settings.spec(args.key)
            value, is_set = next((v, i) for sp, v, i in settings.rows() if sp is s)
            if args.json:
                print(json.dumps({"schemaVersion": 1, "key": s.dotted, "value": value, "isSet": is_set},
                                 ensure_ascii=False, indent=2))
            else:
                print(settings.format_value(value))
        elif action == "set":
            value = settings.set_value(args.key, args.value)
            print(f"{args.key} = {settings.format_value(value)}")
            _auto_setting_note(args.key)
        elif action == "unset":
            if settings.unset_value(args.key):
                print(f"{args.key} unset (default: {settings.format_value(settings.spec(args.key).default)})")
                _auto_setting_note(args.key)
            else:
                print(dimmed(f"{args.key} is not set; nothing to do"), file=sys.stderr)
    except AgyswapError as e:
        if args.json:
            print(json.dumps(error_envelope(e), ensure_ascii=False, indent=2))
        else:
            error(f"agyswap: {e}")
        return 1
    return 0


def _auto_setting_note(key: str) -> None:
    """Autoswitch settings only act while agyswap auto runs, and it is not scheduled by default (cswap #90)."""
    from agyswap import autoswitch

    if key.startswith("autoswitch.") and not autoswitch.loop_running():
        print(dimmed("Note: autoswitch settings take effect while agyswap auto runs, and no agyswap auto loop is running now "
                     "(agyswap auto --once and run --auto read them on each call)"), file=sys.stderr)


def build_simple_parsers() -> dict[str, argparse.ArgumentParser]:
    parsers = {}
    p = _sub("map", "Map a directory to an account; run without an account uses the nearest parent mapping. With no arguments, lists all mappings")
    _acct(p)
    p.add_argument("path", nargs="?", metavar="PATH", help="Directory to map (default: current directory)")
    parsers["map"] = p
    p = _sub("unmap", "Remove a directory mapping (default: current directory)")
    p.add_argument("path", nargs="?", metavar="PATH")
    parsers["unmap"] = p
    # the first word of value_help names the setting in the description
    for name, value, value_help in (("alias", "name", "alias"), ("threshold", "pct", "threshold (50-99.9)"),
                                    ("priority", "tier", "priority (0-1000)")):
        p = _sub(name, f"Set, clear or list an account's {value_help.split(' ')[0]}")
        _acct(p)
        p.add_argument(value, nargs="?", metavar=value.upper(), help=value_help)
        p.add_argument("--unset", action="store_true", help="Clear it")
        parsers[name] = p
    p = _sub("swap", "Exchange two accounts' slot numbers")
    _acct(p, "first", optional=False)
    _acct(p, "second", optional=False)
    parsers["swap"] = p
    p = _sub("move", "Assign an account to a slot number; if the slot is taken, the two accounts swap")
    _acct(p, optional=False)
    p.add_argument("slot", metavar="SLOT")
    parsers["move"] = p
    p = _sub("login", "Create a new account folder, open an interactive agy to log in, and register the account once logged in (the global login is untouched)")
    p.add_argument("--slot", type=int, metavar="NUM", help="Slot number to use")
    p.add_argument("--alias", metavar="NAME", help="Alias (also used as the account folder name)")
    p.add_argument("--email", metavar="EMAIL", help="Email to record by hand (when the login's own email can be read, that one takes precedence)")
    parsers["login"] = p
    return parsers


def _setup_debug(enabled: bool) -> None:
    if not enabled:
        return
    handlers = [logging.StreamHandler(sys.stderr)]
    try:
        ensure_data_root()
        handlers.append(logging.FileHandler(paths.log_file(), encoding="utf-8"))
    except (OSError, AgyswapError):
        pass
    logging.basicConfig(level=logging.DEBUG, handlers=handlers, format="%(asctime)s %(levelname)s %(message)s")


def _predispatch(argv: list[str]) -> int | None:
    """Subcommands with positional arguments, plus retired and not-planned commands. Returns None to hand over to the main parser."""
    verb = argv[0] if argv else ""
    if verb in _RETIRED:
        error(f"agyswap: {_RETIRED[verb]}")
        return 1
    if verb in _NOT_PLANNED:
        error(f"agyswap: {_NOT_PLANNED[verb]}")
        return 1
    if verb == "selftest":
        return selftest()
    if verb == "run":
        return _run_command(argv[1:])
    if verb == "auto":
        return _auto_command(argv[1:])
    if verb == "config":
        return _config_command(argv[1:])
    simple = build_simple_parsers()
    if verb in simple:
        p = simple[verb]
        args = p.parse_args(argv[1:])
        if verb == "login" and args.slot is not None and args.slot < 1:
            p.error("--slot must be 1 or greater")
        _setup_debug(args.debug)
        return _run_manage(_simple_action(p, verb, args))
    if verb == "add" and len(argv) > 1 and not argv[1].startswith("-"):
        error(f"agyswap: add no longer takes a name: to create a new account use agyswap login --alias {argv[1]}")
        return 1
    return None


def _run_command(argv: list[str]) -> int:
    """`run [account] [options] [-- agy args]`: everything after the first `--` goes to agy verbatim (same as cswap)."""
    head, tail = (argv[:argv.index("--")], argv[argv.index("--") + 1:]) if "--" in argv else (argv, [])
    if any(x == "--profile" or x.startswith("--profile=") for x in head):
        error("agyswap: run --profile was removed; use agyswap run <account>")
        return 1
    args = build_run_parser().parse_args(head)
    _setup_debug(args.debug)
    from agyswap import run

    try:
        return run.run(args.account, tail, auto=args.auto, require_session=args.require_session,
                       share_history=args.share_history)
    except AgyswapError as e:
        error(f"agyswap: {e}")
        return 1
    except KeyboardInterrupt:
        print(f"\n{dimmed('Operation cancelled')}", file=sys.stderr)
        return 130


def _simple_action(p: argparse.ArgumentParser, verb: str, args: argparse.Namespace):
    """The argument combinations of alias, threshold and priority are checked as cswap does; returns a function that does the work."""
    from agyswap import manage

    if verb == "login":
        return lambda: manage.login(slot=args.slot, alias=args.alias, email=args.email)
    if verb == "map":
        if args.account is None:
            return manage.list_mappings
        return lambda: manage.map_directory(args.account, args.path)
    if verb == "unmap":
        return lambda: manage.unmap_directory(args.path)
    if verb == "swap":
        return lambda: manage.swap(args.first, args.second)
    if verb == "move":
        return lambda: manage.move(args.account, args.slot)
    value = getattr(args, {"alias": "name", "threshold": "pct", "priority": "tier"}[verb])
    upper = {"alias": "NAME", "threshold": "PCT", "priority": "TIER"}[verb]
    if args.unset and value is not None:
        p.error(f"--unset does not take a {upper} argument")
    if args.unset and args.account is None:
        p.error("NUM|EMAIL|ALIAS is required with --unset")
    if args.account is not None and not args.unset and value is None:
        p.error(f"{upper} is required (or pass --unset)")
    if args.account is None:
        return {"alias": manage.list_aliases, "threshold": manage.list_thresholds,
                "priority": manage.list_priorities}[verb]
    setter = {"alias": manage.set_alias, "threshold": manage.set_threshold, "priority": manage.set_priority}[verb]
    return lambda: setter(args.account, None if args.unset else value)


def _run_manage(action) -> int:
    """Commands that change state: print only after everything is done (cswap #113)."""
    try:
        msgs = action()
    except AgyswapError as e:
        error(f"agyswap: {e}")
        return 1
    except KeyboardInterrupt:
        print(f"\n{dimmed('Operation cancelled')}")
        return 130
    _emit(msgs)
    return 0


def _emit(msgs: list) -> None:
    for style, text in msgs:
        if style == "warn":
            warning(text)
        else:
            print(dimmed(text) if style == "dim" else text)


def _main_manage(a: argparse.Namespace):
    from agyswap import manage

    if a.add_account:
        return lambda: manage.add(slot=a.slot, alias=a.alias, email=a.email)
    if a.add_token is not None:
        return lambda: manage.add_token(a.add_token, slot=a.slot, email=a.email)
    if a.remove_account is not None:
        return lambda: manage.remove(a.remove_account)
    if a.disable_account is not None:
        return lambda: manage.set_disabled(a.disable_account, True)
    if a.enable_account is not None:
        return lambda: manage.set_disabled(a.enable_account, False)
    if a.purge:
        return manage.purge
    return None


# ---------- main parser ----------
def build_main_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog=PROG, usage="%(prog)s <command> [args] [options]", description=DESCRIPTION,
                                epilog=EPILOG, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    p.add_argument("--debug", action="store_true", help="Enable debug logging (to stderr, and also written to agyswap.log)")
    p.add_argument("--token-status", action="store_true", help="Show source-labelled login diagnostics for each account (use with 'list')")
    p.add_argument("--json", action="store_true", help="Emit machine-readable JSON to stdout (use with 'list', 'status', or 'switch')")
    p.add_argument("--strategy", choices=["best", "next-available"], metavar="{best,next-available}",
                   help="With bare 'switch': 'best' jumps to the account with the most headroom; 'next-available' rotates to the next account, skipping any at their limit")
    p.add_argument("--model", metavar="NAMES", help="With 'switch --strategy': also count these model categories' limits when comparing accounts (comma-separated, or 'all')")
    p.add_argument("--slot", type=int, metavar="NUM", help="Specify slot number when adding account (use with 'add' or 'add-token')")
    p.add_argument("--email", metavar="EMAIL", help="Email address for the account (use with 'add' or 'add-token')")
    p.add_argument("--account", metavar="NUM|EMAIL|ALIAS", help="Limit export to one account (use with 'export')")
    p.add_argument("--alias", metavar="NAME", help="Set a short display alias for the account (use with 'add')")
    p.add_argument("--force", action="store_true",
                   help="Overwrite existing accounts during import, or an existing file during export; with 'switch <num|email>', "
                        "write the account folder's login without saving the current login back first")
    p.add_argument("--full", action="store_true",
                   help="Include each account folder's agy settings.json in export (default: the login and roster settings only)")
    p.add_argument("--include-settings", action="store_true",
                   help="With 'import': also write the agy settings.json an export --full carries into account folders that have none. "
                        "agy settings can start programs (MCP servers, hooks), so use it only for exports you made yourself")
    p.add_argument("--hold", type=float, metavar="SECONDS",
                   help="With 'import-usage': keep this machine from fetching the imported accounts for this many seconds")
    p.add_argument("--no-mouse", action="store_true",
                   help="With 'tui' or 'watch': leave mouse tracking off, so text can be selected in the terminal")
    g = p.add_mutually_exclusive_group(required=False)
    for flag, kwargs in (
        ("--add-account", {"action": "store_true"}), ("--remove-account", {"metavar": "NUM|EMAIL"}),
        ("--disable-account", {"metavar": "NUM|EMAIL"}), ("--enable-account", {"metavar": "NUM|EMAIL"}),
        ("--list", {"action": "store_true"}), ("--switch", {"action": "store_true"}),
        ("--switch-to", {"metavar": "NUM|EMAIL"}), ("--status", {"action": "store_true"}),
        ("--purge", {"action": "store_true"}), ("--export", {"metavar": "PATH"}),
        ("--import", {"dest": "import_", "metavar": "PATH"}), ("--import-usage", {"metavar": "PATH"}),
        ("--tui", {"action": "store_true"}), ("--watch", {"action": "store_true"}),
        ("--upgrade", {"action": "store_true"}), ("--add-token", {"metavar": "TOKEN|-", "nargs": "?", "const": ""}),
    ):
        g.add_argument(flag, help=argparse.SUPPRESS, **kwargs)
    return p


def check_flags(p: argparse.ArgumentParser, a: argparse.Namespace) -> None:
    """Reject a flag used with the wrong command (checks as in cswap; --email also works with add)."""
    if not (a.add_account or a.list or a.switch or a.status or a.purge or a.tui or a.watch or a.upgrade
            or a.remove_account is not None or a.disable_account is not None or a.enable_account is not None
            or a.switch_to is not None or a.export is not None or a.import_ is not None
            or a.import_usage is not None or a.add_token is not None):
        p.error(f"no command given — try '{PROG} help'")
    for sub, metavar, value in (("remove", "NUM|EMAIL", a.remove_account), ("disable", "NUM|EMAIL", a.disable_account),
                                ("enable", "NUM|EMAIL", a.enable_account), ("switch", "NUM|EMAIL", a.switch_to),
                                ("export", "PATH", a.export), ("import", "PATH", a.import_),
                                ("import-usage", "PATH", a.import_usage)):
        if value is not None and not value:
            p.error(f"'{sub}' requires a non-empty {metavar}")
    if a.token_status and not a.list:
        p.error("--token-status can only be used with 'list'")
    if a.json and not (a.list or a.status or a.switch or a.switch_to):
        p.error("--json can only be used with 'list', 'status', or 'switch'")
    if a.json and a.token_status:
        p.error("--token-status cannot be combined with --json")
    if a.strategy is not None and not a.switch:
        p.error("--strategy can only be used with bare 'switch'")
    if a.model is not None and a.strategy is None:
        p.error("--model can only be used with 'switch --strategy best' or 'switch --strategy next-available'")
    if a.slot is not None and not (a.add_account or a.add_token is not None):
        p.error("--slot can only be used with 'add' or 'add-token'")
    if a.slot is not None and a.slot < 1:
        p.error("--slot must be 1 or greater")
    if a.email is not None and not (a.add_account or a.add_token is not None):
        p.error("--email can only be used with 'add' or 'add-token'")
    if a.account is not None and not a.export:
        p.error("--account can only be used with 'export'")
    if a.alias is not None and not a.add_account:
        p.error("--alias can only be used with 'add'")
    if a.force and not (a.import_ or a.switch_to or a.export):
        p.error("--force can only be used with 'import', 'export' or 'switch <num|email>'")
    if a.full and not a.export:
        p.error("--full can only be used with 'export'")
    if a.include_settings and not a.import_:
        p.error("--include-settings can only be used with 'import'")
    if a.no_mouse and not (a.tui or a.watch):
        p.error("--no-mouse can only be used with 'tui' or 'watch'")
    if a.hold is not None and a.import_usage is None:
        p.error("--hold can only be used with 'import-usage'")
    if a.hold is not None and not (math.isfinite(a.hold) and a.hold >= 0):
        p.error("--hold must be a non-negative number of seconds")


def selftest() -> int:
    import unittest

    root = Path(__file__).resolve().parents[2] / "tests"
    if not root.is_dir():
        error(f"agyswap: test folder not found: {root} (selftest must run from an editable install in the repo)")
        return 1
    suite = unittest.defaultTestLoader.discover(str(root), top_level_dir=str(root.parent))
    result = unittest.TextTestRunner(stream=sys.stderr, verbosity=0, failfast=True).run(suite)
    if result.wasSuccessful():
        print(f"selftest ok ({result.testsRun} tests)")
        return 0
    return 1


def pending_switch_warning() -> str | None:
    """Any command that starts and sees pending_switch.json warns and says how to finish up."""
    path = paths.pending_switch_file()
    if not path.exists():
        return None
    data = read_json(path, {})
    data = data if isinstance(data, dict) else {}
    ends = [f"agyswap switch {data[k]}" for k in ("from", "to") if isinstance(data.get(k), (int, str))]
    how = f"finish it with {' or '.join(ends)}" if ends else "finish it with agyswap switch <num|email|alias>"
    return f"agyswap: the last switch did not finish ({path}); {how}"


_NO_PENDING_CHECK = {"help", "-h", "--help", "--version", "selftest"}


def dispatch(argv: list[str]) -> int:
    if not (argv and argv[0] in _NO_PENDING_CHECK):
        try:
            note = pending_switch_warning()
        except AgyswapError as e:  # the data folder is refused (paths.base)
            if "--json" in argv:
                print(json.dumps(error_envelope(e), ensure_ascii=False, indent=2))
            else:
                error(f"agyswap: {e}")
            return 1
        if note:
            warning(note, file=sys.stderr)
    handled = _predispatch(argv)
    if handled is not None:
        return handled
    if not argv and sys.stdout.isatty() and sys.stdin.isatty():
        from agyswap import tui

        if tui.available():  # without textual a bare agyswap keeps the usage error, as before
            argv = ["--tui"]
    p = build_main_parser()
    a = p.parse_args(translate_subcommand(argv))
    check_flags(p, a)
    _setup_debug(a.debug)
    if a.upgrade:  # before anything reads the data folder, as in cswap
        from agyswap import update_check

        try:
            return update_check.run_upgrade()
        except KeyboardInterrupt:
            print(f"\n{dimmed('Upgrade cancelled')}")
            return 130
    if a.tui or a.watch:
        return _tui_command(a)
    action = _main_manage(a)
    if action is not None:
        code = _run_manage(action)
    elif a.export or a.import_ or a.import_usage:
        code = _transfer_command(a)
    else:
        code = _data_command(a)
    if code == 0 and not (a.json or a.purge):
        _update_notice()
    return code


def _update_notice() -> None:
    """Passive notice on stderr: at most one git ls-remote a day, silent on any failure. Skipped with --json and after purge,
    which would otherwise recreate the cache inside the folder it just removed."""
    from agyswap import update_check

    msg = update_check.check_for_update()
    if msg:
        print(f"\n{muted(msg)}", file=sys.stderr)


def _tui_command(a: argparse.Namespace) -> int:
    from agyswap import tui

    if not tui.available():
        error(f"agyswap: {tui.INSTALL_HINT}")
        return 1
    try:
        return tui.run("watch" if a.watch else "dashboard", no_mouse=a.no_mouse)
    except AgyswapError as e:
        error(f"agyswap: {e}")
        return 1


def _transfer_command(a: argparse.Namespace) -> int:
    """export, import and import-usage print their own notices to stderr; stdout carries only an export to '-'."""
    from agyswap import transfer

    try:
        if a.export:
            transfer.export_accounts(a.export, account=a.account, full=a.full, force=a.force)
        elif a.import_:
            transfer.import_accounts(a.import_, force=a.force, include_settings=a.include_settings)
        else:
            transfer.import_usage(a.import_usage, hold_s=a.hold)
    except AgyswapError as e:
        error(f"agyswap: {e}")
        return 1
    except OSError as e:  # for example a path Windows refuses; import has already put every account folder back
        error(f"agyswap: {e.strerror or e}" + (f": {e.filename}" if e.filename else ""))
        return 1
    except KeyboardInterrupt:
        print(f"\n{dimmed('Operation cancelled')}", file=sys.stderr)
        return 130
    return 0


def _api_key_note() -> None:
    """agy may use the key from the environment instead, so a switch can look as if it had no effect (cswap #233; whether agy honours the key is not verified)."""
    if os.environ.get("GEMINI_API_KEY"):
        warning("agyswap: GEMINI_API_KEY is set in the environment; agy may use that key instead, so a switch may appear to have no effect (not verified)",
                file=sys.stderr)


def _switch_models(a: argparse.Namespace) -> tuple[tuple[str, ...], str | None]:
    """Only the usage-aware strategies read model limits: --model wins, otherwise autoswitch.model applies (same as cswap)."""
    from agyswap import settings
    from agyswap.switch import parse_models

    if a.strategy is None:
        return (), None
    if a.model is not None:
        return parse_models(a.model), "cli"
    models = parse_models(settings.load()["autoswitch.model"])
    return models, ("autoswitch.model" if models else None)


def _print_switch(payload: dict) -> None:
    for w in payload["warnings"]:
        warning(w)
    if payload["switched"] or payload["reason"] == "activated":
        print(accent(payload["message"]))
        print(dimmed("A newly started agy will use this account; whether an agy or the App that is already running needs a restart is not verified yet"))
    else:
        print(payload["message"])


def _switch_command(a: argparse.Namespace) -> dict | None:
    from agyswap import switch

    _api_key_note()
    if a.switch_to is not None:
        payload = switch.switch_to(a.switch_to, force=a.force)
    else:
        models, source = _switch_models(a)
        if models and not a.json:
            where = "--model" if source == "cli" else source
            print(dimmed(f"Using configured model limits: {', '.join(models)} (from {where})"))
        payload = switch.switch(strategy=a.strategy, models=models)
        if models:
            payload["models"], payload["modelSource"] = list(models), source
    if a.json:
        return payload
    if payload["reason"] == "unmanaged-account":
        # Text mode adds it automatically as cswap does; JSON mode avoids that kind of unexpected side effect
        from agyswap import manage

        msgs = manage.add()
        print(f"{accent('Notice:')} Active account '{payload['from']['email']}' was not managed. It has been automatically added:")
        _emit(msgs)
        print(dimmed("Please run the switch command again to switch to the next account."))
        return None
    _print_switch(payload)
    return None


def _data_command(a: argparse.Namespace) -> int:
    """list, status and switch: JSON is printed in this one place and the commands only return a payload (same as cswap)."""
    from agyswap import commands
    from agyswap.state import load_context

    try:
        if a.switch or a.switch_to:
            payload = _switch_command(a)
        else:
            ctx = load_context()
            if a.status:
                _api_key_note()
            payload = (commands.cmd_list(ctx, json_output=a.json, token_status=a.token_status) if a.list
                       else commands.cmd_status(ctx, json_output=a.json))
    except AgyswapError as e:
        if a.json:
            print(json.dumps(error_envelope(e), ensure_ascii=False, indent=2))
        else:
            error(f"agyswap: {e}")
        return 1
    except KeyboardInterrupt:
        print(f"\n{dimmed('Operation cancelled')}", file=sys.stderr if a.json else sys.stdout)
        return 130
    if a.json and payload is not None:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


def main(argv: list[str] | None = None) -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError, OSError):
            pass
    sys.exit(dispatch(sys.argv[1:] if argv is None else argv))


if __name__ == "__main__":
    main()
