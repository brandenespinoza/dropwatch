# DropWatch — product specification

A local Python command-line tool that checks a Navidrome music library against
Deezer and prints releases by artists in the library that appear not to be
owned.

This document describes the product **as built**. Sections that were open
questions in the original brief now record the decision that was made and why.
Constraints marked as invariants still hold and must survive future changes.

Status: complete and working. 443 automated tests, no runtime dependencies.

## Core behavior

A manual run:

1. Loads and validates configuration.
2. Connects to Navidrome and authenticates.
3. Reads artists, albums and (lazily) tracks from the library.
4. Resolves each local artist to a Deezer artist.
5. Retrieves each resolved artist's full Deezer discography.
6. Determines which releases are not in the local library.
7. Prints missing releases to the terminal, grouped and sorted.
8. Prints a compact summary.
9. Exits with a meaningful code.

It is a manually executed command-line tool. It is not a daemon, scheduled
service, web application, background worker, downloader, cloud service or
notification system. **(Invariant.)**

## Output

Results go to **stdout**. Warnings, unresolved artists, the review section and
all logging go to **stderr**, so redirecting stdout yields a clean file.

Results are grouped by release type — Albums, then EPs, then Singles, then
Unclassified — with a count per group. Within each group, releases are sorted by
release date descending, newest first, across all artists. Results are never
grouped by artist.

Each line carries release date, artist, release type, release title and the
Deezer URL. The type column is retained even though the group heading repeats
it, so every line remains self-describing when piped.

```text
RELEASE DATE  ARTIST          TYPE     TITLE            URL

Albums (1)
2026-06-02    Artist Name     Album    Album Title      https://www.deezer.com/album/302127

EPs (1)
2026-07-18    Another Artist  EP       Another Release  https://www.deezer.com/album/825535241

Singles (1)
2026-07-24    Artist Name     Single   Release Title    https://www.deezer.com/album/14894641
```

`--flat` produces one continuous list in pure date order with no group headings
or blank lines.

Release type is one of **Album**, **EP**, **Single**, or **Unknown** when the
release cannot be classified responsibly.

Long titles are truncated with an ellipsis rather than wrapped. URLs are never
truncated — the title absorbs the squeeze, because a clipped URL is useless.
Column alignment accounts for double-width CJK characters.

Normal output contains no raw API responses, architecture detail, debug logging
or match scores.

The summary is compact:

```text
42 missing releases: 8 albums, 6 EPs, 28 singles
5 artists could not be resolved
3 releases require review
```

Lines with a zero count are omitted. A partial run adds a line saying so.

### Live progress

While scanning, each artist that yields something prints a one-line summary to
stderr as it completes, beneath a transient line naming the artist in flight:

```text
[47/196] Fleetwood Mac — 3 missing (2 albums, 1 single)
[52/196] Ghost — unresolved, 2 candidates
```

Summaries, not rows: printing findings in full here would duplicate the report
and the sorted table would read as redundant. Artists with nothing to say are
omitted so the scroll stays dense.

This resolves the tension between streaming and global ordering by using the
stream split already in place — **stdout is the answer, stderr is the process**.
Progress never touches stdout, so redirection stays clean and the final table
keeps its global sort. **(Invariant.)** Suppressed by `--no-progress`, and
skipped when stderr is not a terminal.

## Command-line interface

Every subcommand is named. A bare invocation prints help and does nothing:
scanning contacts two servers and takes minutes, which is not a reasonable
thing to start by accident, and the flags shaping a scan belong to it rather
than to the program as a whole.

```bash
dropwatch scan
```

Scan flags given before a subcommand are a usage error naming both the flag and
the correct form, rather than argparse's bare "unrecognized arguments".

Installed via `pipx install`; see Installation. From a checkout,
`python3 dropwatch.py` and `python3 -m dropwatch` are equivalent. The
root `dropwatch.py` is a shim that puts `src/` ahead of the script directory
on the import path, which is required because the shim and the package share a
name.

