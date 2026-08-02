"""Interactive review of ambiguous releases.

Every release the matcher could not settle used to print to stderr on every
run, with no way to answer it. This is that answer: the user says whether they
own it, the decision is stored against the Deezer release id, and the question
is not asked again.
"""

from __future__ import annotations

import re
import sys

from .config import invocation_name
from .errors import ExitCode, DropWatchError
from .models import DECISION_MISSING, DECISION_OWNED
from .state import Store

_ID_IN_URL = re.compile(r"deezer\.com/(?:[a-z]{2}/)?album/(\d+)")


def extract_album_id(text: str) -> str | None:
    """Accept a bare Deezer album id or the URL printed in the results."""
    match = _ID_IN_URL.search(text or "")
    if match:
        return match.group(1)
    stripped = (text or "").strip()
    return stripped if stripped.isdigit() else None


def describe_release(store: Store, provider, release_id: str) -> dict:
    """Find a release to show: from the review queue, else fetch it."""
    for entry in store.load_review():
        if str(entry.get("id")) == release_id:
            return entry
    entry = {
        "id": release_id,
        "artist": "",
        "title": f"Deezer release {release_id}",
        "type": "",
        "date": "",
        "reason": "not in the review queue; decided directly",
        "url": f"https://www.deezer.com/album/{release_id}",
    }
    if provider is None:
        return entry
    try:
        payload = provider.album_summary(release_id)
    except DropWatchError:
        return entry
    if payload:
        entry["title"] = payload.get("title") or entry["title"]
        entry["artist"] = (payload.get("artist") or {}).get("name", "")
        entry["date"] = payload.get("release_date") or ""
        entry["type"] = (payload.get("record_type") or "").title()
    return entry


def _show(entry: dict, position: int, total: int, current: str | None) -> None:
    print(f"\n─── {position}/{total} ───")
    print(f"{entry['artist']} — {entry['title']}")
    print(f"  {entry['type']}, {entry['date']}")
    print(f"  {entry['reason']}")
    if entry.get("url"):
        print(f"  {entry['url']}")
    if current:
        print(f"  currently marked: {current}")
    print("  [o] I own it   [m] I don't, report it   [s]kip   [u]ndo   [q]uit")


def decide_one(
    store: Store, provider, target: str, decision: str | None
) -> int:
    """Record a decision about a single release named by id or URL."""
    release_id = extract_album_id(target)
    if release_id is None:
        print(
            f"error: {target!r} is not a Deezer album id or URL.",
            file=sys.stderr,
        )
        print(
            "  Copy the URL from the results list, or just its trailing number.",
            file=sys.stderr,
        )
        return ExitCode.USAGE

    entry = describe_release(store, provider, release_id)
    label = f"{entry['artist']} — {entry['title']}" if entry["artist"] else entry["title"]

    if decision == "clear":
        if store.clear_release_decision(release_id):
            print(f"Cleared the decision for {label}.")
        else:
            print(f"No decision was stored for {label}.")
        return ExitCode.OK

    if decision is not None:
        store.set_release_decision(release_id, decision)
        if decision == DECISION_OWNED:
            print(f"Marked {label} as owned. It will not be reported again.")
        else:
            print(f"{label} will be reported as missing.")
        return ExitCode.OK

    if not sys.stdin.isatty():
        print("error: deciding interactively needs a terminal.", file=sys.stderr)
        print(
            f"  Use `{invocation_name()} fix --album {release_id} --own` instead.",
            file=sys.stderr,
        )
        return ExitCode.CONFIG

    decisions = store.release_decisions()
    _show(entry, 1, 1, decisions.get(release_id))
    _ask(store, release_id)  # returns a status string, not an exit code
    return ExitCode.OK


def run_review(store: Store) -> int:
    """Walk the ambiguous releases from the last scan. Returns an exit code."""
    if not sys.stdin.isatty():
        print("error: review needs an interactive terminal.", file=sys.stderr)
        print(
            f"  Non-interactively, use `{invocation_name()} scan` and read the "
            "review section on stderr.",
            file=sys.stderr,
        )
        return ExitCode.CONFIG

    entries = store.load_review()
    if not entries:
        print("Nothing to review. Run a scan first.")
        return ExitCode.OK

    decisions = store.release_decisions()
    print(f"{len(entries)} release(s) need review from the last scan.")
    print("Press Enter to leave one undecided.")

    changed = 0
    for position, entry in enumerate(entries, start=1):
        release_id = str(entry["id"])
        _show(entry, position, len(entries), decisions.get(release_id))

        outcome = _ask(store, release_id)
        if outcome == "quit":
            _report(changed)
            return ExitCode.OK
        if outcome == "changed":
            changed += 1

    _report(changed)
    return ExitCode.OK


def _ask(store: Store, release_id: str):
    """Prompt for one release. Returns "changed", "skipped" or "quit"."""
    while True:
        try:
            answer = input("  > ").strip().lower()
        except EOFError:
            answer = "q"

        if answer in ("q", "quit"):
            return "quit"
        if answer == "" or answer in ("s", "skip"):
            return "skipped"
        if answer in ("o", "own", "owned"):
            store.set_release_decision(release_id, DECISION_OWNED)
            print("  Marked as owned. It will not be reported again.")
            return "changed"
        if answer in ("m", "missing"):
            store.set_release_decision(release_id, DECISION_MISSING)
            print("  Marked as missing. It will appear in the main list.")
            return "changed"
        if answer in ("u", "undo"):
            if store.clear_release_decision(release_id):
                print("  Decision cleared; it will be judged normally again.")
                return "changed"
            print("  No decision was stored for this release.")
            return "skipped"
        print("  Enter o, m, s, u or q.")


def _report(changed: int) -> None:
    if changed:
        print(f"\n{changed} decision(s) recorded. Run a scan to see the effect.")
    else:
        print("\nNothing changed.")
