# unciv-ai

Play Unciv against LLM opponents.

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

Pick [Balanced Pangaea](examples/pangaea.toml) or
[Two Continents](examples/two-continents.toml). You’ll be assigned a civilization
at random and take the first turn.

```sh
map_name=pangaea # or two-continents
game_dir="$HOME/.local/state/unciv-host/$map_name"
.venv/bin/civ-agent game start --config "examples/$map_name.toml" --state-dir "$game_dir"
.venv/bin/civ-agent game status --state-dir "$game_dir"
```

The game starts in the background. When `game status` shows a `browser.url`,
open it and enter the password from `desktop/password.txt` in your game directory.

To start another match, use a different `game_dir`. You can copy either preset,
edit the settings, and pass your file to `--config`.

## Stop and resume

Closing the browser leaves the game running. Run `.venv/bin/civ-agent game stop`
to save and stop the game, then `.venv/bin/civ-agent game resume` when you’re ready
to continue. Pass the original `--state-dir` to both commands.
