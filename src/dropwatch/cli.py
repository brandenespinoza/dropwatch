"""Command-line interface.

`scan` is the operation everything else exists around: `status` says what a
scan could not settle, `fix` walks those questions, and `map` / `block` answer
them directly when you already know the answer.

Every subcommand must be named. A bare invocation prints help and does
nothing — scanning talks to two servers and takes minutes, which is not a
reasonable thing to start by accident.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from collections import Counter
from pathlib import Path

from . import __version__
from .config import (
    SETTINGS,
    SETTINGS_BY_KEY,
    USER_AGENT,
    Config,
    describe_settings,
    find_env_file,
    invocation_name,
    load_config,
    load_dotenv,
    normalize_url,
    save_settings,
    user_config_dir,
)
from .deezer import DeezerProvider, make_rate_limiter
from .errors import ExitCode, DropWatchError
from .http import HttpClient
from .logging_setup import setup_logging
from .models import DatePrecision, ReleaseDate, ReleaseType
from .navidrome import NavidromeClient
from .report import (
    build_summary,
    print_results,
    print_review,
    print_summary,
    print_unresolved,
    sort_releases,
)
from .scan import Scanner, ScanOptions
from .state import MappingTarget, Store

log = logging.getLogger("dropwatch.cli")

SUBCOMMANDS = {
    "scan", "setup", "config", "check", "fix", "status", "map",
    "unmap", "block", "unblock", "cache",
}

#: Earlier spellings, still accepted but no longer advertised. `resolve` and
#: `review` were one command split by what it acted on, which meant knowing
#: which of them applied before you could start; `artists` was the read-only
#: half of `resolve` with no equivalent for `review`.
LEGACY_COMMANDS = {"resolve", "review", "artists"}

_TYPE_ALIASES = {
    "album": ReleaseType.ALBUM,
    "albums": ReleaseType.ALBUM,
    "ep": ReleaseType.EP,
    "eps": ReleaseType.EP,
    "single": ReleaseType.SINGLE,
    "singles": ReleaseType.SINGLE,
    "unknown": ReleaseType.UNKNOWN,
}


def _global_options() -> argparse.ArgumentParser:
    """Options accepted both before and after the subcommand."""
    common = argparse.ArgumentParser(add_help=False)
    # SUPPRESS keeps the subparser from overwriting a value that was given
    # before the subcommand ("dropwatch -vv scan") with its own default.
    common.add_argument(
        "-v",
        "--verbose",
        action="count",
        default=argparse.SUPPRESS,
        help="verbose output to stderr; repeat for debug logging",
    )
    common.add_argument(
        "--env-file",
        type=Path,
        default=argparse.SUPPRESS,
        help="path to the config file (default: ./.env, then ~/.config/dropwatch/.env)",
    )
    return common


def build_parser() -> argparse.ArgumentParser:
    common = _global_options()
    parser = argparse.ArgumentParser(
        prog=invocation_name(),
        parents=[common],
        description=(
            "List releases by artists in your Navidrome library that Deezer has "
            "and you appear not to own."
        ),
        epilog=(
            "Start with `dropwatch setup`, then `dropwatch scan`. Configuration is "
            "read from the environment, then ./.env, then ~/.config/dropwatch/.env."
        ),
    )
    parser.add_argument("--version", action="version", version=f"dropwatch {__version__}")

    sub = parser.add_subparsers(dest="command")

    scan = sub.add_parser(
        "scan", parents=[common], help="scan the library and list missing releases"
    )
    scan.add_argument(
        "--artist",
        action="append",
        default=[],
        metavar="NAME",
        help="restrict the scan to this local artist (repeatable)",
    )
    scan.add_argument("--limit", type=int, default=None, help="scan at most N artists")
    scan.add_argument(
        "--since",
        type=_parse_since,
        default=None,
        metavar="DATE",
        help="only consider releases on or after this date: YYYY, YYYY-MM or YYYY-MM-DD",
    )
    scan.add_argument(
        "--type",
        action="append",
        default=[],
        metavar="TYPE",
        choices=sorted(_TYPE_ALIASES),
        help="only report these release types (repeatable)",
    )
    scan.add_argument(
        "--refresh",
        action="store_true",
        help="ignore cached Deezer data and refetch everything",
    )
    scan.add_argument(
        "--flat",
        action="store_true",
        help="one continuous list sorted by date, without release-type groups",
    )
    scan.add_argument("--no-progress", action="store_true", help="suppress the progress line")

    sub.add_parser(
        "setup",
        parents=[common],
        help="guided setup: enter your Navidrome details and test the connection",
    )

    config_cmd = sub.add_parser(
        "config", parents=[common], help="view or change individual settings"
    )
    config_sub = config_cmd.add_subparsers(dest="config_action")
    config_sub.add_parser("path", parents=[common], help="print the settings file location")
    setter = config_sub.add_parser("set", parents=[common], help="change one setting")
    setter.add_argument("key", choices=[s.key for s in SETTINGS])
    setter.add_argument("value")
    unsetter = config_sub.add_parser("unset", parents=[common], help="remove one setting")
    unsetter.add_argument("key", choices=[s.key for s in SETTINGS])
    config_sub.add_parser(
        "password", parents=[common], help="set the Navidrome password (prompted, not echoed)"
    )

    sub.add_parser(
        "check",
        parents=[common],
        help="verify Navidrome connectivity and credentials, then exit",
    )

    status = sub.add_parser(
        "status", parents=[common], help="what needs your attention, and what you have decided"
    )
    status.add_argument(
        "--decided",
        action="store_true",
        help="list saved mappings, blocks and release decisions",
    )

    fixer = sub.add_parser(
        "fix",
        parents=[common],
        help="answer the questions the last scan could not settle",
    )
    fixer.add_argument(
        "target",
        nargs="?",
        default=None,
        metavar="ARTIST",
        help="shorthand for --artist",
    )
    subject = fixer.add_mutually_exclusive_group()
    subject.add_argument(
        "--artist",
        metavar="NAME",
        help="re-open one artist, even if it is already mapped",
    )
    subject.add_argument(
        "--album",
        metavar="ID|URL",
        help="a Deezer album id or URL from the results list",
    )
    decision_group = fixer.add_mutually_exclusive_group()
    decision_group.add_argument(
        "--own",
        dest="decision",
        action="store_const",
        const="owned",
        help="with --album: I have this release; stop reporting it",
    )
    decision_group.add_argument(
        "--missing",
        dest="decision",
        action="store_const",
        const="missing",
        help="with --album: I do not have it; always report it",
    )
    decision_group.add_argument(
        "--clear",
        dest="decision",
        action="store_const",
        const="clear",
        help="with --album: forget a previous decision about it",
    )
    fixer.set_defaults(decision=None)

    mapper = sub.add_parser(
        "map",
        parents=[common],
        help="map a local artist to one or more Deezer artists",
    )
    mapper.add_argument("local_name")
    mapper.add_argument(
        "deezer_id",
        nargs="+",
        metavar="DEEZER_ID",
        help="one or more Deezer artist IDs; several are merged into one artist",
    )

    unmap = sub.add_parser(
        "unmap",
        parents=[common],
        help="clear everything known about an artist, back to unresolved",
    )
    unmap.add_argument("local_name")

    for name, verb in (("block", "stop"), ("unblock", "resume")):
        parser_ = sub.add_parser(
            name,
            parents=[common],
            help=f"{verb} reporting an artist, or a single release",
        )
        parser_.add_argument(
            "target",
            nargs="?",
            default=None,
            metavar="ARTIST",
            help="shorthand for --artist",
        )
        which = parser_.add_mutually_exclusive_group()
        which.add_argument(
            "--artist",
            metavar="NAME|ID|URL",
            help="a local artist name, or a Deezer artist id or URL",
        )
        which.add_argument(
            "--album",
            metavar="ID|URL",
            help="a Deezer album id or URL, of any type: album, EP or single",
        )

    cache = sub.add_parser("cache", parents=[common], help="inspect or clear local state")
    cache.add_argument("--clear", action="store_true", help="delete cached API responses")
    cache.add_argument(
        "--reset-mappings", action="store_true", help="delete all artist mappings"
    )
    cache.add_argument(
        "--reset-decisions",
        action="store_true",
        help="forget every review decision",
    )

    return parser


def _parse_since(text: str) -> ReleaseDate:
    """Accept a year, a year-month, or a full date for --since."""
    parsed = ReleaseDate.parse(text)
    if parsed.precision is DatePrecision.UNKNOWN:
        raise argparse.ArgumentTypeError(
            f"{text!r} is not a date. Use YYYY, YYYY-MM or YYYY-MM-DD."
        )
    return parsed


#: Flags that belong to `scan` and nothing else. Used only to recognise the
#: old bare-invocation habit and say where they moved to.
SCAN_FLAGS = {
    "--artist", "--limit", "--since", "--type", "--refresh", "--flat",
    "--no-progress",
}

#: Flags that consume the next token, so it is a value rather than a subcommand.
VALUE_FLAGS = {"--env-file", "--artist", "--limit", "--since", "--type"}


def _apply_aliases(argv: list[str]) -> list[str]:
    """Rewrite the old command names onto the current ones.

    Done here rather than with argparse aliases because the old commands had
    different argument shapes: `review 558123` named a release positionally,
    where `fix` reserves the positional for an artist.
    """
    if not argv or argv[0] not in LEGACY_COMMANDS:
        return argv

    command, rest = argv[0], argv[1:]

    if command == "artists":
        return ["status", *("--decided" if a == "--mappings" else a for a in rest)]

    if command == "resolve":
        return ["fix", *rest]

    # `review <id>` targeted a release; a bare `review` walked the queue.
    if rest and not rest[0].startswith("-"):
        return ["fix", "--album", *rest]
    return ["fix", *rest]


def _misplaced_scan_flags(argv: list[str]) -> list[str]:
    """Scan flags given before any subcommand, in the order they appear.

    A bare invocation used to mean `scan`, so `dropwatch --since 2025` ran one.
    It no longer does, and argparse alone would answer "unrecognized arguments:
    --since 2025", which omits the one thing worth saying.
    """
    found: list[str] = []
    skip_next = False
    for token in argv:
        if skip_next:
            skip_next = False
            continue  # a flag's value, not a subcommand
        if not token.startswith("-"):
            break  # a subcommand ends the top-level region
        name, joined, _ = token.partition("=")
        if name in SCAN_FLAGS and name not in found:
            found.append(name)
        if not joined and name in VALUE_FLAGS:
            skip_next = True
    return found


def _make_clients(config: Config, store: Store, refresh: bool):
    navidrome_http = HttpClient(
        timeout=config.request_timeout,
        user_agent=USER_AGENT,
        verify_tls=config.verify_tls,
    )
    deezer_http = HttpClient(
        timeout=config.request_timeout,
        user_agent=USER_AGENT,
        rate_limiter=make_rate_limiter(),
    )
    client = NavidromeClient(config, navidrome_http)
    provider = DeezerProvider(deezer_http, cache=None if refresh else store)
    return client, provider


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    parser = build_parser()

    stray = _misplaced_scan_flags(argv)
    if stray:
        program = invocation_name()
        verb = "belongs" if len(stray) == 1 else "belong"
        print(
            f"error: {', '.join(stray)} {verb} to `{program} scan`.",
            file=sys.stderr,
        )
        print(f"  Try: {program} scan {' '.join(argv)}", file=sys.stderr)
        return ExitCode.USAGE

    args = parser.parse_args(_apply_aliases(argv))
    # Restore the defaults that SUPPRESS omitted.
    args.verbose = getattr(args, "verbose", 0)
    args.env_file = getattr(args, "env_file", None)
    setup_logging(args.verbose)

    if args.command is None:
        # No subcommand does nothing but explain itself. Scanning is a slow,
        # network-heavy operation and should be asked for by name.
        parser.print_help()
        return ExitCode.OK

    command = args.command
    try:
        if command == "scan":
            return cmd_scan(args)
        if command == "setup":
            return cmd_setup(args)
        if command == "config":
            return cmd_config(args)
        if command == "check":
            return cmd_check(args)
        if command == "fix":
            return cmd_fix(args)
        if command == "status":
            return cmd_status(args)
        if command == "map":
            return cmd_map(args)
        if command == "unmap":
            return cmd_unmap(args)
        if command in ("block", "unblock"):
            return cmd_block(args, undo=command == "unblock")
        if command == "cache":
            return cmd_cache(args)
    except DropWatchError as exc:
        _report_error(exc)
        return exc.exit_code
    except KeyboardInterrupt:
        print("\ninterrupted", file=sys.stderr)
        return ExitCode.FAILURE

    parser.error(f"unknown command {command!r}")
    return ExitCode.USAGE


def _report_error(exc: DropWatchError) -> None:
    print(f"error: {exc.message}", file=sys.stderr)
    if exc.hint:
        print(f"  {exc.hint}", file=sys.stderr)


def _open_store(config: Config) -> Store:
    return Store(config.cache_path, max_age_hours=config.cache_max_age_hours)


def _load(args) -> Config:
    """Config for a command that does not need a Navidrome server."""
    return load_config(env_file=args.env_file, require_navidrome=False)


def _deezer_only(config: Config, store: Store) -> DeezerProvider:
    """A Deezer client. Deezer's public API needs no credentials."""
    return DeezerProvider(
        HttpClient(
            timeout=config.request_timeout,
            user_agent=USER_AGENT,
            rate_limiter=make_rate_limiter(),
        ),
        cache=store,
    )


