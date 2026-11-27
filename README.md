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
```

The first time you start a game in a terminal, Civ-agent prints a sign-in URL.
Open it and sign in with the OpenAI subscription the players will use. The launch
then continues. For unattended launches, run `.venv/bin/civ-agent auth login`
once before starting the game. For other model providers, see the
[full-game guide](docs/guides/Full-vanilla-run.md).

## Start playing

Pick [Balanced Pangaea](examples/pangaea.toml) or
[Two Continents](examples/two-continents.toml). You’ll be assigned a civilization
at random and take the first turn.

```sh
map_name=pangaea # or two-continents
game_dir="$HOME/.local/state/unciv-host/$map_name"
.venv/bin/civ-agent game start --config "examples/$map_name.toml" --state-dir "$game_dir"
```

The game runs in the background. When it is ready, open the play URL printed in
the terminal and enter the password shown below it. You can look up the URL
under `browser.url` in `.venv/bin/civ-agent game status --state-dir "$game_dir"`
and the password in `desktop/password.txt` in your game directory.

To start another match, use a different `game_dir`. The sibling `auth` directory
holds the shared login for all matches. You can copy either preset,
edit the settings, and pass your file to `--config`.

## Stop and resume

Closing the browser leaves the game running. Run `.venv/bin/civ-agent game stop`
to save and stop the game, then `.venv/bin/civ-agent game resume` when you’re ready
to continue. Pass the original `--state-dir` to both commands.
