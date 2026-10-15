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
| `[game]` | `civilizations`, `randomize`, `master_seed`, `map_seed`, `starting_position_seed`, `assignment_seed`, `map_type`, `map_size`/`map_radius`, `difficulty`, `speed`, `starting_era`, `city_states`, `victories` |
| `[agents]` | `provider` (`openai` or `openrouter`), `model`, `reasoning_effort`, `communication_reasoning_effort`, `review_interval`, `review_attempts`, `strategy_guide`, `civilization_guide`, `fast` |
| `[[players]]` | Unique `name`, `controller` (`agent`, `human`, `built_in_ai`; defaults to `agent`); optional agent `provider`/`model`/`reasoning_effort`/`communication_reasoning_effort` |
| `[limits]` | `max_turns`, `max_invocations`, `max_seconds`, `turn_timeout`, `startup_timeout`, `max_concurrent_jobs`, `model_capacity_retries`, `model_capacity_retry_delay` |
| `[resources.controller]`, `[resources.desktop]`, `[resources.agent]` | `memory_mib`, `cpus`, `pids` |
| `[runtime]` | `image`, `desktop_image`, `browser_port` |

`strategy_guide = true` supplies optional general strategy advice.
`civilization_guide = true` adds the assigned civilization's pre-written advice
after it, including during strategic reviews; it can also be used independently.
Both default to false. Civilization advice currently supports Inca, France,
Greece, Arabia, China, and Persia; enabling it for another agent civilization
fails configuration validation after assignments are resolved. See the
[strategy-guide instructions](Strategy-guide.md) for CLI flags and prompt details.
Gameplay and strategic reviews receive the enabled references. Background
communication uses compact diplomacy guidance instead, avoiding repeated copies
of the full references for acknowledgement-sized exchanges.

`communication_reasoning_effort` independently controls background diplomacy.
When omitted, known gameplay levels above `medium` are capped at `medium`; lower,
`none`, and unknown model-specific levels are preserved. Gameplay and strategic
reviews continue to use `reasoning_effort`.

Players identify competitors independently of their civilizations. List exactly
one player per entry in `game.civilizations`. With `randomize = false` (the default),
the lists are paired in order. With `randomize = true`, player assignments are
shuffled using `assignment_seed`; humans and built-in AI participate too.

Every seed is optional, independently:

```toml
[game]
master_seed = 42
randomize = true
# map_seed = 101                 # Optional terrain override.
# starting_position_seed = 202   # Optional native setup/placement override.
# assignment_seed = 303          # Optional player-to-civilization override.
```

Only omitted child seeds are derived from `master_seed`, using separate stable
hashes. Overriding one never shifts the other streams. If the master is omitted,
the launcher chooses and records it. All seeds are signed 64-bit integers;
`map_seed = 0` is rejected because Unciv treats zero as the current time. Zero is
valid for the master, starting-position and assignment seeds.

`map_seed` controls native terrain generation. `starting_position_seed` controls
native civilization/city-state selection, starting positions, start normalization
and resource placement. The same seeds, settings, roster order and engine version
reproduce the opening map and positions. Each game still has a unique engine ID;
these seeds do not promise identical later combat, AI or model decisions.

**After assigning civilizations**, turns are ordered human first, then agents,
then all built-in AI players. Each group retains civilization-list order; this
does not change which civilization anyone controls. `game status` shows the
resolved players in turn order.

CLI options override corresponding TOML fields. `--player-model "Luna 1=gpt-6-sol"`
and `--player-reasoning-effort "Luna 2=high"` select players by stable names.
Use `--civilizations`, `--randomize`/`--no-randomize`, `--master-seed`, `--map-seed`,
`--starting-position-seed`, and `--assignment-seed` for setup overrides. Changing
the master leaves any explicit child seeds in the file unchanged.
Define controller types in TOML.

`resolved.json` and `container.json` freeze every seed, explicit assignments,
image IDs and login location; subsequent TOML edits do not alter the game.
Resume reads saved assignments and never reshuffles. Player model/effort choices
apply to gameplay, chat and reviews.

### Mix OpenAI and OpenRouter models

