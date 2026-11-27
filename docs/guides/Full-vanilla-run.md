# Run games in containers

`civ-agent game` runs one controller per game under rootless Podman. The
controller runs Pydantic AI agents and the native JVM host; human games add the
native desktop. Shared services manage subscription login renewal and optional
Decisions requests. Games run in the background, survive terminal closure/logout,
and retain their saved configuration and image IDs on resume. There is no
Temporal server, PostgreSQL database or player-worker service.
The [container architecture](../architecture/Containers.md) explains the boundaries.

## One-time setup

Use Linux with rootless Podman, Netavark, cgroup v2, nftables, and a systemd user
session. Building images also requires JDK 21+, Python 3.12+, uv and the selected
engine checkout.

```sh
git submodule update --init --recursive
uv sync --locked
.venv/bin/civ-agent image build
.venv/bin/civ-agent runtime setup
.venv/bin/civ-agent auth login
```

Image builds invoke the Gradle wrapper directly. `image build --unciv-dir ../Unciv`
selects both code and assets from that engine checkout. `--tag localhost/my-civ`
chooses the image name.
Running a built image needs no JVM source checkout.

When a non-login shell omits `XDG_RUNTIME_DIR` or `DBUS_SESSION_BUS_ADDRESS`,
the launcher fills them from the existing, user-owned `/run/user/UID` directory
and its bus socket. Explicit settings are preserved. A systemd user session must
already exist; this does not create one.

Image builds use `podman --cgroup-manager=cgroupfs build` so their `RUN` steps
do not need systemd D-Bus authentication. This avoids crun's `sd-bus call` /
`interactive authentication` failure in shell environments where the runtime
cannot use the user manager. The option applies only to image builds; game
containers retain their normal systemd supervision and resource limits.

`runtime setup` enables systemd user lingering so games survive logout.
`game start` and `game resume` in a terminal guide you through a missing
OpenAI login and continue once you sign in. Use the separate `auth login`
command above when preparing an unattended launch.
`auth login` prints a browser login URL using Pydantic AI's native subscription
login flow. Finish sign-in in that browser; the callback returns to
`http://localhost:1455/auth/callback`. When running the command over SSH, forward
port 1455 to the machine running the command first. Login runs on the host and
does not need a built runtime image or a Codex process.

The renewable login lives in
`~/.local/state/unciv-host/auth/openai-subscription.json`; a shared manager renews
it through Authlib and gives the controller only access tokens. `auth status` reports
whether the login exists and the manager is running. Use `--auth-dir`
consistently to select another durable login. Changing the account requires
stopping the manager and affected games first. To start a fresh match, choose
a new game state directory and keep this shared auth directory. A launcher can
replace a stopped legacy broker or a verified service still attached to a
deleted auth directory. If a live broker is attached to an existing directory
and its ownership record is missing, the launcher reports the recovery command.

Stop the shared login manager with:

```sh
.venv/bin/civ-agent auth stop
```

Use `--auth-dir PATH` to select another login directory. Repeated stops succeed
when the manager is already stopped or absent. Saved credentials are retained;
the separate Decisions service keeps running. Starting or resuming an OpenAI
game starts the login manager again. Check its state with `civ-agent auth status`.

Recreate the shared login and Decisions services from the latest built runtime
image with:

```sh
.venv/bin/civ-agent auth restart
```

This restarts configured services, retains credentials and the Decisions spending
ledger, and verifies Decisions readiness without a paid API request. Use
`--auth-dir PATH` for another auth directory or `--image IMAGE` for another runtime
image. The command checks the image's current Decisions protocol before stopping
either service; stale images fail with a rebuild instruction. Run
`civ-agent image build` first to include source changes. Stop affected
games before upgrading their shared services; saved games retain their pinned
runtime images.

Game-only image updates can reuse an already running Decisions broker when the
selected image and the authenticated broker health endpoint both match the
launcher's complete version contract. This permits updating a stopped game
alongside other active games. A protocol or policy mismatch still requires
rebuilding and explicitly restarting the shared services.

### Google Cloud / gcloud

Gemini players use Pydantic AI's native `GoogleCloudProvider` and `GoogleModel`.
Authenticate to the Cloud project whose billing account holds your credits, enable
the API, then import Application Default Credentials (ADC):

```sh
gcloud auth login --update-adc
gcloud config set project YOUR_PROJECT_ID
gcloud services enable aiplatform.googleapis.com
.venv/bin/civ-agent auth login --provider google-cloud --project YOUR_PROJECT_ID
.venv/bin/civ-agent auth status --provider google-cloud
```

