# unciv-ai

A private headless host and MCP adapter project for agents playing Unciv, depending on a separately maintained Unciv fork. Agents must eventually have every ordinary human player action and information surface while receiving only information their player is allowed to know. Scope covers the bundled Vanilla and Gods & Kings rulesets; mod content is excluded.

The repository provides a headless executable and authenticated HTTPS MCP server. The player interface supports explored-map observations, current-turn movement and settlement, worker jobs, city yields and citizen assignment, construction queues, research, policies, free Great People, informational alerts including first contact, known-civilization identity, validated war declarations and peace negotiations, and validated turn advancement. Checkpoints persist game state and command receipts together. A supervisor starts a fresh Codex conversation for each game turn through HTTPS agent services, carrying forward concise summaries and following the host's committed turn changes.

**This is an incomplete gameplay implementation.** Further city controls, multi-turn movement orders, special unit actions, naval/air combat, broader diplomacy, full desktop integration, and agent filesystem/network isolation remain outstanding. Consult the [coverage ledger](docs/Player-api-coverage.md) rather than treating a passing turn-flow test as full parity.

The [encounter slice](docs/Encounter-and-diplomacy.md) lets players inspect and
acknowledge first contact, then continue scheduled turns. It adds an initial view
of known rivals and tests encounters and recovery across checkpoint restarts.
Ordinary [informational popups](docs/Informational-popups.md) include their full
prose, quotes and civilization-specific technology descriptions. Agents are
instructed to read and note their content, and acknowledgement requires a current
`player_turn` observation.

The [war and peace slice](docs/War-and-peace.md) adds legal diplomatic options,
war-message acknowledgement, and peace offers with gold and resource terms,
including accept/reject/counteroffer and durable retries. Gameplay launchers and
`civ-agent match start` accept `--model NAME --reasoning-effort LEVEL`.

Read [AGENTS.md](AGENTS.md) for confidentiality and contribution instructions, and [Development workflow](docs/Development-workflow.md) for engine branches, local overrides and dependency updates.

With JDK 21 or newer:

```sh
git submodule update --init --recursive
./gradlew engineInfo build
```

To develop against a sibling engine checkout:

```sh
./gradlew -PuncivDir=../Unciv engineInfo build
```

Install the Python package and development tools with [uv](https://docs.astral.sh/uv/).
Python 3.12 or newer is required:

```sh
uv sync --locked
uv run civ-agent --help
```

To play the Great People exercise, with OpenSSL and a logged-in Codex CLI on `PATH`,
run these in three separate terminals from this directory:

```sh
uv run civ-agent host start --scenario great-people  # Wait for MCP-READY
uv run civ-agent agent start Rome    # Second terminal
uv run civ-agent agent start Greece  # Third terminal
```

New games require an explicit scenario. The host creates credentials and the
prepared Great People setup for each player. Subsequent launches resume the checkpoint;
supplying `--scenario great-people` also verifies that it is the intended exercise.
Use a fresh match directory when switching exercises; see [Great People](docs/Great-people.md)
for the complete automatic-turn launch commands.
Each Codex session gets its civilization's token automatically
and starts with a one-turn request. See [Local launchers](docs/Local-launchers.md)
for custom matches, ports, and conversation resumption.

All Python commands use the `civ-agent` entrypoint: `host start`, `host inspect`,
`agent start`, `agent-server start`, `match start/resume/status/pause/stop/manual`,
and `history export`. Add `--help` after any command for its options. The former
Python scripts have been replaced by this package; existing match directories and
saved credentials retain their layout.

For automatic turns, start one HTTPS service per civilization and run the supervisor.
See [automatic turns and HTTPS setup](docs/Automatic-turns.md) for commands, limits,
pause/resume, and separate-machine configuration.
Player archives use readable paths such as `agents/Rome/turn_1/`: `commands.jsonl`
contains detailed calls and receipts, while `summary.md` contains the API's turn
number and the agent's brief public report, such as `# Turn 0` followed by its
actions and reasons. Earlier completed summaries supply the next turn's context;
retry information stays in the debug records. On resume after an interrupted turn,
the host restores that turn's starting checkpoint and the agent starts a fresh
conversation. Failed attempts stay under `turn_1/attempts/attempt_1/` for debugging
and are excluded from turn memory. A durably committed `END_TURN` is retained even
if its response was lost. The scheduler displays
live player output and saves responses, exposed reasoning summaries and tool history; see [history locations and existing-run export](docs/Automatic-turns.md#live-output-and-saved-history).

To verify the agents' actions visually, run `uv run civ-agent host inspect Rome` or
`uv run civ-agent host inspect Greece`. This exports the checkpoint and opens a separate
inspection copy in the desktop UI. Omit the civilization to view the actual active
player. See [inspection options](docs/Local-launchers.md#inspect-what-the-agents-did).

To generate and load a lifecycle scenario:

```sh
./gradlew generateS01Scenarios
./gradlew run --args="--save $PWD/build/generated/s01-scenarios/vanilla.json --data-dir /tmp/unciv-s01"
```

- [Player protocol](docs/Player-api-protocol.md): current tools, request examples, checkpoints, and limitations.
- [Economy and unit actions](docs/Economy-and-unit-actions.md): finances/resources, gold purchases, upgrades, pillage/disband and a prepared exercise.
- [City management](docs/City-management.md): focus/growth, specialists and great-person progress, building sales, shared tiles and a two-city exercise for each civilization.
- [Great People](docs/Great-people.md): usable abilities, on-demand research/policy details, and an exercise for both players to generate and use people.
- [Local launchers](docs/Local-launchers.md): start the host and interactive Codex players without copying tokens.
- [Exploration and settlement](docs/Exploration-and-settlement.md): launch a fresh duel using existing player credentials, movement semantics, and validation.
- [Implementation design](docs/Player-api-plan.md): responsibilities, milestones, and acceptance gates.
- [Coverage ledger](docs/Player-api-coverage.md): implemented and outstanding action and information surfaces.

Engine changes under development live in the selected checkout. A clean checkout at the recorded submodule revision does not include uncommitted engine changes.

The [basic combat slice](docs/Combat.md) adds land/city attacks, tactical orders,
promotions, capture choices and durable combat reports with terminal result delivery.
