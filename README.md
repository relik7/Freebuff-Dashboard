# Freebuff Dashboard

**Freebuff Dashboard** is a local, read-only view over the conversation
databases Freebuff Desktop keeps on this machine. It discovers your projects,
renders every thread in full and answers searches live — with no backend, no
account, no writes to the databases it reads, and no dependencies beyond the
Python standard library. Two views carry it: **Search**, one query across every
project, and **Activity**, the board of the turns in flight.

> **Local-first:** everything runs on `127.0.0.1`; the source databases are
> opened read-only with a denying authorizer, and the tool never reads
> `state.json` or renders sponsored parts.
>
> **Standard library only:** Python 3.11+, no third-party packages, no build
> step. The SQLite build's capabilities are probed at startup and reported by
> `/api/status`.

## The views

**Global search** — one query over every project, with the two modes (*words*,
*exact*), the scopes, the five categories and a date range as filters; an empty
query is a browse.

![Global search: the query, the filters along the top and the results below](screenshots/global-search.jpg)

**Thread view** — a whole conversation, turns paired by `seq`: user and agent
prose as Markdown, every thinking part and tool call its own collapsed entry,
and the files a turn changed as one diff card.

![Thread view: one conversation, with its thinking, tool calls and diff card](screenshots/thread-view.jpg)

**Activity view** — `/activity`, the turns in flight while they run, pushed over
the same event stream. A row carries the thread's model, its message count and
its newest message, glows when the turn ends, and is itself a link into that
thread. This shot holds two rows: one thread still **running**, and one that has
just **finished** — it stays on the board, tracking its newest message, while it
waits out `activity.close_seconds` before its row is removed.

![Activity view: a running thread beside a finished one waiting out the close timeout](screenshots/activity.jpg)

## How it works

When you open the page, the server:

1. **Discovers projects** — every `projects/*/project.json` under the Freebuff
   Desktop config root, labelled by its `projectPath`.
2. **Reads each database read-only** — `mode=ro` plus a SQLite authorizer that
   denies writes, updates, deletes, drops and pragmas, so a bug cannot become a
   write.
3. **Renders conversations in full** — turns paired by `seq`, prose as
   Markdown, every thinking part and tool call its own collapsed entry, a
   turn's changed files as one diff card.
4. **Searches live** — two modes (words and exact) across three scopes, with
   five categories: *User messages*, *Agent responses*, *Thinking*, *Tool
   runs*, *File diffs*; dates, *hide closed*, and an empty query as a browse.
5. **Pushes refreshes** — a watch beat re-reads the sources on an interval and
   pushes what changed to every open page over server-sent events; the page
   never polls.
6. **Draws the board** — `/activity` (or `python fb-dashboard.py --activity`) is
   a live view of the turns in flight, pushed the same way: a turn that
   finishes glows in the spectrum for `activity.close_seconds` seconds (30 by
   default) and slides off, its row still tracking the newest message until the
   board lets it go. Every row is a link into that thread.

## Run

You need Python 3.11+ and nothing else.

**Windows:**

```bat
run.cmd
```

**Linux, macOS, WSL:**

```sh
./run.sh
```

Then browse to `http://127.0.0.1:8770` (the port comes from your config; the
server binds loopback only and refuses `0.0.0.0`).

### Try it without touching a real profile

```bat
run-mock.cmd        Windows
./run-mock.sh       Linux, macOS, WSL
```

The mock launcher builds a mock Freebuff profile in the folder it runs from —
three fictional projects with open, closed and archived threads — and serves it
on `http://127.0.0.1:8771`. Real Freebuff profiles are never used, and nothing
machine-specific ships inside the archive.

### Directly

```sh
python fb-dashboard.py --help
python fb-dashboard.py --version
python fb-dashboard.py --port 8800 --no-index
```

Flags: `--host`, `--port`, `--config`, `--no-index` (live SQL only),
`--activity` (start on the activity page of running turns instead of the
search page), and `--version`, which prints the build identity — computed
from content, not declared.

## Configuration

Copy `config.json.example` to `config.json` beside the launcher; every key is
optional and documented in the file itself. That file is read whenever it is
there — the tool looks in the folder it is run from first, then beside the
launcher — and `--config <path>` names a different one instead. Whichever it
reads is printed in the run log (`config: …`, or that no file was found) and
reported by `/api/status` under `config.path`, so a setting that looks ignored
is never a guess. The main keys:

- `server` — host, port, `open_browser`, `open_path` (what the browser is
  opened on; `/activity` is the activity page).
- `data` — `freebuff_config_root` (where the profiles live; the default is the
  platform's own config home and `/api/status` reports what was chosen),
  `projects`, `exclude`, and `watch_seconds`, the beat that drives the push.
- `activity` — `close_seconds`: how long the activity page keeps a turn that
  has finished in front of you before its row is removed, in seconds (`30` by
  default, `0` slides it off at once). `/api/status` reports the value in use.
- `search` — default mode, scope, categories, result limit, snippet length.
- `ui` — theme, `hide_closed`.

Search runs live over the databases today; the ranked conversation index is
future work, and `--no-index` keeps the tool off the index entirely.

## Layout

```
fb-dashboard.py               entry point and CLI
dashboard/
├── app.py                    HTTP server, routes, the event stream
├── board.py                  the board: the turns in flight, live
├── build_id.py               content-computed build identity
├── config.py                 config loading and the profile root
├── corpus.py                 SQL over the sources, schema aliases
├── fleet.py                  discovery, read-only connections
├── markdown.py               escape-first Markdown renderer
├── reader.py                 rows become conversations, entries, diffs
├── search.py                 live search, categories, filters
├── watch.py                  the beat that pushes refreshes
└── pages/                    index.html, app.css, app.js — the served UI,
                              activity.html, activity.css, activity.js — the
                              board
tools/
└── make_test_profile.py      builds the mock Freebuff profile
tests/
├── run_tests.py              the runner
├── fixture.py, harness.py, stamps.py, suites.py, testlog.py
├── config/                   fixtures the gates run the tool with
└── test_*.py                 the gates
config.json.example           every default, documented
run.cmd, run.sh               start the server
run-mock.cmd, run-mock.sh     mock profile + server
screenshots/                  the three views above
```

## The guarantees

- **Read-only on the source databases.** `mode=ro` with a denying authorizer on
  every connection; `tests/test_readonly.py` hashes the databases before and
  after a full session and fails on any change.
- **`state.json` is never read** — it holds bearer tokens — and **sponsored
  parts are never indexed or rendered**; both are structural, not filters.
- **Loopback only.** The server binds `127.0.0.1` and refuses `0.0.0.0`; a
  second start on the same port fails in `bind()` by design.
- **Degrades, never assumes.** The interpreter, the SQLite build and its
  capabilities are probed at startup and reported by `/api/status`.