| Command | Purpose |
|---|---|
| `scan` | Scan the library and list missing releases |
| `setup` | Guided setup: collect settings, test the connection, save |
| `config` | shows every setting and its source; `set`, `unset`, `password`, `path` |
| `check` | Validate connectivity and credentials, then exit |
| `status` | What the last scan could not settle; `--decided` lists every saved answer |
| `fix [artist]` | Walk those questions — artists then releases — or re-open one artist |
| `fix --album <id\|url>` | Decide on one release: `--own`, `--missing`, `--clear` |
| `map <local> <id>...` | Pin a local artist to one or more Deezer artists |
| `unmap <local>` | Clear everything known about an artist, back to unresolved |
| `block --artist <name\|id\|url>` | Never report releases for this artist; `unblock` undoes it |
| `block --album <id\|url>` | Never report this one release; `unblock --album` undoes it |
| `cache` | Show state location, age and entry counts by expiry class; `--clear`, `--reset-mappings`, `--reset-decisions` |

`resolve`, `review` and `artists` are still accepted as the earlier names for
`fix` and `status`, but are not advertised: splitting the same task by what it
acted on meant knowing which command applied before you could start.

Scan flags: `--artist NAME` (repeatable), `--limit N`, `--since YEAR`,
`--type TYPE` (repeatable), `--flat`, `--refresh`, `--no-progress`.

`--since` accepts `YYYY`, `YYYY-MM` or `YYYY-MM-DD`. Comparison stops at
whichever side is less precise, so a release dated only `2024` is not excluded
by a cutoff of `2024-06-01` — it may fall after it, and filtering it out would
hide a release on a technicality.

`types` may be set persistently so the default output matches how the user
collects; an explicit `--type` overrides it. Concurrency is not configurable:
Navidrome track fetching is threaded at a fixed width, and the dominant cost —
Deezer — is rate-limited and necessarily serial, so a worker knob would promise
a speed-up the architecture cannot deliver.

Global flags, accepted before or after the subcommand: `-v` / `-vv`,
`--env-file PATH`.

The command hierarchy is deliberately shallow. A normal run needs no arguments
and is never interactive.

## Installation

`./install.sh` builds a virtual environment under
`~/.local/share/dropwatch`, installs the package into it **non-editably**,
and links the console script into `~/.local/bin`. The non-editable install is
deliberate: the application is copied, so the source directory can be moved or
deleted without breaking the installed command.

The script is idempotent — it rebuilds the environment from scratch on each run
so an upgrade cannot inherit stale state — checks for Python 3.10+, and reports
when `~/.local/bin` is absent from `PATH`. `--uninstall` removes the
application and launcher while leaving user settings and cache in place.

Running from a checkout with `python3 dropwatch.py` or
`python3 -m dropwatch` remains equivalent.

Because an installed command runs from arbitrary working directories,
configuration and state are both resolved to absolute locations rather than
relative to the current directory.

## Configuration

Settings are read from the first source that supplies them:

1. Real environment variables
2. `--env-file PATH`, or `$DROPWATCH_ENV`
3. `./.env` in the current working directory
4. `~/.config/dropwatch/.env` (override with `$DROPWATCH_CONFIG_DIR`)

A checkout with its own `.env` keeps working while the installed command finds
user-level configuration from anywhere. When nothing is found, the error lists
every location searched. An explicitly named `--env-file` that does not exist is
an error rather than a silent fallback.

Settings are managed through commands rather than by hand-editing, though the
file stays a plain `.env` that can be edited directly.

`setup` is the guided path, used for both first run and later changes. It offers
current values as defaults, **tests the connection before saving**, and on
failure lets the user correct the input in place or save anyway when the server
is merely unreachable. It requires a TTY and says so, pointing at `config set`
for scripted use.

`config` covers targeted changes: `list` (every setting, its value and its
source, password masked), `set`, `unset`, `password` and `path`.

**A secret is never accepted as a command-line argument** — it would be recorded
in shell history and visible in `ps`. `config password` prompts with echo
disabled and confirms. **(Invariant.)**

