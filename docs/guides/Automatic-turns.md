# Automatic turns over HTTPS

The supervisor starts a fresh Codex conversation for each civilization's new game
turn, supplies selected private memory and a scoped gateway credential, and watches
the host for a committed handoff. A model's
claim that it ended a turn does not advance scheduling. The game API, operator API,
and agent-service control all use authenticated HTTPS with certificate verification.

Each agent service controls its local Codex app-server using a private Unix socket.
That internal connection uses WebSocket framing over the socket, with no stdio
protocol or plaintext network listener. It stays inside the agent's deployment
boundary. See the [Codex app-server documentation](https://learn.chatgpt.com/docs/app-server);
the service also checks against the installed app-server protocol in tests.

## Run a match

Use Linux, uv, Python 3.12 or newer, OpenSSL, JDK 21 or newer, the initialized engine
checkout, and a logged-in Codex CLI with `app-server --listen unix://PATH` support.
Install the Python package and its locked dependencies:

```sh
uv sync --locked
```

Run these in separate terminals from the repository. `uv run` uses the project
environment automatically:

```sh
uv run civ-agent host start
uv run civ-agent agent-server start Rome
uv run civ-agent agent-server start Greece
uv run civ-agent agent-server start France
uv run civ-agent match start --max-turns 20 --max-invocations 72 --turn-timeout 300 --max-seconds 7200
```

Wait for `MCP-READY` and each service's `AGENT-READY` message before starting the supervisor. The host
uses port 8765; Rome, Greece and France services default to 8865, 8866 and 8867. The default match
directory is `~/.local/state/unciv-host/demo`, overridden by `UNCIV_HOST_DIR` or
`--state-dir`. Use the same directory for these local commands. Existing checkpoints
resume automatically; choose a new directory for a new match.

Each running agent service needs its own port, including services for other matches.
If a port is already in use, stop the previous service or select a free `--port`.
The supervisor discovers the selected ports from the match directory; its command
does not need port overrides.

New games default to [Vanilla with Rome, Greece and France](Vanilla-game.md), using
Quick speed and ordinary starting conditions. The linked guide gives larger
scheduler limits for a full game. The only scripted scenario is
[trade-cycle](../scenarios/Trade-cycle-scenario.md), covering eleven trade stages and two further
rounds with Rome and Greece. Select it with `--scenario trade-cycle`;
supplying it again checks the actual checkpoint on
resume. A directory's name does not select a scenario. The runbook explains the
intentional negotiation deadline and native-save verification.

Agents aim to win using the enabled victory conditions in `player_turn.rules`.
Prepared exercises instead supply `player_turn.scenario.instructions`.
The common prompt also covers gameplay rules, receipt/retry handling, and the current turn review. The service owns request IDs and
supplies stable instructions, permitted observations and selected private knowledge.
Restart agent services after changing their prompt code; already running services
continue using the version they loaded.

Before handing off, agents read `player_turn.turnReview`, which lists confirmed
actions and remaining movement. `END_TURN` includes the current review ID. Leaving
idle units with legal destinations is allowed; `deferReason` is optional audit
context. Changes require a fresh observation and review.
The service also puts `tacticalReview` and `strategyReview` before the detailed
snapshot. These highlight confirmed losses, the military roster, visible hostile
units, nearby civilian risks, available city bombardments and plans needing review.
These observations do not require attacks, movement, or written explanations.
See [tactical review](../reference/Tactical-review.md).
`previousTurnReview` carries the prior review and deferred choices into the next turn
alongside the agent's own summaries.

The host generates and preserves a local CA, HTTPS identity, `players.json`, and a
separate `operator.json`. Each agent service generates its own control credential
and publishes an endpoint descriptor. Credentials never appear in command arguments.
The supervisor sends only the active player's temporary token to its service; the
operator token and other players' transcripts are not forwarded.

The limits above allow twenty **game rounds**, up to sixty ordinary three-player
activations plus twelve additional activations for recovery. Omitted limits default
to 20 rounds, 48 activations, one hour, and 180 seconds per activation.

## Select models and reasoning effort

The combined launcher supports per-civ settings directly, for example:

```sh
uv run civ-agent game start --model gpt-6-luna --reasoning-effort max \
  --agent-model Greece=gpt-5.6-luna --agent-reasoning-effort France=high
```

Both per-civ options are repeatable and retained by `game resume`; unspecified
settings use the global defaults. See the [full-game guide](Full-vanilla-run.md).

When starting services manually, set both options on each service to choose
different settings for each civilization:

```sh
uv run civ-agent agent-server start Rome --model YOUR_ROME_MODEL --reasoning-effort high
uv run civ-agent agent-server start Greece --model YOUR_GREECE_MODEL --reasoning-effort medium
uv run civ-agent match start
```

Or override both settings for every player from the scheduler:

```sh
uv run civ-agent match start --model YOUR_MODEL --reasoning-effort high
uv run civ-agent match resume
```

For each setting, a new conversation uses the scheduler override, then its service's
default, then the local Codex configuration. `--reasoning` is an alias for
`--reasoning-effort`. Effort names are model-specific; the launcher forwards the
selected name and Codex validates support. The installed Codex 0.153.4 protocol
accepts nonempty effort names rather than prescribing a fixed list. No model or
effort override is imposed when these flags are omitted.

The scheduler saves its overrides in `runner/monitoring.sqlite3`; `resume` keeps them without
repeating the flags and rejects conflicting overrides. The service also persists
the resolved model and effort for the player's match, reports them in activation status,
and reapplies them to each fresh turn conversation and after a restart. Changing
service defaults does not silently change an ongoing player's settings. The resolved values
appear in scheduler output, its saved session metadata, and the service's private
`activations/*.json` records. Choose a new match to change settings for ongoing
players. Interactive `civ-agent agent start` accepts the same two options; see
[local launchers](Local-launchers.md).

Each activation identifies the game turn it must play. After native handoff, the
next player can start while provider completion and accounting drain. There is
no model-driven post-turn memory phase. Independent strategy reviews run
concurrently before each civilization's first move on turn 0, then after every K
turns starting at K (default 5). Other civilizations can play while a review runs,
but its civilization's first or next turn waits for the report before starting its model.
This wait does not consume that turn's timeout.
See [background reviews](../architecture/Background-turn-memory.md).

## Private memory and recorded history

Each service keeps its own private memory database, durable review inputs, receipts
and event journal. Models receive selected information through memory/history
tools; workspace reports are never an authority. Completed and interrupted
attempts remain inspectable without generating folders for every turn.

The scheduler records sanitized events in `runner/monitoring.sqlite3`, indexed by
player, zero-based game turn, activation and epoch. Resume rewinds the interrupted
active turn and uses restored private notes and observations. Earlier operator
streams remain available under their original epochs. See [monitoring](../architecture/Monitoring.md)
and [chat, memory and recovery](Chat-memory-and-recovery.md).

## Live output and saved history

For a live dashboard in another terminal, run
`uv run civ-agent match watch --state-dir /path/to/match`. It shows player status,
scores, token/call totals, per-turn timings, and scrollable activity with player and
turn selection. It attaches to existing records without restarting or interrupting
the game. See [terminal dashboard](Match-dashboard.md) for controls and data sources.

`civ-agent match start` prints turn headers, consistent player colors, indented
assistant messages and confirmed-action summaries from native receipts. Errors
and warnings remain visible in normal output. One renderer coordinates messages
and background bookkeeping, so another player's completion cannot run into a
partially streamed message.

Colors default to `--color auto`: enabled in terminals, disabled in redirected
output. Use `--color always` or `--color never` to override detection; `NO_COLOR`
disables colors in every mode. Labels remain readable without color. Output is
append-only and stays in terminal scrollback.

Completed turns show gameplay/bookkeeping time, game API calls, and input, cached,
output and reasoning tokens. Cached tokens are part of input tokens; reasoning
tokens are part of output tokens. Totals use matching host records from the
monitoring database. Game API totals count host `tool:` calls once, excluding
the overlapping `command:` breakdown. If the host record is unavailable, timing and
usage cover the finalized activation only and are labeled accordingly. Partial
usage is marked partial. Missing counts display `unavailable`; model-request counts
are currently available only in offline benchmark traces. Display adds no model
requests or game API calls.

Add `--verbose` to also display preloaded startup context, tool calls, results,
streamed tool output and exposed reasoning summaries. Large tool results have
short terminal previews. Full sanitized history is always saved without terminal
formatting, regardless of verbosity, and each activation prints its local
`runner/monitoring.sqlite3` path. Use the dashboard or an explicit export to inspect it.

Color and verbosity apply to the current invocation, so they can be changed on resume:

```sh
uv run civ-agent match resume --state-dir /tmp/civ-economy-1 --color never --verbose
```

The agent service appends accepted events through one ordered background I/O
worker. Events and accounting are durable before completion. The periodic
service task samples accounting only; no Markdown, notebooks or turn archives are
rendered during gameplay or recovery. Status and heartbeat requests remain on the
HTTPS event loop while durable writes run.

The scheduler requires the current service's history and health APIs. Missing
terminal history blocks that civilization's next turn. A second stale conflict on one operation,
or an internal state-update failure, stops the run for inspection; communication
failures are checked even while another civilization is playing. See the
[recovery diagnostics](Chat-memory-and-recovery.md#recovery-diagnostics).

New MCP tool-result records keep structured JSON once when its accompanying plain
text contains exactly the same JSON value. Distinct text, annotations, other content
blocks and result metadata are retained. The model-facing adapter also removes
duplicate JSON while preserving MCP compatibility; see
[agent context and tools](../architecture/Agent-context.md#responses-focused-reads-and-commands).
Existing event logs retain their original contents and cursors.

The supervisor fetches events over authenticated HTTPS and commits them directly
to SQLite. Event sequence numbers recover the cursor after restart, so earlier
events are not duplicated or printed again. Final events are collected before an
activation is cleared. Each service also keeps its original private journal under
`SERVICE_DIR/history/<activation hash>/events.jsonl`, enabling remote services and
saved review inputs. Operator records are owner-only and never sent to a player.

Reasoning entries contain the summaries that Codex exposes. The service requests
summaries with `summary: "auto"`; availability depends on the model. Raw reasoning
and encrypted reasoning payloads are excluded. This follows the distinction in the
[Codex app-server item documentation](https://learn.chatgpt.com/docs/app-server#items).
Credentials are redacted, and terminal control characters are removed from display.

To generate readable conversations and telemetry on demand:

```sh
uv run civ-agent history export --state-dir /path/to/match
uv run civ-agent history export Greece --state-dir /path/to/match
uv run civ-agent history export Rome --state-dir /path/to/match --notebooks
```

Each export creates a fresh directory under `STATE_DIR/exports/`. It reads only
committed monitoring records, without contacting services or starting models.
`--notebooks` includes notes, turn summaries and receipts from local private
memory databases, using read-only connections that leave active jobs untouched.
Use `--output-dir` to choose another destination. Older file-based monitoring is
not imported; start a fresh match with the current version.

The file-to-database import preserves the current turn-recovery format. Matches
with earlier checkpoint or memory formats require a fresh match directory.

## Pause, inspect and resume

From another terminal:

```sh
uv run civ-agent match status
uv run civ-agent match pause
uv run civ-agent host inspect Rome
uv run civ-agent match resume
```

Ctrl-C in the supervisor also requests a pause. Pause fences player mutations before
interrupting model work. `stop` leaves the match paused and retains attempt records
and all consumed budgets. Host and agent leases expire independently if the supervisor
disappears. A restarted scheduled host starts paused and revokes old activation tokens.

After an interruption or host restart, use:

```sh
uv run civ-agent match resume
```

The supervisor first confirms that the previous agent process has stopped and checks
the host's durable handoff counter. If `END_TURN` committed, it retains the completed
turn even when its response never reached the agent. The finalized summary can then
be used for later turns.

If no handoff committed, resume restores the coordinated checkpoint from the start
of that player's turn and starts a fresh conversation. Messages and agent state
are restored together with the game. The old attempt remains in operator archives.
A completed handoff is retained even when its response was lost.

Pending strategic reviewers restart from their durable frozen inputs. The default is three
attempts per task; exhaustion stops for inspection. Increase `--review-attempts` on
`game resume` (or the agent server) to authorize more attempts. Operator pauses and exhausted time budgets do
not start additional retries.

A partial restore stays fenced and the next resume continues it. A `HALTED` host
must first restart from its native checkpoint so the scheduler can determine
whether the handoff committed. See [turn recovery](../architecture/Turn-recovery.md).

Limits and usage persist across resume. Wall time includes paused time and downtime.
Increase a limit explicitly when continuing a completed short run:

```sh
uv run civ-agent match resume --max-turns 30 --max-invocations 70 --max-seconds 7200
```

These are cumulative limits from the original start, not a new allowance. Reported
token usage is accounting; this implementation does not enforce a token or dollar cap.
Usage from restarted Codex processes is charged to the current activation even when
the native counters reset. Older stored usage figures are not rewritten automatically.

To return to the interactive `civ-agent agent start` sessions, stop the scheduler and restore
manual control:

```sh
uv run civ-agent match stop
uv run civ-agent match manual
uv run civ-agent agent start Rome
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

On each agent machine, run `civ-agent agent-server start CIV` with its own `--service-dir`
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

The `state/run` record in `STATE_DIR/runner/monitoring.sqlite3` stores explicit civilization conversation IDs, in-flight
activation, original start/deadline, consumed invocations, usage and stop reason.
Agent services keep durable activation records and Codex keeps each turn's conversation
history. Each civilization has a separate private memory database and service journal.
Separate directories are not a filesystem security boundary: trusted local services
still inherit the user's Codex login, configuration and available tools.

The operator interface is a separate `POST /operator` endpoint with lease,
activation, communication, budget, checkpoint and recovery operations. Activation checks
the match epoch, server instance, active civilization and durable handoff counter
under the same executor lock as gameplay. Only one lease owns the match. A handoff
counter increments per player turn; it is operator metadata, absent from player
snapshots. Epoch and receipts persist in checkpoints; activation tokens do not.
Recovery reconciles durable handoffs before deciding what to replay. The current
turn boundary coordinates the native game, shared communication and each private
memory store. Resume rewinds an interrupted turn, creates a new epoch and revokes
the abandoned timeline's credentials. Completed turns stay committed; partial
restoration cannot resume gameplay.

Automated tests exercise HTTPS authentication/certificates, real engine development
over twenty rounds with restart, stale activations, duplicate schedulers, lease
expiry, interrupted turns, exact receipt replay and finite budgets. Scripted tests
do not establish a completed live-model duel, full UI parity, or a safe filesystem
and network isolation boundary. Naval/air combat, broader diplomacy and several mandatory choice
surfaces remain outside the implemented gameplay slice.

## Terminal results

A winning capture can finish the game without an `END_TURN`. The supervisor checks
that authoritative terminal state before scheduling further gameplay. It drains the active
service, records committed final commands with a terminal outcome, and preserves
the existing handoff count. Capture decisions remain playable until resolved.

The operator-only `results` response contains separately filtered results for each
human civilization. The supervisor sends each service only its own payload over
HTTPS; the service durably stores its private `result.json`. This record includes the outcome, pending combat reports and complete informational
popup text, including defeated leaders' final messages. This starts
no extra model turn. Manual sessions can use the read-only `player_result` tool.

Result acknowledgements are persisted separately per civilization. If delivery is
interrupted, resume retries only the missing deliveries. Services validate the
civilization and epoch and accept exact repeats; a conflicting result is rejected.
The run becomes complete only when all configured players have acknowledged their
result. Final turn summaries retain the same simple format as ordinary turns.
