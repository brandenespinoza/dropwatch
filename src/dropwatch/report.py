"""Terminal output.

Results go to stdout as a plain aligned table so the output pipes cleanly.
Warnings, unresolved artists and the review section go to stderr, keeping
stdout to just the answer the user asked for.
"""

from __future__ import annotations

import shutil
import sys
import unicodedata
from dataclasses import dataclass, field

from .config import invocation_name
from .models import MissingRelease, ReleaseType, ReviewItem, UnresolvedArtist

# "RELEASE DATE" is itself 12 cells wide, so the column must be wider than the
# header or the header runs into the next one.
DATE_WIDTH = 14
TYPE_WIDTH = 9
ARTIST_GAP = 2
URL_GAP = 2
MIN_TITLE_WIDTH = 20
MAX_ARTIST_WIDTH = 30
# Wide enough that a piped table still fits the title and the Deezer URL
# (https://www.deezer.com/album/1234567890) without squeezing either.
FALLBACK_TERMINAL_WIDTH = 140


def display_width(text: str) -> int:
    """Width in terminal cells, counting CJK and emoji as two columns."""
    return sum(2 if unicodedata.east_asian_width(c) in "WF" else 1 for c in text)


def _pad(text: str, width: int) -> str:
    padding = width - display_width(text)
    return text + " " * padding if padding > 0 else text


def _truncate(text: str, width: int) -> str:
    if display_width(text) <= width:
        return text
    if width <= 1:
        return "…"
    out, used = [], 0
    for char in text:
        char_width = 2 if unicodedata.east_asian_width(char) in "WF" else 1
        if used + char_width > width - 1:
            break
        out.append(char)
        used += char_width
    return "".join(out) + "…"


def _fit(text: str, width: int) -> str:
    return _pad(_truncate(text, width), width)


def terminal_width(stream=sys.stdout) -> int:
    if not stream.isatty():
        return FALLBACK_TERMINAL_WIDTH
    return max(60, shutil.get_terminal_size((FALLBACK_TERMINAL_WIDTH, 24)).columns)


@dataclass
class Summary:
    missing: int = 0
    albums: int = 0
    eps: int = 0
    singles: int = 0
    unknown: int = 0
    unresolved_artists: int = 0
    review_items: int = 0
    artists_scanned: int = 0
    partial_reasons: list[str] = field(default_factory=list)

    @property
    def partial(self) -> bool:
        return bool(self.partial_reasons)


def sort_releases(items: list[MissingRelease]) -> list[MissingRelease]:
    """Global reverse-chronological order, newest first.

    Within a date, less precise dates fall below fully dated releases, and
    unknown dates sink to the bottom. Remaining ties resolve alphabetically by
    artist then title so repeated runs print identically.
    """
    return sorted(items, key=lambda item: item.sort_key, reverse=True)


#: Group headings, in the order they are printed.
GROUP_ORDER = (ReleaseType.ALBUM, ReleaseType.EP, ReleaseType.SINGLE, ReleaseType.UNKNOWN)
GROUP_LABELS = {
    ReleaseType.ALBUM: "Albums",
    ReleaseType.EP: "EPs",
    ReleaseType.SINGLE: "Singles",
    ReleaseType.UNKNOWN: "Unclassified",
}


def _layout(items: list[MissingRelease], total_width: int) -> tuple[int, int, int]:
    """Column widths for artist, title and URL, fitted to the terminal."""
    artist_width = min(
        MAX_ARTIST_WIDTH,
        max([display_width(i.local_artist) for i in items] + [len("ARTIST")]),
    )
    url_width = max([display_width(i.release.link) for i in items] + [len("URL")])
    fixed = DATE_WIDTH + artist_width + ARTIST_GAP + TYPE_WIDTH + url_width + URL_GAP
    title_width = max(MIN_TITLE_WIDTH, total_width - fixed)
    return artist_width, title_width, url_width


def _row(item: MissingRelease, artist_width: int, title_width: int) -> str:
    # The type column is kept even when grouping, so every line stays
    # self-describing when the output is piped somewhere.
    return (
        _fit(str(item.date), DATE_WIDTH)
        + _fit(item.local_artist, artist_width + ARTIST_GAP)
        + _fit(item.release_type.value, TYPE_WIDTH)
        # Truncate to the title width but pad to the wider column, so a title
        # that fills its column still keeps a gap before the URL.
        + _pad(_truncate(item.release.title, title_width), title_width + URL_GAP)
        + item.release.link
    ).rstrip()