The file is written atomically: to a temporary file in the same directory, mode
600, then renamed. A failed write cannot truncate a working config, and the file
is never briefly world-readable. Keys the user added by hand are preserved.

Credentials are stored in this file rather than the macOS Keychain. At mode 600
the file is already unreadable by other users, and Keychain does not defend
against processes running as the user — the realistic threat — while adding a
subprocess dependency and a second place configuration can hide.

```text
NAVIDROME_URL=http://your-server:4533
NAVIDROME_USERNAME=
NAVIDROME_PASSWORD=
```

Optional, with defaults:

```text
REQUEST_TIMEOUT_SECONDS=20
CACHE_PATH=~/.local/state/dropwatch/state.sqlite3
CACHE_MAX_AGE_HOURS=24
RELEASE_TYPES=                   # e.g. album,ep — empty means every type
```

`NAVIDROME_URL` is the base URL; `/rest` is appended internally, and a URL that
already ends in `/rest` is corrected. A bare `host:port` is accepted and assumed
to be http. URLs carrying credentials, query strings or fragments are rejected.
No default host is assumed — localhost is never presumed.

There is no checked-in example file: `setup` writes the real one, and a template
that can drift from the settings table is worse than none.

A `.gitignore` covers `.env`, `*.sqlite3` and the usual Python artifacts.

## Environment

Runs locally on macOS against a Navidrome instance on another machine, typically
reachable over a private network or VPN. The tool never configures networking
and assumes the host already resolves.

Python 3.10 or newer; developed and tested on 3.12.

## Navidrome access

Read-only, via the Subsonic / OpenSubsonic REST API. Endpoints used:

- `ping.view` — connectivity, authentication and protocol confirmation
- `getArtists.view` — all album artists
- `getAlbumList2.view` — all albums, paginated 500 at a time
- `getAlbum.view` — tracks for one album, fetched lazily and cached

Album listing is bulk-paginated rather than per-artist, so a library with
hundreds of artists costs a handful of requests instead of hundreds.

Authentication uses Subsonic's salted-token scheme — `t=md5(password+salt)` with
a fresh random salt per request — so the password never appears in a URL, proxy
log or traceback.

Before a full scan, `check` validates the URL, opens a bounded connection,
authenticates, and confirms the endpoint is Subsonic-compatible.

The following conditions are distinguished, each with its own error type and an
actionable hint:

| Condition | Detected via |
|---|---|
| Hostname cannot be resolved | `socket.gaierror` |
| Host offline / no route | `OSError` errno 65, 51, 101, 113 |
| Port incorrect | `ConnectionRefusedError` |
| Connection timed out | `TimeoutError` |
| TLS validation failed | `ssl.SSLCertVerificationError` |
| URL path incorrect | HTTP 404 from the REST base |
| Not Subsonic-compatible | HTML body, non-JSON, or missing `subsonic-response` |
| Credentials rejected | HTTP 401/403, or Subsonic error codes 40–44, 50 |
| Unexpected API response | Any other Subsonic `status: failed` |

Network failures are never reported as credential failures. **(Invariant.)**

Timeouts are configurable so the tool cannot hang when the host is offline.

**The tool must not** modify Navidrome or its database, trigger a scan, change
playlists, ratings or favourites, rename, move, retag or delete files, write
into the music-library directory, or download music. Navidrome and the library
are strictly read-only. A test asserts that no request reaches a mutating
endpoint. **(Invariant.)**

## Deezer access

**Interface: `https://api.deezer.com` — Deezer's public, unauthenticated REST
API. Provenance: public and documented by Deezer, but carrying no stability
guarantee. No credentials and no OAuth application required.**

Catalog access is entirely anonymous: artist search, paginated discographies,
album metadata and full track listings with ISRCs are all available without
authenticating. The tool therefore holds no Deezer credential of any kind, and
sends none. **(Invariant.)**

No third-party Deezer library is used, so there is nothing to audit for hidden
downloading or decryption behaviour.

Endpoints: `/search/artist`, `/artist/{id}`, `/artist/{id}/albums`,
`/album/{id}`, `/album/{id}/tracks`.

