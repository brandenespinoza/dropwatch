# DropWatch — guide

The path through DropWatch: connect, scan, answer what it could not decide, and
download what you choose.

This is the walkthrough. Every flag and setting is in
[COMMANDS.md](COMMANDS.md); why any of it works this way is in
[DESIGN.md](DESIGN.md).

---

## Before your first scan

```bash
dropwatch setup     # asks for your Navidrome details and tests them
dropwatch check     # re-run any time the connection looks wrong
```

`check` validates the URL, connects, authenticates, and confirms the endpoint is
Subsonic-compatible. When something is wrong it says which thing — network
failures and credential failures are never conflated:

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

---

## Your first scan

```bash
dropwatch scan
```

Results go to stdout; warnings, unresolved artists and the review section go to
stderr, so `dropwatch scan > missing.txt` gives you a clean file. Each result
line ends with the release's Deezer URL, so the output composes with whatever
you do next.

While it runs, findings appear as each artist resolves:

```text
[47/196] Fleetwood Mac — 3 missing (2 albums, 1 single)
[52/196] Ghost — unresolved, 2 candidates
[58/196] Alabama — 12 missing (4 albums, 8 singles), 3 to review
[61/196] Genesis
```

Artists with nothing new stay off the scroll. These are summaries — the sorted
table still arrives at the end, on stdout.

**The first run is the slow one.** One Deezer search plus a discography per
artist, against an ~8/s rate limit. `dropwatch scan --since 2024` is the
effective lever. Later runs are served from cache and cost almost nothing.

