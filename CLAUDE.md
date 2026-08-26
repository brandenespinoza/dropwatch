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

- Tests: `python3 -m pytest` (692 tests, ~6s). They never touch the network and
  never execute a downloader. Keep it that way.
- Comments explain **why**, not what — usually the alternative that was rejected
  and the reason. Match that; it is the house style and it carries real
  information.
- The installed `dropwatch` command is a **copied snapshot**, not an editable
  install, so source edits do not reach it. After changing code the user runs:
  `uv tool install --force .` (this checkout is the install source).
