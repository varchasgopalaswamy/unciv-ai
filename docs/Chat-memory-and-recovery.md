# Chat, memory and recovery

See the [architecture diagram](Async-chat-architecture.md) for the component and
data flows.

Chat and memory are available throughout the [trade-cycle scenario](Trade-cycle-scenario.md),
the only launchable gameplay exercise. Follow that runbook to start the host,
both agent services and the scheduler. Public/private messages and social
commitments are also covered by internal regression fixtures.

Add `--model NAME --reasoning-effort LEVEL` to `match start` to select settings
for both gameplay and communication. The defaults allow one gameplay job and one
communication job per civilization, with four jobs across the match. Configure
these with `--jobs-per-civilization 1|2` and `--max-concurrent-jobs N`.
Invocation and time budgets include communication jobs and interrupted attempts.

## What the model controls

The model chooses entities and intent:

```json
{"kind":"QUEUE_CONSTRUCTION","target":"city1","selection":"Granary"}
```

The service supplies the host credential, request identity, observed revision,
turn-review acknowledgement and mandatory dependency checks. It stores the exact
request before transmission and reconciles uncertain responses against host
receipts. The model gets a revocable credential for its job's HTTPS gateway.
The interactive `agent start` launcher also uses this gateway and requires its
civilization's service to be running; it no longer connects the model directly
to the host.

Tools include `chat_read`, `chat_history`, `chat_send`, `commitment_offer`,
`commitment_decide`, `memory_read`, `memory_search`, `memory_update` and `history_read`, alongside
the existing player observations and validated gameplay commands. The
[economic trade tools](Asynchronous-trade.md) allow both job types to inspect,
propose and decide exact binding terms. Only the current player opens a formal
negotiation; gameplay then waits while both sides communicate and decide. Its
counteroffer limit, deadline and pending state survive restart. Communication jobs cannot issue ordinary
gameplay commands. General shell and browsing tools are disabled
for player jobs. The scoped gateway's tools are preapproved for this authorized
match; this does not grant the model operator access.

The player must still read popup text, warnings, trade terms and turn review, and
explicitly choose consequential confirmations such as declaring war. Supplying
bookkeeping automatically does not invent a gameplay decision or refresh an
unseen observation. Each successful gameplay action requires a fresh `player_turn`
before the next action. Unit, city, economic, diplomatic and private-policy
evidence have separate versions; the host also retains its conservative full-view
revision check and performs engine legality checks atomically.

A concurrent change returns a recoverable `STALE_ASSUMPTIONS` result naming the
affected records and the reads needed to reconsider that action. It explicitly
confirms that nothing was dispatched. The agent can skip work another job already
handled and continue its turn; a rejected decision is never automatically replayed
against fresh assumptions. Uncertain network responses retain their separate
stop-and-reconcile behavior.

Communication jobs normally read `chat_read` for compact, player-bound turn,
scenario, economy and diplomatic context. Full gameplay observations remain on
demand. This keeps map details and construction menus out of ordinary message
handling and trade negotiation.

## Delivery and commitments

Public chat includes everyone from the start. Private chat is one-to-one.
Chat neither establishes in-game contact nor reveals map state. Application code
maintains HTTPS long polling with a durable, recipient-specific cursor. The model
never polls. Private messages and public mentions trigger communication jobs after
a one-second batch window; unmentioned public messages wait for the next relevant
activation. Failed jobs await new evidence or explicit resume instead of repeatedly
calling the model on an idle inbox.

The host stores three separate records: exact statements/offers, explicit consent
to particular terms, and confirmed engine outcomes. Offering terms constitutes
the author's consent. Everyone else must accept explicitly. Proposed amendments
retain the existing accepted promise until new terms receive mutual consent.
Accepted social peace promises produce private, nonblocking `socialAdvisories`
in turn overviews and relevant war-trade reads. The advice quotes the accepted
terms and conditions so the player can judge the conflict. Players may proceed
with any native-legal action without withdrawing or reconsidering a promise.
Optional withdrawal or reconsideration records a stated change of intent.
Conditions are recorded text, not automatically interpreted predicates. Social
consent never creates an engine treaty, transfers resources, changes action
availability, or invalidates native game revisions or consent to a trade.
See [the host rule boundary](Social-commitments.md).

Human commands handle authentication and request IDs too:

```sh
uv run civ-agent chat send Rome --state-dir /tmp/civ-chat-1 --text 'Greetings, everyone'
uv run civ-agent chat send Rome --state-dir /tmp/civ-chat-1 --to Greece --text 'Shall we discuss peace?'
uv run civ-agent chat inbox Greece --state-dir /tmp/civ-chat-1
uv run civ-agent chat follow Greece --state-dir /tmp/civ-chat-1
uv run civ-agent chat ledger Greece --state-dir /tmp/civ-chat-1
```