Narrowing a scan, changing what counts as a release type, scanning one artist:
see [scan in COMMANDS.md](COMMANDS.md#scan).

---

## Reading the report

Releases are grouped by type — Albums, EPs, Singles, Unclassified — and within
each group sorted by release date, newest first, across all artists. Never
grouped by artist. `--flat` gives one continuous list in pure date order.

Deezer sometimes supplies partial dates, so a less precise date sorts *below*
the fully dated releases it overlaps: `2024-06` falls under every dated June
release, a bare `2024` under every dated 2024 release, and unknown dates last.
Remaining ties break alphabetically, so repeated runs print identically.

### The NOTES column

A `NOTES` column appears when a release is something other than what its title
suggests — `remix`, `live`, `acoustic`, `deluxe`, `remaster`, `compilation`:

```text
RELEASE DATE  ARTIST         TYPE     TITLE      NOTES  URL

Singles (2)
2025-12-12    The Elovaters  Single   Castaway   remix  https://www.deezer.com/album/863456162
2025-06-06    The Elovaters  Single   Lil Bit           https://www.deezer.com/album/755753571
```

That first row is why the column exists. Deezer titles the *release* plainly
`Castaway` and puts the marker only on the track it contains — "Castaway
(Sunset Tsunami & MO2 Remix)" — so without the column it reads as a duplicate
of a song you already own, with nothing on the line to explain itself.

A marker has to be on **every** track to be listed, since one remix among twelve
album tracks says nothing about the album, and venue detail collapses, so a live
record whose tracks each name a different city reads as one `live` rather than
eleven. The column is sized to its contents and is **absent entirely** when
nothing in the results is flagged, so an unremarkable list keeps every cell for
the title. The same notes appear beside each release in `dropwatch rip`.

---

## Answering what the scan could not decide

A scan produces two kinds of "I don't know": *which Deezer artist is your
"Ghost"* — there are six — and *do you already own this release*. It never
guesses at either.

```bash
dropwatch status    # what is waiting
dropwatch fix       # work through it: artists first, then releases
```

### Artists

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

**Selecting several is the interesting one.** Deezer routinely splits one act
across duplicate artist entries, each holding part of the catalogue — the two
Algorhythm entries above share *Illusion* and *Island*. Answering `1 2` maps
both and merges their discographies, de-duplicating the overlap, so you get
complete coverage without double entries.

A mapped artist never returns to the unresolved list, so re-open it by name:

```bash
dropwatch fix "Ghost"
```

Mappings persist between runs and always win over automatic matching. A normal
scan is never interactive. Every answer has a non-interactive equivalent
(`map`, `unmap`, `block`) — see [COMMANDS.md](COMMANDS.md#answering-what-the-scan-couldnt).

### Releases

Releases the matcher cannot settle go to a review section rather than being
claimed as missing:

```text
─── 1/5 ───
Alabama — American Christmas
  Album, 2017-10-06
  title closely resembles local album 'Christmas' but is not identical
  https://www.deezer.com/album/558123
  [o] I own it   [m] I don't, report it   [b]lock   [s]kip   [u]ndo   [q]uit
```

Decisions are stored against the Deezer release ID, so the same question is
never asked twice.

**`o` and `b` both make it go away; the difference is what you are saying.** `o`
records that it is in your library. `b` records only that you do not want to
hear about it — a karaoke edition, a territorial duplicate, an album you have
decided you will never buy. Reach for `b` whenever the honest answer to "do you
own it?" is no but you still want it gone.

---

## Downloading with streamrip

DropWatch does not download anything itself. `dropwatch rip` walks the releases
from your last scan and, for each one you pick, runs a command *you* configured
against that release's Deezer URL. The default targets
[streamrip](https://github.com/nathom/streamrip).

### Setting it up

**1. Install streamrip.** It is a separate program with its own release cycle —
DropWatch does not ship it, depend on it, or install it. Follow streamrip's own
installation instructions.

**2. Configure streamrip's credentials, in streamrip.** Whatever account details
it needs live in *its* configuration. DropWatch never reads, stores or logs
them. Confirm it works on its own before involving DropWatch:

```bash
rip url https://www.deezer.com/album/302127
```

**3. Check DropWatch's template.** The default already matches streamrip's
invocation, so there is usually nothing to change:

```bash
dropwatch config          # look for: rip-command    rip url {url}
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

Enter skips rather than downloads, because holding a key down through a long
list should not start forty downloads. Releases run one at a time with the
downloader's output passed straight through, so its progress bars work and you
see a failure when it happens. One failure does not stop the walk; Ctrl-C
abandons the download in progress and returns you to the prompt.

You are asked in the order the results printed — albums, then EPs, then singles,
newest first — so you can work down until you have had enough and quit knowing
what you skipped was the smaller, older material.

`o` and `b` are here for the same reason they are in `fix`: the walk is where
you are already looking at the release.

### What the walk offers

**`rip` offers what the last scan reported.** That is the whole rule, and it has
no exceptions — settings scope the *scan*, not the walk.

It matters because the stored queue is not simply the last scan's results: a
narrow scan leaves the rest of the queue alone, so it sits on top of whatever
earlier, wider scans left behind. `rip` replays the scope the scan actually ran
with, and the count line says so:

```text
9 release(s) from the last scan (favourites only, since 2024).
```

If the scope hides the queue entirely, `rip` names the filter responsible rather
than advising a scan that would change nothing. Nothing is ever deleted by this
— widen the scan and the releases come back. The reasoning, and the one case
that made this rule necessary, are in [DESIGN.md](DESIGN.md#downloading).

### Ripping records nothing

A download is not a claim that you own something, so ripping writes nothing to
local state and the release keeps appearing until Navidrome indexes the new
files and you scan again. Your library stays the authority on what you have.

`o` and `b` are the exceptions, and only because you asked for them — a
successful rip is never turned into an `o` on your behalf. To clear a release
before the next scan:

```bash
dropwatch fix --album 302127 --own
```

### Using a different downloader

`rip-command` is a template. `{url}` is where the Deezer URL goes, and it can
sit anywhere in the command:

```bash
dropwatch config set rip-command "rip url {url} --quality 2"
dropwatch config set rip-command "/usr/local/bin/my-fetch --out /music {url}"
dropwatch config set rip-command "~/bin/queue-it.sh {url}"
```

Any tool that takes a URL works, including a script of your own. The template is
validated when you set it, so a typo is caught by `config set` rather than
surfacing later when you are looking at forty releases. It is split into
arguments with no shell involved, and the URL is substituted after the split, so
it is always exactly one argument.

---

## What to expect from the matching

You do not need to know how matching works to use DropWatch, but two of its
habits are worth recognising in the output:

- **A remaster of an album you own is not reported; a live version of it is.**
  Cosmetic edition markers ("Deluxe Edition", "2011 Remaster", "Explicit") are
  ignored when deciding identity, while version markers ("Live", "Acoustic",
  "Remix", "Demo") are preserved, because they denote a different recording.
- **A single of a song you already have on an album or EP is not reported**, even
  when the two run to different lengths — the single edit and the album cut are
  the same song, and you own it. What counts is the credit, not the shelf it
  sits on: a guest appearance on someone else's record settles the single just
  as well as a track on the artist's own album. A single is still reported when
  it carries something you do not have — a B-side, an acoustic take, a remix —
  and when the song exists locally only on another single, where nothing but the
  duration is available to judge it.
- **When it cannot tell, it asks rather than guesses.** A release it cannot
  settle goes to the review section, not the main list. The bias is deliberate:
  quietly filtering out something you own is a smaller problem than telling you
  to go buy something twice.

The full account — artist corroboration, recording coverage, ISRCs, duplicate
collapsing and the six verdicts — is in [DESIGN.md](DESIGN.md).

---

## Settings, state and cache

`dropwatch config` prints every setting and where each value came from, which is
what you want when a change appears not to take effect — a shell variable
shadowing the file is called out where it happens:

```text
  url            http://other:9000  (from $DROPWATCH_URL)
```

Two worth knowing early: `types` (set it to `album,ep` if you collect albums and
do not want singles reported) and `favorites`. The password is never accepted as
a command-line argument — `dropwatch config password` prompts for it.

Cache, artist mappings and decisions share one SQLite file. A second run over
the same library is cheap because immutable album data is cached for 30 days
while volatile listings use the shorter configured lifetime. Clearing the cache
never deletes your mappings.

Every setting, every `cache` flag and the full precedence chain:
[COMMANDS.md](COMMANDS.md#settings).
