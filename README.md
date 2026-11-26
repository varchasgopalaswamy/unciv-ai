# unciv-ai

A private game host and player API for agents playing Unciv. The host uses a
separately maintained Unciv fork and gives each player authenticated tools,
private memory and only the game information that player may see. Native Unciv
rules govern actions; social promises remain nonbinding player communications.

The project targets ordinary human-player actions in the bundled Vanilla and
Gods & Kings rulesets. The [coverage ledger](docs/reference/Player-api-coverage.md)
lists supported actions, ruleset limits, and remaining gaps.

## Run a game

Use Linux with rootless Podman/Netavark, nftables, cgroup v2 and systemd.
Image builds need JDK 21+, Python 3.12+, [uv](https://docs.astral.sh/uv/),
and the selected Unciv engine checkout.
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
reasoning, built-in Egypt, and two city-states. One controller per game runs
Pydantic AI agents and the native JVM host. Shared brokers manage login renewal
and optional Decisions requests. Logs, native checkpoints and token/call/timing
records remain in the game directory.

Start a fresh game with this runtime. Saves from the retired Temporal runtime
remain readable archives; this runtime does not execute or convert them.

To join as the local human player, stream Unciv's native desktop controls and
integrated chat/trade panel to your browser:

```sh
.venv/bin/civ-agent game start --state-dir ~/.local/state/unciv-host/human-game \
  --config examples/human-game.toml
```

The human plays first, has no model turn timeout, and uses the same authoritative
game as the agents. `game status --state-dir PATH` gives the local browser URL;
the password is in `PATH/desktop/password.txt`. Closing the browser leaves play
running. Use `game stop` to pause and `game resume` to restart.

TOML examples: [human game](examples/human-game.toml) and
[benchmark game template](examples/benchmark.toml), plus a
[mixed OpenAI/OpenRouter roster](examples/mixed-providers.toml). Pass `--config FILE` to
`game start`, or generate reusable benchmark inputs with
`batch generate --config FILE --games 20 --output INPUTS --batch-seed 42`
and launch them with `batch start --inputs INPUTS --state-dir PATH --parallel 2`.

See the [full-game guide](docs/guides/Full-vanilla-run.md) for directories,
timeouts, stopping and resuming. Use the [local launcher guide](docs/guides/Local-launchers.md)
for interactive play, human communication and desktop inspection.

To watch the default full-game run from another terminal:

```sh
.venv/bin/civ-agent match watch --state-dir ~/.local/state/unciv-host/vanilla-full
```

For local agent/model/tool traces, use [Phoenix](http://127.0.0.1:6006).
See [monitoring](docs/architecture/Monitoring.md) for the persistent local service,
per-game tracing configuration and content-free export policy.

## Documentation

- [Documentation index](docs/README.md): operating guides, architecture, player
  references and reproducible scenarios.
- [Development workflow](docs/Development-workflow.md): source checks, the pinned
  engine dependency and local checkout overrides.

Read [AGENTS.md](AGENTS.md) for confidentiality and contribution instructions.
Engine changes belong in the separately maintained engine checkout; a host commit
records its pinned revision and does not commit engine edits.
