"""Collapsing duplicate Deezer catalogue entries.

Deezer routinely lists the same underlying release several times: territorial
variants, explicit and clean pairs, and reissues each get their own album ID.
Printing all of them would bury the actual news.

Grouping is by base title, version markers, artist and release type, so
genuinely distinct products stay separate: a live album never merges into the
studio album, and a single never merges into the album of the same name.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from typing import Callable, Iterable, TypeVar

from .models import DeezerRelease, ReleaseType
from .normalize import parse_title

log = logging.getLogger("dropwatch.dedupe")

T = TypeVar("T")


def _group_key(release: DeezerRelease, type_hint: str, artist_key: str | None = None) -> tuple:
    """Identity of a release for duplicate detection.

    `artist_key` overrides the Deezer artist id. That matters when one local
    artist is mapped to several Deezer ids: the same album listed under two of
    them is one release, and keying on the Deezer id would keep both.
    """
    parsed = parse_title(release.title)
    return (
        artist_key if artist_key is not None else release.artist_id,
        parsed.base,
        parsed.versions,
        type_hint,
    )


def _canonical_sort_key(release: DeezerRelease) -> tuple:
    """Order within a duplicate group; the first entry wins.

    The earliest date is preferred because Deezer's duplicates are usually
    reissues of one original product, and the original date is the honest
    answer to "when did this come out". Remaining ties are broken by richness
    then by ID so runs are reproducible.
    """
    date = release.release_date
    # Unknown dates sort last within the group rather than winning by default.
    date_rank = date.sort_key if date.year else (9999, 99, 99, 9)
    return (
        date_rank,
        -(release.nb_tracks or 0),
        -release.fans,
        release.id,
    )


def deduplicate(
    releases: Iterable[DeezerRelease],
    type_of: Callable[[DeezerRelease], str] | None = None,
    artist_key: str | None = None,
) -> tuple[list[DeezerRelease], dict[str, list[DeezerRelease]]]:
    """Return (canonical releases, duplicates keyed by canonical release ID)."""
    type_of = type_of or (lambda r: (r.record_type or "").lower())

    by_upc: dict[str, list[DeezerRelease]] = defaultdict(list)
    groups: dict[tuple, list[DeezerRelease]] = defaultdict(list)

    for release in releases:
        # An identical UPC is the same commercial product, full stop.
        if release.upc:
            by_upc[release.upc].append(release)
        else:
            groups[_group_key(release, type_of(release), artist_key)].append(release)

    for upc, members in by_upc.items():
        groups[("upc", upc)].extend(members)

    canonical: list[DeezerRelease] = []
    duplicates: dict[str, list[DeezerRelease]] = {}

    for members in groups.values():
        members.sort(key=_canonical_sort_key)
        winner, rest = members[0], members[1:]
        canonical.append(winner)
        if rest:
            duplicates[winner.id] = rest
            log.debug(
                "Collapsed %d duplicate listing(s) into %r", len(rest), winner.title
            )

    canonical.sort(key=_canonical_sort_key)
    return canonical, duplicates


def dedupe_results(items: list, type_getter: Callable[[T], ReleaseType]) -> list:
    """Second-pass dedupe over final results, using the resolved release type.

    Applied after classification because the type is part of the identity of a
    release, and it is only known reliably once detail has been fetched.
    """
    seen: dict[tuple, object] = {}
    ordered: list = []
    for item in items:
        release: DeezerRelease = item.release
        # Keyed on the local artist so merged Deezer ids collapse together.
        key = _group_key(release, type_getter(item).value, item.local_artist.casefold())
        existing = seen.get(key)
        if existing is None:
            seen[key] = item
            ordered.append(item)
            continue
        # Keep whichever entry the canonical rule prefers.
        if _canonical_sort_key(release) < _canonical_sort_key(existing.release):
            ordered[ordered.index(existing)] = item
            seen[key] = item
    return ordered
