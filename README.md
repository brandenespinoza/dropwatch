# DropWatch

Lists releases on Deezer, by artists already in your Navidrome library, that you
appear not to own. One command, results printed to the terminal, newest first.

```text
RELEASE DATE  ARTIST          TYPE     TITLE            URL

Albums (1)
2026-06-02    Artist Name     Album    Album Title      https://www.deezer.com/album/302127

EPs (1)
2026-07-18    Another Artist  EP       Another Release  https://www.deezer.com/album/825535241

Singles (1)
2026-07-24    Artist Name     Single   Release Title    https://www.deezer.com/album/14894641

42 missing releases: 8 albums, 6 EPs, 28 singles
5 artists could not be resolved
3 releases require review
```

## What it does

- Reads your artists, albums and tracks from Navidrome over its Subsonic API.
- Finds each artist on Deezer, corroborating the match against your album titles.
- Pulls each artist's full Deezer discography, handling pagination.
- Works out which releases are not in your library, being careful about
  reissues, deluxe editions and singles whose tracks you already own.
- Prints the result sorted globally by release date, newest first.
- Hands the releases you pick to a downloader you configured —
  [streamrip](https://github.com/nathom/streamrip) by default — with
  `dropwatch rip`. See
  [Downloading with streamrip](docs/GUIDE.md#downloading-with-streamrip).

## What it does not do

It never writes to Navidrome: no scans, ratings, favourites, playlists or tag
edits, and your music files are never opened. It never touches your Deezer
account — the public catalog API needs no credentials and none are sent. It does
not use MusicBrainz, in any form. It is not a daemon or a service: it runs when
you run it and then exits.

It does not download music either. `dropwatch rip` runs a command *you*
configured, in a separate process, with credentials it never sees and cannot
read. No downloader ships with it and none is a dependency.

## Requirements

- Python 3.10 or newer (macOS ships 3.9; `brew install python` if `python3 -V`
  reports anything older).
- No third-party runtime dependencies — the standard library covers HTTP, JSON
  and SQLite.
- Network access to Navidrome, and to `api.deezer.com`.

## Install

```bash
./install.sh
```

That builds an isolated environment under `~/.local/share/dropwatch`, copies the
application into it, and links the `dropwatch` command into `~/.local/bin`.
Nothing is added to your system Python, and **the source directory is not needed
afterwards** — move it or delete it and the command keeps working.

Re-run `./install.sh` any time to upgrade in place. `./install.sh --uninstall`
removes it, leaving your settings and cache alone.

<details>
<summary>Running from the source directory instead</summary>

No install needed — `python3 dropwatch.py` takes exactly the same arguments, and
reads `.env` from the project directory if one is there.

```bash
python3 dropwatch.py setup
python3 dropwatch.py scan
```

</details>

## Quickstart

```bash
dropwatch setup     # enter your Navidrome details; it tests the connection
dropwatch scan      # list the releases you appear not to own
dropwatch fix       # answer what the scan could not decide
dropwatch rip       # walk the list, downloading the ones you pick
```

`scan` is the one you will run most. `fix` exists because a scan never guesses:
artists it could not pin down on Deezer, and releases it could not judge, wait
for you instead of being silently resolved either way. `rip` walks what the last
scan reported and hands each release you choose to your downloader.

Full walkthrough: [docs/GUIDE.md](docs/GUIDE.md).

## Where things live

| | |
|---|---|
| Application | `~/.local/share/dropwatch/` |
| Command | `~/.local/bin/dropwatch` |
| Settings | `~/.config/dropwatch/.env` (mode 600) |
| Cache and mappings | `~/.local/state/dropwatch/state.sqlite3` |

All absolute, so it behaves the same from any directory.

Credentials come from that `.env` or the environment, are never written into
source, tests or logs, and are redacted from tracebacks and debug output.
Navidrome authentication uses Subsonic's salted-token scheme, so your password
never appears in a URL or a proxy log. Details in
[docs/DESIGN.md](docs/DESIGN.md).

## Known limitations

- **Deezer is the whole world.** An artist or release absent from Deezer cannot
  be reported. Libraries heavy on small labels or non-Western catalogues will
  see more unresolved artists.
- **Local files have no ISRCs.** The Subsonic API does not expose them, so
  identity against your library rests on title, version marker and duration
  (±5 s) — a heuristic, not a proof.
- **Compilations count as owned.** A greatest-hits collection whose every track
  you already own is suppressed rather than reported as a product.
- **Deezer's `record_type` is inconsistent.** Track count and duration correct
  the obvious mislabels; genuine EP/album boundary cases print as `Unknown`.
- **The first run is slow.** One Deezer search plus a discography per artist,
  against an ~8/s rate limit. `--since` is the effective lever; later runs are
  served from cache.

## Documentation

| | |
|---|---|
| [docs/GUIDE.md](docs/GUIDE.md) | How to use it: first scan, reading the report, review, downloading |
| [docs/COMMANDS.md](docs/COMMANDS.md) | Every command, flag and setting |
| [docs/DESIGN.md](docs/DESIGN.md) | Why it works the way it does, and what must stay true |
