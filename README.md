# unciv-ai

A private headless host and MCP adapter project for agents playing Unciv, depending on a separately maintained Unciv fork. Agents must eventually have every ordinary human player action and information surface while receiving only information their player is allowed to know. Scope covers the bundled Vanilla and Gods & Kings rulesets; mod content is excluded.

The repository currently contains the development build, a headless engine dependency smoke test, and planning documents. Game hosting and the player/MCP APIs are not implemented yet.

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

- [Headless implementation path](docs/Headless-player-agent-path.md): the recommended approach, repository responsibilities, and initial atomic steps.
- [Player API review](docs/Player-agent-API-review.md): the action and information inventory and privacy requirements.
- [Original implementation plan](docs/Player-agent-implementation-plan.md): the broader View-based backlog and linked PR boundaries. The headless path identifies which dependencies can be avoided.

Source links in the plans point into the pinned `vendor/Unciv` submodule. Each document records the revision it reviewed; historical source line anchors can differ from a later dependency revision.
