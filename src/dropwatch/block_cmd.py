"""`block` and `unblock`: suppressing an artist, or one release.

Two things can be unwanted in the results, and they are identified in different
ways. An artist is a row in your library, so it is named locally. A release is
a Deezer record, so it is named by its Deezer id or URL — the one printed in
the results list, which is the whole point: you see a line you don't want and
paste it back.

Both spellings are accepted for `--artist` regardless, because an artist with
no Deezer counterpart at all is exactly the kind you most want to block, and
it has no id to give.
"""

from __future__ import annotations

import re
import sys

from .config import invocation_name
from .errors import ExitCode, DropWatchError
from .models import DECISION_BLOCKED
from .state import Store

_ARTIST_ID_IN_URL = re.compile(r"deezer\.com/(?:[a-z]{2}/)?artist/(\d+)")


def artist_id_or_none(text: str) -> str | None:
    """A Deezer artist id from a URL or a bare number, else None (a name)."""
    match = _ARTIST_ID_IN_URL.search(text or "")
    if match:
        return match.group(1)
    stripped = (text or "").strip()
    return stripped if stripped.isdigit() else None


def resolve_artist(store: Store, provider, value: str) -> tuple[list[str], str | None]:
    """Turn a name, Deezer id or Deezer URL into local artist name(s).

    Returns (names, error). An id already mapped resolves to every local artist
    pointing at it; an unmapped id is looked up on Deezer and its name used,
    which is what matches when the library spells the artist the same way.
    """
    value = (value or "").strip()
    if not value:
        return [], "no artist given"

    deezer_id = artist_id_or_none(value)
    if deezer_id is None:
        return [value], None

    mapped = store.mappings_for_deezer_id(deezer_id)
    if mapped:
        return [m.local_name for m in mapped], None

    if provider is None:
        return [], f"nothing is mapped to Deezer artist {deezer_id}"

    try:
        artist = provider.get_artist(deezer_id)
    except DropWatchError as exc:
        return [], f"could not reach Deezer: {exc}"
    if artist is None:
        return [], f"Deezer has no artist with id {deezer_id}"
    return [artist.name], None


def _fail(message: str, hint: str | None = None) -> int:
    print(f"error: {message}.", file=sys.stderr)
    if hint:
        print(f"  {hint}", file=sys.stderr)
    return ExitCode.USAGE


# --- artists ---------------------------------------------------------------


def block_artist(store: Store, provider, value: str) -> int:
    names, error = resolve_artist(store, provider, value)
    if error:
        return _fail(error)
    for name in names:
        store.block_artist(name)
        print(f"Blocking {name!r}. No releases by this artist will be reported.")
    print(f"Undo with: {invocation_name()} unblock --artist {_quote(names[0])}")
    return ExitCode.OK


def unblock_artist(store: Store, provider, value: str) -> int:
    names, error = resolve_artist(store, provider, value)
    if error:
        return _fail(error)

    for name in names:
        mapping = store.get_mapping(name)
        if mapping is None:
            print(f"{name!r} is not blocked; nothing to undo.")
            continue
        if not mapping.is_blocked:
            # Clearing here would silently throw away a mapping the user chose.
            print(f"{name!r} is not blocked — it is mapped to {mapping.describe()}.")
            print(f"  To clear that mapping: {invocation_name()} unmap {_quote(name)}")
            continue
        store.clear_mapping(name)
        print(f"No longer blocking {name!r}. It will be resolved on the next scan.")
    return ExitCode.OK


# --- releases --------------------------------------------------------------


def block_album(store: Store, provider, value: str) -> int:
    from .review_ui import describe_release, extract_album_id

    release_id = extract_album_id(value)
    if release_id is None:
        return _fail(
            f"{value!r} is not a Deezer album id or URL",
            "Copy the URL from the results list, or just its trailing number.",
        )

    entry = describe_release(store, provider, release_id)
    store.set_release_decision(release_id, DECISION_BLOCKED)
    print(f"Blocking {_label(entry)}. It will not be reported again.")
    print(f"Undo with: {invocation_name()} unblock --album {release_id}")
    return ExitCode.OK


def unblock_album(store: Store, provider, value: str) -> int:
    from .review_ui import describe_release, extract_album_id

    release_id = extract_album_id(value)
    if release_id is None:
        return _fail(
            f"{value!r} is not a Deezer album id or URL",
            "Copy the URL from the results list, or just its trailing number.",
        )

    entry = describe_release(store, provider, release_id)
    if store.clear_release_decision(release_id):
        print(f"No longer blocking {_label(entry)}. It will be judged normally again.")
    else:
        print(f"No decision was stored for {_label(entry)}.")
    return ExitCode.OK


def _label(entry: dict) -> str:
    artist = entry.get("artist")
    return f"{artist} — {entry['title']}" if artist else entry["title"]


def _quote(name: str) -> str:
    return f'"{name}"' if " " in name else name
