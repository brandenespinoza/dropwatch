"""Interactive resolution of ambiguous artists.

The scan never asks questions; this is where the answers get given. For each
artist the scan could not resolve confidently, the user picks one or more
Deezer artists, blocks the artist, clears what is known, or leaves it alone.

Candidates are shown with a few of their album titles, because "which Ghost is
mine" is answerable from a track listing and almost never from a fan count.
"""

from __future__ import annotations

import re
import sys
from dataclasses import dataclass

from .config import invocation_name
from .deezer import DeezerProvider
from .errors import ExitCode, DropWatchError
from .models import DeezerArtist
from .normalize import parse_title
from .state import MappingTarget, Store

#: Candidates shown per artist. Each costs a cached discography request.
MAX_SHOWN = 6
#: Album titles listed under each candidate.
SAMPLE_TITLES = 3

_ID_IN_URL = re.compile(r"deezer\.com/(?:[a-z]{2}/)?artist/(\d+)")


@dataclass
class Candidate:
    artist: DeezerArtist
    titles: list[str]


def _extract_id(text: str) -> str | None:
    """Accept a bare Deezer artist id or a full artist URL."""
    match = _ID_IN_URL.search(text)
    if match:
        return match.group(1)
    stripped = text.strip()
    return stripped if stripped.isdigit() else None


def _load_candidates(
    provider: DeezerProvider, raw: list[dict], local_albums: set[str]
) -> list[Candidate]:
    candidates: list[Candidate] = []
    for entry in raw[:MAX_SHOWN]:
        artist = DeezerArtist(
            id=str(entry.get("id", "")),
            name=entry.get("name", ""),
            nb_fan=int(entry.get("fans") or 0),
        )
        titles: list[str] = []
        try:
            releases = provider.get_discography(artist.id)
        except DropWatchError:
            releases = []
        # Prefer titles the local library also has: those are the decisive ones.
        shared = [r for r in releases if parse_title(r.title).base in local_albums]
        for release in (shared + releases)[: SAMPLE_TITLES * 2]:
            if release.title not in titles:
                titles.append(release.title)
            if len(titles) >= SAMPLE_TITLES:
                break
        candidates.append(Candidate(artist, titles))
    return candidates


def _show(name: str, reason: str, candidates: list[Candidate], mapped: bool) -> None:
    print(f"\n{name}")
    print(f"  {reason}")
    if not candidates:
        print("  No candidates were recorded for this artist.")
    for index, candidate in enumerate(candidates, start=1):
        artist = candidate.artist
        print(
            f"  {index}. {artist.name}  "
            f"[id {artist.id}]  {artist.nb_fan:,} fans"
        )
        for title in candidate.titles:
            print(f"       {title}")
    actions = ["number(s) to map", "[s]kip", "[b]lock"]
    if mapped:
        actions.append("[c]lear")
    actions += ["[d] enter an id or URL", "[q]uit"]
    print("  " + "  ".join(actions))


def _parse_selection(answer: str, count: int) -> list[int] | None:
    """Parse "1", "1 3", "1,3" into zero-based indexes. None if not a selection."""
    tokens = [t for t in re.split(r"[\s,]+", answer.strip()) if t]
    if not tokens or not all(t.isdigit() for t in tokens):
        return None
    picks = []
    for token in tokens:
        index = int(token) - 1
        if not 0 <= index < count:
            return None
        if index not in picks:
            picks.append(index)
    return picks


def entry_for_artist(provider: DeezerProvider, name: str) -> dict:
    """Build a picker entry for any artist, searching Deezer fresh.

    Needed because a mapped artist never appears in the unresolved list again,
    so without this there is no way back to the picker after a wrong choice.
    """
    try:
        found = provider.search_artists(name, limit=MAX_SHOWN)
    except DropWatchError as exc:
        found = []
        reason = f"could not search Deezer: {exc}"
    else:
        reason = (
            f"{len(found)} Deezer artist(s) match this name"
            if found
            else "no Deezer artist matches this name"
        )
    return {
        "name": name,
        "reason": reason,
        "candidates": [
            {"id": a.id, "name": a.name, "fans": a.nb_fan} for a in found
        ],
    }


def run_resolve(
    store: Store,
    provider: DeezerProvider,
    local_albums: set[str],
    only: str | None = None,
) -> int:
    """Walk unresolved artists, or re-open one by name. Returns an exit code."""
    if not sys.stdin.isatty():
        print(
            "error: resolve needs an interactive terminal.",
            file=sys.stderr,
        )
        print(
            f"  Use `{invocation_name()} map \"<artist>\" <id> [<id> ...]` in scripts.",
            file=sys.stderr,
        )
        return ExitCode.CONFIG

    if only:
        unresolved = [entry_for_artist(provider, only)]
        existing = store.get_mapping(only)
        if existing is not None:
            print(f"{only} is currently mapped to: {existing.describe()}")
    else:
        unresolved = store.load_unresolved()
        if not unresolved:
            print(f"Nothing to resolve. Run `{invocation_name()} scan` first.")
            return ExitCode.OK
        print(f"{len(unresolved)} unresolved artist(s) from the last scan.")

    print("Press Enter to leave an artist as it is.")

    changed = 0
    for position, entry in enumerate(unresolved, start=1):
        name = entry["name"]
        existing = store.get_mapping(name)
        candidates = _load_candidates(provider, entry.get("candidates", []), local_albums)

        print(f"\n─── {position}/{len(unresolved)} ───")
        _show(name, entry.get("reason", ""), candidates, mapped=existing is not None)

        while True:
            try:
                answer = input("  > ").strip()
            except EOFError:
                answer = "q"

            if answer.lower() in ("q", "quit"):
                _report(changed)
                return ExitCode.OK

            if answer == "" or answer.lower() in ("s", "skip"):
                break

            if answer.lower() in ("b", "block"):
                store.block_artist(name)
                print(f"  Blocking {name}. It will not be reported again.")
                changed += 1
                break

            if answer.lower() in ("c", "clear"):
                if store.clear_mapping(name):
                    print(f"  Cleared {name}. It will be resolved from scratch next scan.")
                    changed += 1
                else:
                    print("  Nothing was stored for this artist.")
                break

            manual = answer[1:].strip() if answer.lower().startswith("d") else answer
            deezer_id = _extract_id(manual)
            if deezer_id and not _parse_selection(answer, len(candidates)):
                try:
                    artist = provider.get_artist(deezer_id)
                except DropWatchError as exc:
                    print(f"  Could not reach Deezer: {exc}")
                    continue
                if artist is None:
                    print(f"  Deezer has no artist with id {deezer_id}.")
                    continue
                store.set_mapping(name, [MappingTarget(artist.id, artist.name)])
                print(f"  Mapped {name} -> {artist.name} [{artist.id}]")
                changed += 1
                break

            picks = _parse_selection(answer, len(candidates))
            if picks is None:
                print("  Enter one or more numbers, or s / b / d / q.")
                continue

            targets = [
                MappingTarget(candidates[i].artist.id, candidates[i].artist.name)
                for i in picks
            ]
            store.set_mapping(name, targets)
            joined = ", ".join(f"{t.deezer_name} [{t.deezer_id}]" for t in targets)
            print(f"  Mapped {name} -> {joined}")
            if len(targets) > 1:
                print("  Their discographies will be merged and de-duplicated.")
            changed += 1
            break

    _report(changed)
    return ExitCode.OK


def _report(changed: int) -> None:
    if changed:
        print(f"\n{changed} artist(s) updated. Run a scan to pick up the change.")
    else:
        print("\nNothing changed.")