def cmd_scan(args) -> int:
    config = load_config(env_file=args.env_file)

    if args.type:
        types = {_TYPE_ALIASES[t] for t in args.type}
    elif config.release_types:
        types = {t for t in ReleaseType if t.value in config.release_types}
    else:
        types = None
    options = ScanOptions(
        artist_filters=args.artist,
        limit=args.limit,
        since=args.since,
        types=types,
        progress=not args.no_progress,
    )

    with _open_store(config) as store:
        client, provider = _make_clients(config, store, refresh=args.refresh)
        client.ping()

        scanner = Scanner(client, provider, store, config)
        result = scanner.run(options)

        items = sort_releases(result.missing)
        print_results(items, grouped=not args.flat)
        summary = build_summary(
            items,
            result.unresolved,
            result.review,
            result.artists_scanned,
            result.partial_reasons,
        )
        if items:
            print(file=sys.stdout)
        print_summary(summary)
        print_review(result.review)
        print_unresolved(result.unresolved)

    return ExitCode.PARTIAL if summary.partial else ExitCode.OK


def _settings_path(args) -> Path:
    """Where `config` writes. Follows discovery, else the user config dir."""
    if args.env_file:
        return Path(args.env_file).expanduser()
    found = find_env_file(None)
    return found if found is not None else user_config_dir() / ".env"


