# Run the full Vanilla game locally

From the repository directory, using the existing project environment:

```sh
.venv/bin/civ-agent game start
```

The launcher starts the host, the three agent services and the ordinary scheduler.
It plays Rome, Greece and France with **gpt-6-luna / medium** by default against native Egypt,
with two city-states. Setup remains Vanilla, Ancient, Quick/Prince, Small Pangaea
and all five native victory types. Agents choose their actions without guided
milestones. The host builds through its normal source-checkout launcher.

Use `--model` and `--reasoning-effort` to select defaults for the agents,
including gameplay, chat and background strategic reviews:

```sh
.venv/bin/civ-agent game start --model gpt-6-luna --reasoning-effort high
```

Override either setting for individual civilizations with repeatable
`--agent-model CIV=MODEL` and `--agent-reasoning-effort CIV=LEVEL` options:

```sh
.venv/bin/civ-agent game start --state-dir ~/.local/state/unciv-host/mixed-models \
  --model gpt-6-luna --reasoning-effort max \
  --agent-model Greece=gpt-5.6-luna \
  --agent-reasoning-effort France=high
```

This gives Rome gpt-6-luna/max, Greece gpt-5.6-luna/max, and France
gpt-6-luna/high. Each setting falls back independently to its global default.
Use the civilization names from `--agents`; overrides for built-in opponents,
unknown civilizations, or duplicate overrides for the same setting are rejected.

The launcher passes the resolved settings to each agent service and shows them
at startup. It omits a scheduler-wide override for a setting with per-civ choices,
so the scheduler preserves those choices. Launch metadata records the resolved
settings under `agentSettings`; job telemetry records the effective settings
reported by Codex. Editing the launcher does not change running services.

## Choose the game

Map and roster changes are ordinary options; there is no Kotlin edit required:

```sh
.venv/bin/civ-agent game start --state-dir ~/.local/state/unciv-host/my-game \
  --map-radius 26 --city-states 8 \
  --agents Rome Greece France --built-in-ai Egypt \
  --model gpt-6-luna --reasoning-effort high --strategy-guide --fast
```

Use `--map-size Tiny|Small|Medium|Large|Huge` instead of `--map-radius` for an
Unciv preset. Map names are case-insensitive. A new game's default remains Small
Pangaea with three agents and Egypt; the example above chooses a larger map.
Supply `--built-in-ai` without names for no built-in opponents. Native rules
validate civilization names and map generation. `--seed` selects a reproducible
map; otherwise the seed is random and recorded in the launch metadata.

`--strategy-guide` supplies the [optional strategy reference](Strategy-guide.md).
`--fast` requests fast priority for model work. Both default to off and are saved
for resume. Their `--no-...` forms explicitly disable them. Model and reasoning
settings apply to gameplay, communication and background strategic reviews.

The default match directory is `~/.local/state/unciv-host/vanilla-full`.
The host uses port **8971**, followed by one consecutive port per agent; change
this with `--port`. `--repo-dir` selects the host source checkout, and
`--unciv-dir` selects an engine checkout for both code and runtime assets.
The host builds through its normal source launcher.

The launcher starts the existing services and calls the scheduler directly.
Ordinary play does not run a test module or another launcher subprocess.

## Limits and stopping

| Limit | Default |
| --- | ---: |
| Agent turn timeout | 3,600 seconds (one hour) |
| Startup allowance per service, including initial build | 900 seconds (15 minutes) |
| Overall elapsed-time limit | 604,800 seconds (seven days) |
| Round limit from the original start | 331, enough for the native Quick time victory |
| Total model jobs, including chat and interrupted attempts | 10,000 |

