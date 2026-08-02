# Command reference

Every command, flag and setting in `dropwatch`.

Every subcommand is named explicitly. A bare `dropwatch` prints this list
and does nothing — scanning talks to two servers and takes minutes, so it
is asked for by name: `dropwatch scan`.

---

## By task

| I want to… | Command |
|---|---|
| Configure it the first time | `dropwatch setup` |
| See what's configured, and where each value comes from | `dropwatch config` |
| Change one setting | `dropwatch config set <key> <value>` |
| Change the password | `dropwatch config password` |
| Confirm Navidrome is reachable | `dropwatch check` |
| List releases I don't own | `dropwatch scan` |
| List only recent ones | `dropwatch scan --since 2024-01` |
| List only albums | `dropwatch scan --type album` |
| **See what needs my attention** | `dropwatch status` |
| **Answer it** | `dropwatch fix` |
| See what I've already decided | `dropwatch status --decided` |
| Fix one artist matched to the wrong Deezer artist | `dropwatch fix "<artist>"` |
| Never report an artist again | `dropwatch block --artist "<artist>"` |
| Dismiss one release from the results | `dropwatch block --album <url>` |
| Undo a block | `dropwatch unblock --artist \|--album <x>` |
| Force fresh data from Deezer | `dropwatch scan --refresh` |

---

## Setup and configuration

### `setup`

```bash
dropwatch setup
```

Guided first-run configuration. Prompts for URL, username and password, tests
the connection before saving, and retries up to three times if the connection
fails. Writes to `~/.config/dropwatch/.env` at mode 600. Press Enter at any
prompt to keep the existing value.

### `config`

```bash
dropwatch config                      # show every setting and its source
dropwatch config path                 # print the settings file location
dropwatch config set <key> <value>    # change one setting
dropwatch config unset <key>          # remove one setting
dropwatch config password             # set the password (prompted, not echoed)
```

A bare `config` prints each setting, its effective value and where that value
came from — `environment`, a file path, or `default`. That third column is the
answer to "why isn't my change taking effect".

`config set password` is refused: it would put the password in your shell
history and in `ps` output. Use `config password`.

### Settings

| Key | Environment variable | Default | Meaning |
|---|---|---|---|
| `url` | `NAVIDROME_URL` | — | Navidrome base URL. Required. A bare `host:port` is assumed to be http. A trailing `/rest` is stripped. |
| `username` | `NAVIDROME_USERNAME` | — | Navidrome username. Required. |
| `password` | `NAVIDROME_PASSWORD` | — | Navidrome password. Required. Never echoed. |
| `timeout` | `REQUEST_TIMEOUT_SECONDS` | `20` | Per-request timeout in seconds. |
| `cache-path` | `CACHE_PATH` | `~/.local/state/dropwatch/state.sqlite3` | Where local state lives. |
| `cache-max-age` | `CACHE_MAX_AGE_HOURS` | `24` | Lifetime for volatile cache entries. Album track lists are kept 30 days regardless. |
| `types` | `RELEASE_TYPES` | all | Release types to report, comma separated: `album`, `ep`, `single`, `unknown`. |

Precedence, highest first:

1. A real environment variable (`NAVIDROME_URL`, etc.)
2. The settings file — `~/.config/dropwatch/.env`, or `$DROPWATCH_ENV` if set

A value set in your shell environment silently overrides the file. `config`
tells you when that's happening.

There is no `--env-file` flag and no `./.env` in the working directory. Both
were ways to change settings without going through `config`, and a stray `.env`
in whatever directory you happened to be standing in could quietly decide which
server got scanned.

### `check`

```bash
dropwatch check
```

Verifies connectivity and credentials, prints the server version and the number
of visible album artists, then exits. No Deezer traffic, no state written.

---

## Scanning

### `scan`

```bash
dropwatch scan                             # the whole library
dropwatch scan --since 2024-06-01          # only releases on or after this date
dropwatch scan --artist "Björk" --artist "Ghost"
dropwatch scan --limit 20 --type album
```

| Flag | Effect |
|---|---|
| `--artist NAME` | Restrict to this local artist. Repeatable. |
| `--limit N` | Scan at most N artists. |
| `--since DATE` | Only consider releases on or after `YYYY`, `YYYY-MM` or `YYYY-MM-DD`. |
| `--type TYPE` | Only report `album`, `ep`, `single` or `unknown`. Repeatable. Overrides the `types` setting for this run. |
| `--refresh` | Ignore cached Deezer data and refetch everything. Slow. |
| `--flat` | One continuous list sorted by date, without release-type groups. |
| `--no-progress` | Suppress the progress line. |

