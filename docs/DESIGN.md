# DropWatch — design decisions and invariants

Why this tool is shaped the way it is, and what must stay true as it changes.

**This document does not describe what DropWatch does.** [README.md](../README.md)
is the overview, [GUIDE.md](GUIDE.md) is the walkthrough and
[COMMANDS.md](COMMANDS.md) is the reference; all three are better at it, and a
fourth description would only drift from the others. What lives here is the
reasoning that has nowhere else to go: constraints that must survive future
edits, and alternatives already tried and rejected.

It began as a product specification written before the code. That job is done —
the tool is built — so what remains is the part that is still load-bearing.

## Using this document

Anything marked **(Invariant.)** is a product decision, not an implementation
detail. Changing one is a decision to build a different tool, and is fine if
that is what you mean to do — but it is never a refactor, a cleanup or a
side-effect of some other change.

Adding behaviour rarely belongs here. Add it to README and COMMANDS. Come here
only when you are changing a rule, or when you have found a reason one of these
rules is wrong.

---

## The three prohibitions

These define the tool more than any feature does.

**Acquisition and decryption code must never enter this codebase.** `rip` is a
real downloader integration and a headline feature: it starts a program the user
named in a setting — streamrip by default — in a separate process, and reads only
its exit status. What is forbidden is implementing the download *here*.
Vendoring, importing, reimplementing or depending on a downloader violates this,
however convenient it would be to make `rip` work out of the box. The boundary
is what keeps that tool's credentials, its failure modes and its dependency tree
on the far side of a process boundary. **(Invariant.)**

**MusicBrainz is not used, in any form.** No APIs, databases, dumps,
identifiers, tags, Picard, libraries sourcing from it, services repackaging it,
fallbacks or enrichment. Deezer's own catalogue is the sole metadata source. The
zero-dependency install is part of the enforcement: nothing can query it
silently. **(Invariant.)**

**Navidrome and the music library are strictly read-only.** No triggered scans,
no ratings, favourites, playlists or tag edits, no renaming, moving or deleting,
no writing into the library directory. Music files are never opened. A test
asserts no request reaches a mutating endpoint. **(Invariant.)**

And one more shape constraint: it is a manually executed command-line tool, not
a daemon, scheduled service, web application, background worker, cloud service
or notification system. **(Invariant.)**

---

## Rejected alternatives

The obvious improvements that were tried, or considered and refused. Each was
rejected for a reason that still holds.

| Alternative | Why not |
|---|---|
| Vendor or import a downloader so `rip` works out of the box | Forfeits the process boundary, the credential isolation and the zero-dependency install — all at once |
| MusicBrainz for better artist/release identity | See above. Deezer's catalogue is the only source, and dependencies are the enforcement mechanism |
| `--env-file` flag | Duplicated `$DROPWATCH_ENV` exactly while appearing in every subcommand's help |
| `./.env` in the working directory | Let an unrelated project file silently decide which server got scanned |
| macOS Keychain for credentials | Mode 600 already excludes other users; Keychain does not defend against processes running *as* the user — the realistic threat — and adds a subprocess dependency plus a second place config can hide |
| A `requests`-style HTTP library | `urllib` surfaces the raw socket and TLS exceptions the error taxonomy is built on; a friendlier wrapper hides exactly what is needed |
| A checked-in `.env.example` | Would drift from the real settings table. `setup` writes the real thing |
| Splitting artist names on `&`, `feat.`, `vs.`, commas | Those substrings occur inside real names, and splitting fabricates artists that do not exist |
| Keying duplicate detection on the Deezer artist ID | A merged artist's releases carry several Deezer IDs, so it printed each album once per source entry |
| Replacing the stored queues on each scan | A single-artist rescan discarded every other artist's pending questions |
| One cache lifetime for everything | Short enough to catch new releases means re-fetching thousands of immutable album records daily |
| A bare `dropwatch` meaning `scan` | Scanning contacts two servers and takes minutes; it should be asked for by name |
| Recording a successful rip as "owned" | The tool would be inferring ownership from its own action — precisely what "the library is the sole authority" forbids |
| Scoping the `rip` walk by the standing settings rather than the last scan | `scan --all-artists` under a saved `favorites = true` reported everything, then hid all of it from the walk and advised re-scanning favourites |
| Stamping favourite-ness onto each stored entry instead of recording the names | The stamp outlived the artist's time as a favourite, and no later favourites scan covered them to correct it, so `rip` offered releases the scan had not reported |

