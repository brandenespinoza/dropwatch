"""Handing releases from the last scan to an external downloader.

DropWatch does not download anything and does not know how. It prints a Deezer
URL and runs whatever command you configured against it; the default targets
streamrip (``rip url <url>``), but the setting is a template, so any tool that
takes a URL works, as does a script of your own.

The subprocess boundary is deliberate rather than a shortcut. Credentials for
the download service stay in that tool's own configuration and never reach
this one, a crash there cannot take this process's state with it, and
switching downloaders is a settings change instead of a code change.

Ripping still records nothing. A download is not a claim that you own
something, so the files land on disk, Navidrome indexes them on its own
schedule, and the next scan sees them the same way it sees everything else.
The only writes are ``o`` and ``b``, which store the same two decisions ``fix``
stores — answers the user gave, not inferences drawn from having downloaded
something. Nothing is recorded as a consequence of a download succeeding.
"""

from __future__ import annotations

import shlex
import shutil
import subprocess
import sys

from .config import URL_PLACEHOLDER, invocation_name
from .errors import ConfigError, ExitCode
from .models import DECISION_BLOCKED, DECISION_OWNED, ReleaseDate
from .report import GROUP_ORDER

#: Returned by `run_one` when the command could not be started at all, as
#: distinct from a command that ran and failed.
NOT_RUN = -1

#: Type order, taken from the report rather than restated, so the walk and the
#: printed table can never disagree about what comes first.
_TYPE_RANK = {release_type.value: rank for rank, release_type in enumerate(GROUP_ORDER)}

#: Decisions that suppress a release, matching what `determine_ownership` does
#: with them. DECISION_MISSING is the third and means the opposite, so it is
#: deliberately absent.
_SUPPRESSING = (DECISION_OWNED, DECISION_BLOCKED)


def pending(entries: list[dict], decisions: dict[str, str]) -> list[dict]:
    """Drop releases already answered with a decision that suppresses them.

    The queue is what the last scan concluded, so a decision recorded since
    then has not reached it. Without this, blocking a release here would
    re-offer it on the next walk — which is the one thing blocking is supposed
    to prevent — and a release marked owned by `fix --album <id> --own` would
    keep being offered after the user said they have it.

    Filtered at read time for the same reason the ordering is: the stored queue
    is rewritten by scans that know nothing about decisions made since.
    """

    def suppressed(entry: dict) -> bool:
        release_id = str(entry.get("id") or "")
        # An entry with no id is looked up against nothing: a decision cannot
        # have been recorded for it, and the empty string must not match one.
        return bool(release_id) and decisions.get(release_id) in _SUPPRESSING

    return [entry for entry in entries if not suppressed(entry)]


def order_queue(entries: list[dict]) -> list[dict]:
    """Albums, then EPs, then singles, newest first within each group.

    The stored queue is in scan order — artists alphabetically, discography
    order within each — which is the order the work happened in, not an order
    anyone wants to download in. Sorting happens here rather than when the
    queue is written because a filtered scan merges old entries with new ones
    and would undo it: read time is the only place the order is guaranteed.

    Mirrors the report's ordering, so the walk arrives in the same sequence the
    table printed.
    """

    def key(entry: dict):
        rank = _TYPE_RANK.get(entry.get("type") or "", len(GROUP_ORDER))
        # Negated for descending: the components are non-negative ints, and an
        # unknown date is all zeros, so it sinks below every real date.
        date = tuple(-n for n in ReleaseDate.parse(entry.get("date")).sort_key)
        return (rank, date, (entry.get("artist") or "").casefold(),
                (entry.get("title") or "").casefold())

    return sorted(entries, key=key)


def build_command(template: str, url: str) -> list[str]:
    """Split the template into argv, then substitute the URL into the tokens.

    Order matters. Substituting into the string first and splitting afterwards
    would let a URL containing a space or a quote become extra arguments;
    splitting first means the URL lands as exactly one argv entry whatever it
    contains. Nothing is passed through a shell.
    """
    try:
        parts = shlex.split(template)
    except ValueError as exc:
        raise ConfigError(
            f"rip-command is not a valid command line: {exc}",
            hint=f"Check for an unbalanced quote: {template!r}",
        ) from None
    if not parts:
        raise ConfigError(
            "rip-command is empty.",
            hint=f'Set one: `{invocation_name()} config set rip-command "rip url {URL_PLACEHOLDER}"`',
        )
    if URL_PLACEHOLDER not in template:
        raise ConfigError(
            f"rip-command does not contain {URL_PLACEHOLDER}, so it would ignore the release.",
            hint=f'For example: "rip url {URL_PLACEHOLDER}"',
        )
    return [part.replace(URL_PLACEHOLDER, url) for part in parts]


def _label(entry: dict) -> str:
    artist = entry.get("artist") or ""
    title = entry.get("title") or f"Deezer release {entry.get('id', '')}"
    return f"{artist} — {title}" if artist else title


def _show(entry: dict, position: int, total: int) -> None:
    print(f"\n─── {position}/{total} ───")
    print(_label(entry))
    details = ", ".join(p for p in (entry.get("type"), entry.get("date")) if p)
    if details:
        print(f"  {details}")
    # "probably missing" is worth surfacing here: it is the matcher saying it
    # could not fully rule out that you already have this.
    if entry.get("ownership") == "probably_missing":
        print("  probably missing — not certain you lack this")
    if entry.get("url"):
        print(f"  {entry['url']}")


