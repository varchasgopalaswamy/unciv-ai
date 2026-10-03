# unciv-ai

A private game host and player API for agents playing Unciv. The host uses a
separately maintained Unciv fork and gives each player authenticated tools,
private memory and only the game information that player may see. Native Unciv
rules govern actions; social promises remain nonbinding player communications.

The project targets ordinary human-player actions in the bundled Vanilla and
Gods & Kings rulesets. Mods are out of scope. Full gameplay coverage and completed
full-game acceptance remain open; see the [coverage ledger](docs/reference/Player-api-coverage.md)
and [current roadmap](docs/Roadmap.md).

## Run a game

Use Linux with rootless Podman/Netavark, nftables, cgroup v2 and systemd.
Image builds need JDK 21+, Python 3.12+, [uv](https://docs.astral.sh/uv/),
and the standalone Codex release.
From the repository directory:

```sh
git submodule update --init --recursive
uv sync --locked
.venv/bin/civ-agent image build
.venv/bin/civ-agent runtime setup
.venv/bin/civ-agent auth login
.venv/bin/civ-agent game start
```

The launcher starts Rome, Greece and France with `gpt-6-luna` at medium
reasoning, built-in Egypt, and two city-states. It manages the host, agent
services and scheduler in isolated containers, and records logs,
checkpoints and token/call/timing telemetry.

To join as the local human player, stream Unciv's native desktop controls and
integrated chat/trade panel to your browser:

```sh
.venv/bin/civ-agent game start --state-dir ~/.local/state/unciv-host/human-game \
  --human France --agents Rome Greece --built-in-ai Egypt
```

The human plays first, has no model turn timeout, and uses the same authoritative
game as the agents. `game status --state-dir PATH` gives the local browser URL;
the password is in `PATH/desktop/password.txt`. Closing the browser leaves play
running. Use `game stop` to pause and `game resume` to restart.

TOML examples: [human game](examples/human-game.toml) and
[model benchmark matrix](examples/benchmark.toml). Pass `--config FILE` to
`game start` or `batch start`.

See the [full-game guide](docs/guides/Full-vanilla-run.md) for directories,
timeouts, stopping and resuming. Use the [local launcher guide](docs/guides/Local-launchers.md)
for interactive play, human communication and desktop inspection, or
[automatic turns](docs/guides/Automatic-turns.md) for individually managed services.

To watch the default full-game run from another terminal:

```sh
.venv/bin/civ-agent match watch --state-dir ~/.local/state/unciv-host/vanilla-full
```

## Documentation

- [Documentation index](docs/README.md): operating guides, architecture, player
  references and reproducible scenarios.
- [Current roadmap](docs/Roadmap.md): unfinished acceptance and observed follow-ups.
- [Development workflow](docs/Development-workflow.md): source checks, the pinned
  engine dependency and local checkout overrides.
- [Historical archive](docs/archive/README.md): completed plans, sprint records,
  benchmarks and live-game reviews.

Read [AGENTS.md](AGENTS.md) for confidentiality and contribution instructions.
Engine changes belong in the separately maintained engine checkout; a host commit
records its pinned revision and does not commit engine edits.
