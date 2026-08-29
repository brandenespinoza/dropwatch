# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

# DropWatch

Lists Deezer releases by artists in a Navidrome library that the user appears not
to own. Python 3.10+, standard library only, no runtime dependencies.

## Hard rules

These are not preferences. Each has already been decided against, with reasons
in [docs/DESIGN.md](docs/DESIGN.md); breaking one builds a different tool.

- **Never make it a downloader.** No acquisition or decryption code, no
  downloader shipped or depended on. `rip` runs a command the user configured,
  in a separate process, and reads only its exit status. Do not vendor, import
  or reimplement one — not even to make `rip` work without setup.
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
python3 -m pytest                                   # 696 tests
python3 -m pytest tests/test_rip.py -q              # one file
python3 -m pytest tests/test_rip.py::TestBuildCommand::test_quoted_arguments_survive
python3 -m pytest -k scope                          # by name

python3 dropwatch.py scan --help                    # run from source, no install
uv tool install --force .                           # push source edits to the installed command
```

`pytest` is the only dev dependency; no linter or formatter is configured.

The installed `dropwatch` is a **copied snapshot**, not an editable install, so
source edits do not reach it until reinstalled — hence the `--force` line above.

`tests/conftest.py` points `$DROPWATCH_CONFIG_DIR` and `$DROPWATCH_STATE_DIR` at a
tmp directory and unsets `$DROPWATCH_ENV` for every test, so the suite cannot touch
real config or state. Set those two by hand to point a manual `dropwatch.py` run at
scratch state.

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

Three documents, three jobs. Keeping them distinct is what stops them drifting.

| File | Owns | Update when |
|---|---|---|
| `README.md` | The guide — what it does, worked examples | Behaviour a user would notice changes |
| `docs/COMMANDS.md` | The reference — every command, flag, setting | Any command-line surface changes |
| `docs/DESIGN.md` | Invariants, rejected alternatives, measured API quirks | A *rule* changes — rare by design |

A typical feature touches the first two. If you find yourself describing
behaviour in `DESIGN.md`, it belongs in the other two instead.

## Working here

- Tests never touch the network and never execute a downloader — `conftest.py`
  fakes both APIs. Keep it that way.
- Comments explain **why**, not what — usually the alternative that was rejected
  and the reason. Match that; it is the house style and it carries real
  information.
- Hint text builds its examples from `invocation_name()` rather than hardcoding
  `dropwatch`, because there are three ways in and the hint should be pasteable
  from whichever one the user used.
