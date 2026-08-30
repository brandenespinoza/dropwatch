# DropWatch — guide

How to use DropWatch, start to finish: connecting to Navidrome, reading a scan,
answering what it could not decide, and downloading what you choose.

[README.md](../README.md) is the overview. [COMMANDS.md](COMMANDS.md) is the
reference for every command, flag and setting. [DESIGN.md](DESIGN.md) is why it
works the way it does.

---

## Before your first scan

```bash
dropwatch check
```

That validates the URL, connects, authenticates, and confirms the endpoint is
Subsonic-compatible. If something is wrong it says which thing:

| Message | Meaning |
|---|---|
| Could not resolve the hostname | DNS cannot find the host |
| Connection refused | Host is up, nothing listening on that port |
| No route to host | Host is offline or off the tailnet |
| Connection timed out | Host is unreachable or asleep |
| TLS certificate verification failed | HTTPS certificate problem |
| Navidrome rejected the credentials | Username or password is wrong |
| ... is not a Subsonic-compatible API | URL points at something else |
| Navidrome returned 404 | URL path is wrong |

Network failures and credential failures are never conflated.

---

## Your first scan

```bash
dropwatch scan
```

That is the whole normal workflow. Results go to stdout; warnings, unresolved
artists and the review section go to stderr, so `dropwatch scan > missing.txt`
gives you a clean file.

Useful flags:

```bash
dropwatch scan --since 2024            # a year...
dropwatch scan --since 2026-06         # ...a month...
dropwatch scan --since 2026-06-15      # ...or an exact date
dropwatch scan --type album --type ep  # skip singles
dropwatch scan --type all              # every type, whatever the setting says
dropwatch scan --artist "Björk"        # one artist
dropwatch scan --favorites             # only your Navidrome favourites
dropwatch scan --flat                  # no type groups, pure date order
dropwatch scan --refresh               # ignore the cache, refetch
dropwatch scan -v                      # progress detail on stderr
```

Each result line ends with the release's Deezer URL, so the output composes with
whatever you want to do next.

While a scan runs, findings appear on stderr as each artist is resolved:

```text
[47/196] Fleetwood Mac — 3 missing (2 albums, 1 single)
[52/196] Ghost — unresolved, 2 candidates
[58/196] Alabama — 12 missing (4 albums, 8 singles), 3 to review
[61/196] Genesis
```

Artists with nothing new stay off the scroll, so the list stays dense. These are
summaries, not the report — the sorted table still arrives at the end, on
stdout. Nothing about the live view touches stdout, so `dropwatch scan > out.txt`
is unaffected. `--no-progress` turns it off, and it is skipped automatically
when stderr is not a terminal.

The first run is the slow one. A large library means one Deezer search plus a
discography per artist, and two more requests per candidate release, against an
~8/s rate limit. `--since` is the effective lever. Later runs are served from
cache.

---

## Reading the report

Results are grouped by release type — Albums, then EPs, then Singles, then
Unclassified — and within each group sorted descending by release date, newest
first, across all artists. They are never grouped by artist.

The `TYPE` column is kept even though the group heading repeats it, so every
line stays self-describing when the output is piped somewhere.

For one continuous list sorted purely by date, with no group headings:

```bash
dropwatch scan --flat
```

Deezer sometimes gives partial dates (`2019-00-00`), so precision is tracked and
used as the tiebreak: within the same period, a less precise date sorts *below*
the fully dated releases it overlaps.

```text
2024-06-15   full date
2024-06      month only, below every dated June release
2024-01-01   full date
2024         year only, below every dated 2024 release
unknown      always last
```

Remaining ties break alphabetically by artist then title, so repeated runs print
identically.

---

## What a scan leaves for you

A scan produces two kinds of "I don't know": *which Deezer artist is your
"Ghost"* — there are six — and *do you already own this release*. It never
guesses at either.

```bash
dropwatch status
```

