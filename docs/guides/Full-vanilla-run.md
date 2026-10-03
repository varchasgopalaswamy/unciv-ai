# Run games in containers

`civ-agent game` runs a durable game under rootless Podman. Each agent has its own
container; a controller runs Unciv and the scheduler. Games run in the background,
survive terminal closure/logout, and use saved configuration and image IDs on resume.
The [container architecture](../architecture/Containers.md) explains the boundaries.

## One-time setup

Use Linux with rootless Podman, Netavark, cgroup v2, nftables, and a systemd user
session. Building images also requires JDK 21+, Python 3.12+, uv, the selected
engine checkout, and the standalone Linux Codex release (including its
`codex-code-mode-host` companion).

```sh
git submodule update --init --recursive
uv sync --locked
.venv/bin/civ-agent image build
.venv/bin/civ-agent runtime setup
.venv/bin/civ-agent auth login
```

Image builds invoke the Gradle wrapper directly. `image build --unciv-dir ../Unciv`
selects both code and assets from that engine checkout. `--codex PATH` selects a
standalone Codex executable; `--tag localhost/my-civ` chooses the image name.
Running a built image needs no JVM source checkout.

`runtime setup` enables systemd user lingering so games survive logout.
`auth login` opens a dedicated device login using your existing account. It leaves
your everyday Codex home unchanged. The renewable login lives in
`~/.local/state/unciv-host/auth`; a shared manager renews it and gives workers only
short-lived access tokens. `auth status` reports whether the login exists and the
manager is running. Use `--auth-dir` consistently to select another durable login.
Changing the account requires stopping the manager and affected games first.

## Start and watch

```sh
.venv/bin/civ-agent game start --state-dir ~/.local/state/unciv-host/my-game
.venv/bin/civ-agent game status --state-dir ~/.local/state/unciv-host/my-game
.venv/bin/civ-agent match watch --state-dir ~/.local/state/unciv-host/my-game
```

Defaults: Rome, Greece and France use **gpt-6-luna / medium**, with native Egypt
and two city-states on Small Pangaea, Ancient, Quick/Prince. Victory conditions
are the native defaults. Games start with concurrent opening reviews; later
reviews run after every fifth complete round. Quitting the TUI leaves play running.
The TUI is an operator view and exposes all players' private information.

The launcher returns after starting a background supervisor. `game status` and
`container-status.json` report eventual startup failures or stops; the launch
command's exit status is not the eventual game outcome. Logs are durable.

## Configure with TOML

Start from [the human example](../../examples/human-game.toml),
[the eight-player example](../../examples/eight-player-game.toml), or
[the benchmark example](../../examples/benchmark.toml):

```sh
.venv/bin/civ-agent game start --config examples/human-game.toml \
  --state-dir ~/.local/state/unciv-host/human-game
.venv/bin/civ-agent game start --config examples/human-game.toml --dry-run
```

`--dry-run` validates configuration through the packaged native engine without
starting agents. Unknown TOML keys, duplicate player names/civilizations, multiple humans,
invalid limits, and model overrides for non-agent seats are rejected.
Native setup validates civilization, map type/size, difficulty, speed, era and victory
names. Mod content is out of scope. Choose `map_size` or a custom `map_radius`.

| Section | Settings |
| --- | --- |
| `[game]` | `civilizations`, `randomize`, `assignment_seed`, `seed`, `map_type`, `map_size`/`map_radius`, `difficulty`, `speed`, `starting_era`, `city_states`, `victories` |
| `[agents]` | `model`, `reasoning_effort`, `review_interval`, `review_attempts`, `strategy_guide`, `fast` |
| `[[players]]` | Unique `name`, `controller` (`agent`, `human`, `built_in_ai`; defaults to `agent`); optional agent `model`/`reasoning_effort` |
| `[limits]` | `max_turns`, `max_invocations`, `max_seconds`, `turn_timeout`, `startup_timeout`, `max_concurrent_jobs` |
| `[resources.controller]`, `[resources.desktop]`, `[resources.agent]` | `memory_mib`, `cpus`, `pids` |
| `[runtime]` | `image`, `desktop_image`, `browser_port` |
| `[batch]` | `seeds`, `repetitions`, `parallel`, `rotate_players` |

