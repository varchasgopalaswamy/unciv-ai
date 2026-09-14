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
[trade-cycle](Trade-cycle-scenario.md), covering eleven trade stages and two further
rounds with Rome and Greece. Select it with `--scenario trade-cycle`;
supplying it again checks the actual checkpoint on
resume. A directory's name does not select a scenario. The runbook explains the
intentional negotiation deadline and native-save verification.

Agents aim to win using the enabled victory conditions in `player_turn.rules`.
Prepared exercises instead supply `player_turn.scenario.instructions`.
The common prompt also covers gameplay rules, receipt/retry handling, and the current turn review. The service owns request IDs and
adds archive and final-summary instructions once, then selected notes and new events.
Restart agent services after changing their prompt code; already running services
continue using the version they loaded.

Before handing off, agents read `player_turn.turnReview`, which lists confirmed
actions and remaining movement. `END_TURN` includes the current review ID. Leaving
idle units with legal destinations also requires a short `deferReason`, so an agent
must act or explain its choice. Changes require a fresh observation and review.
The service also puts `tacticalReview` and `strategyReview` before the detailed
snapshot. These highlight confirmed losses, the military roster, visible hostile
units, nearby civilian risks, available city bombardments and plans needing review.
An unused bombardment, a civilian near a visible hostile, or an injured military
unit near one requires acting or giving `deferReason` before ending. This permits
intentional restraint; it never requires an attack. See [tactical review](Tactical-review.md).
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

Set both options on each service to choose different settings for each civilization:

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

The scheduler saves its overrides in `runner/run.json`; `resume` keeps them without
repeating the flags and rejects conflicting overrides. The service also persists
the resolved model and effort for the player's match, reports them in activation status,
and reapplies them to each fresh turn conversation and after a restart. Changing
service defaults does not silently change an ongoing player's settings. The resolved values
appear in scheduler output, its saved session metadata, and the service's private
`activations/*.json` records. Choose a new match to change settings for ongoing
players. Interactive `civ-agent agent start` accepts the same two options; see
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
    turn_0/
      commands.jsonl   Dense commands, observations and receipts from retained attempts
      summary.md       Short account of actions, reasons and outcomes
      summary.json     Typed decisions, receipts and memory assessments
      attempts/
        attempt_1/
          commands.jsonl   That attempt's commands and receipts
          summary.md       Public report and attempt outcome
          events.jsonl     Full public messages and tool events
          source.json      Match, civilization, game turn and attempt metadata
    turn_1/
      commands.jsonl
      summary.md
  Greece/
    turn_0/
      commands.jsonl
      summary.md
```

Folders, summaries, receipts and the player API use the same zero-based turn number:
turn 0 is `turn_0`, turn 1 is `turn_1`, and so on.
Use a fresh state directory for this archive format; older archives are not migrated.
Names such as Rome and Greece appear directly; unusual characters are encoded so
names cannot escape their directories or collide with another civilization's name.
The archive records failed or interrupted attempts as well as completed turns.
Each attempt retains its diagnostics. Normal interruptions preserve confirmed
actions, messages and memory; fresh jobs use selected notes and new events.
Older detailed evidence is available through the gateway's memory/history tools.
Explicit whole-match restoration archives the abandoned epoch's histories.
See [chat, memory and recovery](Chat-memory-and-recovery.md) for the current
concurrency, context and recovery contract.

Each summary has one heading with the turn number returned by the player API.
`summary.json` records typed decisions, confirmed outcomes, observations and memory
assessments; `summary.md` renders these in at most 200 words. For example:

```markdown
# Turn 0