def cmd_setup(args) -> int:
    from .setup_wizard import run_setup

    return run_setup(env_file=args.env_file)


def cmd_config(args) -> int:
    # A bare `config` shows the settings; there is no separate `list`.
    action = getattr(args, "config_action", None) or "show"
    path = _settings_path(args)

    if action == "path":
        print(path)
        return ExitCode.OK

    if action == "show":
        rows = describe_settings(args.env_file)
        width = max(len(r.setting.key) for r in rows)
        value_width = max(len(r.display) for r in rows)
        for row in rows:
            source = "" if row.value is None else f"({row.source})"
            line = f"{row.setting.key:<{width}}  {row.display:<{value_width}}  {source}"
            print(line.rstrip())
        print(
            f"\nChange one with: {invocation_name()} config set <key> <value>"
            f"\n         or all: {invocation_name()} setup"
        )
        missing = [r.setting.key for r in rows if r.setting.required and not r.value]
        if missing:
            # stdout is block-buffered when piped; flush so the note below
            # cannot overtake the table it refers to.
            sys.stdout.flush()
            print(
                f"\nNot configured: {', '.join(missing)}. "
                f"Run `{invocation_name()} setup`.",
                file=sys.stderr,
            )
        return ExitCode.OK

    if action == "password":
        if not sys.stdin.isatty():
            print("error: setting a password needs an interactive terminal.", file=sys.stderr)
            return ExitCode.CONFIG
        import getpass

        value = getpass.getpass("Navidrome password: ")
        if not value:
            print("No password entered; nothing changed.", file=sys.stderr)
            return ExitCode.CONFIG
        if value != getpass.getpass("Confirm: "):
            print("error: passwords did not match.", file=sys.stderr)
            return ExitCode.CONFIG
        _write_one(path, SETTINGS_BY_KEY["password"].env_var, value)
        print(f"Password updated in {path}")
        return ExitCode.OK

    setting = SETTINGS_BY_KEY[args.key]

    if action == "unset":
        _write_one(path, setting.env_var, None)
        print(f"Removed {args.key} from {path}")
        return ExitCode.OK

    # `set`. Secrets are rejected at the parser via choices, but guard anyway:
    # a password in argv lands in shell history and is visible to `ps`.
    if setting.secret:
        print(
            f"error: refusing to take a {args.key} on the command line, where it "
            "would be saved in your shell history.",
            file=sys.stderr,
        )
        print(f"  Use: {invocation_name()} config password", file=sys.stderr)
        return ExitCode.CONFIG

    value = args.value
    if args.key == "url":
        value = normalize_url(value)
    _write_one(path, setting.env_var, value)
    print(f"Set {args.key} = {value}")

    conflict = next(
        (r for r in describe_settings(args.env_file) if r.setting.key == args.key), None
    )
    if conflict is not None and conflict.source == "environment":
        print(
            f"note: ${setting.env_var} is set in your environment and still "
            "overrides this file.",
            file=sys.stderr,
        )
    return ExitCode.OK


