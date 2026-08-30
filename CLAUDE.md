# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

# DropWatch

Lists Deezer releases by artists in a Navidrome library that the user appears not
to own, then hands the ones they pick to a downloader they configured. Python
3.10+, standard library only, no runtime dependencies.

## Hard rules

These are not preferences. Each has already been decided against, with reasons
in [docs/DESIGN.md](docs/DESIGN.md); breaking one builds a different tool.

- **Integrate downloaders; never implement one.** `rip` is a real downloader
  integration and a headline feature: it hands a release URL to a command the
  user configured — streamrip by default — in a separate process, and reads only
  its exit status. What must not enter this codebase is the download itself: no
  acquisition or decryption code, and no downloader vendored, imported,
  reimplemented or added as a dependency, not even to make `rip` work without
  setup. That boundary is what keeps the install dependency-free and the
  downloader's credentials out of this tool.
- **Never use MusicBrainz**, directly or through a dependency. Deezer's
  catalogue is the only metadata source. The zero-dependency install is part of
  how this is enforced — think hard before adding any dependency.
- **Never write to Navidrome.** No triggered scans, ratings, favourites,
  playlists or tag edits; music files are never opened.
- **Never make it a daemon**, service, watcher or scheduled job.
- **Never put a secret in argv**, and never let one reach logs, output or state.
- **Never infer ownership from the tool's own actions.** The library is the sole
  authority on what is owned; a successful download is not a claim of ownership.

## Commands

```bash
python3 -m pytest                                   # 731 tests
python3 -m pytest tests/test_rip.py -q              # one file
python3 -m pytest tests/test_rip.py::TestBuildCommand::test_quoted_arguments_survive
python3 -m pytest -k scope                          # by name

python3 dropwatch.py scan --help                    # run from source, no install
uv tool install --editable .                        # install `dropwatch` so it tracks this tree
.claude/backup.sh --dry-run                         # what the next backup would push
```

`pytest` is the only dev dependency; no linter or formatter is configured.

The installed `dropwatch` is an **editable** install (a `uv tool` symlinked into
`~/.local/bin`), so the command runs this working tree directly and source edits
are live with no reinstall step. `./install.sh` builds a *copied snapshot* instead
and repoints the same launcher — running it silently replaces the editable install,
and edits stop reaching `dropwatch` until `uv tool install --editable .` is re-run.

`tests/conftest.py` points `$DROPWATCH_CONFIG_DIR` and `$DROPWATCH_STATE_DIR` at a
tmp directory and unsets `$DROPWATCH_ENV` for every test, so the suite cannot touch
real config or state. Set those two by hand to point a manual `dropwatch.py` run at
scratch state.

## Backups

Every Claude Code session ends by running `.claude/backup.sh`, the `Stop` hook in
`.claude/settings.json`: it stages everything, commits whatever was left over as
`Save work in progress — <timestamp>`, and pushes the current branch. Work is never
stranded on this machine, but those sweep-up messages say nothing useful — commit
deliberately when a change deserves a name, and the hook only carries the remainder.
It is safe to run by hand and does nothing when the tree is clean and pushed.

## Architecture

In one line: read Navidrome → resolve each local artist to Deezer artist ids →
fetch discographies → classify and match each release against the library → report
what is missing, and store it for `rip` and `fix`.

**Chokepoints.** Each hard rule has exactly one file that could break it, which is
what makes them auditable:

| Module | Sole owner of |
|---|---|
| `http.py` | Network I/O, on `urllib` |
| `deezer.py` | The unofficial API surface; everything downstream sees plain dataclasses |
| `navidrome.py` | Subsonic — read-only, salted-token auth |
| `rip_ui.py` | `subprocess`; nothing else in the tree starts a process |
| `secrets.py` | The `Secret` wrapper that redacts itself and refuses serialisation |

**Scanning is two-pass per artist** (`scan.py::_scan_artist`). Pass one judges the
whole discography on album titles alone — cheap, and it settles anything clearly
owned. Only survivors are worth fetching local tracks and Deezer album detail for;
those are re-judged at recording level, then owned albums vouch for advance singles
via ISRC. Pass one's verdicts are reused, never recomputed.