Players identify competitors independently of their civilizations. List exactly
one player per entry in `game.civilizations`. With `randomize = false` (the default),
the lists are paired in order. With `randomize = true`, player assignments are
shuffled using `assignment_seed`; humans and built-in AI participate too. Omitted
map and assignment seeds are generated independently and saved before launch.
Set both seeds to reproduce the assignment schedule and map seed.

**After assigning civilizations**, turns are ordered human first, then agents,
then all built-in AI players. Each group retains civilization-list order; this
does not change which civilization anyone controls. `game status` shows the
resolved players in turn order. A map seed controls native map generation; Unciv
also uses per-game randomness for starting locations and other setup details, so
it does not guarantee identical starting positions across separate games.

CLI options override corresponding TOML fields. `--player-model "Luna 1=gpt-6-sol"`
and `--player-reasoning-effort "Luna 2=high"` select players by their stable names.
Use `--civilizations`, `--randomize`/`--no-randomize`, and `--assignment-seed` to
override assignment settings. Define controller types in TOML.
`resolved.json` and `container.json` freeze settings,
both seeds, explicit assignments, image IDs and login location; subsequent TOML edits do not alter the game.
Resume reads the saved assignments and never reshuffles. The old per-player
`civilization` field and model-only rotation format are removed; existing games
from that format require their previous checkout/images to resume.
Player model/effort choices apply to gameplay, chat and reviews.

The default limits are a one-hour agent turn timeout, 900-second startup timeout,
seven days total elapsed time, 10,000 model jobs and the native speed's turn limit
plus one (331 for Quick). Pauses count toward total elapsed time. Native victory
ends the run earlier. Human turns have no model timeout, but whole-match limits apply.

Default limits per container: controller 4 GiB/2 CPUs, graphical controller
6 GiB/2 CPUs, each agent 2 GiB/1 CPU. These are ceilings, not reservations. Size batch
concurrency for actual machine capacity and the shared account's usage limits.

## Play a civilization yourself

```sh
.venv/bin/civ-agent game start --config examples/human-game.toml \
  --state-dir ~/.local/state/unciv-host/human-game
.venv/bin/civ-agent game status --state-dir ~/.local/state/unciv-host/human-game
```

Once startup finishes, open the `browser.url` printed by `game status`. Enter the
password from `STATE_DIR/desktop/password.txt`. The browser streams the native
Unciv window through noVNC; it is not a separate game implementation. Browser
ports bind to **localhost only**. `browser_port = 0` allocates a free port for each
game; `--port` overrides it. Internal game/agent ports are never published.

The human plays first and sees only their civilization's view. Native city, unit,
research and diplomacy controls apply. **Chat** provides public/private messages
and nonbinding promises. **Trade** uses native controls and the shared negotiation
service. Human negotiations have no response deadline. Messages sent during
strategic reviews queue for delivery when reviews finish; trades wait. Unsent
chat drafts and focus survive navigation/background activity within the desktop
session. The operator TUI is separate from the human player's interface.

Closing the browser merely disconnects the viewer. To stop play, use the native
**Stop game** menu or `game stop`. Resume reconnects to the saved game, with any
interrupted turn rewound. Native arbitrary save/load and Undo stay disabled in the
managed match; per-unit automation remains available. Only one human seat per game
is currently supported.

## Stop, remove and resume

```sh
.venv/bin/civ-agent game stop --state-dir ~/.local/state/unciv-host/my-game
.venv/bin/civ-agent game remove --state-dir ~/.local/state/unciv-host/my-game
.venv/bin/civ-agent game resume --state-dir ~/.local/state/unciv-host/my-game
```

`stop` quiesces the scheduler and agent work. `remove` additionally removes game
containers, networks and firewall rules. **Both retain the entire state directory.**
Resume recreates infrastructure using the saved image IDs and rewinds the interrupted
turn, including communication and affected private state. Completed handoffs remain
committed; pending reviews restart from their frozen inputs. Recovery does not
require a surviving container or a model conversation in memory.