Output is written to stdout in two parts: the results and a summary. The
"needs review" and "unresolved artists" sections go to stderr. Piping to a file
therefore captures the results and leaves the follow-up prompts on your
terminal.

Interrupting with Ctrl-C is safe. Everything already fetched stays cached, so a
re-run resumes cheaply.

---

## Answering what the scan couldn't

A scan produces two kinds of "I don't know":

1. **Which Deezer artist is your "Ghost"?** There are six. It won't guess.
2. **Do you already own this release?** The title nearly matches something you
   have, but not quite.

`status` tells you how many of each are outstanding. `fix` walks them.

### `status`

```bash
dropwatch status              # what needs you, and a summary of what's saved
dropwatch status --decided    # every mapping, block and release decision
```

```text
Needs you
  5 artist(s) could not be matched to Deezer
  3 release(s) need a decision

  Work through them:  dropwatch fix

Saved
  12 artist mapping(s)
  3 blocked artist(s)
  7 release decision(s): 2 blocked, 4 owned, 1 missing

  List them:  dropwatch status --decided
```

`--decided` is where you go to find a wrong answer and undo it. It lists
mappings, blocked artists and release decisions separately, each with the
command that reverses it. Release titles are looked up from cache; only a
release never seen before costs a request.

### `fix`

```bash
dropwatch fix                        # walk everything pending
dropwatch fix "Ghost"                # re-open one artist, even if mapped
dropwatch fix --album 558123 --own   # answer one release, no prompts
```

A bare `fix` walks the artists first, then the releases. Both need a terminal.

**For each artist** it shows up to six Deezer candidates with a few of their
album titles — titles you already own are listed first, because that's what
actually identifies the right artist.

| Key | Action |
|---|---|
| `1`, or `1 3`, or `1,3` | Map to these candidates. Several are merged and de-duplicated. |
| `d <id or URL>` | Map to a Deezer artist not in the list. |
| `s` or Enter | Skip, leave as-is. |
| `b` | Block this artist. |
| `c` | Clear what's stored, resolve from scratch next scan. |
| `q` | Quit. |

Deezer routinely splits one act across duplicate artist entries, each holding
part of the catalogue — that's why several can be selected at once.

**For each release** it shows the release and why it couldn't be settled.

| Key | Action |
|---|---|
| `o` | I own it. Stop reporting it. |
| `m` | I don't. Always report it. |
| `s` or Enter | Skip, leave undecided. |
| `u` | Undo a decision made earlier. |
| `q` | Quit. |

Non-interactively, `fix --album <id|URL>` takes `--own`, `--missing` or
`--clear`. Copy the URL straight from the results list.

### Answering directly — `map`, `unmap`, `block`, `unblock`

When you already know the answer, skip the prompts.

```bash
dropwatch map "Ghost" 1160651 4859761          # several are merged
dropwatch unmap "Ghost"                        # forget everything, back to unresolved

dropwatch block --artist "Karaoke Hits Vol 3"  # never report this artist
dropwatch block --artist 1160651               # ...or name it by Deezer id
dropwatch block --album 558123                 # one release, any type
dropwatch unblock --artist "..." | --album 558123
```

`block --artist` accepts three spellings of the same artist: the **local name**
as it appears in your Navidrome library, a **Deezer artist id**, or a **Deezer
artist URL**. An id already mapped resolves back to your local name; an unmapped
id is looked up on Deezer and its name used. The local name always works, which
matters because an artist with no Deezer counterpart at all — a karaoke
compilation, a mis-tagged folder — is exactly the kind you most want to block.

Name matching folds case, accents and punctuation, so `"bjork"` finds `Björk`.
Quote names containing spaces. `block "<artist>"` without a flag is shorthand
for `block --artist "<artist>"`.

`unblock --artist` lifts a block and nothing else: if the artist is mapped
rather than blocked it says so and leaves the mapping alone. `unmap` is the
bigger hammer — it clears mappings *and* blocks, returning the artist to
unresolved.

### Which suppression to use