---

## Invariants by area

### Output and process

Results go to **stdout**; warnings, unresolved artists, the review section and
all logging go to **stderr**. Redirecting stdout yields a clean file. Live
progress therefore never touches stdout, which is what lets the scan stream its
work while the final table keeps its global sort — **stdout is the answer,
stderr is the process**. **(Invariant.)**

Normal output contains no raw API responses, architecture detail, debug logging
or match scores. A normal scan is never interactive.

### Navidrome

Authentication uses Subsonic's salted-token scheme (`t=md5(password+salt)`, fresh
salt per request), so the password never appears in a URL, proxy log or
traceback.

**Network failures are never reported as credential failures.** Each condition —
DNS, no route, refused, timeout, TLS, wrong path, not-Subsonic, rejected
credentials — has its own error type and an actionable hint. **(Invariant.)**

Album listing is bulk-paginated rather than per-artist: hundreds of artists cost
a handful of requests instead of hundreds.

### Deezer

Catalogue access is entirely anonymous — search, discographies, album metadata
and ISRC-bearing track lists all work unauthenticated. **The tool holds no Deezer
credential of any kind and sends none.** **(Invariant.)**

It must not download or decrypt audio, circumvent subscription restrictions, or
modify any account, playlist, favourite or listening history. It must never
automate a browser login, read cookies or inspect a browser profile.
**(Invariant.)**

Quota and transient errors are retried three times with exponential backoff and
jitter. **Authentication errors are never retried.** **(Invariant.)**

All Deezer interaction is isolated in `deezer.py` — the only unofficial surface
in the tree. Matching, classification and reporting see plain dataclasses, so
the provider could be replaced without touching them.

### Artist identification

Artist names are **never** split on `&`, `and`, `feat.`, `featuring`, `with`,
`vs.` or commas. **(Invariant.)**

A name match alone is never sufficient. Candidates are corroborated against
local album titles, and the candidate sharing releases with the library beats a
more popular same-named artist; fan count is only a weak tiebreaker.
**Another artist's releases are never reported on name similarity alone.**
**(Invariant.)** Ambiguous and not-found artists are listed on stderr and never
guessed at. One unresolved artist never prevents others from processing.

Because a merged artist's releases carry different Deezer artist IDs, duplicate
detection keys on the **local** artist. **(Invariant.)**

The stored unresolved, review and missing queues are **merged, not replaced**,
keyed on the artists a run actually covered. A full scan covers everyone and so
behaves as a replacement; a filtered scan refreshes only its own share.
**(Invariant.)** The consequence is that the stored queue is the union of every
scan rather than the last one — see *Downloading* for what that costs.

### Ownership

Only *missing* and *probably missing* appear in the main list. *Ambiguous* goes
to a review section on stderr and is **never silently treated as missing**.
**(Invariant.)**

**The bias is deliberate: avoiding a false claim outweighs producing an
artificially complete list.** When evidence conflicts, the release is ambiguous.
**(Invariant.)**

An ambiguous release is a question, so it must be answerable — otherwise the
review section is write-only and grows without bound. A stored decision
short-circuits ownership on the next scan, so the same question is never asked
twice.