The import reads `GOOGLE_APPLICATION_CREDENTIALS`, or the standard gcloud ADC
file under `CLOUDSDK_CONFIG` / `~/.config/gcloud`. Use `--credentials-file PATH`
to select a private ADC JSON file explicitly and `--location us-central1` to
choose a location instead of the default `global`. The chosen project is also
used as the quota project, overriding an older quota project in imported ADC.
The account must have permission to use Vertex AI and consume services in that
project. This setup uses Google Cloud billing; an AI Studio key is a separate route.

Only a private copy of ADC and the project/location settings are mounted read-only
into the trusted game controller. The gcloud configuration directory is not
mounted. Google's auth library renews credentials in the controller; credentials
are absent from model inputs, saved role sessions, and container arguments.
After switching accounts or renewing the source ADC login, import it again before
starting or resuming affected games. Login import and `auth status` are local
checks; they do not establish that the account has access to a particular model.

Start a Google Cloud game with:

```sh
.venv/bin/civ-agent image build
.venv/bin/civ-agent game start --config examples/google-cloud-game.toml \
  --state-dir ~/.local/state/unciv-host/gemini-game
```

Or set `provider = "google-cloud"` and a Gemini model ID on individual players,
`economy_model`, or `military_model` in a mixed-provider game. Main, subordinate,
communication, review, and peace roles all use the same native adapter. Both Code
Mode and direct tools use the existing local tool execution path. Gemini starts
with implicit caching, and transient failures use the ordinary agent retry policy.
`fast` remains an OpenAI-only option and does not select a Google priority tier.
Google-only games do not require an OpenAI subscription login or an OpenRouter key.

## Start and watch

```sh
.venv/bin/civ-agent game start --state-dir ~/.local/state/unciv-host/my-game
.venv/bin/civ-agent game status --state-dir ~/.local/state/unciv-host/my-game
.venv/bin/civ-agent match watch --state-dir ~/.local/state/unciv-host/my-game
```

Defaults: Rome, Greece and France use **gpt-6-luna / medium**, with native Egypt
and two city-states on Small Pangaea, Ancient, Quick/Prince. Victory conditions
are the native defaults. Quitting the TUI leaves play running.
The TUI is an operator view and exposes all players' private information.

Expected provider waits keep the game alive. The affected role publishes its
waiting state and retries within the configured policy. A provider wait does not
restart the turn. Application bugs stop for inspection and require explicit
`game resume` after a fix. Operator stop and whole-game budgets still apply.

The dashboard shows the controller stage and each active role separately. Main
finishing does not mean the turn has ended: Economy or Military can still be
working, followed by native completion validation and handoff. Recorded model
request counters separate gameplay work from background communication.

The launcher returns after starting a background supervisor. `game status` and
`container-status.json` report eventual startup failures or stops; the launch
command's exit status is not the eventual game outcome. Logs are durable.

## Record and export a replay

Pass `--record-replay` to `game start` or set `record_replay = true` in
`[runtime]`. Use a runtime image built with replay support. Resume with
`--record-replay` to start recording an existing game from its saved position.
After recording starts, resume continues the archive automatically.

Keep `exploration.json.replay/` beside `exploration.json` when moving a game.
The archive contains private operator data, including the whole world. The
civilization export applies the visibility recorded at the time of each event.

```sh
.venv/bin/civ-agent replay --state-dir PATH
.venv/bin/civ-agent replay --state-dir PATH --civilization Greece --output greece.jsonl
.venv/bin/civ-agent replay --state-dir PATH --civilization Greece --video greece.mp4
.venv/bin/civ-agent replay --state-dir PATH --web replay.html
```

Open `replay.html` directly or embed it in a static page. It includes the viewer
and replay data in one file and works offline. Switch civilizations at the same
point in time, step through visible events, or jump to a turn. The Decisions tab
shows recorded agent rationales and strategic reviews. The Diplomacy tab shows
the selected civilization's messages, proposals, and outcomes. Older diplomacy
and strategic reviews can have turn-level timing.

The civilization selector shows each saved model or controller when
`resolved.json` is available. The viewer embeds Unciv's HexaRealm terrain art,
unit sprites and flags, and territorial border art from the bundled engine.
City nameplates, banners, and borders use the ruleset's civilization colors
and the selected perspective's recorded observations. Use
`--unciv-dir PATH` to select the matching engine checkout when exporting a game
built with an override.

Use the minimap to center the view, the zoom buttons or mouse wheel to change
scale, and hover or tap to inspect an explored tile. The Research and Policies
panels follow the selected civilization and replay position. City production
and other private progress appear in the owner's view. The native replay adds
these details at committed checkpoints. Snapshot exports show values recorded
in the original player observation, including cost and turn estimates when present.

