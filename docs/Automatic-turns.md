# Automatic turns over HTTPS

The supervisor starts a fresh Codex conversation for each civilization's new game
turn, supplies its earlier turn summaries and a single-turn credential, and watches
the host for a committed handoff. A model's
claim that it ended a turn does not advance scheduling. The game API, operator API,
and agent-service control all use authenticated HTTPS with certificate verification.

Each agent service controls its local Codex app-server using a private Unix socket.
That internal connection uses WebSocket framing over the socket, with no stdio
protocol or plaintext network listener. It stays inside the agent's deployment
boundary. See the [Codex app-server documentation](https://learn.chatgpt.com/docs/app-server);
the service also checks against the installed app-server protocol in tests.

## Run a match

Use Linux, Python 3.10 or newer, OpenSSL, JDK 21 or newer, the initialized engine
checkout, and a logged-in Codex CLI with `app-server --listen unix://PATH` support.
Install the Python service dependency in a virtual environment:

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r scripts/requirements.txt
source .venv/bin/activate
```

Run these in separate terminals from the repository, with the virtual environment
active in the agent-service terminals:

```sh
./scripts/launch_host
./scripts/launch_agent_service Rome
./scripts/launch_agent_service Greece
./scripts/run_agents --max-turns 20 --max-invocations 48 --max-seconds 3600
```

Wait for `MCP-READY` and each service's `AGENT-READY` message before starting the supervisor. The host
uses port 8765; Rome and Greece services default to 8865 and 8866. The default match
directory is `~/.local/state/unciv-host/demo`, overridden by `UNCIV_HOST_DIR` or
`--state-dir`. Use the same directory for these local commands. Existing checkpoints
resume automatically; choose a new directory for a new match.

For immediate diplomacy testing, create a new match with nearby starts and an extra
Scout for each player:

```sh
./scripts/launch_host --new --close-starts --state-dir /tmp/civ-diplomacy
```

Use that state directory for both agent services and `run_agents` as well. The
capital sites are five tiles apart on connected land. The extra Scouts start next
to each other, triggering normal first-contact messages before the first turn.
Both civilizations retain their usual Settler and Warrior, and normal fog of war
applies. `--close-starts` only applies to new games; omit it when resuming.

The default gameplay prompt recognizes this scenario through `player_turn.scenario`
and asks Rome to declare war once, then offer peace as soon as negotiation is legal.
The proposal contains the offered Peace Treaty on each side, with no payments.
Greece reads the declaration and offer, then accepts when allowed. Both continue
ordinary turns while negotiation is unavailable or a response is pending. The host
reports the exercise as complete after war and peace; this survives restart and
treaty expiry, so the prompt does not ask for another war. Ordinary duels keep their
usual open-ended diplomacy. The same instructions apply to interactive `launch_agent`
sessions using the default prompt.

To adopt the new turn folders and recovery format, stop the previous host and agent
services to free their ports, then use a new directory in all four terminals. Old
runner state is deliberately rejected; existing match files are left intact:

```sh
./scripts/launch_host --state-dir /tmp/civ-encounters --new
./scripts/launch_agent_service Rome --state-dir /tmp/civ-encounters
./scripts/launch_agent_service Greece --state-dir /tmp/civ-encounters
./scripts/run_agents --state-dir /tmp/civ-encounters
```

`launch_host` builds the current code and selected engine before starting. First
contact now supplies an introduction and an explicit `ACKNOWLEDGE_ALERT` action;
the agent prompts ask players to read and acknowledge supported messages. Each
player's contact message and command appear in their normal saved tool history.
Diplomacy provides known-rival identity, war/peace observations and available actions.
Agents read declaration warnings and incoming peace terms before choosing a response.
They can keep taking turns while waiting for an opponent to answer a proposal.

Before handing off, agents read `player_turn.turnReview`, which lists confirmed
actions and remaining movement. `END_TURN` includes the current review ID. Leaving
idle units with legal destinations also requires a short `deferReason`, so an agent
must act or explain its choice. Changes require a fresh observation and review.
`previousTurnReview` carries the prior review and deferred choices into the next turn
alongside the agent's own summaries.

The host generates and preserves a local CA, HTTPS identity, `players.json`, and a
separate `operator.json`. Each agent service generates its own control credential
and publishes an endpoint descriptor. Credentials never appear in command arguments.
The supervisor sends only the active player's temporary token to its service; the
operator token and other players' transcripts are not forwarded.

The limits above allow twenty **game rounds**, up to forty ordinary Rome/Greece
activations plus eight additional activations for recovery. Defaults are the same
as shown. `--turn-timeout` defaults to 180 seconds per activation.

## Select models and reasoning effort

Set both options on each service to choose different settings for each civilization:

```sh
./scripts/launch_agent_service Rome --model YOUR_ROME_MODEL --reasoning-effort high
./scripts/launch_agent_service Greece --model YOUR_GREECE_MODEL --reasoning-effort medium
./scripts/run_agents
```

Or override both settings for every player from the scheduler:

```sh
./scripts/run_agents --model YOUR_MODEL --reasoning-effort high
./scripts/run_agents resume
```

For each setting, a new conversation uses the scheduler override, then its service's
default, then the local Codex configuration. `--reasoning` is an alias for
`--reasoning-effort`. Effort names are model-specific; the launcher forwards the
selected name and Codex validates support. The installed Codex 0.153.4 protocol
accepts nonempty effort names rather than prescribing a fixed list. No model or
effort override is imposed when these flags are omitted.

The scheduler saves its overrides in `runner/run.json`; `resume` keeps them without
repeating the flags and rejects conflicting overrides. The service also persists
the resolved model and effort for the player's match, reports them in activation status,
and reapplies them to each fresh turn conversation and after a restart. Changing
service defaults does not silently change an ongoing player's settings. The resolved values
appear in scheduler output, its saved session metadata, and the service's private
`activations/*.json` records. Choose a new match to change settings for ongoing
players. Interactive `launch_agent` accepts the same two options; see
[local launchers](Local-launchers.md).

Each activation explicitly identifies the game turn it must play. A report about an
earlier turn does not satisfy that request. After a committed handoff, the supervisor
allows up to thirty seconds for the agent's final report, within the activation and
wall-time budgets, before interrupting it. The next player starts only after the
previous activation has stopped.

## Turn folders and memory

Each civilization gets a readable workspace. For the default local setup:

```text
STATE_DIR/agents/
  Rome/
    turn_1/
      commands.jsonl   Dense commands, observations and receipts from retained attempts
      summary.md       Short account of actions, reasons and outcomes
      attempts/
        attempt_1/
          commands.jsonl   That attempt's commands and receipts
          summary.md       Public report, outcomes and rollback status
          events.jsonl     Full public messages and tool events
          source.json      Match, civilization, game turn and attempt metadata
    turn_2/
      commands.jsonl
      summary.md
  Greece/
    turn_1/
      commands.jsonl
      summary.md
```

Folder numbers are the game's zero-based turn plus one: game turn 0 is `turn_1`.
Names such as Rome and Greece appear directly; unusual characters are encoded so
names cannot escape their directories or collide with another civilization's name.
The archive records failed or interrupted attempts as well as completed turns.
Each attempt has its own numbered directory. After rollback, its commands and
report remain there for debugging and are removed from the turn's top-level
`commands.jsonl` and `summary.md`. Retry details stay in the attempt directories;
they do not enter the next turn's memory. These files contain
only that civilization's permitted observations and public account of its decisions.

At the next game turn, the service starts a new Codex thread and concatenates the
player's earlier summaries in turn order into its starting context. Earlier full
snapshots and receipts stay in the player's turn folders; the agent can read them
when it needs details. Old conversation transcripts are not resumed for a new turn.
If an attempt stops before a committed handoff, resuming restores the host's
checkpoint from the start of that player's turn and starts another fresh conversation.
The discarded attempt's commands and reports remain available for debugging, but
its actions and observations do not enter later turn memory.

Each summary contains one heading with the turn number returned by the player API,
followed by the agent's brief public report. For example, `turn_1/summary.md` contains:

```markdown
# Turn 0

Moved the scout east to explore. Continue toward the coast next turn.
```

The report is limited to 200 words. If there is no final report, the service uses
the last available public note, or just the heading if no note was captured.
Receipt counts, handoff status, retry information and debugging labels stay in
the detailed attempt records. Full messages remain in each attempt's event log.

Only turns with a host-confirmed handoff enter the next turn's context. The service
concatenates its stored trusted summaries in turn order; edits to workspace copies
do not change the next turn's context.

## Live output and saved history

`run_agents` prints civilization-prefixed assistant messages, exposed reasoning
summaries, tool activity and errors as they arrive. Large tool results have short
terminal previews; the history retains their complete sanitized content. Each
activation prints its local `history.md` path.

The supervisor fetches the events over the existing authenticated HTTPS service
connection and stores them on the scheduler machine under:

```text
STATE_DIR/runner/history/Rome/
  source.json                     Match and civilization binding
  turn_1/
    commands.jsonl                Finalized copy of the player's detailed archive
    summary.md                    Finalized copy of the player's short summary
    attempt_<activation UUID>/
      history.md                  Readable conversation and tool history
      events.jsonl                Timestamped events, including streamed deltas
      source.json                 Match, civilization, turn and activation identifiers
```

Open `summary.md` to browse the match quickly, or an attempt's `history.md` to inspect
the complete exposed conversation. The cursor is recovered from the saved events on
resume, so earlier events are not duplicated or printed again. Final events are
collected before the activation is cleared. Each agent service also retains its
original events and Markdown under `SERVICE_DIR/history/<activation hash>/`.
The default `SERVICE_DIR` is `STATE_DIR/agent-services/Rome`, with the civilization
name changed for each service. Completed archives are fetched over HTTPS, so the
scheduler's readable mirror also works when the player workspace is on another
machine. Separate attempts stay in separate folders. Each civilization folder binds
to a match epoch and refuses to mix a different match into it.
The files are owner-only and are never sent to another player.

Reasoning entries contain the summaries that Codex exposes. The service requests
summaries with `summary: "auto"`; availability depends on the model. Raw reasoning
and encrypted reasoning payloads are excluded. This follows the distinction in the
[Codex app-server item documentation](https://learn.chatgpt.com/docs/app-server#items).
Credentials are redacted, and terminal control characters are removed from display.

For conversations from before event logging was enabled, export this match's saved
Codex histories on the machine holding those conversations:

```sh
./scripts/export_agent_history        # Both saved players
./scripts/export_agent_history Greece
```

The exporter prints readable paths under `STATE_DIR/runner/history-imports/`.
Each export creates a fresh directory, preserving earlier exports. It reads only
the conversation IDs recorded in this match's `runner/run.json` and leaves the
original Codex histories unchanged. `--codex-home`, `--state-dir` and `--output-dir`
support other local locations. Older conversations without recorded reasoning
summaries cannot supply those summaries retroactively.

Start this workflow with a new match directory after updating the scheduler and both
services. Earlier scheduler formats are rejected rather than silently migrating an
interrupted game. Existing match logs and Codex conversations are left untouched;
the legacy exporter above remains available for reviewing them.

## Pause, inspect and resume

From another terminal:

```sh
./scripts/run_agents status
./scripts/run_agents pause
./scripts/inspect_game Rome
./scripts/run_agents resume
```

Ctrl-C in the supervisor also requests a pause. Pause fences player mutations before
interrupting model work. `stop` leaves the match paused and retains attempt records
and all consumed budgets. Host and agent leases expire independently if the supervisor
disappears. A restarted scheduled host starts paused and revokes old activation tokens.

After an interruption or host restart, use:

```sh
./scripts/run_agents resume
```

The supervisor first confirms that the previous agent process has stopped and checks
the host's durable handoff counter. If `END_TURN` committed, it retains the completed
turn even when its response never reached the agent. The finalized summary can then
be used for later turns.

If no handoff committed, the host restores the full game from the checkpoint captured
at the start of that player's turn. This discards every mutation from the unfinished
attempt, including commands whose receipts had already arrived. The host restores
the corresponding receipt state, rotates stale-state revisions and revokes the old
temporary credentials. The scheduler marks that attempt as rolled back, preserves its
detailed logs, and starts a fresh conversation with summaries from earlier completed
turns. The match epoch and consumed budgets remain unchanged.

Rollback is an authenticated operator action; agents cannot request it themselves.
A failed connection, missing or inconsistent turn-start checkpoint, unconfirmed
process shutdown, unsupported decision or incomplete turn pauses the run. The
scheduler does not repeatedly restart a failing agent or choose game actions to
bypass its blocker. Inspect the attempt logs before resuming when the cause needs
attention. While paused, `inspect_game` may show the unfinished attempt's current
position; rollback takes place when the supervisor resumes it.

Returning the host to manual control discards the automatic restore point. If an
agent attempt was unfinished, finish that player's turn manually before resuming
the supervisor; it will not label a newly captured manual position as a rollback.

If the host reports `HALTED` after a mutation or checkpoint error, stop and restart
the host from its durable checkpoint first, then run `run_agents resume`. An
in-memory rollback cannot resolve an uncertain disk write; the restored checkpoint
determines whether `END_TURN` committed. Resume cannot clear `HALTED` itself.

Limits and usage persist across resume. Wall time includes paused time and downtime.
Increase a limit explicitly when continuing a completed short run:

```sh
./scripts/run_agents resume --max-turns 30 --max-invocations 70 --max-seconds 7200
```

These are cumulative limits from the original start, not a new allowance. Reported
token usage is accounting; this implementation does not enforce a token or dollar cap.
Usage from restarted Codex processes is charged to the current activation even when
the native counters reset. Older stored usage figures are not rewritten automatically.

To return to the interactive `launch_agent` sessions, stop the scheduler and restore
manual control:

```sh
./scripts/run_agents stop
./scripts/run_agents manual
./scripts/launch_agent Rome
```

Scheduled mode fences the long-lived player credentials against new mutations.
Manual mode restores those credentials after the scheduler lease is gone. Status
and ordinary permitted observations remain available while paused.

## Separate machines

No service-control request requires shared game saves or a shared agent workspace.
Provision a certificate whose SAN matches each endpoint hostname, and give clients
its CA certificate. Do not copy CA signing keys or server private keys to clients.
The generated certificates are convenient for local development; production
certificate provisioning and container/network isolation remain operator concerns.

For the host, `--listen-host 0.0.0.0 --public-host game.example.test` generates a
matching development identity. Alternatively pass `--tls-config /absolute/tls.json
--ca-file /absolute/ca.pem`. The strict owner-only TLS JSON has:

```json
{
  "keyStorePath": "/absolute/host.p12",
  "keyStorePasswordFile": "/absolute/password.txt",
  "keyAlias": "host",
  "listenHost": "0.0.0.0",
  "publicHost": "game.example.test"
}
```

The PKCS12 store uses the same password for the store and its private key. Its file,
password file and configuration must have owner-only permissions. Only TLS 1.2/1.3
are enabled; there is no production HTTP listener or certificate-verification bypass.

On each agent machine, run `launch_agent_service CIV` with its own `--service-dir`
and `--workspace`, `--mcp-url https://game.example.test:8765/mcp`, and
`--mcp-ca-file /absolute/game-ca.pem`. Set `--listen-host`, `--public-host` and
`--port` for that service. Existing PEM identities use `--cert-file`, `--key-file`
and `--ca-file`. The agent needs its own Codex login and the host CA, with no operator
or long-lived player credential file.

On the scheduler machine, configure the host with `--url https://game.example.test:8765
--ca-file /absolute/game-ca.pem --operator-config /absolute/operator.json`. Supply
`--agents-config /absolute/agents.json` with one entry per civilization:

```json
{
  "agents": [
    {
      "civilizationId": "Rome",
      "url": "https://rome.example.test:8865/agent",
      "tokenFile": "/absolute/rome-control.json",
      "caFile": "/absolute/rome-ca.pem"
    }
  ]
}
```

Add Greece's entry in the same list. Service control token files contain
`{"token":"<43-character Base64URL credential>"}` and require owner-only permissions.
Paths in this list are local to the scheduler. Copy each service's token and public
CA certificate to the scheduler securely; its workspace and Codex history stay on
the agent machine. Agent URLs must remain available when reconciling an interrupted
activation. Authenticated control redirects and plaintext URLs are rejected.

## State and remaining limits

`STATE_DIR/runner/run.json` stores explicit civilization conversation IDs, in-flight
activation, original start/deadline, consumed invocations, usage and stop reason.
Agent services keep durable activation records and Codex keeps each turn's conversation
history. Each civilization's working directory holds its own turn archives and notes.
Separate directories are not a filesystem security boundary: trusted local services
still inherit the user's Codex login, configuration and available tools.

The operator interface is a separate `POST /operator` endpoint with `status`,
`acquire`, `heartbeat`, `activate`, `rollback`, `pause`, `release` and `manual`. Activation checks
the match epoch, server instance, active civilization and durable handoff counter
under the same executor lock as gameplay. Only one lease owns the match. A handoff
counter increments per player turn; it is operator metadata, absent from player
snapshots. Epoch and receipts persist in checkpoints; activation tokens do not.
The turn-start restore point is stored inside the same atomic checkpoint. Rollback
requires the current lease, match epoch, server instance, handoff counter and active
civilization; it cannot undo a handoff that has already committed.
Rollback also names the interrupted activation. Its durable attempt binding rejects
delayed requests from a discarded attempt after a fresh attempt has started.

Automated tests exercise HTTPS authentication/certificates, real engine development
over twenty rounds with restart, stale activations, duplicate schedulers, lease
expiry, interrupted turns, exact receipt replay and finite budgets. Scripted tests
do not establish a completed live-model duel, full UI parity, or a safe filesystem
and network isolation boundary. Combat, broader diplomacy and several mandatory choice
surfaces remain outside the implemented gameplay slice.
