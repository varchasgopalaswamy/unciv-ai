# unciv-ai

A private headless host and MCP adapter project for agents playing Unciv, depending on a separately maintained Unciv fork. Agents must eventually have every ordinary human player action and information surface while receiving only information their player is allowed to know. Scope covers the bundled Vanilla and Gods & Kings rulesets; mod content is excluded.

The repository provides a headless executable and authenticated local MCP server. The player interface supports explored-map observations, current-turn unit movement and settlement, turn decisions, research queues and free technologies, policy adoption, city production, renaming, free Great People, informational alerts, and validated turn advancement. Checkpoints persist game state and command receipts together. Integration scenarios cover both bundled rulesets, with a fresh Vanilla duel exercised through MCP.

**This is an incomplete gameplay implementation.** Worker jobs, city management, multi-turn movement orders, special unit actions, combat, diplomacy, full desktop integration, and an isolated autonomous runner remain outstanding. Local scripts can launch interactive Codex players. Consult the [coverage ledger](docs/Player-api-coverage.md) rather than treating a passing turn-flow test as full parity.

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

To play a local Rome/Greece duel, with Python 3 and a logged-in Codex CLI on `PATH`,
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
