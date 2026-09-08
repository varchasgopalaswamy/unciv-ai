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
`run_agents` accept `--model NAME --reasoning-effort LEVEL`.

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

To play a local Rome/Greece duel, with Python 3, OpenSSL and a logged-in Codex CLI on `PATH`,
run these in three separate terminals from this directory:

```sh
./scripts/launch_host          # Wait for MCP-READY
./scripts/launch_agent Rome    # Second terminal
./scripts/launch_agent Greece  # Third terminal
```

The host creates credentials and a game on first launch, and resumes its checkpoint
on subsequent launches. Each Codex session gets its civilization's token automatically
and starts with a one-turn request. See [Local launchers](docs/Local-launchers.md)
for custom matches, ports, and conversation resumption.

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

To verify the agents' actions visually, run `./scripts/inspect_game Rome` or
`./scripts/inspect_game Greece`. This exports the checkpoint and opens a separate
inspection copy in the desktop UI. Omit the civilization to view the actual active
player. See [inspection options](docs/Local-launchers.md#inspect-what-the-agents-did).

To generate and load a lifecycle scenario:

```sh
./gradlew generateS01Scenarios
./gradlew run --args="--save $PWD/build/generated/s01-scenarios/vanilla.json --data-dir /tmp/unciv-s01"
```

- [Player protocol](docs/Player-api-protocol.md): current tools, request examples, checkpoints, and limitations.
- [Local launchers](docs/Local-launchers.md): start the host and interactive Codex players without copying tokens.
- [Exploration and settlement](docs/Exploration-and-settlement.md): launch a fresh duel using existing player credentials, movement semantics, and validation.
- [Implementation design](docs/Player-api-plan.md): responsibilities, milestones, and acceptance gates.
- [Coverage ledger](docs/Player-api-coverage.md): implemented and outstanding action and information surfaces.

Engine changes under development live in the selected checkout. A clean checkout at the recorded submodule revision does not include uncommitted engine changes.

The [basic combat slice](docs/Combat.md) adds land/city attacks, tactical orders,
promotions, capture choices and durable combat reports with terminal result delivery.