The Statistics panel charts the saved Unciv rankings for the civilizations in
the export. Hover over the chart for a turn's exact values, or click to go to
that turn. A completed game shows its victory result at the end of the replay.

```html
<iframe src="replay.html" title="Game replay" sandbox="allow-scripts" width="100%" height="720"></iframe>
```

The HTML contains the data for every included civilization, including private
messages and authored notes. Use `--civilization Greece --web greece.html` to
export a single perspective, and share the file only with its intended audience.
Keep `coordinator/` with the game when exporting agent rationales and reviews.

For a save created before native recording, use `--from-snapshots --web replay.html`
with the same state directory. This export uses the saved player's map observation
for each available turn and includes recorded decisions, reviews, and diplomacy.
Dashed arrows connect confirmed action endpoints. A player without saved
observations gets a territory history view based on the engine's tile history.

The video export requires FFmpeg with SVG decoding and H.264 encoding. It draws
an original hex map from recorded observations and marks visible movement and
combat. The archive reconstructs committed engine states from recorded changes
and checks each state's hash. It also carries resolved movement and sight
events inside a turn, including native AI turns. Playback uses the recorded
outcomes. Re-running the simulation from the saved inputs alone would require
additional controls for engine randomness, clock inputs and desktop actions.

## Configure with TOML

Start from [Balanced Pangaea](../../examples/pangaea.toml) or
[Two Continents](../../examples/two-continents.toml).

```sh
.venv/bin/civ-agent game start --config examples/pangaea.toml \
  --state-dir ~/.local/state/unciv-host/human-game
.venv/bin/civ-agent game start --config examples/pangaea.toml --dry-run
```

`--dry-run` validates configuration through the packaged native engine without
starting agents. Unknown TOML keys, duplicate player names/civilizations, multiple humans,
invalid limits, and model overrides for non-agent seats are rejected.
Native setup validates civilization, map type/size, difficulty, speed, era and victory
names. Mod content is out of scope. Choose `map_size` or a custom `map_radius`.

| Section | Settings |
| --- | --- |
| `[game]` | `civilizations`, `randomize`, `master_seed`, `map_seed`, `starting_position_seed`, `assignment_seed`, `map_type`, `map_size`/`map_radius`, `difficulty`, `speed`, `starting_era`, `city_states`, `victories` |
| `[agents]` | `provider` (`openai`, `openrouter`, or `google-cloud`), `model`, `reasoning_effort`, `communication_reasoning_effort`, `review_interval`, `review_attempts`, `economy_interval` and `military_interval` (each default 5 own turns, with earlier event-driven reviews), `strategy_guide`, `civilization_guide`, `fast` |
| `[[players]]` | Unique `name`, `controller` (`agent`, `human`, `built_in_ai`; defaults to `agent`); optional agent `provider`/`model`/`reasoning_effort`/`communication_reasoning_effort` |
| `[limits]` | `max_turns`, `max_seconds`, `turn_timeout`, `startup_timeout`, `max_concurrent_jobs` |
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

The benchmark templates also enable optional Decisions effort selection for
OpenAI Main/Economy activations. `reasoning_effort` remains their default and can
still be overridden per player or through `economy_model`/`military_model`.
Configure the selector in `[agents]`:

```toml
reasoning_effort = "medium"
decisions_enabled = true
decisions_effort_enabled = true
decisions_effort_levels = ["low", "medium", "high", "xhigh", "max"]
decisions_effort_min_probability = 0.50
decisions_effort_min_margin = 0.15
```