| | Scope | Identified by | Undo |
|---|---|---|---|
| `block --artist X` | every release by that artist, forever | local name, Deezer id, or Deezer URL | `unblock --artist X` |
| `block --album X` | one release | Deezer album id or URL | `unblock --album X` |
| `fix --album X --own` | one release, as *owned* | Deezer album id or URL | `fix --album X --clear` |

`block --album` and `--own` both stop a release being reported, and they are
deliberately different: `--own` records a claim about your library, `block`
records only that you don't want to hear about it. `unblock --album` clears
either one.

### Older command names

`resolve`, `review` and `artists` still work and always will — they map onto
the commands above. They are no longer listed in `--help`.

| Old | Now |
|---|---|
| `resolve` | `fix` |
| `resolve "Ghost"` | `fix "Ghost"` |
| `review` | `fix` |
| `review 558123 --own` | `fix --album 558123 --own` |
| `artists` | `status` |
| `artists --mappings` | `status --decided` |

---

## State

One SQLite file holds everything the tool remembers. It contains two very
different kinds of thing, and the difference is the whole point of this section:

**Cached API responses** — Deezer search results, discographies, album detail
and track lists, plus your Navidrome album track lists. Disposable. Deleting
them costs a slow next scan and nothing else. Two expiry classes:

| Class | Contents | Lifetime |
|---|---|---|
| Volatile | artist searches, discography listings | `cache-max-age`, 24h by default |
| Stable | album detail and track lists | 30 days |

The split exists because a released album's track list never changes, while a
discography listing exists precisely to change. Expiring both on the same clock
re-fetched thousands of immutable records for nothing. Navidrome album tracks
are cached against a fingerprint of the album's song count and duration, so they
invalidate themselves when you actually change the album.

**Your decisions** — artist mappings, blocks, and review decisions. Not
disposable. These are the answers you typed, and nothing can recreate them.

### `cache`

```bash
dropwatch cache                    # what's stored: path, counts, cache age
dropwatch cache --clear            # delete cached API responses
dropwatch cache --reset-mappings   # delete all artist mappings
dropwatch cache --reset-decisions  # forget every review decision
```

`--clear` only touches the disposable half; the next scan just runs slowly.
Reach for it when Deezer data looks stale or wrong. `--refresh` on a scan does
the same thing for one run without deleting anything.

`--reset-mappings` and `--reset-decisions` destroy the other half. They are
immediate and unconfirmed, and there is no undo.

---

## What needs a Navidrome server

Only `scan` and `check` talk to Navidrome, and only they require credentials.
`fix` uses them if present — the artist picker highlights candidates whose album
titles you already own — but works without.

Everything else reads and writes the local state file, or talks to Deezer, whose
public API needs no credentials at all: `status`, `cache`, `block`, `unblock`,
`map`, `unmap`, and `config`.

---

## Global flags

Accepted before or after the subcommand.

| Flag | Effect |
|---|---|
| `-v`, `-vv` | Verbose to stderr; twice for debug logging. |
| `--version` | Print the version and exit. |
| `-h`, `--help` | Help. Works per-subcommand: `dropwatch scan --help`. |

---

## Files

| Path | Contents |
|---|---|
| `~/.config/dropwatch/.env` | Settings, including the password. Mode 600. |
| `~/.local/state/dropwatch/state.sqlite3` | Cache, artist mappings, review decisions, last scan's queues. |
| `~/.local/share/dropwatch/` | The installed virtualenv. |
| `~/.local/bin/dropwatch` | The symlink on your `PATH`. |

`$DROPWATCH_ENV` moves the settings file; `$DROPWATCH_CONFIG_DIR` and
`$DROPWATCH_STATE_DIR` move the directories above (used by the test suite to
keep it out of your real ones).

Deleting the state file loses your mappings and review decisions. Deleting the
`.env` loses your credentials. Nothing else is written anywhere.

---

## Exit codes

| Code | Meaning |
|---|---|
| 0 | Success |
| 1 | Unexpected failure |
| 2 | Command-line usage error |
| 3 | Configuration problem |
| 4 | Navidrome connection or protocol problem |
| 5 | Navidrome rejected the credentials |
| 6 | Deezer problem |
| 7 | Completed, but partially — some artists or albums failed |

Code 7 still prints results; the summary says so. One failing artist never stops
the rest of the scan.
