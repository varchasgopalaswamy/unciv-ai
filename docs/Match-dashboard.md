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

The reader caches unchanged files, accepts atomic replacements, waits for incomplete
JSONL appends, and retains the last readable records with a visible warning if a
file is temporarily corrupt. Epoch and source checks keep histories from different
players or restored timelines from being combined. Message deltas and final text
use the same deduplication as the append-only console renderer.

`tests/test_match_watch.py` covers those reader behaviors, accounting presentation,
keyboard navigation, live refresh, freezing, small terminals, completion, and
clean viewer exit. The UI uses [Textual](https://textual.textualize.io/) and its
[headless test harness](https://textual.textualize.io/guide/testing/).