**Singles are judged on title and credit, not duration** (`release_match.py`).
`LocalIndex._on_a_product` holds every recording that sits on an owned album or
EP *and* is credited to the artist being scanned — credit meaning the song's
`artist`, its structured `artists` or its `albumArtist`, so a guest appearance
counts. A single matching one of those is owned however long it runs. Three
things keep this from over-claiming, and none is incidental: the match is on base
title *plus* version markers, so an acoustic take is still a different song; the
local release must be an album or EP, so a single cannot vouch for a single; and
the credit check is what stops a compilation's other artists from suppressing a
same-titled single. Album editions keep the strict duration test — the relaxation
is passed in per call as `compute_coverage(relax_duration=...)`.

**The report queues are merged on write and filtered on read.** The subtlest thing
here, and it spans three files:

- `scan.py::_merge_queue` refreshes only the artists a run covered, so `--artist X`
  does not discard everyone else's pending questions. The stored queue is therefore
  the union of every scan, not the last one.
- `state.py::ScanScope` records what the last scan *resolved to* — a flag and the
  setting it overrode are the same fact by then. Written whole on every scan, so a
  dropped filter is cleared by the same write that stores the kept ones.
- `rip_ui.py` applies decisions (`pending`), scope (`in_scope`) and order
  (`order_queue`) when the queue is read: a scan knows nothing of decisions taken
  since, and the merge above would undo an order applied at write time.

Artist, type and date replay as predicates over fields a stored entry already
carries. Favourite-ness is not derivable from an entry and `rip` may never ask
Navidrome, so a favourites scan records the names it resolved to and the walk
replays that list; `--limit` is never replayed. Do not move a scope fact onto
the stored entries — a stamp cannot be corrected by a run that no longer visits
that artist, which is how ex-favourites once leaked into the walk. Read
DESIGN.md's *Downloading* section before changing any of it.

**stdout is the answer, stderr is the process.** Results and summary to stdout;
progress, warnings, logging, the review section and unresolved artists to stderr.
Redirecting stdout must yield a clean file.

**State holds two unlike things** (`state.py`). Cached API responses are disposable,
in two expiry classes — album detail and track lists are immutable once published
and kept 30 days, while searches and discographies use `cache-max-age`. Artist
mappings, blocks and release decisions are the user's own answers, and nothing can
recreate them. Schema changes go through `SCHEMA_VERSION` and `_migrate`; stored
strings outlive renames, which is why a block is still `"ignored"` on disk.

**Errors are a taxonomy, not a string** (`errors.py`). Every distinguishable failure
— DNS, refused, timeout, TLS, wrong path, not-Subsonic, rejected credentials — has
its own class carrying an actionable `hint`, and maps to a distinct exit code. Never
collapse two into one.

**Config** resolves environment (`DROPWATCH_` + key, `-` → `_`) over the settings
file over defaults, and `config` prints which won. Writes are atomic, mode 600, and
preserve hand-added keys.

`dropwatch.py` at the root is a launcher shim that puts `src/` *ahead* of the script
directory on `sys.path`; the shim and the package share a name, so that order is
load-bearing.

## Documentation

Four documents, four jobs. Keeping them distinct is what stops them drifting.

| File | Owns | Update when |
|---|---|---|
| `README.md` | The pitch — what it is, install, quickstart, limitations | Install, entry points or a stated limitation changes |
| `docs/GUIDE.md` | The walkthrough — first scan, reading the report, review, downloading | Behaviour a user would notice changes |
| `docs/COMMANDS.md` | The reference — every command, flag, setting | Any command-line surface changes |
| `docs/DESIGN.md` | Invariants, rejected alternatives, measured API quirks | A *rule* changes — rare by design |

A typical feature touches GUIDE and COMMANDS; README only when the shape of the
tool changes, not its details. If you find yourself describing behaviour in
`DESIGN.md`, it belongs in GUIDE or COMMANDS instead.

## Working here

- Tests never touch the network and never execute a downloader — `conftest.py`
  fakes both APIs. Keep it that way. Build on what it already provides rather
  than hand-rolling: the `fake_http`, `config` and `store` fixtures, and the
  `local_artist` / `local_album` / `deezer_release` / `subsonic` builders.
  `test_entry_points.py` is the exception that *does* spawn a real interpreter,
  because both import-path mistakes the shim guards against have happened.
- **`build/` holds a gitignored copy of every source file**, so a repo-wide
  `grep` returns each symbol twice and the second hit is a stale artefact. Search
  `src/` and edit only there; `build/lib/dropwatch/` changes nothing.
- Comments explain **why**, not what — usually the alternative that was rejected
  and the reason. Match that; it is the house style and it carries real
  information.
- Hint text builds its examples from `invocation_name()` rather than hardcoding
  `dropwatch`, because there are three ways in and the hint should be pasteable
  from whichever one the user used.