Public `send` accepts repeated `--mention CIV`. The CLI also provides `offer`,
`accept`, `withdraw` and `reconsider`; `offer --proposal ID` proposes revised terms.
Acceptance displays the exact terms for human confirmation. Run each command with
`--help` for its arguments.

## Private memory and readable history

`agent-services/Rome/civilization/memory.sqlite3` is authoritative private memory.
Events, immutable record revisions, pending requests, receipts and jobs are
transactional. The service generates `agents/Rome/SOUL.md`, `STRATEGY.md`,
`economy.md`, relationship files and a memory index from active typed records. Editing
these projections does not change canonical memory; use `memory_update`.

Observations retain their turn and source. Beliefs require observed sources and
uncertainty, with the service pinning evidence revisions. Private plans and
authenticated player statements remain distinct. “My army is nowhere near your
border” records a claim by its author, without erasing an earlier unit observation.

Fresh jobs receive selected active records and new events, with a default 12,000-character
private-memory allowance, rather than every prior turn summary. Older evidence is available through memory/history tools.
`agents/Rome/turn_0/summary.json` stores typed decisions and outcomes; `summary.md`
renders a concise account of turn 0 from those records, independently of final model prose;
`commands.jsonl` records detailed calls and controller-confirmed receipts, and
`receipts.jsonl` contains the exact persisted requests and responses. Communication
history is under `agents/Rome/communication/`, with permitted incoming events in
`chat.jsonl`. Public Codex reasoning summaries are recorded, not hidden reasoning.
The supervisor mirrors gameplay history under `runner/history/`.

Memory can be created, revised, replaced and retired. Consequential actions require
intent with a reason or an active, reviewed plan. Retirement removes current context
and Markdown while preserving revision history. See [structured memory](Structured-memory.md)
for schemas, examples and the [approved plan](Structured-memory-plan.md). This format
requires a fresh match; the previous freeform store is rejected.

## Interruption and emergency restore

Normal interruption keeps confirmed actions, messages and memory. Resume starts
fresh computation, reconciles uncertain requests and re-observes the current game.
It does not rewind an unfinished turn.

The scheduler automatically captures a complete checkpoint at the start of each
numbered game round. It briefly fences approvals and state commits across services,
settles outstanding requests, and publishes a manifest only after every component
is durable. Long-running model computation does not hold the mailbox or prevent
this boundary. Memory snapshots reference verified immutable records rather than
copying all transcripts.

For an explicit whole-match rewind:

```sh
uv run civ-agent match rollback --state-dir /tmp/civ-chat-1 --round 0
uv run civ-agent match resume --state-dir /tmp/civ-chat-1
```

Use the number displayed by `player_turn`; the initial round is 0, corresponding
to the `turn_0` directory. Stop or pause an active supervisor before rollback.
Restore replaces all participants together, rotates player credentials in
`players.json`, creates a new match epoch, regenerates projections and archives
the abandoned timeline for the operator. Old requests and credentials remain
invalid after restart. Model invocation/usage accounting and elapsed-time budgets
are retained. Increase exhausted limits explicitly on resume if needed.
If the original host used a custom `--players` path, restart it and any interactive
launchers with the newly written `STATE_DIR/players.json` after restoration.

A failed or interrupted restore leaves the match fenced. Repeat the same rollback
command to finish it; normal resume and host activation cannot release a partial
restore. The host and services must all be available. Keep their private databases
and `round-checkpoints` directories together: manifests reference those records.
This is coordinated recovery, not a standalone backup of every transcript.

## Validation and limits

Tests cover three-civilization privacy, responsive overlapping jobs, idle/burst
dispatch, stale peace/policy decisions, consent changes and expiration, uncertain
requests, concurrent memory updates, incomplete capture/restore and credential
revocation across restart. A live Rome/Greece run additionally verified actual
chat, consent, development and scheduled handoffs against host records and saves.
Both players founded capitals and completed their turns. Restoring round 0 then
removed those cities, messages, commitments and private notes from the active
timeline while retaining usage. Old credentials were rejected after a host restart.

The September 12, 2026 verification passed 214 Python tests (plus 75 subtests),
56 Kotlin tests, Ruff and Pyrefly. The live scenario validates this bounded slice;
it is not a completed match or an exhaustive action/privacy audit.

Further trade categories and a browser chat interface remain later work. Full
player API coverage and operating-system/container isolation also remain separate
milestones. HTTPS and scoped tools provide the communication boundary for those
deployments; they do not by themselves isolate files readable by a process's OS
account. See the [design](Async-chat-and-memory-plan.md) and
[coverage ledger](Player-api-coverage.md).
