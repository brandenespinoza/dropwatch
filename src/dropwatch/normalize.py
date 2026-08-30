"""Text normalization for artist and release titles.

The central distinction, and the reason this module is not just
``str.lower()``: some parenthetical suffixes are *cosmetic* and must be
ignored when deciding whether two releases are the same product ("Deluxe
Edition", "2011 Remaster"), while others are *meaningful* and must be
preserved because they denote a different recording ("Live", "Acoustic",
"Radio Edit"). Collapsing the second group would make the tool claim you own
a live album because you own the studio one.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from difflib import SequenceMatcher

# --- Character folding -----------------------------------------------------

_APOSTROPHES = dict.fromkeys(map(ord, "‘’‛ʼ´`"), "'")
_QUOTES = dict.fromkeys(map(ord, "“”„«»"), '"')
_DASHES = dict.fromkeys(map(ord, "‐‑‒–—―−"), "-")
_TRANSLATIONS = {**_APOSTROPHES, **_QUOTES, **_DASHES}

_PUNCT_RE = re.compile(r"[^\w\s]", re.UNICODE)
_WS_RE = re.compile(r"\s+")


def fold(text: str) -> str:
    """Case-, accent- and punctuation-insensitive form used for comparison.

    Accents are folded so "Bjork" matches "Björk", but non-Latin scripts are
    left intact rather than transliterated, since we have no reliable way to
    romanise them consistently across two catalogues.
    """
    if not text:
        return ""
    text = text.translate(_TRANSLATIONS)
    # NFKD splits accents into combining marks so they can be dropped.
    text = unicodedata.normalize("NFKD", text)
    text = "".join(c for c in text if not unicodedata.combining(c))
    text = text.casefold()
    text = text.replace("&", " and ")
    text = text.replace("+", " and ")
    # Apostrophes are deleted rather than turned into spaces so "Don't" and
    # "Dont" fold together instead of becoming "don t".
    text = text.replace("'", "")
    text = _PUNCT_RE.sub(" ", text)
    return _WS_RE.sub(" ", text).strip()


def tokens(text: str) -> list[str]:
    return fold(text).split()


# --- Marker vocabularies ---------------------------------------------------

# Cosmetic: two releases differing only by these are the same recording.
_EDITION_PATTERNS = [
    r"(?:super\s+)?deluxe(?:\s+(?:edition|version|remaster(?:ed)?))?",
    r"expanded(?:\s+(?:edition|version))?",
    r"(?:\d+(?:st|nd|rd|th)\s+)?anniversary(?:\s+(?:edition|version|remaster(?:ed)?))?",
    r"(?:\d{4}\s+)?(?:digital\s+|digitally\s+)?remaster(?:ed)?(?:\s+version)?(?:\s+\d{4})?",
    r"original\s+recording\s+remastered",
    r"bonus\s+track(?:s)?(?:\s+(?:version|edition))?",
    r"with\s+bonus\s+tracks?",
    r"(?:special|standard|limited|collector'?s|platinum|gold|tour|premium)\s+edition",
    r"(?:explicit|clean|edited)(?:\s+(?:version|content))?",
    r"international(?:\s+(?:version|edition))?",
    r"(?:uk|us|usa|eu|european|japan|japanese|german|french|australian|canadian)"
    r"\s+(?:version|edition|release)",
    r"re[\s-]?issue",
    r"reissued",
    r"original\s+(?:motion\s+picture\s+)?(?:album|master)",
    r"the\s+remaster(?:ed)?",
]

# Meaningful: these denote a genuinely different recording or mix.
_VERSION_PATTERNS = [
    r"live(?:\s+(?:at|from|in|on)\s+.+)?(?:\s+(?:version|session|recording))?",
    r"acoustic(?:\s+(?:version|mix|session))?",
    r"unplugged",
    r"(?:.+\s+)?remix(?:es|ed)?",
    r"(?:club|extended|radio|dub|instrumental|dance|house|12\"?|7\"?)\s+(?:mix|edit|version)",
    r"radio\s+edit",
    r"instrumental(?:\s+version)?",
    r"a\s?cappella",
    r"demo(?:\s+version)?",
    r"alternat(?:e|ive)\s+(?:take|version|mix|cut)",
    r"(?:re[\s-]?recorded|re[\s-]?recording)(?:\s+version)?",
    r".{0,20}'?s\s+version",  # "Taylor's Version"
    r"(?:bbc\s+|peel\s+|studio\s+)?session(?:s)?",
    r"mono(?:\s+version)?",
    r"stereo(?:\s+version)?",
    r"karaoke(?:\s+version)?",
    r"(?:piano|orchestral|symphonic|string|choral)\s+(?:version|arrangement)",
    r"(?:single|album|extended|short|long|full[\s-]?length|original)\s+(?:version|edit|mix|cut)",
    r"(?:slowed|sped\s?up|reverb)(?:\s*\+?\s*reverb)?",
    r"(?:spanish|french|german|italian|japanese|korean|portuguese|english)\s+version",
    r"(?:cover|tribute|karaoke)(?:\s+version)?",
    r"edit",
    r"reprise",
]

_EDITION_RE = re.compile(r"^(?:%s)$" % "|".join(_EDITION_PATTERNS), re.IGNORECASE)
_VERSION_RE = re.compile(r"^(?:%s)$" % "|".join(_VERSION_PATTERNS), re.IGNORECASE)

# Secondary characteristics, recognised anywhere in the title. Internal only.
_TRAIT_PATTERNS = {
    "live": r"\blive\b|\bunplugged\b|\bconcert\b",
    "compilation": r"\bgreatest\s+hits\b|\bbest\s+of\b|\banthology\b|\bcollection\b"
    r"|\bcompilation\b|\bessentials?\b|\bsingles\b",
    "remix": r"\bremix(?:es|ed)?\b|\bmixes\b",
    "soundtrack": r"\bsoundtrack\b|\bo\.?s\.?t\.?\b|\bmotion\s+picture\b|\bscore\b",
    "deluxe": r"\bdeluxe\b",
    "expanded": r"\bexpanded\b",
    "remaster": r"\bremaster(?:ed)?\b",
    "reissue": r"\bre[\s-]?issue(?:d)?\b",
    "mixtape": r"\bmixtape\b",
    "promo": r"\bpromo(?:tional)?\b",
    "anniversary": r"\banniversary\b",
    "acoustic": r"\bacoustic\b",
    "instrumental": r"\binstrumental\b",
    "karaoke": r"\bkaraoke\b",
    "tribute": r"\btribute\b|\bperformed\s+by\b|\bin\s+the\s+style\s+of\b",
}
_TRAIT_RES = {name: re.compile(p, re.IGNORECASE) for name, p in _TRAIT_PATTERNS.items()}

# Segments split off a title: (bracketed) [bracketed] or " - trailing".
_BRACKET_RE = re.compile(r"[\(\[\{]([^\)\]\}]*)[\)\]\}]")
_DASH_SPLIT_RE = re.compile(r"\s+[-–—]\s+")


@dataclass(frozen=True)
class ParsedTitle:
    """A release title decomposed into identity, cosmetics and version."""

    original: str
    base: str
    editions: frozenset[str]
    versions: frozenset[str]
    traits: frozenset[str]

    @property
    def is_plain(self) -> bool:
        """True when nothing marks this as a special version."""
        return not self.versions

    def same_recording_as(self, other: ParsedTitle) -> bool:
        """Same underlying recording: identical base *and* version markers."""
        return self.base == other.base and self.versions == other.versions


def _classify_segment(segment: str) -> str:
    """Return 'edition', 'version' or 'other' for a bracketed/dashed segment."""
    cleaned = fold(segment)
    if not cleaned:
        return "other"
    if _EDITION_RE.match(cleaned):
        return "edition"
    if _VERSION_RE.match(cleaned):
        return "version"
    # "feat. X" inside brackets is neither; treat as droppable noise on titles.
    if re.match(r"^(?:feat|ft|featuring|with)\b", cleaned):
        return "edition"
    return "other"


def parse_title(title: str) -> ParsedTitle:
    """Split a title into base identity, cosmetic editions and version markers.

    Unrecognised segments stay in the base title, so an unfamiliar suffix makes
    two titles differ rather than silently collapsing them together.
    """
    original = (title or "").strip()
    editions: set[str] = set()
    versions: set[str] = set()

    remainder = original
    kept_segments: list[str] = []

    def handle(segment: str) -> None:
        kind = _classify_segment(segment)
        if kind == "edition":
            editions.add(fold(segment))
        elif kind == "version":
            versions.add(_canonical_version(segment))
        else:
            kept_segments.append(segment)

    # Bracketed segments first, removing them from the remaining text.
    for match in _BRACKET_RE.finditer(original):
        handle(match.group(1))
    remainder = _BRACKET_RE.sub(" ", remainder)

    # Then " - suffix" segments; the first chunk is always the title itself.
    parts = _DASH_SPLIT_RE.split(remainder)
    head, tail = parts[0], parts[1:]
    for segment in tail:
        handle(segment)

    base_source = " ".join([head, *kept_segments])
    base = fold(base_source)

    traits = {name for name, rx in _TRAIT_RES.items() if rx.search(original)}
    # A version marker implies the matching trait even if worded unusually.
    for version in versions:
        for name in ("live", "remix", "acoustic", "instrumental", "karaoke"):
            if name in version:
                traits.add(name)

    return ParsedTitle(
        original=original,
        base=base,
        editions=frozenset(editions),
        versions=frozenset(versions),
        traits=frozenset(traits),
    )


def _canonical_version(segment: str) -> str:
    """Collapse version wording so "Live at X" and "Live in Y" both read live.

    Venue and year detail is dropped: two live recordings of the same album are
    treated as the same version, which is the conservative choice here.
    """
    cleaned = fold(segment)
    for keyword in (
        "live",
        "acoustic",
        "unplugged",
        "remix",
        "instrumental",
        "karaoke",
        "demo",
        "mono",
        "stereo",
        "radio edit",
        "reprise",
    ):
        if re.search(rf"\b{re.escape(keyword)}\b", cleaned):
            return keyword
    return cleaned


# --- Artist names ----------------------------------------------------------

_LEADING_ARTICLE_RE = re.compile(r"^(?:the|a|an|los|las|le|la|les|el|die|der|das)\s+")


def artist_key(name: str) -> str:
    return fold(name)


def artist_key_variants(name: str) -> set[str]:
    """Comparison keys for an artist name.

    Deliberately does *not* split on ``&``, ``and``, ``feat.``, ``vs.`` or
    commas: those strings occur inside real artist names ("Florence + the
    Machine", "Earth, Wind & Fire"), and splitting them invents artists that
    do not exist.
    """
    base = fold(name)
    if not base:
        return set()
    variants = {base}
    without_article = _LEADING_ARTICLE_RE.sub("", base)
    if without_article:
        variants.add(without_article)
    # "and" spelled out vs symbol is already unified by fold(); also try the
    # compact form for names like "AC/DC" -> "ac dc" -> "acdc".
    variants.add(base.replace(" ", ""))
    return {v for v in variants if v}


def credited_to(credit: str, variants: set[str]) -> bool:
    """Does this credit string name an artist with one of these keys?

    Matched on whole tokens rather than substrings, because a display credit is
    free text: "Ghost" must not be found inside "Ghostface Killah", while
    "Ghost" does have to be found inside "Ghost feat. Tobias Forge". Structured
    credit arrays are already split into real names and compare exactly; this
    exists for the flat display strings, which are not.
    """
    words = fold(credit).split()
    if not words:
        return False
    for variant in variants:
        wanted = variant.split()
        if not wanted:
            continue
        span = len(wanted)
        if any(words[i : i + span] == wanted for i in range(len(words) - span + 1)):
            return True
    return False


# --- Similarity ------------------------------------------------------------


def similarity(a: str, b: str) -> float:
    """Ratio in [0, 1] over already-folded strings."""
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    return SequenceMatcher(None, a, b).ratio()


def token_overlap(a: str, b: str) -> float:
    """Jaccard-style containment: fraction of the smaller token set shared."""
    ta, tb = set(a.split()), set(b.split())
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / min(len(ta), len(tb))


def title_similarity(a: str, b: str) -> float:
    """Blend of sequence ratio and token containment for folded titles."""
    return max(similarity(a, b), token_overlap(a, b) * 0.95)


# --- Track titles ----------------------------------------------------------


def track_key(title: str, version: str = "") -> tuple[str, frozenset[str]]:
    """Identity key for a recording: base title plus meaningful version markers.

    Deezer exposes ``title_version`` separately ("(Live)"), while local tags
    usually fold it into the title, so both are parsed the same way.
    """
    # Deezer's title_version arrives either bare ("Live") or already
    # parenthesised ("(Live)"), so it is normalised to the bracketed form that
    # local tags use before parsing.
    marker = (version or "").strip().strip("()[]{}").strip()
    combined = f"{title} ({marker})" if marker else (title or "")
    parsed = parse_title(combined)
    return parsed.base, parsed.versions


def durations_match(a: int | None, b: int | None, tolerance: int = 5) -> bool | None:
    """Compare durations in seconds.

    Returns None when either side is unknown, so callers can tell "different"
    from "cannot tell". The default tolerance absorbs encoder padding and
    gapless trimming without letting a genuinely different edit slip through.
    """
    if not a or not b:
        return None
    return abs(a - b) <= tolerance
