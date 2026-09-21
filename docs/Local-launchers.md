# Local launchers

The `civ-agent` package launches the checkpointed host and interactive Codex players. It needs
uv, Python 3.12 or newer, OpenSSL, JDK 21 or newer, the initialized engine checkout, and a Codex CLI on `PATH`
that you have already logged into. The host builds through the repository's Gradle
wrapper, so the first launch may need to download build dependencies.

Interactive launch now requires the civilization's HTTPS service. Start
`civ-agent agent-server start Rome`, `civ-agent agent-server start Greece`, and
`civ-agent agent-server start France`
in additional terminals before the interactive commands below. See
[chat, memory and recovery](Chat-memory-and-recovery.md) for automatic play.

## Start playing

Install once, then start the host from the repository directory in the first terminal:

```sh
uv sync --locked
uv run civ-agent host start
```

Wait for `MCP-READY:https://localhost:8765/mcp`. In a second terminal, from the
repository directory:

```sh
uv run civ-agent agent start Rome
```

In a third terminal:

```sh
uv run civ-agent agent start Greece
```

In a fourth terminal:

```sh
uv run civ-agent agent start France
```

The default match directory is `~/.local/state/unciv-host/demo`, or the value of
`UNCIV_HOST_DIR` if set. On first launch, the host creates owner-only `players.json`
credentials and starts a Quick Vanilla game between Rome, Greece, and France.
Each player aims to win; see the [ordinary-game setup](Vanilla-game.md).
Add `--city-states 2` when creating a new ordinary game to include two native
city-states (default 0). Omit this option on resume.
`--scenario trade-cycle` instead selects the prepared Rome/Greece trade exercise.
`--scenario war-paid` and `--scenario war-joint` select the three-player
[guided war exercises](War-trades-live-scenario.md), with private negotiating roles.
`--scenario agreement-lifecycle` selects the three-player
[agreement expiry and resource-interruption exercise](Agreement-lifecycle-live-scenario.md).
Later launches resume `exploration.json` and retain the same credentials; an
explicit scenario option also checks that the saved game belongs to that exercise.
Missing credentials for an existing checkpoint must be restored or supplied with
`--players`; the command will not silently replace them.

Rome acts first in a fresh game. Each session's starting prompt asks it to inspect
the available tools and current turn, play one supported turn, end it once, and
report back. A player waiting for the other civilization reports that and stops.
After the other player finishes, ask **“Play my next turn.”** A blocked or unsupported
decision is reported for your attention. These sessions do not continuously poll
or coordinate turns automatically.

For repeated turns, use the [HTTPS agent services and automatic scheduler](Automatic-turns.md).
The interactive commands on this page retain their one-turn behavior.
The [full Vanilla launcher](Full-vanilla-run.md) starts all local services and
runs a full match with generous timeouts and saved diagnostics in one command.

Each player has its own working directory under `STATE_DIR/agents`, such as
`agents/Rome` or `agents/Greece`. Unusual characters in civilization names are encoded
so distinct names cannot collide or escape the workspace directory. The launcher
uses the selected host token in trusted code to open a scoped job at the player's
HTTPS service. Codex receives only that job's gateway token through
`UNCIV_PLAYER_TOKEN`. Tokens are never printed or placed in command arguments,
and the launcher does not edit global Codex configuration. The service supplies
request IDs, revisions and retry bookkeeping for the model's proposals.

These are trusted local sessions: they inherit your normal Codex login, settings,
with general shell and browsing tools disabled. Separate working directories and prompt instructions
do not prevent filesystem access to operator state or another player's files.
The isolated runner remains a separate project milestone.

Use Ctrl-C in the host terminal to stop the server. The checkpoint retains accepted
commands. Exit each Codex session independently. Restarting `civ-agent host start` resumes
the game; `civ-agent agent start` starts a new conversation unless given `--resume`.

To use `civ-agent` without the `uv run` prefix, activate the project environment:

```sh
source .venv/bin/activate
civ-agent agent start Rome
```

For an independent installation, run `uv tool install .` from the repository.
Host startup and desktop inspection still need the JVM checkout. Select it with
`--repo-dir /absolute/path/to/checkout` or `CIV_AGENT_REPO`; when neither is given,
the command searches the current directory's ancestors and the editable package's
source tree. `--unciv-dir` continues to select the engine relative to that host
checkout. Agent commands, match control, history export, and
`host inspect --export-only` do not require a checkout on the agent machine.

## Choose a match or resume a conversation

The default is an [ordinary three-player Vanilla game](Vanilla-game.md).
Prepared exercises include [the complete trade cycle](Trade-cycle-scenario.md),
[three-cornered war](War-trades-live-scenario.md), and
[agreement lifecycle](Agreement-lifecycle-live-scenario.md), selected with
`--scenario`. The host
prints the loaded scenario before `MCP-READY`, and `player_turn.scenario` supplies
its objectives to the agents. The default prompt follows those objectives without
instructions from earlier exercises. See [automatic turns](Automatic-turns.md)
for the complete setup.

Before ending any turn, the default prompt asks the player to review confirmed
actions and remaining unit movement. The command must identify the current review
and explain any choice to leave units idle with legal moves remaining.

For a separate fresh match, choose a new directory. Pass the same directory and
port to both players:

