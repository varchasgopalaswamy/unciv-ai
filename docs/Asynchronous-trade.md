# Asynchronous economic trade

The Kotlin host owns each proposal's concrete terms. Players negotiate in chat,
then publish an economic proposal with typed items, quantities and durations.
Publishing records the author's consent. The other player accepts or declines the
stored proposal; acceptance never reconstructs terms from prose.

This slice supports bilateral gold, gold per turn, luxury resources and strategic
resources between met, surviving human major civilizations at peace. One side can
be empty for a gift. Existing peace negotiations retain their separate commands.
City transfers, open borders, embassies, research agreements, city-states and
third-party clauses remain outside this slice.

## Player tools

| Tool | Purpose |
| --- | --- |
| `trade_options(partner)` | Current trade-table maxima, engine durations, unavailable reasons and active agreements |
| `trade_read(proposal)` | Full canonical terms, consent, status, execution outcome and previous versions |
| `trade_offer(partner, give, receive, …)` | Publish terms; optionally counteroffer/revise an inspected proposal |
| `trade_decide(proposal, decision)` | Accept, decline, or (as author) withdraw the inspected terms |

`give` and `receive` in an offer are from the **proposing player's** perspective.
The host converts them to canonical clauses with explicit `from` and `to` players.
Both parties see the same clauses. Each item contains `type`, `name`, `amount` and
`duration`, using the offered names and durations from `trade_options`.

The model supplies only the proposal ID and decision when accepting:

```json
{"proposal":"d9bc1p2", "decision":"accept"}
```

Python attaches the exact version inspected by that job with `trade_read`, along
with credentials, epoch and a durable request ID. A different job reading a
counteroffer does not update the first job's inspected version. Reading chat does
not implicitly approve new terms. A stale response requires another inspection
and decision.

A counteroffer or revision creates a new immutable version, superseding the
pending version and recording only its new author's consent. Accepted versions
cannot be amended or withdrawn retroactively. The default proposal expiration is
the current game turn plus three; `expiresTurn` sets a different absolute deadline.
That deadline concerns acceptance, not the duration of the resulting engine deal.

## Execution and recovery

Trade computation and messages remain asynchronous. Both gameplay and
communication jobs have trade tools; communication jobs retain no permission to
move units, build, attack or end a gameplay turn. There is no model polling.

Routine communication starts with `chat_read`. Its compact `context` contains
the current turn, that player's scenario role, own economic totals and known
diplomatic relations. Exact quantities and terms come from the trade tools. The
full `player_turn` view remains available on demand for questions that need map
or city detail; negotiation does not require fetching its action menus.

If another job changes a relevant private plan or observation before dispatch,
the gateway returns `STALE_ASSUMPTIONS` with `applied: false`, `recoverable: true`,
the changed record keys and `recovery.reads`. No command was sent. The agent reads
those records, reconsiders the affected action and either makes a fresh decision
or skips work already completed by the other job. A repeated conflict defers that
action while other gameplay continues. Unrelated messages do not invalidate a
plan. Replaying the rejected call returns the same rejection even after a refresh;
it cannot execute an obsolete decision. Uncertain transmitted requests still
stop the job and require receipt reconciliation.

The host's dedicated bilateral-consent path runs during either player's gameplay
turn without switching Unciv's active player. It also remains available during
the scheduler's gaps between gameplay activations. Operator communication barriers
and whole-match pause fence it alongside chat. This is an explicit scheduling
extension to hotseat, not general permission to take an inactive player's turn.

The host reuses `TradeLogic` for the desktop's offer table and transfer effects,
and `TradeEvaluation` for engine validity. It additionally requires the supported
categories, exact current maxima and durations, positive nonduplicate quantities,
valid participants and safe integer balances. Pending proposals reserve no assets;
acceptance rechecks current supply under the same executor lock as all other
mutations. A competing expense or accepted deal can therefore invalidate an offer.
War or enacted peace invalidates pending ordinary proposals between those players.

