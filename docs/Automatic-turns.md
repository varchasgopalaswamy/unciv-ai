# Automatic turns over HTTPS

The supervisor resumes the active civilization's Codex conversation, gives it a
single-turn credential, and watches the host for a committed handoff. A model's
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

For a fresh match after the first-contact update, stop the previous host and agent
services to free their ports, then use a new directory in all four terminals:

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
Diplomacy currently provides known-rival identity and war/peace observations;
other diplomatic choices still require future implementations.

The host generates and preserves a local CA, HTTPS identity, `players.json`, and a
separate `operator.json`. Each agent service generates its own control credential
and publishes an endpoint descriptor. Credentials never appear in command arguments.
The supervisor sends only the active player's temporary token to its service; the
operator token and other players' transcripts are not forwarded.

The limits above allow twenty **game rounds**, up to forty ordinary Rome/Greece
activations plus eight additional activations for recovery. Defaults are the same
as shown. `--turn-timeout` defaults to 180 seconds per activation. `--model NAME`
selects a model for the run; otherwise Codex uses its configured model.

Each activation explicitly identifies the game turn it must play. A report about an
earlier turn does not satisfy that request. After a committed handoff, the supervisor
allows up to thirty seconds for the agent's final report, within the activation and
wall-time budgets, before interrupting it. The next player starts only after the
previous activation has stopped.

## Live output and saved history

`run_agents` prints civilization-prefixed assistant messages, exposed reasoning
summaries, tool activity and errors as they arrive. Large tool results have short
terminal previews; the history retains their complete sanitized content. Each
activation prints its local `history.md` path.

The supervisor fetches the events over the existing authenticated HTTPS service
connection and stores them on the scheduler machine under:

```text
STATE_DIR/runner/history/<epoch hash>/<civilization hash>/<activation hash>/
  history.md       Readable conversation and tool history
  events.jsonl     Timestamped events, including streamed deltas
  source.json      Match, civilization and activation identifiers
```

Open `history.md` in your editor. The cursor is recovered from the saved events on
resume, so earlier events are not duplicated or printed again. Final events are
collected before the activation is cleared. Each agent service also retains its
original events and Markdown under `SERVICE_DIR/history/<activation hash>/`.
The files are owner-only operator artifacts and are never sent to another player.

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

After updating from an older service, restart both `launch_agent_service` processes
in their terminals, wait for `AGENT-READY`, then run `./scripts/run_agents resume`.
The existing conversations and checkpoint are retained. No host restart is needed
for these Python service and scheduler changes. Older services report that live
history is unavailable until restarted.

## Pause, inspect and resume

From another terminal:

```sh
./scripts/run_agents status
./scripts/run_agents pause
./scripts/inspect_game Rome
./scripts/run_agents resume
```

Ctrl-C in the supervisor also requests a pause. Pause fences player mutations before
interrupting model work. `stop` leaves the match paused and retains conversation IDs
and all consumed budgets. Host and agent leases expire independently if the supervisor
disappears. A restarted scheduled host starts paused and revokes old activation tokens.

After an uncertain interruption or host restart, inspect the checkpoint and the
player's previous tool calls. If recovery is appropriate, explicitly use:

```sh
./scripts/run_agents resume --continue-turn
```

This resumes the same conversation with instructions to reconcile exact command
receipts. It does not create a replacement conversation to replay uncertain actions.
If no durable conversation ID exists, the supervisor stops for inspection. Unsupported
decisions, failed connections, unconfirmed cancellation and incomplete turns pause
the run; the scheduler does not select game actions to bypass them.

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
Agent services keep durable activation records and Codex keeps its conversation
history. Each civilization's working directory holds only its own intended notes.
Separate directories are not a filesystem security boundary: trusted local services
still inherit the user's Codex login, configuration and available tools.

The operator interface is a separate `POST /operator` endpoint with `status`,
`acquire`, `heartbeat`, `activate`, `pause`, `release` and `manual`. Activation checks
the match epoch, server instance, active civilization and durable handoff counter
under the same executor lock as gameplay. Only one lease owns the match. A handoff
counter increments per player turn; it is operator metadata, absent from player
snapshots. Epoch and receipts persist in checkpoints; activation tokens do not.

Automated tests exercise HTTPS authentication/certificates, real engine development
over twenty rounds with restart, stale activations, duplicate schedulers, lease
expiry, interrupted turns, exact receipt replay and finite budgets. Scripted tests
do not establish a completed live-model duel, full UI parity, or a safe filesystem
and network isolation boundary. Combat, diplomatic actions and several mandatory choice
surfaces remain outside the implemented gameplay slice.