### Behaviours verified empirically against the live API

These were measured before the client was written, and the client works around
each:

- `/album/{id}` embeds **at most 25 tracks** and reports `tracks.next: null`
  even when the album has more. The dedicated `/album/{id}/tracks` endpoint
  paginates correctly **and** additionally returns `isrc`, `disk_number` and
  `track_position`, which the embedded list omits. Track data is therefore
  always read from the dedicated endpoint.
- Application errors arrive with **HTTP 200** and an `error` object in the body,
  so HTTP status alone is not a success signal.
- Rate limit is roughly **50 requests per 5 seconds per IP** — 60 concurrent
  requests produced 49 successes and 11 quota errors (code 4).
- Discography pagination is genuine beyond 100 entries (verified to 472 across
  5 pages) and `nb_album` matches the paginated total.
- Artist search does not rank the obvious match first: searching `Björk` returns
  `Björk & Toffe` (31 fans) ahead of `Björk` (id 630).

### Request discipline

Requests are limited to 8/s through a token bucket, below the measured ceiling.
Quota and transient errors are retried three times with exponential backoff and
jitter. Authentication errors are never retried. **(Invariant.)** Everything
fetched is cached.

Work is staged to bound request volume: cheap listing data settles anything that
clearly matches an owned album, and the two-request detail fetch happens only for
releases that still look missing afterwards.

### Prohibitions

**The tool must not** use Deezer to download or decrypt audio, circumvent
subscription restrictions, or modify the account, playlists, favourites or
listening history. It must never automate a Deezer browser login, read cookies
or inspect a browser profile. **(Invariant.)**

All Deezer interaction is isolated in `src/dropwatch/deezer.py`. Matching,
classification and reporting see only plain dataclasses, so the provider can be
replaced without touching them.

## Absolute prohibition: no MusicBrainz

MusicBrainz is not used directly or indirectly — no APIs, databases, dumps,
identifiers, tags, Picard, libraries sourcing from it, services repackaging it,
fallbacks or enrichment. Deezer's own catalogue is the sole metadata source.
The tool has zero runtime dependencies, so nothing can query it silently.
**(Invariant.)**

## Artist identification

Names are compared after folding case, accents, apostrophe variants, `&`/`and`,
`+`, and punctuation, plus a leading-article variant and a whitespace-stripped
variant. Non-Latin scripts are preserved rather than transliterated.

Artist names are **never** split on `&`, `and`, `feat.`, `featuring`, `with`,
`vs.` or commas, because those substrings occur inside real names.
**(Invariant.)**

A name match alone is insufficient. Candidates are corroborated against local
album titles, and the candidate that actually shares releases with the library
wins over a more popular same-named artist. Fan count is used only as a weak
tiebreaker. Karaoke, tribute, cover-band and "in the style of" acts are filtered
out unless the local artist is itself one. `Various Artists` and similar
placeholders are skipped.

Resolution outcomes: **resolved**, **ambiguous**, **not found**, **ignored**,
**error**. Confidence is recorded:

| Situation | Outcome |
|---|---|
| Manual mapping present | resolved, confidence 1.0 |
| Exact name + shared album titles | resolved, 0.95 |
| Approximate name + shared album titles | resolved, 0.8 |
| Exact name, library has no albums to corroborate | resolved, 0.75 |
| Lone exact name, no shared titles | resolved, 0.6, logged |
| Several exact names, none matching local albums | ambiguous |
| Only approximate names, no shared titles | ambiguous |
| No sufficiently similar name | not found |

Ambiguous and not-found artists are listed on stderr and never guessed at.
Another artist's releases are never reported on name similarity alone.
**(Invariant.)** One unresolved artist never prevents others from processing.

### Manual mappings

Mappings persist in SQLite and always override automatic matching. **One local
artist may map to several Deezer IDs**, because the catalogue routinely splits
an act across duplicate artist entries each holding part of the discography.
Mapped discographies are merged and de-duplicated by release ID.