def render_table(
    items: list[MissingRelease], width: int | None = None, grouped: bool = True
) -> list[str]:
    """Render the result table.

    `items` must already be globally sorted; grouping only partitions that
    order, so releases stay newest-first within each type.
    """
    if not items:
        return []
    total_width = width or terminal_width()
    artist_width, title_width, _ = _layout(items, total_width)

    header = (
        _fit("RELEASE DATE", DATE_WIDTH)
        + _fit("ARTIST", artist_width + ARTIST_GAP)
        + _fit("TYPE", TYPE_WIDTH)
        + _fit("TITLE", title_width + URL_GAP)
        + "URL"
    )
    lines = [header]

    if not grouped:
        lines.extend(_row(i, artist_width, title_width) for i in items)
        return lines

    for release_type in GROUP_ORDER:
        group = [i for i in items if i.release_type is release_type]
        if not group:
            continue
        lines.append("")
        lines.append(f"{GROUP_LABELS[release_type]} ({len(group)})")
        lines.extend(_row(i, artist_width, title_width) for i in group)
    return lines


def print_results(
    items: list[MissingRelease], stream=sys.stdout, grouped: bool = True
) -> None:
    for line in render_table(items, terminal_width(stream), grouped=grouped):
        print(line, file=stream)


def _plural(count: int, singular: str, plural: str | None = None) -> str:
    return f"{count} {singular if count == 1 else (plural or singular + 's')}"


def print_summary(summary: Summary, stream=sys.stdout) -> None:
    parts = []
    if summary.albums:
        parts.append(_plural(summary.albums, "album"))
    if summary.eps:
        parts.append(_plural(summary.eps, "EP"))
    if summary.singles:
        parts.append(_plural(summary.singles, "single"))
    if summary.unknown:
        parts.append(f"{summary.unknown} unclassified")

    headline = _plural(summary.missing, "missing release")
    if parts:
        headline += ": " + ", ".join(parts)
    print(headline, file=stream)

    if summary.unresolved_artists:
        print(
            f"{_plural(summary.unresolved_artists, 'artist')} could not be resolved",
            file=stream,
        )
    if summary.review_items:
        verb = "requires" if summary.review_items == 1 else "require"
        print(f"{_plural(summary.review_items, 'release')} {verb} review", file=stream)
    if summary.partial:
        print(
            "output is partial: " + "; ".join(summary.partial_reasons),
            file=stream,
        )


def print_review(items: list[ReviewItem], stream=sys.stderr) -> None:
    if not items:
        return
    print(f"\nNeeds review ({len(items)}):", file=stream)
    for item in sorted(items, key=lambda i: (i.local_artist.casefold(), i.release.title)):
        print(
            f"  {item.local_artist} — {item.release.title} "
            f"[{item.release_type.value}, {item.release.release_date}]: {item.reason}",
            file=stream,
        )
    print(f"\n  Decide on these with: {invocation_name()} fix", file=stream)


def print_unresolved(items: list[UnresolvedArtist], stream=sys.stderr) -> None:
    if not items:
        return
    print(f"\nUnresolved artists ({len(items)}):", file=stream)
    for item in sorted(items, key=lambda i: i.name.casefold()):
        print(f"  {item.name}: {item.reason}", file=stream)
        for candidate in item.candidates[:3]:
            print(
                f"      candidate: {candidate.name} "
                f"(deezer id {candidate.id}, {candidate.nb_fan:,} fans)",
                file=stream,
            )
    program = invocation_name()
    print(
        f"\n  Work through these with: {program} fix\n"
        f'  Or set one directly:     {program} map "<artist>" <id> [<id> ...]',
        file=stream,
    )


def build_summary(
    items: list[MissingRelease],
    unresolved: list[UnresolvedArtist],
    review: list[ReviewItem],
    artists_scanned: int,
    partial_reasons: list[str],
) -> Summary:
    summary = Summary(
        missing=len(items),
        unresolved_artists=len(unresolved),
        review_items=len(review),
        artists_scanned=artists_scanned,
        partial_reasons=list(partial_reasons),
    )
    for item in items:
        if item.release_type is ReleaseType.ALBUM:
            summary.albums += 1
        elif item.release_type is ReleaseType.EP:
            summary.eps += 1
        elif item.release_type is ReleaseType.SINGLE:
            summary.singles += 1
        else:
            summary.unknown += 1
    return summary