The service offers only levels advertised by the current model. It applies the
most probable choice when both thresholds pass, otherwise retaining the role's
default. `none` is excluded. Other providers and roles keep their configured
effort. Selection and fallback diagnostics are saved in activation records;
selected effort does not change the next turn's default. Set
`decisions_effort_enabled = false` for fixed effort. See
[reasoning effort](../architecture/Decisions-advisory-layer.md#reasoning-effort)
for input bounds, recovery and validation limits.

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
native civilization/city-state selection, which civilization occupies each start,
start normalization and resource placement. `assignment_seed` independently
controls which player controls each civilization when `randomize = true`.
Changing only `assignment_seed` preserves civilization start locations and map
resources, including when humans, agents and built-in AI exchange civilizations.
Native civilization start biases and controller-dependent difficulty bonuses still
apply; a civilization's starting unit inventory can change when its controller
changes between an external player and built-in AI.
The same map/starting-position seeds, settings, civilization roster and engine
version reproduce the opening map and positions. Each game still has a unique
engine ID; these seeds do not promise identical later combat, AI or model decisions.

**After assigning civilizations**, turns are ordered human first, then agents,
then all built-in AI players. Each group retains civilization-list order; this
does not change civilization placement or which civilization anyone controls.
`game status` shows the resolved players in turn order.

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
providers use Pydantic AI agents, typed game tools, Harness memory and plans, and
programmatic hand-off in the same controller. OpenAI subscription players use the dedicated native OAuth
login; OpenRouter players use an API key.
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
never put it in TOML. The controller mounts this file read-only when a configured
role uses OpenRouter. Model construction selects the assigned provider's
credentials. Renewable subscription credentials stay in the login manager.
An entirely OpenRouter roster requires no OpenAI
login. `auth status --provider openrouter` reports key presence without contacting
the provider. Recreate stopped game containers with `game resume` after rotating a key.

Set `provider = "openrouter"` and the full OpenRouter model ID in any agent's
`[[players]]` table. Provider/model/effort travel with that named player through
random assignment across generated games. Use `--provider` for the default or
`--player-provider "Player name=openrouter"` alongside `--player-model` for a CLI
override. The provider also appears in per-job telemetry, strategy status, and
JSON/CSV batch results. `fast = true` requests OpenAI priority only.

Choose models that support **tool calling and structured outputs**; reviews use
typed output schemas. Set `reasoning_effort = "none"` for models without reasoning;
otherwise choose a supported effort. Native startup validates the selected SDK
profile and settings. Confirm that your account can access the configured model
before starting a long match.

Pydantic AI's `OpenRouterModel` owns the OpenRouter transport. Model IDs and
backend availability can change; OpenRouter may route a model across serving
providers. Recorded `provider = "openrouter"` identifies the gateway. Token counts
come from native SDK usage and do not establish provider billing or subscription
monetary costs.

Existing native games retain their saved images and frozen provider/job settings.
Temporal saved games remain read-only archives in this version. Start fresh games
with the new runtime; there is no converter.

The default limits are a one-hour agent turn timeout, 900-second startup timeout,
seven days total elapsed time, 10,000 model invocations and the native speed's turn limit
plus one (331 for Quick). Pauses count toward total elapsed time. Native victory
ends the run earlier. Human turns have no model timeout, but whole-match limits apply.

Default limits per container: controller 4 GiB/2 CPUs, graphical controller
6 GiB/2 CPUs. Agent tasks share that controller budget. These are ceilings, not reservations. Size batch
concurrency for actual machine capacity and the shared account's usage limits.

## Play a civilization yourself

```sh
.venv/bin/civ-agent game start --config examples/pangaea.toml \
  --state-dir ~/.local/state/unciv-host/human-game
.venv/bin/civ-agent game status --state-dir ~/.local/state/unciv-host/human-game
```

When the game is ready, `game start` and `game resume` print the play URL and
password in the terminal. You can also find the link under `browser.url` in
`game status` and the password in `STATE_DIR/desktop/password.txt`. The browser
streams the native Unciv window through noVNC. Browser
ports bind to **localhost only**. `browser_port = 0` allocates a free port for each
game; `--port` overrides it. Internal game/agent ports are never published.

The human plays first and sees only their civilization's view. While an AI player
takes a turn, the waiting message shows its general progress. Native city, unit,
research and diplomacy controls apply. **Chat** provides public/private messages
and nonbinding promises. **Trade** uses native controls and the shared negotiation
service. Human negotiations have no response deadline. Messages sent during
strategic reviews queue for delivery when reviews finish; trades wait. Unsent
chat drafts and focus survive navigation/background activity within the desktop
session. The operator TUI is separate from the human player's interface.

Closing the browser merely disconnects the viewer. To stop play, use the native
**Stop game** menu or `game stop`. Resume reconnects to the saved native game. Native arbitrary save/load and Undo stay disabled in the
managed match; per-unit automation remains available. Only one human seat per game
is currently supported.

## Stop, remove and resume

```sh
.venv/bin/civ-agent game stop --state-dir ~/.local/state/unciv-host/my-game
.venv/bin/civ-agent game remove --state-dir ~/.local/state/unciv-host/my-game
.venv/bin/civ-agent game resume --state-dir ~/.local/state/unciv-host/my-game
```

`stop` stops admitted work and saves the native position. `remove` additionally
removes game containers, networks and firewall rules. **Both retain the entire
state directory.** `resume` recreates the controller using the saved image IDs.

An interrupted player turn starts a fresh agent conversation at the latest
native position. Committed actions, messages, authored plans, receipts and spent
usage remain recorded. The world is not rewound. Code resolves uncertain native
requests before starting new work; it never resends an unknown old action based
only on a missing acknowledgement. Per-attempt correction allowances are
refreshed; the original game clock and total limits remain in force. Roles and
reports have no model-request cap, including across fresh conversations. Role
passes, handoff attempts, model invocations, observation recoveries, summaries
and offline assessments have no execution-count ceiling. Provider waits keep
retrying with backoff. Consecutive output-validation failures are bounded by the
role request's recorded `attempt_limit` (normally two failed runs, each with its
existing SDK correction allowance). Exhaustion stops the controller with a saved
failure instead of creating more fresh jobs. Successful role completions reset
this allowance when new work wakes the role.
Historical `request_limit`, `max_invocations`, and capacity-retry fields remain
readable as archive metadata and are not enforced by the local runtime.