Because a merged artist's releases carry different Deezer artist IDs, duplicate
detection keys on the **local** artist rather than the Deezer one. Keying on the
Deezer ID would print an album once per source entry. **(Invariant.)**

A mapping expresses: local artist name, one or more Deezer artist IDs with their
canonical names, ignore, and clear. Clearing removes every mapped ID *and* any
ignore flag, returning the artist to unresolved so the next scan resolves it
from scratch.

`resolve` with an artist name re-opens that artist regardless of its current
state, searching Deezer fresh. Without it a mapped artist is unreachable: it
resolves successfully and so never returns to the unresolved list, leaving a
wrong choice uncorrectable through the picker.

The stored unresolved and review queues are **merged, not replaced**, keyed on
the artists a run actually covered. A full scan covers everyone and so behaves
as a replacement; a filtered scan refreshes only its own share instead of
discarding every other artist's pending questions. **(Invariant.)**

Without an artist name, `resolve` walks the unresolved artists from the last scan, showing each
candidate with a few album titles — those matching the local library first,
since that is the decisive evidence. Per artist the user may select one or
more candidates, skip (leaving it unresolved), ignore permanently, clear,
supply a Deezer ID or URL directly, or quit while keeping earlier decisions.
It requires a TTY and points at `map` for scripted use.

A normal scan is never interactive.

## Release classification

Deezer's `record_type` is the primary signal, cross-checked against track count
and total duration. Structural evidence overrides the declared type only when
the disagreement is large; confidence below 0.4 yields `Unknown`.

Boundaries: album ≥ 7 tracks or ≥ 30 minutes; EP 4–6 tracks; single ≤ 3 tracks.
Few but very long tracks remain an album. `compilation` maps to Album and
records the trait.

Secondary characteristics are recognised internally and inform matching and
deduplication without replacing the primary type: live, compilation, remix,
soundtrack, deluxe, expanded, remaster, reissue, mixtape, promo, anniversary,
acoustic, instrumental, karaoke, tribute.

## Ownership determination

Every release resolves to one of: **owned**, **probably owned**, **missing**,
**probably missing**, **ambiguous**, **ignored**.

Only *missing* and *probably missing* appear in the main list. *Ambiguous* goes
to a separate review section on stderr and is never silently treated as missing.
**(Invariant.)**

An ambiguous release is a question, so it must be answerable. `review` walks the
ambiguous releases from the last scan and records a decision — owned or missing
— against the Deezer release ID. A stored decision short-circuits ownership
determination on the next scan, so the same question is never asked twice.
Without this the review section is write-only and grows without bound.

Decisions apply to **any** release, not only ambiguous ones, so a release in the
main list can be dismissed the same way: `review <id|url> --own`. The results
table prints each release's Deezer URL precisely so it can be used as the
handle. `--missing` forces a release into the report, `--clear` reverts to
normal judgement.

Two independent lines of evidence are used.

**Title identity.** Titles decompose into a base title, cosmetic edition markers
and meaningful version markers.

*Cosmetic — ignored for identity:* deluxe, super deluxe, expanded, anniversary,
remaster (including year forms), bonus track version, special/standard/limited/
collector's/platinum/tour edition, explicit, clean, international and
territorial editions, reissue.

*Meaningful — preserved, because they denote a different recording:* live,
acoustic, unplugged, remix, club/extended/radio/dub mix, radio edit,
instrumental, a cappella, demo, alternate take, re-recording, "'s Version",
session, mono, stereo, karaoke, piano/orchestral arrangements, single/album
version, slowed, sped up, language versions, cover, tribute, reprise.

Unrecognised suffixes stay in the base title, so an unfamiliar marker makes
titles differ rather than silently merging them. Superficial differences in
capitalisation, punctuation, Unicode form, apostrophes, ampersands, whitespace
and brackets are absorbed. Meaningful distinctions are never normalised away.
**(Invariant.)**

**Recording coverage.** Each Deezer track is looked up against every track owned
by that artist, matched on base title, version marker and duration (±5 s, which
absorbs encoder padding without hiding a different edit). Coverage counts tracks
as same, different, unknown or absent.

