# unciv-ai

A private headless host and MCP adapter project for agents playing Unciv, depending on a separately maintained Unciv fork. Agents must eventually have every ordinary human player action and information surface while receiving only information their player is allowed to know. Scope covers the bundled Vanilla and Gods & Kings rulesets; mod content is excluded.

The repository provides a headless executable that loads a saved game and shuts down without advancing turns, plus a Kotlin prototype for player-bound summaries, owned-city observations, and validated city renaming with retry receipts. Integration scenarios cover two Human players in both bundled rulesets. Complete action/information coverage and MCP transport remain later work.

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

- [Headless runtime](docs/Headless-runtime.md): launch commands, scenario generation, settings and runtime ownership.
- [Player-summary prototype](docs/Player-summary-prototype.md): Kotlin session binding, the initial observation fields, and isolation checks.
- [City-rename prototype](docs/City-rename-prototype.md): owned-city handles, command validation, serialization, retry receipts, and failure handling.
- [Free great-person selection](docs/Free-great-person-selection.md): shared engine operation and before/after UI saves and launch commands.
- [Session handoff](docs/Session-handoff.md): verified setup state, retained decisions, and the next implementation step.
- [Headless implementation path](docs/Headless-player-agent-path.md): the recommended approach, repository responsibilities, and initial atomic steps.
- [Player API review](docs/Player-agent-API-review.md): the action and information inventory and privacy requirements.
- [Original implementation plan](docs/Player-agent-implementation-plan.md): the broader View-based backlog and linked PR boundaries. The headless path identifies which dependencies can be avoided.

Source links in the plans point into the pinned `vendor/Unciv` submodule. Each document records the revision it reviewed; historical source line anchors can differ from a later dependency revision.