*Owned* and *blocked* both suppress a release and are kept apart because they
say different things: only *owned* is a claim about the library. Both are
offered wherever a release is shown, because the honest answer to "do you own
it?" is often no while the user still never wants to see it again — a karaoke
edition, a territorial duplicate. Without a block there, the only options are to
lie or to leave the question unanswered forever. `o` and `b` mean the same thing
in every prompt that offers them; one key meaning two things across one tool is
a trap. **(Invariant.)**

Meaningful distinctions are never normalised away when comparing titles. An
unrecognised suffix keeps titles apart rather than silently merging them.
**(Invariant.)**

A single whose only recording is already on an owned album is not reported; one
carrying an exclusive B-side, remix, acoustic take, live version or materially
different edit is. Where recording identity cannot be established, it goes to
review rather than being asserted as missing. **(Invariant.)**

Only a recording the library actually holds may vouch for another release. An
album settled as owned on its title alone may be part-owned, and its published
tracklist may name songs that are not out yet, so ISRC evidence is drawn from
tracks matched in the library rather than from the album's tracklist. Vouching
from the tracklist once suppressed exactly the advance singles a scan exists to
find. **(Invariant.)**

Never collapsed as duplicates: live versus studio, a single versus an album of
the same name, different artists, or genuinely distinct releases with similar
titles. **(Invariant.)**

### Downloading

The process boundary is the design, not an expedient: the downloader's
credentials stay in its own configuration and never enter this tool's settings,
memory or logs; its failures cannot corrupt state here; and switching
downloaders is a settings change rather than a code change.

`rip-command` is validated when set, then at run time **split into arguments
before the URL is substituted**, so the URL is always exactly one argument
whatever it contains, and no shell is involved. **(Invariant.)**

The queue is filtered when **read**, not when written, on two axes:

- **Decisions.** A release already answered is left out, including decisions
  made through `fix`. The stored queue is a scan's conclusion and knows nothing
  of decisions taken since, so without this a block taken during a walk would be
  re-offered by the next one — the one thing blocking exists to prevent.
  `DECISION_MISSING` means the opposite and never filters. **(Invariant.)**
- **Scope.** A walk offers only what the last scan's scope would still report.
  Because the queues merge rather than replace (see *Artist identification*), a
  wider earlier scan's releases stay stored; without this filter the walk opens
  on whatever sorts first across that union, routinely something the current
  scope excludes. **(Invariant.)**

Scope is **one rule with no exceptions**: the walk offers what the last scan
reported. The scan records the scope it resolved to — not the flags as typed,
since a flag and the setting it overrode are the same fact by then — and `rip`
replays that record. **(Invariant.)**

An earlier design made this two rules, following the standing `favorites`
setting but the last scan's `--since`, on the reasoning that a standing
preference is the user's current declaration of interest. It was wrong, and the
way it was wrong is worth keeping: `scan --all-artists` under a saved
`favorites = true` printed a full report, then handed `rip` a scope that hid
every line of it and advised re-scanning favourites — the opposite of what had
just been asked for. Settings scope the *scan*. Once a scan has run, its
results are the only thing a walk can be faithful to, and a setting changed
afterwards describes a scan that never happened.

| Axis | Replayed via | Recorded |
|---|---|---|
| `--artist` | The entry's own `artist`, folded as the scan folded it | With the scope |
| `--favorites` | The favourite artist names the scan resolved to | With the scope |
| `--type` | The entry's own `type` | With the scope |
| `--since` | The entry's own `date` | With the scope |
| `--limit` | *Nothing* | Not recorded |

The asymmetry in that table is the part worth understanding before changing
any of it. Three axes are predicates over fields a stored entry already
carries, so they apply correctly even to entries written by a scan that used no
such filter — which is precisely the case that leaks. Favourite-ness is not
derivable from an entry and only Navidrome knows it, so the scan records the
names it resolved to and the walk replays them, exactly as `--artist` does;
`rip` must never contact Navidrome to ask. `--limit` is excluded on principle
rather than convenience: it truncates how much work a scan does rather than
describing which releases belong in the results, so there is no predicate to
replay and replaying one would be a lie.