Combined rules:

- Title and version match, Deezer track count ≤ local → **owned**
- Title matches, Deezer adds tracks, all extras owned → **owned**
- Title matches, nearly all owned, none differing → **probably owned**
- Title matches, edition adds unowned tracks → **probably missing**
- Title matches but years differ by more than one with no edition marker →
  **ambiguous** (possible re-recording)
- No title match, every recording owned with durations confirmed → **owned**
- No title match, all owned but some durations unconfirmed → **probably owned**
- No title match, no recordings owned → **missing**
- No title match, partial overlap → **missing** (album/EP) or
  **probably missing** (single)
- Title closely resembles a local album but is not identical → **ambiguous**
- Track detail unavailable for a single → **ambiguous**

Where both a single and an owned album expose ISRCs, identical ISRCs prove
identical recordings and the single is suppressed.

The bias is deliberate: avoiding a false claim outweighs producing an
artificially complete list. When evidence conflicts, the release is ambiguous.
**(Invariant.)**

## Singles

A single whose only recording is already on an owned album is not reported. A
single carrying an exclusive B-side, a remix, an acoustic take, a live version
or a materially different edit is reported. Where recording identity cannot be
established, the single goes to review rather than being asserted as missing.
**(Invariant.)**

Evidence used: ISRC, track title, version marker, track duration, release date,
track-list overlap, and known local album tracks.

## Duplicate releases and editions

Duplicates are collapsed to one canonical entry. Grouping is by artist, base
title, version markers and release type; an identical UPC collapses
unconditionally. Canonical selection prefers the earliest release date — Deezer
duplicates are usually reissues of one original product — then the richest
entry, then fan count, then ID, so results are deterministic.

Collapsed: territorial duplicates, explicit/clean pairs, identical reissues,
duplicate product listings, multiple IDs for one release, and remasters with no
content difference. A deluxe edition whose extras are all owned is treated as
owned.

Never collapsed: live versus studio, a single versus an album of the same name,
different artists, or genuinely distinct releases with similar titles.
**(Invariant.)**

## Release dates and sorting

The most precise reliable date is used, degrading through full date → year and
month → year → unknown. Deezer's `2019-00-00` forms are parsed to the correct
precision.

Sorting is descending. Within the same period, a less precise date sorts *below*
the fully dated releases it overlaps; unknown dates sort last. Ties break
alphabetically by artist then title, so repeated runs print identically.

```text
2024-06-15   full date
2024-06      month only, below every dated June release
2024-01-01   full date
2024         year only, below every dated 2024 release
unknown      always last
```

Grouping by release type partitions this order; it does not reorder within a
group. `--flat` removes grouping entirely.

Automated tests cover reverse-chronological ordering, partial dates, unknown
dates and tie stability.

## Local state and caching

One SQLite file at `~/.local/state/dropwatch/state.sqlite3`, created mode
600. Chosen over JSON because it is written from several threads and gives
atomic commits from the standard library; a partially written JSON file would
lose everything.

Stores: cached Deezer responses, cached local track lists keyed by an album
fingerprint (song count + duration), artist mappings, ignored artists, ignored
releases, and the last scan's unresolved list.

Expiry is per key class, not uniform. Album metadata and track lists
(`album:`, `album_tracks:`) are fixed once a release is published and are kept
for 30 days; discography listings and artist searches (`discography:`,
`search:`) exist to change and use `CACHE_MAX_AGE_HOURS` (default 24). A single
lifetime cannot serve both — short enough to catch new releases means
re-fetching thousands of immutable records daily. A configured lifetime longer
than 30 days is respected rather than shortened, and setting it to 0 disables
expiry for every class. `--refresh` bypasses the cache entirely.

Every response is committed immediately, so interrupting a scan loses no fetched data, and a
re-run resumes cheaply because artist order is deterministic.

A failed refresh never discards already-cached data. **(Invariant.)**
No credentials are ever written to local state. **(Invariant.)**