```sh
# First terminal
uv run civ-agent host start --state-dir /tmp/my-civ-match --new --port 8766
# Second terminal, once MCP-READY appears
uv run civ-agent agent start Rome --state-dir /tmp/my-civ-match --port 8766
# Third terminal
uv run civ-agent agent start Greece --state-dir /tmp/my-civ-match --port 8766
# Fourth terminal
uv run civ-agent agent start France --state-dir /tmp/my-civ-match --port 8766
```

`--new` refuses an existing checkpoint. Omit it to resume the match; `--seed` applies
only to new games. Directory names do not select or change the scenario.
Use a persistent directory instead
of `/tmp` for a match you want to keep.

The host generates and reuses a private local CA and server certificate under
`STATE_DIR/tls`. Its operator credential is separate from player credentials. For
remote endpoints, pass `--url https://HOST:PORT --ca-file /absolute/ca.pem` to the
agent. Supply a matching hostname/certificate to the host; see
[custom HTTPS deployment](Automatic-turns.md#separate-machines).

Both scripts accept `--players /absolute/path/players.json`. The host also accepts
`--checkpoint /absolute/path/checkpoint.json` and
`--unciv-dir ../Unciv`; that engine override selects both code and runtime assets,
as described in the [development workflow](Development-workflow.md).

The agent launcher accepts any unambiguous civilization name in the credentials
file, case-insensitively. It also supports:

```sh
uv run civ-agent agent start Rome --dry-run
uv run civ-agent agent start Rome --model YOUR_MODEL --reasoning-effort high
uv run civ-agent agent start Rome --prompt "Inspect my position and suggest a plan before acting."
uv run civ-agent agent start Rome --resume YOUR_SESSION_ID
```

`--reasoning` is an alias for `--reasoning-effort`. Omitted settings use Codex's
configuration; an effort must be supported by the selected model. These overrides
apply only to the launched session and do not rewrite the global Codex config.
For automatic gameplay, set per-player defaults on `civ-agent agent-server start`, or
override all players with `civ-agent match start --model YOUR_MODEL --reasoning-effort high`;
see [automatic turns](Automatic-turns.md#select-models-and-reasoning-effort).

Automatic gameplay also writes concise turn summaries and detailed receipts.
Fresh jobs receive selected structured memory. On interruption, confirmed work
is preserved and uncertain responses are reconciled; a new conversation observes
the current game. Whole-match rollback is an explicit operator command.
See [turn folders and memory](Automatic-turns.md#turn-folders-and-memory).

Use a conversation ID belonging to that civilization and match when resuming.
The launcher requires an explicit ID to avoid accidentally selecting the other
player's most recent conversation. `--dry-run` prints the command with the token
omitted, without contacting the host or starting Codex. Normal launches check
the certificate and hostname, and configure `civ` as a required MCP server so Codex
reports initialization failures. The saved endpoint includes the host port, so it normally needs no override.

## Inspect what the agents did

From the repository directory, open a snapshot in the desktop UI:

```sh
uv run civ-agent host inspect        # Open as the saved active civilization
uv run civ-agent host inspect Rome   # Open Rome's perspective
uv run civ-agent host inspect Greece # Open Greece's perspective
```

Run one command at a time. The command exports the checkpoint, builds the selected
desktop engine, and loads the chosen save automatically. Click through the
civilization's ready screen to inspect its units, cities, production and research.
For a different match or engine checkout, use the same selection as the host:

```sh
uv run civ-agent host inspect Rome --state-dir /tmp/my-civ-match
uv run civ-agent host inspect --checkpoint /absolute/path/checkpoint.json --unciv-dir ../Unciv
```

The default state directory and `UNCIV_HOST_DIR` behavior match `civ-agent host start`.
Every invocation creates a fresh owner-only directory under `STATE_DIR/inspection`,
containing `Agent snapshot.json` and an inspection save for each Human civilization
in its `SaveFiles` folder. The command prints the directory and actual active
civilization. You can open the other exported perspectives through **Load game**.
Add `--export-only` to export without launching a window, or `--output-dir PATH`
to choose the parent directory for these exports.

The snapshot preserves the captured gameplay state. Per-civilization copies override
`currentPlayer` to select that view; use `Agent snapshot.json` to verify whose turn
it actually was. All exports remove any old custom save destination. Desktop saves
and gameplay affect only the inspection copy, and each run retains earlier exports.
Generated desktop assets stay in this repository's build directory, using the same
engine checkout as the desktop code.

The host may remain running: checkpoint replacement is atomic, so an export captures
one committed position. To compare against a particular agent report, let it finish
and stop the host with Ctrl-C before exporting. The desktop is a snapshot, with no
live updates or connection back to the host. Rerun `civ-agent host inspect` for a newer snapshot.
These full-state exports are for you as the operator, not for player agents.
Online multiplayer checkpoints are rejected because a desktop multiplayer session
can write back to its server; this viewer supports local matches.

## Verify the package

```sh
uv run pytest -v
```

The launcher tests use temporary files and mocked process/network calls. They
cover credential generation and preservation, selected-player token routing,
separate directories, startup arguments, resumption, and failure handling. Inspection
tests also verify that exports preserve gameplay data, omit host receipts, clear old
save destinations, retain earlier copies, and leave the checkpoint untouched. They
do not invoke a model or establish complete gameplay coverage or isolation.

See [the trade cycle](Trade-cycle-scenario.md) for the current exercise's
initial state, objectives, and verification commands.