def _write_one(path: Path, env_var: str, value: str | None) -> None:
    """Update a single key, leaving every other setting untouched."""
    values = load_dotenv(path)
    if value is None:
        values.pop(env_var, None)
    else:
        values[env_var] = value
    save_settings(path, values)


def cmd_check(args) -> int:
    config = load_config(env_file=args.env_file)
    with _open_store(config) as store:
        client, _ = _make_clients(config, store, refresh=False)
        server = client.ping()
        artists = client.get_artists()
    print(f"Connected to {config.navidrome_url} ({server})")
    print(f"{len(artists)} album artists visible")
    return ExitCode.OK


def _local_album_titles(config: Config, store: Store) -> tuple[set[str], object]:
    """Album titles you own, for the artist picker, plus a Deezer client.

    The titles are an aid, not a requirement: they let the picker put the
    decisive evidence first — "which Ghost is mine" is answerable from a track
    listing. Without a configured server the picker still works.
    """
    if not config.has_navidrome:
        return set(), _deezer_only(config, store)

    client, provider = _make_clients(config, store, refresh=False)
    titles: set[str] = set()
    try:
        from .normalize import parse_title

        for album in client.get_all_albums():
            base = parse_title(album.name).base
            if base:
                titles.add(base)
    except DropWatchError as exc:
        log.info("Could not read local albums for comparison: %s", exc)
    return titles, provider