Resume retains model/settings and limits. Limits can explicitly increase, for example
`--max-seconds 1209600`. `--image`/`--desktop-image` explicitly upgrade images; otherwise
rebuilding a tag has no effect on existing games. Image upgrades require compatible
saved game data; there are no migrations for the earlier launcher. Preserve pinned
images when pruning Podman storage. For backups, stop the game and retain its full
directory, the login directory, and the images (or reproducible image artifacts).
A stopped game's directory can move; pass its new path to resume. If the login
also moves, supply the new `--auth-dir`.

A failed service stops its game for inspection, not other games. There is no automatic
model/game retry loop. Host reboot requires explicit resume; lingering covers logout,
not automatic boot-time relaunch of games.

## Benchmark batches

```sh
.venv/bin/civ-agent batch start --config examples/benchmark.toml \
  --state-dir ~/.local/state/unciv-host/benchmark --dry-run
.venv/bin/civ-agent batch start --config examples/benchmark.toml \
  --state-dir ~/.local/state/unciv-host/benchmark
.venv/bin/civ-agent batch status --state-dir ~/.local/state/unciv-host/benchmark
.venv/bin/civ-agent batch stop --state-dir ~/.local/state/unciv-host/benchmark
.venv/bin/civ-agent batch resume --state-dir ~/.local/state/unciv-host/benchmark --retry-failed
```

The matrix expands map seeds × repetitions × player rotations. With
`rotate_players = true`, every block contains one game per player: shuffle once
if `game.randomize` is enabled, then cyclically rotate the complete named player
profiles through all civilizations. Every player, including human and built-in AI,
gets every civilization exactly once per map seed and repetition. Model and effort
remain attached to player names. Distinct players with identical settings still
get separate rotations. Final turn ordering always keeps built-in AI last.

Assignment schedules are independent of map seeds: each repetition has its own
reproducible assignment seed, reused across all maps in that repetition. With
`rotate_players = false`, each seed/repetition produces one game. The four-player
benchmark example produces eight runs (two seeds × four assignments). The
eight-player example produces eight runs per seed; change its human player to an
agent for unattended benchmarking. `game start` always starts just one game,
regardless of the `[batch]` section.

`parallel` bounds simultaneously running games; `--parallel` overrides it. Human
batches with concurrent games must use automatically allocated browser ports.

The queue is persisted before launching. A failed game is recorded and the next
queued game can run. Resume continues pending/paused runs; `--retry-failed` opts into
retrying failures. Completed and budget-exhausted runs are retained without relaunching. Explicit `--image` and
`--desktop-image` upgrades affect unfinished resumed runs. Each game has its own
`games/gNNNN` directory and can be watched with `match watch`. `results.json` and
`results.csv` collect player names, assigned civilizations, effective model/effort,
assignment seeds, stop status, available game results and failures.
Per-turn tokens, API calls, timing, winners and final player scores retain their
existing database accounting; see [telemetry](../reference/Telemetry.md).

## Files to retain for diagnosis

Keep the entire state directory. Key files are:

- `resolved.json`, `container.json`: frozen configuration, IDs and image references.
- `supervisor.log`, `launches/<id>/*.log`, `host.log`: controller and service logs.
- `container-status.json`, `runtime.json`, `resources.json`/`resources.jsonl`: status, failures and resource samples.
- `exploration.json`, `turn-recovery/`: game and coordinated turn boundaries.
- `runner/monitoring.sqlite3`: scheduler, events and accounting for the TUI.
- `agent-services/<civ>/`: private databases, reviews, receipts and diagnostics.
- `agents/<civ>/`, `codex/<civ>/`: isolated workspaces and Codex state.
- `desktop/`: native client/VNC state for human games.

The [saved-position driver](../scenarios/Strategic-positions.md) remains a source-level
development tool. Ordinary launches and opening benchmarks use these container commands.
