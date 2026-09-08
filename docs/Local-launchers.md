# Local launchers

These scripts launch the checkpointed host and interactive Codex players. They need
Python 3, OpenSSL, JDK 21 or newer, the initialized engine checkout, and a Codex CLI on `PATH`
that you have already logged into. The host builds through the repository's Gradle
wrapper, so the first launch may need to download build dependencies.

## Start playing

Run this from the repository directory in the first terminal:

```sh
./scripts/launch_host
```

Wait for `MCP-READY:https://localhost:8765/mcp`. In a second terminal, from the
repository directory:

```sh
./scripts/launch_agent Rome
```

In a third terminal:

```sh
./scripts/launch_agent Greece
```

The default match directory is `~/.local/state/unciv-host/demo`, or the value of
`UNCIV_HOST_DIR` if set. On first launch, the host creates owner-only `players.json`
credentials and starts a new Rome/Greece duel. Later launches resume
`exploration.json` and retain the same credentials. This also resumes the checkpoint
from the earlier manual exploration setup when using its original directory.
Missing credentials for an existing checkpoint must be restored or supplied with
`--players`; the script will not silently replace them.

Rome acts first in a fresh game. Each session's starting prompt asks it to inspect
the available tools and current turn, play one supported turn, end it once, and
report back. A player waiting for the other civilization reports that and stops.
After the other player finishes, ask **“Play my next turn.”** A blocked or unsupported
decision is reported for your attention. These sessions do not continuously poll
or coordinate turns automatically.

For repeated turns, use the [HTTPS agent services and automatic scheduler](Automatic-turns.md).
The interactive commands on this page retain their one-turn behavior.

Each player has its own working directory under `STATE_DIR/agents`, such as
`agents/Rome` or `agents/Greece`. Unusual characters in civilization names are encoded
so distinct names cannot collide or escape the workspace directory. The launcher
reads the selected token and passes it through
`UNCIV_PLAYER_TOKEN`, with a per-session `civ` MCP configuration. Tokens are never
printed or placed in command arguments, and your global Codex configuration is not
edited. The launcher loads the host URL and CA certificate from `endpoints.json`,
verifies the HTTPS connection, and configures Codex to trust that CA. Plain HTTP
URLs are rejected. This uses Codex's documented
[MCP bearer-token environment setting](https://learn.chatgpt.com/docs/extend/mcp?surface=cli).

These are trusted local sessions: they inherit your normal Codex login, settings,
and other configured tools. Separate working directories and prompt instructions
do not prevent filesystem access to operator state or another player's files.
The isolated runner remains a separate project milestone.

Use Ctrl-C in the host terminal to stop the server. The checkpoint retains accepted
commands. Exit each Codex session independently. Restarting `launch_host` resumes
the game; `launch_agent` starts a new conversation unless given `--resume`.

To use the shorter command `launch_agent Rome`, add the scripts directory to the
current shell's path while in the repository:

```sh
export PATH="$PWD/scripts:$PATH"
launch_agent Rome
```

## Choose a match or resume a conversation

For diplomacy testing, add `--close-starts` when creating a new match. Rome and
Greece start five tiles apart, with an extra Scout each. The Scouts start adjacent
so first contact happens immediately through normal visibility. Omit this flag
when resuming an existing match. The default player prompt recognizes the scenario:
Rome declares war once and proposes peace with only a Peace Treaty on each side
when negotiation becomes legal; Greece reads and accepts that offer. Both keep
playing while they wait. Completion persists across restarts and treaty expiry.
Ordinary duels keep their usual diplomacy choices. See
[automatic turns](Automatic-turns.md) for the complete setup.

Before ending any turn, the default prompt asks the player to review confirmed
actions and remaining unit movement. The command must identify the current review
and explain any choice to leave units idle with legal moves remaining.

For a separate fresh match, choose a new directory. Pass the same directory and
port to both players:

```sh
# First terminal
./scripts/launch_host --state-dir /tmp/my-civ-match --new --seed 42 --port 8766
# Second terminal, once MCP-READY appears
./scripts/launch_agent Rome --state-dir /tmp/my-civ-match --port 8766
# Third terminal
./scripts/launch_agent Greece --state-dir /tmp/my-civ-match --port 8766
```

`--new` refuses an existing checkpoint. Omit it to resume the match; `--seed` applies
only to new games. See [Exploration and settlement](Exploration-and-settlement.md)
for the duel's rules and map-seed limitations. Use a persistent directory instead
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
./scripts/launch_agent Rome --dry-run
./scripts/launch_agent Rome --model YOUR_MODEL --reasoning-effort high
./scripts/launch_agent Rome --prompt "Inspect my position and suggest a plan before acting."
./scripts/launch_agent Rome --resume YOUR_SESSION_ID
```

`--reasoning` is an alias for `--reasoning-effort`. Omitted settings use Codex's
configuration; an effort must be supported by the selected model. These overrides
apply only to the launched session and do not rewrite the global Codex config.
For automatic gameplay, set per-player defaults on `launch_agent_service`, or
override all players with `run_agents --model YOUR_MODEL --reasoning-effort high`;
see [automatic turns](Automatic-turns.md#select-models-and-reasoning-effort).

Automatic gameplay also writes `agents/Rome/turn_1/summary.md` and
`commands.jsonl`, then starts each new game turn with a fresh conversation and the
earlier summaries. These turn archives are managed by the agent service; interactive
`launch_agent` sessions retain the explicit conversation behavior described here.
When an automatic turn is interrupted, `run_agents resume` restores its starting
checkpoint and starts a fresh conversation. Failed-attempt logs are preserved for
inspection, while committed handoffs are retained. Manual interactive sessions keep
the per-command checkpoint behavior described above.
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
./scripts/inspect_game        # Open as the saved active civilization
./scripts/inspect_game Rome   # Open Rome's perspective
./scripts/inspect_game Greece # Open Greece's perspective
```

Run one command at a time. The script exports the checkpoint, builds the selected
desktop engine, and loads the chosen save automatically. Click through the
civilization's ready screen to inspect its units, cities, production and research.
For a different match or engine checkout, use the same selection as the host:

```sh
./scripts/inspect_game Rome --state-dir /tmp/my-civ-match
./scripts/inspect_game --checkpoint /absolute/path/checkpoint.json --unciv-dir ../Unciv
```

The default state directory and `UNCIV_HOST_DIR` behavior match `launch_host`.
Every invocation creates a fresh owner-only directory under `STATE_DIR/inspection`,
containing `Agent snapshot.json` and an inspection save for each Human civilization
in its `SaveFiles` folder. The script prints the directory and actual active
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
live updates or connection back to the host. Rerun `inspect_game` for a newer snapshot.
These full-state exports are for you as the operator, not for player agents.
Online multiplayer checkpoints are rejected because a desktop multiplayer session
can write back to its server; this viewer supports local matches.

## Verify the scripts

```sh
python3 -B -m unittest discover -s scripts/tests -p 'test_*.py' -v
```

The launcher tests use temporary files and mocked process/network calls. They
cover credential generation and preservation, selected-player token routing,
separate directories, startup arguments, resumption, and failure handling. Inspection
tests also verify that exports preserve gameplay data, omit host receipts, clear old
save destinations, retain earlier copies, and leave the checkpoint untouched. They
do not invoke a model or establish complete gameplay coverage or isolation.

For a prepared battlefield with cities and armies already at war, use
`--combat-scenario` on a fresh host. See the [combat scenario](Combat.md#ready-to-play-combat-scenario)
for its layout, exercise instructions and launch commands.