The names are captured **before `--artist` and `--limit` narrow the run**, so
the record describes who was a favourite rather than how many of them that run
got through. A record with no names at all — written before they were kept —
narrows nothing, while an empty list is a favourites scan that resolved to no
one and narrows to nothing. Those two must stay distinguishable: collapsing
them either hides the whole queue or leaks it. **(Invariant.)**

An earlier design stamped a boolean on each entry as it was written instead.
It is worth knowing why that failed, because it looks simpler and is not: a
stamp describes the artist at write time and has no way to stop being true. Once
an artist stopped being a favourite, no favourites scan covered them again, so
the merge could never purge the entry and the walk kept offering a release the
scan no longer reported — the exact failure the scope exists to prevent. The
lesson generalises: **a fact copied onto a stored entry cannot be corrected by
a run that no longer visits it, so scope belongs in the scope record.**

The scope is written **whole on every scan**, so a filter the user dropped is
cleared by the same write that stores the ones they kept. "No cutoff" is a
state the walk reads, never the mere absence of a record. An unreadable record
narrows nothing: walking a shorter list than was asked for hides releases
silently, while ignoring a corrupt record only offers a few extra.
**(Invariant.)**

The date comparison is `ReleaseDate.on_or_after`, the same function the scan
filters with, so the walk and the report cannot disagree about a partial date.
An imprecise or unknown date passes rather than being excluded on a
technicality. **(Invariant.)**

Ordering is applied at read time for the same reason, and the type sequence is
taken from the report's own `GROUP_ORDER` rather than restated, so the walk and
the printed table cannot disagree. **(Invariant.)**

**Ripping records nothing.** A download is not a claim of ownership, so the
release keeps being reported until Navidrome indexes the files and a later scan
sees them — the library remains the sole authority on what is owned.
**(Invariant.)** `o` and `b` are the only writes the walk performs, and they are
not exceptions: they record answers the user gave.

### State

The three report lists are not cache entries and do not expire — they are what
the last scan concluded, and `status`, `fix` and `rip` must still work a week
later.

Expiry is per key class. Album metadata and track lists are fixed once published
and kept 30 days; discography listings and artist searches exist precisely to
change and use the configured lifetime. A lifetime longer than 30 days is
respected rather than shortened; 0 disables expiry entirely.

**A failed refresh never discards already-cached data.** **(Invariant.)**
**No credentials are ever written to local state.** **(Invariant.)**

### Security

- Credentials come from a git-ignored `.env` or the environment; never from
  source, tests, fixtures, examples, logs, tracebacks or output. **(Invariant.)**
- Secrets are wrapped in a `Secret` type that redacts itself everywhere and
  refuses serialisation.
- A logging filter scrubs registered secret values and credential-shaped URL
  parameters from every record, including at `-vv`. **(Invariant.)**
- **A secret is never accepted as a command-line argument** — it would land in
  shell history and be visible to `ps`. **(Invariant.)**
- The settings file is written atomically (temp file in the same directory, mode
  600, then renamed), so a failed write cannot truncate a working config and the
  file is never briefly world-readable. Hand-added keys are preserved.

### Testing

`python3 -m pytest`. **Normal tests never contact the live services and never
execute a downloader.** **(Invariant.)** Both APIs are mocked; no real
credentials appear anywhere.

---

## Measured Deezer behaviour

Established empirically against the live API. Each is worked around in
`deezer.py` or in the matching that consumes it, and none is documented by
Deezer — rediscovering them costs hours.

- `/album/{id}` embeds **at most 25 tracks** and reports `tracks.next: null`
  even when the album has more. The dedicated `/album/{id}/tracks` endpoint
  paginates correctly **and** returns `isrc`, `disk_number` and
  `track_position`, which the embedded list omits. Track data is therefore
  always read from the dedicated endpoint.
- Application errors arrive with **HTTP 200** and an `error` object in the body.
  HTTP status alone is not a success signal.