Use [the mixed-provider example](../../examples/mixed-providers.toml). Both
providers use the same Codex runner, prompts, game tools, memory and review flow.
OpenAI players retain the dedicated Codex login; OpenRouter players use an API key.
Rebuild images after updating the source:

```sh
.venv/bin/civ-agent image build
.venv/bin/civ-agent auth login --provider openrouter
.venv/bin/civ-agent game start --config examples/mixed-providers.toml \
  --state-dir ~/.local/state/unciv-host/mixed-providers
```

The OpenRouter login command prompts for a hidden API key. For unattended setup it
can import `OPENROUTER_API_KEY` from the environment or an owner-only file using
`--key-file PATH`. The key persists in `AUTH_DIR/openrouter.key` with mode `600`;
never put it in TOML. Only OpenRouter agent containers mount this file, read-only.
They receive no OpenAI login-manager socket or renewable credentials. OpenAI-only
agents receive no OpenRouter key. An entirely OpenRouter roster requires no OpenAI
login. `auth status --provider openrouter` reports key presence without contacting
the provider. Recreate stopped game containers with `game resume` after rotating a key.

Set `provider = "openrouter"` and the full OpenRouter model ID in any agent's
`[[players]]` table. Provider/model/effort travel with that named player through
random assignment across generated games. Use `--provider` for the default or
`--player-provider "Player name=openrouter"` alongside `--player-model` for a CLI
override. The provider also appears in per-job telemetry, strategy status, and
JSON/CSV batch results. `fast = true` requests OpenAI priority only.

Choose models that advertise **tool calling and structured outputs**; reviews use
a JSON schema. Before model work, the runner reads OpenRouter's public catalog,
checks these capabilities and reasoning effort, and saves the model's context
limit/capabilities under that player's durable Codex directory. Resumes reuse that
snapshot. Set `reasoning_effort = "none"` for models without reasoning; otherwise
choose an advertised effort. Catalog validation failures stop with an
`OPENROUTER_*` error before inference. Reasoning levels are provider-specific
and do not imply equal compute.