Validation, complete engine transfer, consent, outcome, recipient events and the
request receipt share one serialized operation and atomic native checkpoint. Only
an `ENACTED` result with an engine outcome confirms execution. An unavailable
proposal transfers nothing and returns a reason; an invalidated proposal cannot
revive when resources later recover. Repeated acceptance cannot transfer twice,
even with a new request ID. Unexpected mutation or checkpoint failure halts player
access until recovery from a durable checkpoint.

Proposal versions live at `communication.trades.proposals` inside the private host
checkpoint. Recipient events, observed terms and confirmed receipts enter the
existing private memory/history system. `chat_read` includes compact trade IDs and
statuses; full details are on demand. Agents can cite `observation:trade/<ID>` in
their notes. Whole-match rollback restores these host records, the engine and each
player's memory together under the existing new-epoch recovery protocol.

## Playable verification scenario

Use a fresh directory and four terminals:

```sh
uv run civ-agent host start --state-dir /tmp/civ-trade-1 --scenario async-trade
uv run civ-agent agent-server start Rome --state-dir /tmp/civ-trade-1
uv run civ-agent agent-server start Greece --state-dir /tmp/civ-trade-1
uv run civ-agent match start --state-dir /tmp/civ-trade-1 --max-turns 3 --max-invocations 32
```

Wait for host and agent services to report ready before starting the match. Model
and reasoning options work as in the other scenarios. Both civilizations start
with a capital, connected surplus resources, positive income and 1,000 gold.
Grassland and a Granary provide enough food for five citizens. Legal Barracks and
Monument queues are prepared, with Avoid Growth enabled. The fixture creates
peaceful contact directly and carries no previous war or peace-negotiation history.

Rome's first gameplay job offers one Silk for four gold per turn. Communication
jobs handle Greece's counteroffer of two gold per turn and twenty immediate gold,
then Rome's acceptance. Gameplay jobs continue their turns without duplicating
these responses. Both
players then inspect the enacted deal, update their economic and relationship
notes, and continue scheduled turns without arranging another deal.

Use a new save for this revised setup. Restart the host and both agent services
to load the updated code before rerunning the exercise.

Inspect authoritative player-visible records while the host is running:

```sh
uv run civ-agent trade list Rome --state-dir /tmp/civ-trade-1
uv run civ-agent trade read Rome --state-dir /tmp/civ-trade-1 --proposal DEAL_ID
uv run civ-agent trade options Greece --state-dir /tmp/civ-trade-1 --partner Rome
```

The human CLI also provides `offer`, `accept`, `decline` and `withdraw`. `offer`
takes JSON arrays through `--give-json` and `--receive-json`, with optional
`--proposal` for a counteroffer and `--expires-turn` for its deadline. Human
acceptance displays the current concrete terms and pins that version before
asking for confirmation.

Read `agents/<civilization>/economy.md`, relationship notes and communication
history to assess reasoning. Check the native save, host proposal/outcome and
active agreements to establish what actually happened. Proposal clauses retain
the original duration; native active agreements show remaining duration, following
Unciv's per-player turn accounting. `host inspect` opens an exported save in Unciv.

## Verification

`EngineTradeProbe` performs authenticated HTTPS negotiation while Greece retains
the same gameplay activation. Rome accepts out of turn. It checks exact canonical
clauses, immediate balances, GPT rates, imported/exported resources, engine deal
durations, subsequent handoffs, pending-offer restart and exact retries. It also
covers stale counteroffers and gameplay observations, decline/withdrawal, invalid
durations/duplicates, spent gold, lost resources, expiry, war and round restoration.
Injected checkpoint failures before and after file replacement verify consistent
native state, ledger and receipts after restart.

The three-player communication probe checks hidden proposal access, private
streams/outcomes and unchanged unrelated player revisions. Python tests cover
independent jobs' inspected versions, policy invalidation, narrow tools, exact
receipt recovery, immutable evidence and human confirmation races. These are
scripted checks; a live-model run of this new scenario is a separate acceptance
exercise, and full trade/UI parity remains incomplete.