An explicit resume authorizes one recovery attempt. If the replacement process
fails again, automatic same-container restarts cannot grant another fresh
attempt. Inspect the failure, apply the fix, then run `game resume` again.
Expected provider waits follow their retry policy without requiring this step.

Resume retains model settings and limits. Limits can explicitly increase, for
example `--max-seconds 1209600`. `--image` and `--desktop-image` explicitly upgrade
images; otherwise rebuilding a tag has no effect on saved games. Preserve pinned
images when pruning Podman storage. Stop a game before backing up its full state
directory, login directory and pinned images. No separate database volume is
required. A stopped directory can move; use its new path on resume. If the login
also moves, supply the new `--auth-dir`.

Saves from the retired Temporal runtime remain readable through the operator
commands, but execution is rejected before service startup. `game recover` and
the developer `workflow` commands are removed. There is no save converter or
fallback executor.

Unexpected process exits have bounded systemd restarts of the same container
and original launch. Unfinished work remains stopped until explicit resume.
Lingering covers logout without adding automatic boot-time relaunch. See
[failure recovery](../architecture/Failure-recovery.md) for the boundaries.

## Benchmark batches

For fixed openings screened for comparable starting conditions, use
[Balanced Pangaea](../../examples/pangaea.toml) or
[Two Continents](../../examples/two-continents.toml). Each uses a Medium map at
Quick speed, twelve city-states, one human, one built-in AI, three `gpt-6-luna`
players, and one `gpt-6.1-terra` player. Both pin the map and starting-position
seeds and assign civilizations at random.

Use an ordinary single-game TOML as the template. Batch size and concurrency are
command options, not a second configuration format. Generate twenty reusable
single-game inputs without Podman, model authentication or inference:

```sh
.venv/bin/civ-agent batch generate --config examples/pangaea.toml \
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
`runtime.browser_port = 0`. For unattended benchmarking, copy a preset and change
the human seat to `controller = "agent"` before generating inputs.

```sh
.venv/bin/civ-agent batch status --state-dir ~/.local/state/unciv-host/benchmark
.venv/bin/civ-agent batch stop --state-dir ~/.local/state/unciv-host/benchmark
.venv/bin/civ-agent batch resume --state-dir ~/.local/state/unciv-host/benchmark --retry-failed
```

Both `batch start` and `batch resume` accept `--fast` to request OpenAI priority
or `--no-fast` for the default tier. Omit both to preserve each input's setting
on start, or each game's saved setting on resume. A resume override is saved
for the games selected to resume; failed games require `--retry-failed` and
completed or budget-exhausted games are left unchanged. Other providers keep
their own service tiers.

A failed game is recorded and the next queued game can run. Resume continues
pending/paused runs; `--retry-failed` opts into retrying failures. Completed and
budget-exhausted runs stay finished. Explicit image upgrades affect unfinished
runs. After rebuilding a runtime fix, use `batch resume --retry-failed --image
localhost/civ-agent:runtime --state-dir PATH` to apply it to failed games;
resuming without `--image` retains each game's saved image even when the tag
has been rebuilt. `results.json` and `results.csv` read every game's saved configuration and
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
- `exploration.json`: the latest native game checkpoint.
- `runner/monitoring.sqlite3`: native host result and job accounting for the TUI.
- `native-runtime/controller.json`: controller state, attempts and original game budget.
- `native-runtime/status.json`: current controller and per-role phases.
- `coordinator/<civ>/`: private domain state.
- `player-stores/<civ>/`: native SDK histories, authored memory and role plans.
- `agent-services/<civ>/`: scoped transport credentials.
- `desktop/`: native client/VNC state for human games.