```text
Needs you
  5 artist(s) could not be matched to Deezer
  3 release(s) need a decision

  Work through them:  dropwatch fix
```

`dropwatch fix` walks both piles in order: artists first, then releases.
`dropwatch status --decided` lists everything you have already answered, each
with the command that reverses it.

### Artists that need review

```bash
dropwatch fix
```

```text
Algorhythm
  7 Deezer artists share this name with equal catalogue overlap
  1. Algorhythm  [id 461342]  427 fans
       Illusion
       Island
       Time And Space
  2. Algorhythm  [id 324774101]  34 fans
       Illusion
       Make It Last
       Island
  number(s) to map  [s]kip  [b]lock  [d] enter an id or URL  [q]uit
  >
```

Each candidate lists a few album titles, with any that match your library shown
first — which Ghost is yours is answerable from a track listing and almost never
from a fan count.

| Answer | Effect |
|---|---|
| `1` | Map to that Deezer artist |
| `1 2` | Map to **both** — their discographies are merged |
| Enter, or `s` | Leave it unresolved; you will be asked again next time |
| `b` | Block permanently, never reported again |
| `c` | Clear everything known about this artist, back to unresolved |
| `d 1160651` or a Deezer URL | Use an ID you found yourself |
| `q` | Stop here, keeping what you have already decided |

**Selecting several is the interesting one.** Deezer routinely splits one act
across duplicate artist entries, each holding part of the catalogue — the two
Algorhythm entries above share *Illusion* and *Island*. Mapping both merges
their discographies and de-duplicates the overlap, so you get complete coverage
without double entries.

**Got one wrong?** A mapped artist never returns to the unresolved list, so
re-open it by name:

```bash
dropwatch fix "Ghost"
```

That searches Deezer fresh, shows your current mapping, and lets you pick again,
clear it, or block the artist.

The same things are available non-interactively:

```bash
dropwatch map "Ghost" 1160651              # one
dropwatch map "Ghost" 1160651 4859761      # several, merged
dropwatch block --artist "Karaoke Hits Vol 3"
dropwatch unmap "Ghost"                    # clear, back to unresolved
dropwatch status --decided                 # list what is saved
```

Mappings persist between runs and always win over automatic matching. A normal
scan is never interactive.

### Releases that need review

Releases the matcher cannot settle go to a review section rather than being
claimed as missing. `fix` reaches them after the artists:

```text
─── 1/5 ───
Alabama — American Christmas
  Album, 2017-10-06
  title closely resembles local album 'Christmas' but is not identical
  https://www.deezer.com/album/558123
  [o] I own it   [m] I don't, report it   [b]lock   [s]kip   [u]ndo   [q]uit
```

`o` suppresses it for good, `m` promotes it into the main list, `b` blocks it,
`s` leaves it undecided, `u` clears a decision you made earlier. Decisions are
stored against the Deezer release ID, so the same question is never asked twice.

`o` and `b` both make the release go away, and the difference is what you are
saying. `o` records that it is in your library; `b` records only that you do not
want to hear about it — for a karaoke edition, a territorial duplicate, or an
album you have decided you are never going to buy. Reach for `b` whenever the
honest answer to "do you own it?" is no but you still want it gone.

### Blocking one release from the results

Any release in the main list can be dismissed without going through review —
press `b` on it during `dropwatch rip`, or name it directly:

```bash
dropwatch block --album https://www.deezer.com/album/558123
dropwatch unblock --album 558123          # undo
```

That works for any release type — album, EP or single.

Use `fix --album` instead when you want to record what you actually know about
the release rather than just muting it:

```bash
dropwatch fix --album 558123 --own       # I have it; stop reporting it
dropwatch fix --album 558123 --missing   # I don't; always report it
dropwatch fix --album 558123 --clear     # forget the decision
```

Both suppress a release, but only `--own` records a claim about your library.
`dropwatch cache --reset-decisions` forgets all of them.

---