- Queued Granary in c1: Improve the capital's food supply.
```

After `END_TURN` succeeds, the agent returns its final report without further tool
calls. The report remains in the transcript. Summaries come from committed records
and receipts, including after interruption or reconciliation without a final
response. Retry information and debugging labels remain in detailed attempt files.
See [structured memory](Structured-memory.md) for typed records, retirement, source
references and the intent field on consequential actions.

Canonical memory lives in private SQLite records. Markdown files are projections;
edits to these copies cannot change the next job's context.

## Live output and saved history

`civ-agent match start` prints civilization-prefixed assistant messages, status updates and
errors as they arrive. Add `--verbose` to also display tool calls, results, streamed
tool output and exposed reasoning summaries. Large tool results have short terminal
previews. Full sanitized history is always saved, regardless of this flag, and each
activation prints its local `history.md` path.

Verbosity applies to the current invocation, so it can be changed on resume:

```sh
uv run civ-agent match resume --state-dir /tmp/civ-economy-1 --verbose
```

The agent service appends accepted events through one ordered background I/O
worker. Markdown and turn archives refresh periodically (every two seconds plus
rendering time), and unchanged versions are skipped. Interruption and completion
flush accepted events and derived files before publishing a terminal activation.
Status requests and heartbeats remain on the HTTPS event loop while this work runs.

New MCP tool-result records keep structured JSON once when its accompanying plain
text contains exactly the same JSON value. Distinct text, annotations, other content
blocks and result metadata are retained. This normalization applies to saved history;
gameplay observations and the MCP responses received by Codex keep their existing
format. Existing event logs retain their original contents and cursors.

The supervisor fetches the events over the existing authenticated HTTPS service
connection and stores them on the scheduler machine under:

```text
STATE_DIR/runner/history/Rome/
  source.json                     Match and civilization binding
  turn_0/
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
uv run civ-agent history export        # Both saved players
uv run civ-agent history export Greece
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

If no handoff committed, confirmed commands and memory still remain. The service
settles uncertain requests using exact host receipts before the scheduler starts
fresh computation. It does not rewind the player automatically.

For the emergency option, stop the supervisor and run `civ-agent match rollback
--state-dir DIR --round N`. This restores every player and the game from a completed
round checkpoint under a new epoch, retaining usage and operator audit records.
A partial restore stays fenced until the same rollback command finishes. Details
and prerequisites are in [the recovery runbook](Chat-memory-and-recovery.md).

If the host reports `HALTED` after a mutation or checkpoint error, stop and restart
the host from its durable checkpoint first, then run `civ-agent match resume`. An
in-memory rollback cannot resolve an uncertain disk write; the restored checkpoint
determines whether `END_TURN` committed. Resume cannot clear `HALTED` itself.

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

`STATE_DIR/runner/run.json` stores explicit civilization conversation IDs, in-flight
activation, original start/deadline, consumed invocations, usage and stop reason.
Agent services keep durable activation records and Codex keeps each turn's conversation
history. Each civilization's working directory holds its own turn archives and notes.
Separate directories are not a filesystem security boundary: trusted local services
still inherit the user's Codex login, configuration and available tools.

The operator interface is a separate `POST /operator` endpoint with lease,
activation, communication, budget, checkpoint and recovery operations. Activation checks
the match epoch, server instance, active civilization and durable handoff counter
under the same executor lock as gameplay. Only one lease owns the match. A handoff
counter increments per player turn; it is operator metadata, absent from player
snapshots. Epoch and receipts persist in checkpoints; activation tokens do not.
Scheduled recovery preserves confirmed actions and reconciles exact requests.
Round manifests coordinate the native game, shared communication and each private
memory store. Explicit whole-match restoration creates a new epoch and revokes
the abandoned timeline's credentials; partial restoration cannot resume gameplay.

Automated tests exercise HTTPS authentication/certificates, real engine development
over twenty rounds with restart, stale activations, duplicate schedulers, lease
expiry, interrupted turns, exact receipt replay and finite budgets. Scripted tests
do not establish a completed live-model duel, full UI parity, or a safe filesystem
and network isolation boundary. Naval/air combat, broader diplomacy and several mandatory choice
surfaces remain outside the implemented gameplay slice.

## Terminal results

A winning capture can finish the game without an `END_TURN`. The supervisor checks
that authoritative terminal state before scheduling further gameplay. It drains the active
service, archives committed final commands with a terminal outcome, and preserves
the existing handoff count. Capture decisions remain playable until resolved.

The operator-only `results` response contains separately filtered results for each
human civilization. The supervisor sends each service only its own payload over
HTTPS; the service writes `result.json` and `result.md` in that civilization's
workspace. These files include the outcome, pending combat reports and complete informational
popup text, including defeated leaders' final messages. This starts
no extra model turn. Manual sessions can use the read-only `player_result` tool.

Result acknowledgements are persisted separately per civilization. If delivery is
interrupted, resume retries only the missing deliveries. Services validate the
civilization and epoch and accept exact repeats; a conflicting result is rejected.
The run becomes complete only when all configured players have acknowledged their
result. Final turn summaries retain the same simple format as ordinary turns.