- The rate limit is roughly **50 requests per 5 seconds per IP** — 60 concurrent
  requests produced 49 successes and 11 quota errors (code 4). The client stays
  at 8/s through a token bucket, well below it.
- Discography pagination is genuine beyond 100 entries (verified to 472 across
  5 pages) and `nb_album` matches the paginated total.
- Artist search does not rank the obvious match first: searching `Björk` returns
  `Björk & Toffe` (31 fans) ahead of `Björk` (id 630). This is why name matches
  must be corroborated.
- A **drip-released album is listed in full before it is released**. Kenny
  Chesney's *Silver Sands Marina* (album `1001816031`, dated 2026-09-25) carried
  all 11 tracks with ISRCs while only three had been issued, each of those also
  published as its own single sharing the album track's ISRC — *Goldfish* is
  `QT8AT2600010` on both. A tracklist is therefore evidence of what the album
  *will* contain, never of what can be owned.

## Thresholds

The magic numbers, in one place, with what they mean.

**Artist resolution confidence**

| Situation | Outcome |
|---|---|
| Manual mapping present | resolved, 1.0 |
| Exact name + shared album titles | resolved, 0.95 |
| Approximate name + shared album titles | resolved, 0.8 |
| Exact name, library has no albums to corroborate | resolved, 0.75 |
| Lone exact name, no shared titles | resolved, 0.6, logged |
| Several exact names, none matching local albums | ambiguous |
| Only approximate names, no shared titles | ambiguous |
| No sufficiently similar name | not found |

**Release classification** — album ≥ 7 tracks or ≥ 30 minutes; EP 4–6 tracks;
single ≤ 3 tracks; few but very long tracks stay an album; confidence below 0.4
yields `Unknown`. `compilation` maps to Album and records the trait.

**Matching** — duration tolerance ±5 s, which absorbs encoder padding without
hiding a different edit. Track coverage above 0.85 counts as "essentially all of
it". Title similarity above 0.90 without an exact base match is treated as
suspicious rather than equal. Local and Deezer years may differ by 1.

**Edition markers.** Cosmetic, so ignored for identity: deluxe, super deluxe,
expanded, anniversary, remaster (including year forms), bonus track version,
special/standard/limited/collector's/platinum/tour edition, explicit, clean,
international and territorial editions, reissue. Meaningful, so preserved
because they denote a different recording: live, acoustic, unplugged, remix,
club/extended/radio/dub mix, radio edit, instrumental, a cappella, demo,
alternate take, re-recording, "'s Version", session, mono, stereo, karaoke,
piano/orchestral arrangements, single/album version, slowed, sped up, language
versions, cover, tribute, reprise.

## Structural facts worth keeping true

Standard library only, no runtime dependencies.

- `rip_ui.py` is the **only** module that starts another process. Nothing else
  in the tree calls `subprocess`.
- `deezer.py` is the **only** unofficial API surface.
- `dropwatch.py` at the root is a launcher shim that puts `src/` ahead of the
  script directory on the import path — required because the shim and the
  package share a name.
- Configuration and state resolve to absolute locations, because an installed
  command runs from arbitrary working directories.
- The install is deliberately **non-editable**: the application is copied, so the
  source directory can be moved or deleted without breaking the installed
  command. The corollary is that source edits do not reach the installed
  command until it is reinstalled.

## Constraints that shape the matching

Not limitations to fix — facts that explain why matching works as it does.

- Local files carry no ISRCs; the Subsonic API does not expose them. Recording
  identity against the library therefore rests on title, version and duration,
  and ISRCs are used only Deezer-side, where both sides have them.
- Deezer's `record_type` is inconsistent — the catalogue contains three-track
  "albums" and eight-track "singles" — which is why structural evidence
  cross-checks it and why `Unknown` exists rather than a guess.
- Deezer is the only catalogue, so anything absent from it cannot be reported.