## Downloading with streamrip

DropWatch does not download anything itself. `dropwatch rip` walks the releases
from your last scan and, for each one you choose, runs a command *you*
configured against that release's Deezer URL. The default command targets
[streamrip](https://github.com/nathom/streamrip).

### Setting it up

**1. Install streamrip.** It is a separate program with its own release cycle;
DropWatch does not ship it, depend on it, or install it. Follow streamrip's own
installation instructions.

**2. Configure streamrip's credentials, in streamrip.** Whatever account
details it needs live in *its* configuration, not DropWatch's. DropWatch never
reads, stores or logs them — see [Why the subprocess boundary](#why-the-subprocess-boundary).
Confirm streamrip works on its own before involving DropWatch:

```bash
rip url https://www.deezer.com/album/302127
```

**3. Check DropWatch's command template.** The default already matches
streamrip's invocation, so there is usually nothing to change:

```bash
dropwatch config          # look for: rip-command    rip url {url}
```

If it is missing or you changed it, set it back:

```bash
dropwatch config set rip-command "rip url {url}"
```

**4. Scan, then walk.**

```bash
dropwatch scan
dropwatch rip
```

### The walk

```text
2 release(s) from the last scan.
Press Enter to skip one.

─── 1/2 ───
Fleetwood Mac — Rumours
  Album, 2024-06-02
  https://www.deezer.com/album/302127
  [r] rip   [s]kip   [o] I own it   [b]lock   [a]ll remaining   [q]uit
  > r

  $ rip url https://www.deezer.com/album/302127

  [the downloader's own output appears here]
  ✓ done
```

| Answer | Effect |
|---|---|
| `r` | Run the command for this release, then move on |
| Enter, or `s` | Skip it |
| `o` | I already have it — stop reporting it |
| `b` | Block it — never offer or report it again |
| `a` | Run it, and everything remaining, without asking again |
| `q` | Stop here |

Enter skips rather than downloads, because holding a key down to get through a
long list should not start forty downloads. Releases run one at a time with the
downloader's output passed straight through, so its progress bars work and you
see a failure when it happens. One failed release does not stop the walk, and
Ctrl-C abandons the download in progress and returns you to the prompt.

You are asked in the same order the results printed: **albums first, then EPs,
then singles**, and newest first within each. So the full-length records arrive
before the singles, and this year's before the back catalogue — you can work
down until you have had enough and quit knowing what you skipped was the
smaller, older material.

`o` and `b` are there because the walk is where you are already looking at the
release. They are the same two decisions `fix` records, and a release you have
answered either way is not offered on the next walk.

`dropwatch rip` needs a terminal, and never touches Navidrome or Deezer itself —
the queue is local, saved by the last scan.

### Ripping records nothing

A download is not a claim that you own something, so ripping writes nothing to
local state and the release keeps appearing in the results. It leaves on its own
once Navidrome indexes the new files and you run another scan — your library
stays the authority on what you have, which is the same rule the rest of the
tool follows.

`o` and `b` are the exceptions, and they are exceptions because you asked for
them. Downloading a release still tells the tool nothing — a successful rip is
not turned into an `o` on your behalf. If you want a release gone before the
next scan:

```bash
dropwatch fix --album 302127 --own      # I have it now; stop reporting it
```

### What the walk offers

**`rip` offers what the last scan reported.** That is the whole rule.

It needs stating because the queue is not simply the last scan's results. A scan
covering part of the library leaves the rest of the queue alone — which is what
stops a single-artist rescan from discarding everyone else's pending releases —
so a narrow scan sits on top of whatever earlier, wider scans left behind.

Four things narrow a scan, and the walk replays all four:

```bash
dropwatch scan --favorites --since 2024
dropwatch rip      # the same window the scan just printed

dropwatch scan --artist "Radiohead" --type album
dropwatch rip      # Radiohead albums, not everyone else's singles
```

`--limit` is the exception, because it is not really a filter: it caps how many
artists get scanned rather than saying which releases belong in the results, so
there is nothing to replay.

Settings scope the *scan*, not the walk. If you keep `favorites = true` and want
everything for once, widen the scan and the walk follows:

```bash
dropwatch scan --all-artists
dropwatch rip      # everything, despite the saved setting
```

Whatever is narrowing, the count line says so:

```text
9 release(s) from the last scan (favourites only, since 2024).
```

If the scope hides the queue entirely, `rip` names the filter responsible rather
than advising a scan that would change nothing:

```text
121 release(s) in the queue, none released on or after 2024.
  Re-scan without the cutoff: `dropwatch scan`.
```

Nothing is deleted by any of this. Widen the scan and the releases come back.

### Using a different downloader

`rip-command` is a template, not a hard-coded call to streamrip. `{url}` is
where the Deezer URL goes, and it can sit anywhere in the command:

```bash
dropwatch config set rip-command "rip url {url} --quality 2"
dropwatch config set rip-command "/usr/local/bin/my-fetch --out /music {url}"
dropwatch config set rip-command "~/bin/queue-it.sh {url}"
```

Any tool that accepts a URL works, including a script of your own — nothing
about the downloader is known to DropWatch beyond the command line you gave it.
The template is validated when you set it, so a typo is rejected by `config set`
rather than surfacing later when you are looking at forty releases.

### Why the subprocess boundary

It is deliberate rather than a shortcut:

- Credentials for the download service stay in that tool's own configuration and
  never reach this one. DropWatch cannot read them, log them or leak them.
- A crash in the downloader cannot take this process's state with it.
- Switching downloaders is a settings change instead of a code change.

The template is split into arguments with no shell involved, and the URL is
substituted after the split, so it is always exactly one argument and cannot
become extra flags.

---

## How matching works

**Artists.** Names are compared after folding case, accents, apostrophe styles,
`&`/`and`, and punctuation, plus a leading-article variant so "The Beatles"
matches "Beatles". Names are never split on `&`, `and`, `feat.`, `with`, `vs.`
or commas, because those appear inside real names. A name match alone is not
enough: candidates are corroborated against your album titles, and the one that
actually shares releases with your library wins. This matters — searching Deezer
for "Björk" returns "Björk & Toffe" as the *first* result. Karaoke and tribute
acts are filtered out. When several same-named artists remain and none matches
your albums, the artist is reported unresolved rather than guessed.

**Releases.** Titles are decomposed into a base title, cosmetic edition markers
and meaningful version markers. Edition markers are ignored when deciding
identity — "Deluxe Edition", "2011 Remaster", "20th Anniversary", "Bonus Track
Version", "Explicit", territorial editions. Version markers are preserved,
because they denote a different recording — "Live", "Acoustic", "Remix", "Radio
Edit", "Instrumental", "Demo", "Mono", "Alternate Take", language versions. So a
remaster of an album you own is not reported, but a live version of it is.

Beyond titles, releases are compared by recording coverage: each Deezer track is
looked up against every track you own by that artist, matching on title, version
marker and duration (±5 s, which absorbs encoder padding without hiding a
genuinely different edit).

**Singles** get this treatment specifically. A single whose only track is
already on an album you own is not reported. A single carrying an exclusive
B-side, a remix, an acoustic take, or a materially different edit *is* reported.
Where Deezer supplies ISRCs for both a single and an album you own, identical
ISRCs prove identical recordings and the single is suppressed — but only for the
tracks of that album you actually hold, since an album released a song at a time
is listed by Deezer in full long before it can be owned in full. Where recording
identity cannot be established, the single goes to the review section instead of
being asserted as missing.

**Duplicates.** Territorial variants, explicit/clean pairs, reissues and repeated
product listings are collapsed to one canonical entry — the earliest dated one,
since Deezer duplicates are usually reissues of a single original product. A
deluxe edition whose extra tracks you already own is treated as owned. Live and
studio versions are never merged, and a single is never merged into an album of
the same name.

**Verdicts.** Every release lands on one of: owned, probably owned, missing,
probably missing, ambiguous, or ignored. Only *missing* and *probably missing*
reach the main list. *Ambiguous* goes to the review section on stderr. The bias
is deliberate: a release you actually own being quietly filtered out is a much
smaller problem than being told to go buy something twice.

---

## Settings you will actually change

Re-run `dropwatch setup` to walk through everything again — it offers your
current values as defaults, so pressing Enter keeps them. For a single change:

```bash
dropwatch config                         # every setting and where it came from
dropwatch config set url http://your-server:4533
dropwatch config set timeout 30
dropwatch config password                # prompted, never echoed
dropwatch config unset timeout           # back to the default
dropwatch config path                    # where the file lives
```

`config` shows provenance, which is what you want when a change appears not to
take effect:

```text
Settings file: /Users/you/.config/dropwatch/.env

  url            http://music:4533
  username       branden
  password       ********
  timeout        20
  cache-path     /Users/you/.local/state/dropwatch/state.sqlite3
  cache-max-age  24
  types          all
  favorites      false
  rip-command    rip url {url}
```

A shell variable shadowing the file is called out where it happens:

```text
  url            http://other:9000  (from $DROPWATCH_URL)
```

The password is never accepted as a command-line argument — it would land in
your shell history and be visible to `ps`. `config password` prompts for it
instead.

Settings are read from the first source that has them: real environment
variables, then the settings file (`~/.config/dropwatch/.env`, or `$DROPWATCH_ENV`
if set). That is the whole chain — there is no `--env-file` flag and no `./.env`
in the working directory, because a stray `.env` in a project directory should
not be able to decide which server gets scanned.

### Only care about albums?

If you collect complete albums rather than singles, set that once:

```bash
dropwatch config set types album,ep
```

Every run then reports only those, without repeating `--type` forever. An
explicit `--type` on the command line still wins — including `--type all`. To go
back to everything for good, widen the setting or remove it; both mean the same:

```bash
dropwatch config set types all
dropwatch config unset types
```

---

## Local state and cache

Cached API responses, artist mappings and block lists live in one SQLite file at
`~/.local/state/dropwatch/state.sqlite3` (override with `DROPWATCH_CACHE_PATH`).
It is an absolute path, so it is shared no matter where you run the command
from.

Cached entries expire after 24 hours by default (`DROPWATCH_CACHE_MAX_AGE`) —
but not all of them. An album's metadata and track list cannot change once the
album is out, so those are kept for 30 days; discography listings and artist
searches, which exist precisely to change, use the configured lifetime. That is
what makes a second run cheap. `--refresh` overrides both.

```bash
dropwatch cache                  # where it is, how old it is
dropwatch cache --clear          # drop cached API responses
dropwatch cache --reset-mappings # drop artist mappings
```

Clearing the cache never deletes your manual mappings, and a failed refresh
never discards data that is already cached.

---

## Deezer access and rate limits

DropWatch uses `https://api.deezer.com` — Deezer's public, unauthenticated
catalog API. It needs no credentials at all, and never sends any. Artist search,
full discographies with pagination, album metadata (UPC, label, contributors,
track counts) and complete track listings with ISRCs are all available
anonymously, so there is no Deezer account setup to do.

Nothing here logs in to Deezer, touches a browser profile, or reads cookies. No
third-party Deezer library is used, so there is nothing to audit for hidden
downloading or decryption behaviour.

Deezer allows roughly 50 requests per 5 seconds per IP (measured: 60 concurrent
requests yielded 49 successes and 11 quota errors). Requests are limited to 8/s
with a small burst, quota errors are retried three times with exponential
backoff and jitter, and everything is cached. Detail is only fetched for
releases that still look missing after a cheap first pass, so a second run over
the same library costs almost nothing.