Management: `cache` shows location and age, `cache --clear` drops cached
responses while preserving mappings, `cache --reset-mappings` drops mappings,
`--refresh` bypasses the cache for one run, `artists` inspects unresolved
artists.

## Failure handling and exit codes

One failing artist never stops the scan; the failure is recorded and the run is
reported as partial. Handled: temporary Deezer and Navidrome errors, timeouts,
invalid responses, pagination failures and loops, rate limits, unresolved
artists, missing release dates and partial scans. Retries are bounded.
Authentication failures are never retried.

| Code | Meaning |
|---|---|
| 0 | Success |
| 1 | Unexpected failure, or interrupted |
| 2 | Command-line usage error |
| 3 | Configuration problem |
| 4 | Navidrome connection or protocol problem |
| 5 | Navidrome rejected the credentials |
| 6 | Deezer problem |
| 7 | Completed partially |

Code 7 still prints results, and the summary states that the output is partial.

## Security

- Credentials come from a git-ignored `.env` or the environment; never from
  source, tests, fixtures, examples, logs, tracebacks or output.
  **(Invariant.)**
- Secrets are wrapped in a `Secret` type that redacts itself everywhere and
  refuses serialisation.
- A logging filter scrubs registered secret values and credential-shaped URL
  parameters from every record, including at `-vv`. **(Invariant.)**
- `.env` permissions are checked at startup with a warning if group- or
  world-readable; the state database is created mode 600.
- Navidrome auth uses per-request salted tokens.
- Error messages quote endpoints without their query string.

## Architecture

Standard library only — no runtime dependencies. `urllib` was chosen over a
request library specifically because it surfaces the raw socket and TLS
exceptions the error taxonomy depends on.

```text
dropwatch.py            launcher shim (src/ ahead of the script directory)
src/dropwatch/
  cli.py            argparse, subcommands, exit codes
  config.py         env/.env loading, URL validation, permission checks
  secrets.py        Secret type and redaction registry
  logging_setup.py  stderr logging with a redaction filter
  http.py           urllib wrapper, timeouts, rate limiter, error taxonomy
  navidrome.py      Subsonic client (read-only)
  deezer.py         Deezer public-API provider  ← all unofficial surface
  models.py         dataclasses, ReleaseDate, enums
  normalize.py      folding, edition/version parsing, similarity
  classify.py       Album / EP / Single / Unknown
  artist_match.py   local artist → Deezer artist
  release_match.py  ownership determination, coverage, ISRC refinement
  dedupe.py         canonical release selection
  state.py          SQLite cache and mappings
  scan.py           orchestration
  report.py         terminal output, sorting, grouping, summary
```

## Testing

`python297 -m pytest` — 389 tests, no network access, no real credentials, both
APIs mocked. **(Invariant: normal tests never contact the live services.)**

Covered: artist-name normalization, ambiguous Deezer results, manual mappings,
album-title normalization, deluxe/expanded/remaster matching, track-title
overlap, singles already on owned albums, singles with exclusive tracks,
alternate-version detection, duplicate listings, partial Navidrome
and Deezer failures, the connection error taxonomy, cache expiry and
invalidation, reverse-chronological sorting, unknown and incomplete dates,
grouping, terminal formatting, credential redaction, and every launcher
invocation.

## Known limitations

- Deezer is the only catalogue; anything absent from it cannot be reported.
- Local files carry no ISRCs — the Subsonic API does not expose them — so
  recording identity against the library rests on title, version and duration.
  ISRCs are used only Deezer-side.
- The ±5 s duration tolerance is a heuristic.
- A compilation whose every track is owned is treated as owned and suppressed.
- Deezer's `record_type` is inconsistent; boundary EP/album cases can fall
  either way, and undecidable ones print as `Unknown`.
- The year-gap rule sends identical titles more than a year from the tagged year
  to review, which is noisy if year tags reflect pressing rather than original
  release.
- First run is slow — roughly one search plus a discography per artist, and two
  more requests per candidate release, against an ~8/s limit. `--since` is the
  effective lever. Later runs are served from cache.