def cmd_fix(args) -> int:
    """Answer whatever the last scan could not settle.

    One command for both kinds of question, because having to know whether a
    problem was an artist problem or a release problem before choosing a
    command was the wrong thing to ask of anyone.
    """
    from .resolve_ui import run_resolve
    from .review_ui import decide_one, run_review

    artist = args.artist or args.target
    if artist and args.album:
        print("error: give either an artist or --album, not both.", file=sys.stderr)
        return ExitCode.USAGE
    if args.decision and not args.album:
        print(
            "error: --own/--missing/--clear apply to a release, so they need "
            "--album <id or URL>.",
            file=sys.stderr,
        )
        return ExitCode.USAGE

    config = _load(args)
    with _open_store(config) as store:
        if args.album:
            return decide_one(
                store, _deezer_only(config, store), args.album, args.decision
            )

        if artist:
            local_albums, provider = _local_album_titles(config, store)
            return run_resolve(store, provider, local_albums, only=artist)

        artists_pending = store.load_unresolved()
        releases_pending = store.load_review()
        if not artists_pending and not releases_pending:
            print(f"Nothing to fix. Run `{invocation_name()} scan` first.")
            return ExitCode.OK

        code = ExitCode.OK
        if artists_pending:
            local_albums, provider = _local_album_titles(config, store)
            code = run_resolve(store, provider, local_albums)
            if code != ExitCode.OK:
                return code
        if releases_pending:
            if artists_pending:
                print("\n─── releases ───")
            code = run_review(store)
        return code


