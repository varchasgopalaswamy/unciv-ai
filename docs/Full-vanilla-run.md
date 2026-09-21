# Run the full Vanilla game locally

From the repository directory, using the existing project environment:

```sh
.venv/bin/python scripts/run_vanilla_game.py
```

The launcher starts the host, the three agent services and the ordinary scheduler.
It plays Rome, Greece and France with **gpt-5.6-luna / medium** against native Egypt,
with two city-states. Setup remains Vanilla, Ancient, Quick/Prince, Small Pangaea
and all five native victory types. Agents choose their actions without guided
milestones. The host builds through its normal source-checkout launcher.

The default persistent match directory is
`~/.local/state/unciv-host/vanilla-full`. Ports **8971–8974** must be available.
The terminal shows progress and the launcher also saves its output. A terminal
multiplexer such as `tmux` can keep it running when you disconnect.

For a different directory or ports:

```sh
.venv/bin/python scripts/run_vanilla_game.py \
  --state-dir "$HOME/.local/state/unciv-host/my-vanilla-game" --port 8985
```

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
automatic restart loop. Normal recoverable tool errors can still be handled by
the current agent within its turn, as before.

Override limits with `--turn-timeout`, `--startup-timeout`, `--max-seconds`,
`--max-turns` and `--max-invocations`. `--verbose` also prints detailed tool activity;
the full service history is saved regardless. A new game accepts `--seed`.

## Resume after inspection

For the default directory:

```sh
.venv/bin/python scripts/run_vanilla_game.py --resume
```

For a custom directory, supply the same `--state-dir` and port again. The launcher
refuses to overwrite a saved match without `--resume`. Resume retains accounting,
checkpoints and confirmed work, using the existing scheduler's recovery flow.
It requires both `exploration.json` and `runner/run.json`; if startup failed before
these exist, inspect the startup logs first.

Full-game defaults raise a previous short batch's limits, while retaining any
larger saved limits. Explicit limits may only increase. The elapsed-time limit is
measured from the **original scheduler start**, including pauses; it is not reset
on resume. For example, extend that total allowance to fourteen days with:

```sh
.venv/bin/python scripts/run_vanilla_game.py --resume --max-seconds 1209600
```

The prior three-round efficiency game can also continue from its saved position:

```sh
.venv/bin/python scripts/run_vanilla_game.py --resume \
  --state-dir /tmp/unciv-efficient-live-20260920-1 --port 8985
```

This continuation requires the original `/tmp` directory to remain available.
The default command creates a new match in persistent storage for a long run.

## What to bring back for analysis

Tell Codex the **match directory** and the latest **stop report path** printed by
the launcher. On this machine it can inspect the existing files directly. Keep
the whole match directory until the failure has been diagnosed.

Each launch gets its own `launches/TIMESTAMP-PID/` directory containing:

- `console.log`: scheduler output, diagnostics and the final stop reason.
- `launch.json`: exact command, start time, source/engine revision and model settings.
- `stop.json`: exit status, scheduler reason, unfinished activation, usage and any
  native result/scores available at shutdown.

The match also retains:

- `host.log`, `Rome.log`, `Greece.log`, `France.log`: appended service logs.
- `exploration.json` and `round-checkpoints/`: native state and coordinated recovery points.
- `runner/run.json` and `runner/history/`: progress, per-turn histories and summaries.
- `agent-services/`: per-civilization memory, audits, diagnostics and accounting databases.
- `telemetry/`: per-turn/job token counts, API calls, timings and the native game result.

Exit codes distinguish a native completion (`0`), a stop requiring inspection
(`2`), an exhausted limit (`3`) and a user interrupt (`130`). A nonzero child
process status is propagated. A scheduler returning zero with a blocked turn is
reported as a stop requiring inspection, not a completed game.

The launcher is covered by tests using fake processes, including signal cleanup,
saved-limit protection, diagnostic capture and completion classification. It
does not add gameplay rules or a new recovery mechanism. See
[Vanilla readiness](Vanilla-readiness.md) for the full-game acceptance criteria.