Transport follows [OpenRouter's Codex configuration](https://openrouter.ai/blog/tutorials/codex-cli-openrouter/):
its Responses API receives namespaced tools and structured review schemas.
Model IDs and backend availability can change; OpenRouter may route a model across
serving providers. Recorded `provider = "openrouter"` identifies the gateway,
not the upstream serving company. Tokens reflect counters reported through Codex;
this is not a provider-billing reconciliation or cost estimator.

Existing games retain their saved images. This adds provider identity to persisted
job settings; older games should finish with their pinned images rather than
upgrading them in place. No migration is included.

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

Use an ordinary single-game TOML as the template. Batch size and concurrency are
command options, not a second configuration format. Generate twenty reusable
single-game inputs without Podman, model authentication or inference:

```sh
.venv/bin/civ-agent batch generate --config examples/benchmark.toml \
  --games 20 --batch-seed 42 \
  --output ~/.local/state/unciv-host/benchmark-inputs
.venv/bin/civ-agent batch start --state-dir ~/.local/state/unciv-host/benchmark \
  --inputs ~/.local/state/unciv-host/benchmark-inputs --parallel 2
```

The input directory is reusable. Launch it again into another independent run:

```sh
.venv/bin/civ-agent batch start \
  --inputs ~/.local/state/unciv-host/benchmark-inputs \
  --state-dir ~/.local/state/unciv-host/benchmark-replay --parallel 2
```

`--games 20` means exactly twenty files named `g0001.toml` through `g0020.toml`.
With `--batch-seed`, the set is reproducible. Each game's master is derived from
the batch seed and its index with a stable hash; collision retries are deterministic,
and adding games preserves the earlier files. Without `--batch-seed`, generation
chooses a fresh batch seed. The template's `game.master_seed` never selects the
batch. The batch seed and game index are recorded in each file's comments and the
batch seed is printed by the command.

Keep `randomize = true` and omit child seeds in the template for independently
sampled maps and player/civilization matchings. An explicit `map_seed`,
`starting_position_seed`, or `assignment_seed` is preserved in every copy; omitted
child seeds remain omitted and are derived when the ordinary game input launches.
For example, `assignment_seed = 7` fixes the matching while maps vary. Matching is
sampled rather than balanced, so an assignment can recur by chance. With
`randomize = false`, players stay paired in list order. Built-in AI still takes its
turns last **after** assignment.

Each generated file copies the template and preserves its settings and comments.
It is a normal single-game input:

```sh
.venv/bin/civ-agent game start \
  --config ~/.local/state/unciv-host/benchmark-inputs/g0001.toml \
  --state-dir ~/.local/state/unciv-host/replayed-opening
```

The output directory is published atomically and an existing destination is never
overwritten. You can edit generated inputs before `batch start`; the runner reads
TOMLs in filename order and saves each one under `games/<input-stem>/`. It first
validates and prepares every game, including pinned images, before launching the
batch service. A preparation failure launches no partial batch. Each saved game
contains its input copy, `resolved.json`, and container metadata. Resume reads only
that saved run: changing or deleting the original input directory cannot change
queued or interrupted games. All games in a batch select the same runtime/desktop
image pair.

`--parallel` bounds simultaneous games (default one). Concurrent human games need
`runtime.browser_port = 0`. The eight-player example has a human seat; change it
to an agent for unattended benchmarking. The benchmark and mixed-provider examples
limit play to three turns; omit `limits.max_turns` for full games.

```sh
.venv/bin/civ-agent batch status --state-dir ~/.local/state/unciv-host/benchmark
.venv/bin/civ-agent batch stop --state-dir ~/.local/state/unciv-host/benchmark
.venv/bin/civ-agent batch resume --state-dir ~/.local/state/unciv-host/benchmark --retry-failed
```

A failed game is recorded and the next queued game can run. Resume continues
pending/paused runs; `--retry-failed` opts into retrying failures. Completed and
budget-exhausted runs stay finished. Explicit image upgrades affect unfinished
runs. `results.json` and `results.csv` read every game's saved configuration and
include every seed, named players, assigned civs, effective provider/model/effort,
stop status, results and failures. While the batch service is active they refresh
after launches/completions and at least every ten seconds; `updated_at` in
`results.json` records the latest refresh. Watch the whole batch with
`match watch --state-dir .../benchmark`, then use `[` and `]` to move between
games. A specific game remains directly watchable with
`match watch --state-dir .../games/<stem>`. Existing per-turn token/call/timing
accounting remains in the databases; see [telemetry](../reference/Telemetry.md).

A temporary SQLite lock during a summary read leaves that game's last result in
place and retries at the next refresh. Other games keep running and refreshing.
Both result files mark the affected row with `result_stale` and retain its
`result_updated_at` timestamp; without a previous successful read, the result and
timestamp are empty. The first lock in each outage is logged in `supervisor.log`.
The freshness fields clear/update after a successful read. These fields describe
derived reports only and do not change batch manifests or game checkpoints.
At startup, monitoring readers wait while the database file exists but its schema
has not been committed. Until a result is available, its value and timestamp are
empty; later refreshes pick up the initialized database. This startup wait does
not suppress errors from incompatible schemas or corrupt databases.
The batch supervisor runs from the Python installation that launches `batch
resume`, so supervisor fixes apply to existing paused batches without rebuilding
or replacing their pinned game images. An interrupted player turn follows the
ordinary [turn recovery](../architecture/Turn-recovery.md) procedure; completed
turns are retained.

`batch generate --dry-run` previews the generated TOMLs without publishing a
directory. `batch start --dry-run` previews resolved inputs without creating state
or contacting Podman. These validate the TOML, not native rules; native validation
happens on actual start or `game start --dry-run`.

The old `game.seed` and `[batch]` matrix fields are removed. Use `game.map_seed`
for a terrain override and `batch generate --batch-seed` for a reproducible input
set. There is no migration for old batch manifests or saved configurations; keep
their original checkout/images if resuming them. Rebuild images for new games with
these fields.

## Files to retain for diagnosis

Keep the entire state directory. Key files are:

- `input.toml`: the game input (generated batches include every resolved seed).
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