def cmd_status(args) -> int:
    from .review_ui import describe_release

    config = _load(args)
    program = invocation_name()
    with _open_store(config) as store:
        unresolved = store.load_unresolved()
        review = store.load_review()
        mappings = store.list_mappings()
        decisions = store.release_decisions()

        if args.decided:
            # The provider serves album metadata from the same store, so this
            # only reaches the network for a release never looked up before —
            # worth it, since a bare id does not tell you what you decided on.
            provider = _deezer_only(config, store) if decisions else None
            return _print_decided(store, provider, mappings, decisions, describe_release)

    blocked = [m for m in mappings if m.is_blocked]
    mapped = [m for m in mappings if not m.is_blocked]

    if unresolved or review:
        print("Needs you")
        if unresolved:
            print(f"  {len(unresolved)} artist(s) could not be matched to Deezer")
        if review:
            print(f"  {len(review)} release(s) need a decision")
        print(f"\n  Work through them:  {program} fix")
    elif mappings or decisions:
        print("Nothing pending.")
    else:
        print(f"No scan recorded yet. Run `{program} scan`.")
        return ExitCode.OK

    if mappings or decisions:
        print("\nSaved")
        if mapped:
            print(f"  {len(mapped)} artist mapping(s)")
        if blocked:
            print(f"  {len(blocked)} blocked artist(s)")
        if decisions:
            counts = Counter(decisions.values())
            detail = ", ".join(f"{n} {name}" for name, n in sorted(counts.items()))
            print(f"  {len(decisions)} release decision(s): {detail}")
        print(f"\n  List them:  {program} status --decided")

    return ExitCode.OK


def _print_decided(store: Store, provider, mappings, decisions, describe_release) -> int:
    """Everything you have told it, so a wrong answer can be found and undone."""
    program = invocation_name()
    if not mappings and not decisions:
        print("Nothing decided yet.")
        return ExitCode.OK

    mapped = [m for m in mappings if not m.is_blocked]
    blocked = [m for m in mappings if m.is_blocked]

    if mapped:
        width = max(len(m.local_name) for m in mapped)
        print(f"Artist mappings ({len(mapped)})")
        for mapping in mapped:
            print(f"  {mapping.local_name:<{width}}  ->  {mapping.describe()}")
        print(f'\n  Undo one:  {program} unmap "<artist>"\n')

    if blocked:
        print(f"Blocked artists ({len(blocked)})")
        for mapping in blocked:
            print(f"  {mapping.local_name}")
        print(f'\n  Undo one:  {program} unblock --artist "<artist>"\n')

    if decisions:
        print(f"Release decisions ({len(decisions)})")
        for release_id, decision in sorted(decisions.items()):
            entry = describe_release(store, provider, release_id)
            artist = entry.get("artist")
            label = f"{artist} — {entry['title']}" if artist else entry["title"]
            print(f"  {decision:<8}  {label}")
            print(f"            {entry['url']}")
        print(f"\n  Undo one:  {program} fix --album <id> --clear")

    return ExitCode.OK


