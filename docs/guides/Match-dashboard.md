# Terminal match dashboard

Open the dashboard in another terminal while the game runs:

```sh
uv sync --locked
uv run civ-agent match watch --state-dir ~/.local/state/unciv-host/vanilla-full
```

Use the directory of the match you want to inspect. The Vanilla launcher prints
the matching command under `Dashboard`. The viewer can attach during a turn, before
startup has finished, or after a match has stopped. It requires an interactive
terminal. `NO_COLOR` enables a monochrome display.

## What it shows

- Saved scheduler status, the current turn/player, stop reasons, and the age of the
  most recent records. A saved `running` status does not prove that a process is
  still alive; the record age helps identify a stalled or abandoned run.
  The turn's wall timer includes pauses; the turn table separately reports native
  gameplay and bookkeeping measurements.
- Player scores and controllers, including built-in AI and human players when the
  game exports them. Agent rows distinguish gameplay from background bookkeeping.
- Recorded input, cached-input, output, and reasoning-token counts. Cached tokens
  are part of input; reasoning tokens are part of output. Accounting can remain
  partial while a turn or its bookkeeping is in progress.
  Selecting a turn shows that turn's token breakdown above the activity feed.
- Assistant messages, confirmed actions, errors, and lifecycle changes. Repeated
  startup context and detailed tool activity are hidden unless verbose mode is on.
- Model connection warnings remain visible without verbose mode. A quiet model
  produces a notice after 60 seconds without progress, updated at most once a
  minute. This indicates a wait, not a confirmed timeout. Pending tool calls,
  including negotiations, do not count as model silence. Token-usage updates and
  service heartbeats do not count as model progress.
- Confirmed provider retries show a reason, retry attempt/backoff when available,
  and time without progress. The current player's banner and row show waiting or
  retrying; new model output clears that warning and records recovery in Activity.
  Earlier warnings remain in the saved turn history.
- A turn table with gameplay/bookkeeping time, calls, tokens, and accounting
  completeness. Select an older turn to load its saved event history.
- An API-call breakdown for the selected turn. The Game API total counts host
  `tool:` calls once; `command:` rows are a breakdown of those calls. Model-request
  counts are labeled unavailable because the current records do not contain them.
- The native winner and final player scores when the game finishes.

## Controls

| Control | Effect |
| --- | --- |
| Tab / Shift-Tab | Move between controls. |
| Click a tab | Switch between Activity, Turns, and API calls. |
| Enter on a player | Filter activity and totals to that player. |
| Enter on a turn | Inspect that player's recorded activity for that turn. |
| `a` | Return to all players and recent activity. |
| `l` | Return to recent activity for the selected player. |
| `f` | Toggle following the end of the activity log. Scrolling up also preserves your reading position. |
| Space | Freeze/unfreeze dashboard updates. The game continues. |
| `v` | Toggle verbose context, tool details, and exposed reasoning summaries. |
| `q` / Ctrl-C | Close the viewer. The game continues. |

The activity view retains up to 600 recent entries from the latest two recorded
turns per player, plus ongoing gameplay and bookkeeping attempts. Selecting a turn
loads that turn's attempts. Long entries are bounded in the display; full records
remain in `runner/history/`. Verbose mode can also be selected at launch:

```sh
uv run civ-agent match watch --state-dir /path/to/match --verbose
```

## Data and validation

This is a local operator view of `runner/run.json`, `telemetry/game-result.json`,
`telemetry/turns.jsonl`, and the scheduler's sanitized event mirrors. It makes no
game API or model requests and does not write to the match directory. Viewing,
filtering, freezing, and exiting do not change the scheduler or native game.

The metrics area also shows accepted/rejected memory edits, batches containing
rejections, and reason counts for the selected turn, player, or game. These are
separate from API errors: a successful `memory_update` request can contain failed
edits. Older reports display unavailable or partial memory accounting rather than
assuming there were no rejections. The ordinary console includes the same counts
when reporting a completed turn.

The reader caches unchanged files, accepts atomic replacements, waits for incomplete
JSONL appends, and retains the last readable records with a visible warning if a
file is temporarily corrupt. Epoch and source checks keep histories from different
players or restored timelines from being combined. Message deltas and final text
use the same deduplication as the append-only console renderer.

The service records `model_status` events in its private `diagnostics.jsonl` and
turn history; the scheduler mirrors them to `runner/history/` and prints them in
the normal console log. Gameplay, background memory, and communication jobs use
the same model monitor. These notices do not add model requests, change timeouts,
or retry native game commands. Existing turn failure/interrupt behavior still
applies when Codex exhausts its retries.

The monitor uses [Codex app-server events](https://learn.chatgpt.com/docs/app-server#errors)
and a narrow stderr fallback for `codex_core::responses_retry`, needed because
some automatic retries do not emit protocol errors. Only known error categories,
numeric HTTP status codes, attempt counts, and durations are retained. Raw stderr,
provider error bodies, headers, and credentials are discarded; oversized lines
are drained without being saved. The fallback was checked against Codex 0.155.1;
unrecognized future log formats still leave the elapsed-wait notices available.

`tests/test_match_watch.py` covers those reader behaviors, accounting presentation,
keyboard navigation, live refresh, freezing, small terminals, completion, and
clean viewer exit. The UI uses [Textual](https://textual.textualize.io/) and its
[headless test harness](https://textual.textualize.io/guide/testing/).

`tests/test_model_status.py` exercises waits, retries, tool execution, stale event
filtering, and bounded log parsing without wall-clock delays. The scheduler/service
integration test checks the complete path to console, saved history, and dashboard
activity. An optional installed-Codex test uses a local fake Responses endpoint
to verify actual timeout/retry warnings, with no paid requests:

```sh
CIV_TEST_NATIVE_CODEX=/path/to/codex uv run pytest -q tests/test_model_status_native.py
```

Already running processes keep their loaded code. These notices become available
when the services, scheduler, and dashboard are next started with this version.