def run_one(command: list[str]) -> int:
    """Run the downloader for one release, letting its output through.

    Output is inherited rather than captured so progress bars and prompts from
    the downloader behave normally. Returns its exit status, or NOT_RUN when
    the command could not be started.
    """
    print(f"\n  $ {shlex.join(command)}\n", flush=True)
    try:
        return subprocess.run(command).returncode
    except FileNotFoundError:
        print(f"  error: {command[0]} is not installed or not on PATH.", file=sys.stderr)
        return NOT_RUN
    except PermissionError:
        print(f"  error: {command[0]} is not executable.", file=sys.stderr)
        return NOT_RUN
    except KeyboardInterrupt:
        # Ctrl-C reached the child too. Abandoning one download should return
        # to the prompt, not end the whole session.
        print("\n  interrupted; that release was not finished.", file=sys.stderr)
        return NOT_RUN


def _preflight(command: list[str]) -> str | None:
    """Complain before the walk, not after 40 prompts. Returns an error hint."""
    if shutil.which(command[0]) is None:
        return command[0]
    return None


#: What each answer stores, and what to say once it is stored. Both suppress
#: the release; only `o` claims anything about the library. `fix` draws that
#: distinction and the wording here keeps it rather than offering two
#: identical-looking ways to make something disappear.
_DECISIONS = {
    "own": (DECISION_OWNED, "  Marked as owned. It will not be reported again."),
    "block": (
        DECISION_BLOCKED,
        "  Blocked. It will not be reported again — without claiming you own it.",
    ),
}


def _decide(store, entry: dict, answer: str) -> int:
    """Record what the user said about a release. Returns 1 if stored.

    The same decisions `fix` records, against the same Deezer release id, so
    `fix --album <id> --clear` and `unblock --album <id>` reverse them.
    """
    decision, confirmation = _DECISIONS[answer]
    release_id = str(entry.get("id") or "")
    if not release_id:
        print(
            "  Cannot record that: the scan noted no Deezer id for this release.",
            file=sys.stderr,
        )
        return 0
    store.set_release_decision(release_id, decision)
    print(confirmation)
    return 1


def run_rip(store, config) -> int:
    """Walk the last scan's missing releases, ripping the chosen ones."""
    stored = store.load_missing()
    entries = order_queue(pending(stored, store.release_decisions()))
    if not entries:
        # Distinguished, because "run a scan" is unhelpful advice when the
        # queue is full and the user has simply answered all of it.
        print(
            "Nothing left to rip; every release from the last scan is decided."
            if stored
            else "Nothing to rip. Run a scan first."
        )
        return ExitCode.OK

    if not sys.stdin.isatty():
        print("error: rip needs an interactive terminal.", file=sys.stderr)
        print(
            f"  It asks about each release before running `{config.rip_command}`.",
            file=sys.stderr,
        )
        return ExitCode.CONFIG

    # Built once: the template is a setting, so a broken one is broken for
    # every release and should be reported before anything is asked.
    probe = build_command(config.rip_command, "https://www.deezer.com/album/0")
    missing_binary = _preflight(probe)
    if missing_binary:
        print(f"error: {missing_binary} is not installed or not on PATH.", file=sys.stderr)
        print(
            f"  Install it, or point somewhere else: "
            f"`{invocation_name()} config set rip-command \"...\"`",
            file=sys.stderr,
        )
        return ExitCode.CONFIG

    total = len(entries)
    print(f"{total} release(s) from the last scan.")
    print("Press Enter to skip one.")

    ripped = failed = owned = blocked = 0
    rip_everything = False

    for position, entry in enumerate(entries, start=1):
        url = entry.get("url") or f"https://www.deezer.com/album/{entry.get('id', '')}"
        _show(entry, position, total)

        if not rip_everything:
            answer = _ask()
            if answer == "quit":
                break
            if answer == "skip":
                continue
            if answer == "own":
                owned += _decide(store, entry, answer)
                continue
            if answer == "block":
                blocked += _decide(store, entry, answer)
                continue
            if answer == "all":
                rip_everything = True

        status = run_one(build_command(config.rip_command, url))
        if status == 0:
            ripped += 1
            print("  ✓ done")
        else:
            failed += 1
            if status != NOT_RUN:
                print(f"  ✗ exited {status}", file=sys.stderr)

    _report(ripped, failed, owned, blocked)
    return ExitCode.OK


def _ask() -> str:
    """Prompt for one release. Returns an answer name, or "skip" / "quit"."""
    while True:
        # `o` and `b` are the keys `fix` already uses for these two decisions.
        # A third prompt in the same tool must not spell them differently.
        print(
            "  [r] rip   [s]kip   [o] I own it   [b]lock   "
            "[a]ll remaining   [q]uit"
        )
        try:
            answer = input("  > ").strip().lower()
        except EOFError:
            return "quit"

        if answer in ("q", "quit"):
            return "quit"
        # Enter skips. Starting a download is never the thing that happens
        # when you are holding the key down to get through the list.
        if answer == "" or answer in ("s", "skip"):
            return "skip"
        if answer in ("r", "rip"):
            return "rip"
        if answer in ("o", "own", "owned"):
            return "own"
        if answer in ("b", "block"):
            return "block"
        if answer in ("a", "all"):
            return "all"
        print("  Enter r, s, o, b, a or q.")


def _report(ripped: int, failed: int, owned: int, blocked: int) -> None:
    if not any((ripped, failed, owned, blocked)):
        print("\nNothing ripped.")
        return
    parts = [f"{ripped} ripped"]
    if failed:
        parts.append(f"{failed} failed")
    if owned:
        parts.append(f"{owned} marked owned")
    if blocked:
        parts.append(f"{blocked} blocked")
    print(f"\n{', '.join(parts)}.")
    if ripped:
        # Said plainly because the alternative is assuming a scan is now wrong.
        # Named rather than "these": the decided releases counted on the line
        # above are the ones here that do not come back.
        print(
            "Ripped releases stay in the results until Navidrome indexes the "
            "files and you scan again."
        )
