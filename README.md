# unciv-ai

Play Unciv against LLM opponents.

## First-time setup

You’ll need Linux with rootless Podman, Netavark, nftables, cgroup v2, and a
systemd user session. Install JDK 21+, Python 3.12+, and `uv`.

Run the setup commands from the repository root. Setup can take a while!

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

## Set up Decisions

We use a decision model to figure out what effort setting needs to be used for a 
given turn. If this is disabled, the default will always be used.

Both map presets use Jev by default. Set up Jev or Luna before starting a game.

### Jev with a local server

We’ve tested Jev with Winnow-12B. Any server that implements the same System One
API can be used if it accepts `Winnow-12B` as the model name and serves
`/v1/systemone` without an API key.

Install Winnow’s Linux/NVIDIA prerequisites, then run its setup and server in a
separate terminal. [Winnow installation guide](https://github.com/EldanRing/winnow-inference/blob/main/docs/INSTALL.md)

```sh
git clone https://github.com/EldanRing/winnow-inference.git
cd winnow-inference
python3 scripts/winnow.py setup --model q8
python3 scripts/winnow.py serve --model q8
```

Leave the server running while you play. The game expects it at
`http://127.0.0.1:8091/v1/systemone` on the host. For a different address, set
`CIV_JEV_ENDPOINT` in the systemd user environment to the full URL as seen from
the broker container.

### Luna with the OpenAI API

Luna uses an OpenAI Platform API key and separate API billing. Save the key on
one line in a private file, then install it with mode `600`. [OpenAI Decisions guide](https://developers.openai.com/api/docs/guides/decisions)

```sh
auth_dir="$HOME/.local/state/unciv-host/auth"
mkdir -p "$auth_dir"
install -m 600 /path/to/openai-api-key.txt "$auth_dir/decisions.key"
```

Add `--decisions-backend luna` to `game start` below. If you use a custom
`--auth-dir`, put `decisions.key` there.

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


To start another match, use a different `game_dir`. `game status` also shows the Decisions broker’s health. The upstream status
stays `unchecked` until the broker makes its first classification request.

To start another match, use a different `game_dir`. You can copy either preset,
edit the settings, and pass your file to `--config`.

## Stop and resume

Closing the browser leaves the game running. Run `.venv/bin/civ-agent game stop`
to save and stop the game, then `.venv/bin/civ-agent game resume` when you’re ready
to continue. Pass the original `--state-dir` to both commands.

## Unciv credits

Unciv was created by Yair Morgenstern (`yairm210`) and is developed by the Unciv
community. [Original Unciv project](https://github.com/yairm210/Unciv)

This project runs a modified Unciv engine. Unciv’s source code is licensed under
the Mozilla Public License 2.0 except where otherwise noted. Unciv’s art, icons,
and other media have their own credits and license terms.
[Engine fork](https://github.com/varchasgopalaswamy/unciv-ai-fork) ·
[Unciv license](https://github.com/yairm210/Unciv/blob/master/LICENSE) ·
[Media credits and licenses](https://yairm210.github.io/Unciv/Credits/)

The host code is licensed under GPLv3. [Host license](LICENSE)
