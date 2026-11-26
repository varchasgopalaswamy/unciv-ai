# Local launchers

`civ-agent game` runs Pydantic AI players and the native Unciv host in a game
container. The controller owns the native game lease. Each agent has private
SDK history, memory, and role plans on disk.

Use the [full-game guide](Full-vanilla-run.md) for installation, provider setup,
container boundaries and backups. Built images need rootless Podman and its user
session. Building images additionally needs Python 3.12+, uv, JDK 21+, and the
selected engine checkout. Model execution uses the installed Pydantic stack.

## Start a native game

```sh
uv sync --locked
uv run civ-agent image build
uv run civ-agent runtime setup
uv run civ-agent auth login
uv run civ-agent game start --state-dir ~/.local/state/unciv-host/my-game
```

Use a fresh persistent directory for an independent game. `--config` selects a
TOML setup; `--model`, `--reasoning-effort` and per-player options configure native
model requests. `--reasoning` remains an alias for `--reasoning-effort`. The
launcher freezes actual settings and provider capabilities before model admission.

For native human play alongside agents:

```sh
uv run civ-agent game start --config examples/human-game.toml --state-dir /path/to/human-game
```

The launcher starts the browser-accessible native game window. Human chat and
trade commands use that player's scoped native API credentials:

```sh
uv run civ-agent chat inbox Rome --state-dir /path/to/human-game
uv run civ-agent trade list Rome --state-dir /path/to/human-game
```

The host owns gameplay legality, visibility and confirmed request receipts. Player
workers receive only their own permitted state and credentials. Social promises
remain stated intent; they do not prohibit native-legal actions.

## Control and inspect

```sh
uv run civ-agent game status --state-dir /path/to/game
uv run civ-agent match watch --state-dir /path/to/game
uv run civ-agent match profile --state-dir /path/to/game
uv run civ-agent match failures --state-dir /path/to/game
uv run civ-agent game stop --state-dir /path/to/game
uv run civ-agent game resume --state-dir /path/to/game
```

Stopping pauses admitted native work and shuts down the game container. Resuming
retains the authoritative checkpoint, budgets, and pinned configuration.
Explicit checkpoint recovery is a separate operator action; ordinary resume does
not choose a new conversation or automatically rewind the turn.

`match` contains native read-only inspection and domain scenario verification.
Game lifecycle control belongs to `game`; the interactive `agent`, HTTPS
`agent-server`, and scheduler control commands have been removed.

For repeatable independent games, use `batch generate`, `batch start`, `batch
status`, `batch stop` and `batch resume`; see the
[batch guide](Full-vanilla-run.md#benchmark-batches).

## Export native histories

```sh
uv run civ-agent history export Rome --state-dir /path/to/game
uv run civ-agent history export Rome --state-dir /path/to/game --notebooks
uv run civ-agent history export Rome --state-dir /path/to/game --epoch RETAINED_EPOCH
```

Exports use canonical Pydantic AI messages and native Harness snapshots, events
and tool effects. `--notebooks` includes native notes and all four role plans.
Only the selected player's stores are read. The default epoch is the current
verified publication; historical selection must name a retained ancestor.

These are privileged operator files containing private context. Reads use SQLite
backup copies without provider or game API calls. An export is an
observational report, not a coordinated recovery checkpoint.

## Inspect a native save

```sh
uv run civ-agent host inspect Rome --state-dir /path/to/game
uv run civ-agent host inspect --state-dir /path/to/game --export-only
```

Inspection exports a separate native save. Desktop gameplay changes only the
inspection copy. Use the selected engine checkout for both code and assets with
`--unciv-dir`; see the [development workflow](../Development-workflow.md).
Full-state inspection is an operator capability, never a player tool.

`host start` starts a standalone engine host for development. Use `game start`
for an ordinary game with agent turn scheduling.

## Installation and validation

An activated project environment permits `civ-agent` without `uv run`. An
independent installation uses `uv tool install .`; image-backed gameplay does not
need a source checkout on the player machine. Local host builds and desktop
inspection can select the checkout with `--repo-dir` or `CIV_AGENT_REPO`.

The native CLI tests check parser dispatch, private credential selection, and
checkout-independent help while legacy harness imports are blocked. They do not
start accounts, models or containers. Runtime acceptance is tracked separately.
