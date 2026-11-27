# unciv-ai

Play Unciv in your browser against AI opponents. In the example below, you play
France and take the first turn.

## First-time setup

You’ll need Linux with rootless Podman, Netavark, nftables, cgroup v2, and a
systemd user session. Install JDK 21+, Python 3.12+, and `uv`.

Run the setup commands from the repository root.

```sh
git submodule update --init --recursive
uv sync --locked
.venv/bin/civ-agent image build
.venv/bin/civ-agent runtime setup
.venv/bin/civ-agent auth login
```

The last command prints a URL. Open it and sign in with the OpenAI subscription
the AI players will use. For setup help or other model providers, see the
[full-game guide](docs/guides/Full-vanilla-run.md).

## Start playing

```sh
.venv/bin/civ-agent game start --config examples/human-game.toml \
  --state-dir ~/.local/state/unciv-host/human-game
.venv/bin/civ-agent game status --state-dir ~/.local/state/unciv-host/human-game
```

The game starts in the background. When `game status` shows a `browser.url`,
open it and enter the password from
`~/.local/state/unciv-host/human-game/desktop/password.txt`.

Use a fresh `--state-dir` for each new game. You can copy the example to change
the map, difficulty, or opponents, then pass your file to `--config`.
[Human game configuration](examples/human-game.toml)

## Stop and resume

Closing the browser leaves the game running. Run `.venv/bin/civ-agent game stop`
to save and stop the game, then `.venv/bin/civ-agent game resume` when you’re ready
to continue. Pass the original `--state-dir` to both commands.

[More game guides](docs/README.md)