Native victory stops the run before these limits. Ctrl-C requests a graceful pause;
allow time for the scheduler to fence actions and flush its services. A timed-out,
failed or blocked gameplay turn stops the launcher for inspection. There is no
automatic restart loop. Repeated stale conflicts and internal state-update errors
stop for inspection with durable [recovery diagnostics](Chat-memory-and-recovery.md#recovery-diagnostics).
Normal recoverable tool errors can still be handled by
the current agent within its turn, as before.

Override limits with `--turn-timeout`, `--startup-timeout`, `--max-seconds`,
`--max-turns` and `--max-invocations`. A new game accepts `--seed`.

Normal output shows colored launcher status, player labels, turn headers,
messages, confirmed actions, errors and turn totals. Completion is green; interrupts
and exhausted limits are yellow; failures are red. `--color auto|always|never`
controls terminal colors;
`auto` detects the launcher's terminal, and `NO_COLOR` disables color in every
mode. Saved `console.log` and histories remain plain text. `--verbose` also prints
preloaded startup context and detailed tool activity; the full service history is
saved regardless. See [live output](Automatic-turns.md#live-output-and-saved-history)
for accounting scope and unavailable metrics.

For the [interactive dashboard](Match-dashboard.md), open another terminal and run
the command printed under `Dashboard`, or:

```sh
.venv/bin/civ-agent match watch --state-dir ~/.local/state/unciv-host/vanilla-full
```

It attaches to the current run and shows player scores, activity, calls, tokens,
and per-turn timings. Quitting the viewer leaves the game running.

## Resume after inspection

For the default directory:

```sh
.venv/bin/civ-agent game resume
```

For a custom directory, supply the same `--state-dir`. The port, guide, priority,
startup timeout and checkout choices are saved in `launch-settings.json`.
`game start` refuses to overwrite a saved board. Resume retains accounting and
rewinds to the start of the interrupted turn, including communication and private state.
Completed native handoffs are retained.
It also inherits the saved global defaults and per-civilization model and reasoning
choices. There is no need to repeat these flags on resume. Repeating one override
retains the other saved overrides. Explicitly conflicting settings are rejected
before services start; choose a new match directory to use a different model or
reasoning effort.
It requires `exploration.json` and the original player credentials. If startup
saved the board before saving scheduler state in `runner/monitoring.sqlite3`, resume starts scheduling that
saved board. If no board was created, fix the startup error and repeat `game start`.

Opening reviews run concurrently before the first move on turn 0, from each
civilization's own starting view and abilities. Each civilization waits for its
own report before playing. Later reviews run asynchronously at the end of turns
5, 10, 15, etc. Set `--review-interval K` to change the later cadence; the opening
review stays on turn 0. The interval is retained on resume.
A review uses the player's model, effort, priority and optional strategy
guide. Other civilizations can play while it runs; the reviewed civilization's
next turn waits for the report and preloads it as dated advice. Waiting does not
consume its gameplay timeout. There are no mandatory post-turn memory writes.

Pending reviews restart from frozen inputs after an interruption. The default
is three attempts; `--review-attempts 4` explicitly permits another attempt.
Interrupted gameplay still rewinds to the start of the active turn. A committed
handoff is retained, and provider accounting remains incomplete when final usage
was not received. Older memory/scheduler formats require their original code;
this version requires a fresh match directory.

Resume retains the saved limits exactly. Explicit limits may only increase.
The elapsed-time limit is measured from the **original scheduler start**, including pauses; it is not reset
on resume. For example, extend that total allowance to fourteen days with:

```sh
.venv/bin/civ-agent game resume --max-seconds 1209600
```

## What to bring back for analysis

Tell Codex the **match directory** and the latest **stop report path** printed by
the launcher. On this machine it can inspect the existing files directly. Keep
the whole match directory until the failure has been diagnosed.

Each launch gets its own `launches/TIMESTAMP-PID/` directory containing:

- `console.log`: scheduler output, diagnostics and the final stop reason.
- `launch.json`: resolved options, start time, source/engine revision and model settings.
- `stop.json`: exit status, scheduler reason, unfinished activation, usage and any
  native result/scores available at shutdown.

The match also retains:

- `host.log`, `Rome.log`, `Greece.log`, `France.log`: appended service logs.
- `exploration.json` and `turn-recovery/`: native state and the current coordinated turn boundary.
- `runner/monitoring.sqlite3`: scheduler progress, event streams, telemetry and scores.
- `agent-services/`: per-civilization memory, audits, diagnostics and accounting databases.

Exit codes distinguish a native completion (`0`), a stop requiring inspection
(`2`), an exhausted limit (`3`) and a user interrupt (`130`). A failed service
startup records its exit status and log path. A scheduler returning zero with a blocked turn is
reported as a stop requiring inspection, not a completed game.

The launcher is covered by tests using fake processes, including signal cleanup,
saved-limit protection, diagnostic capture and completion classification. It
does not add gameplay rules or a new recovery mechanism. See the
[roadmap](../Roadmap.md#active-acceptance-complete-an-ordinary-vanilla-game) for
full-game acceptance and [telemetry](../reference/Telemetry.md) for the accounting contract.

## Short opening benchmark

Use the same launcher with a fresh directory and a smaller round budget:

```sh
.venv/bin/civ-agent game start --state-dir /tmp/unciv-opening --seed 20260920 --max-turns 3
```

Reaching the requested limit returns exit code 3 and saves the same telemetry as
a full game. The saved-position evaluation driver in `tests/live_saved_position.py`
is only for preparing isolated comparisons and seeding evaluation memory.