def cmd_map(args) -> int:
    config = _load(args)
    ids = list(dict.fromkeys(args.deezer_id))  # de-duplicate, keep order
    with _open_store(config) as store:
        provider = _deezer_only(config, store)
        targets = []
        for deezer_id in ids:
            artist = provider.get_artist(deezer_id)
            if artist is None:
                print(f"error: Deezer has no artist with id {deezer_id}", file=sys.stderr)
                return ExitCode.DEEZER
            targets.append(MappingTarget(artist.id, artist.name))
        store.set_mapping(args.local_name, targets)
    joined = ", ".join(f"{t.deezer_name} [{t.deezer_id}]" for t in targets)
    print(f"Mapped {args.local_name!r} -> {joined}")
    if len(targets) > 1:
        print("Their discographies will be merged and de-duplicated.")
    return ExitCode.OK


def cmd_unmap(args) -> int:
    config = _load(args)
    with _open_store(config) as store:
        removed = store.clear_mapping(args.local_name)
    if removed:
        print(
            f"Cleared {args.local_name!r}. It will be resolved from scratch on "
            "the next scan."
        )
    else:
        print(f"No saved entry for {args.local_name!r}.")
    return ExitCode.OK


def cmd_block(args, undo: bool) -> int:
    """`block` / `unblock`, for either an artist or a single release."""
    from . import block_cmd

    artist = args.artist or args.target
    if args.album and args.target:
        print(
            "error: give either an artist or --album, not both.", file=sys.stderr
        )
        return ExitCode.USAGE
    if not artist and not args.album:
        verb = "unblock" if undo else "block"
        program = invocation_name()
        print(f"error: nothing to {verb}.", file=sys.stderr)
        print(f'  {program} {verb} --artist "<name, id or URL>"', file=sys.stderr)
        print(f"  {program} {verb} --album <id or URL>", file=sys.stderr)
        return ExitCode.USAGE

    config = _load(args)
    with _open_store(config) as store:
        provider = _deezer_only(config, store)
        if args.album:
            action = block_cmd.unblock_album if undo else block_cmd.block_album
            return action(store, provider, args.album)
        action = block_cmd.unblock_artist if undo else block_cmd.block_artist
        return action(store, provider, artist)


def cmd_cache(args) -> int:
    config = _load(args)
    with _open_store(config) as store:
        if args.clear:
            store.clear_cache()
            print("Cache cleared.")
        if args.reset_mappings:
            count = store.reset_mappings()
            print(f"Removed {count} artist mapping(s).")
        if getattr(args, "reset_decisions", False):
            count = store.reset_release_decisions()
            print(f"Removed {count} review decision(s).")
        if not args.clear and not args.reset_mappings and not getattr(
            args, "reset_decisions", False
        ):
            age = store.cache_age_hours()
            stats = store.cache_stats()
            print(f"State file: {config.cache_path}")
            print(f"Mappings:   {len(store.list_mappings())}")
            print(f"Decisions:  {store.count_release_decisions()}")
            print(
                "Cache age:  "
                + (f"{age:.1f}h" if age is not None else "empty")
            )
            if stats["total"]:
                print(
                    f"Entries:    {stats['total']} "
                    f"({stats['volatile']} refreshed every "
                    f"{config.cache_max_age_hours:g}h, "
                    f"{stats['stable']} long-lived)"
                )
    return ExitCode.OK

