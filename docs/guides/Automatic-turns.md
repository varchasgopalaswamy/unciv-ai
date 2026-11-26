# Automatic turns

Each game runs a controller and a native Unciv host in a rootless Podman
container. The controller schedules agents when the native game hands them a
turn. The host checks gameplay legality and commits actions and receipts.

Follow the [full-game guide](Full-vanilla-run.md) for installation, model access,
image builds, and TOML examples.

```sh
uv run civ-agent game start --config /path/to/game.toml --state-dir /path/to/game
uv run civ-agent game status --state-dir /path/to/game
uv run civ-agent match watch --state-dir /path/to/game
```

Main, Economy, and Military share the active turn. Strategic reviews and
communication have their own schedules. Each agent has private memory, role
plans, and SDK history. The native host waits when a human is active.

## Pause, inspect and resume

```sh
uv run civ-agent game stop --state-dir /path/to/game
uv run civ-agent match failures --state-dir /path/to/game
uv run civ-agent game resume --state-dir /path/to/game
```

Stop drains active work and saves the match. Resume uses the saved checkpoint,
images, configuration, and budgets. Keep the whole game directory when making
a backup. Use [history export](Local-launchers.md#export-native-histories) to
inspect one player's retained history.
