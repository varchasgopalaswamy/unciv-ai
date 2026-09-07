# unciv-ai

A private headless host and MCP adapter project for agents playing Unciv, depending on a separately maintained Unciv fork. Agents must eventually have every ordinary human player action and information surface while receiving only information their player is allowed to know. Scope covers the bundled Vanilla and Gods & Kings rulesets; mod content is excluded.

The repository provides a headless executable and authenticated local MCP server. The player interface supports turn decisions, research queues and free technologies, policy adoption, city production, renaming, free Great People, informational alerts, and validated turn advancement. Optional checkpoints persist game state and command receipts together. Integration scenarios cover both bundled rulesets.

**This is an incomplete gameplay implementation.** Map and unit play, combat, diplomacy, full desktop integration, and the Codex runner remain outstanding. Consult the [coverage ledger](docs/Player-api-coverage.md) rather than treating a passing turn-flow test as full parity.

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

To generate and load a lifecycle scenario:

```sh
./gradlew generateS01Scenarios
./gradlew run --args="--save $PWD/build/generated/s01-scenarios/vanilla.json --data-dir /tmp/unciv-s01"
```

- [Player protocol](docs/Player-api-protocol.md): current tools, request examples, checkpoints, and limitations.
- [Implementation design](docs/Player-api-plan.md): responsibilities, milestones, and acceptance gates.
- [Coverage ledger](docs/Player-api-coverage.md): implemented and outstanding action and information surfaces.

Engine changes under development live in the selected checkout. A clean checkout at the recorded submodule revision does not include uncommitted engine changes.
