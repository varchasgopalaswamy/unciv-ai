# Trade negotiation with paused gameplay

The Kotlin host owns each proposal's concrete terms. Players negotiate in chat,
then publish a binding proposal with typed items, quantities and durations.
Publishing records the author's consent. The other player accepts or declines the
stored proposal; acceptance never reconstructs terms from prose.

Only the current player can initiate a formal negotiation. Off-turn players can
invite them through chat. The first valid offer freezes all gameplay mutations,
including END_TURN, purchases, movement, war declarations and city changes. Other
deals cannot open or enact during that window. Chat, observations and private
memory remain available. The two players negotiate against the same game state.

This supports bilateral gold, gold per turn, luxury and strategic resources,
Open Borders, embassies, research agreements, defensive pacts and city transfers
between met, surviving human major civilizations. At peace, one side can
be empty for a gift. During war, both sides must include the offered Peace Treaty;
peace and economic consideration use the same proposal and consent flow. See
[war and peace](War-and-peace.md) for treaty rules.
Engine declarations of friendship use the same stored proposal and decision flow.
The [third-party war slice](Third-party-war-trades.md) also supports paid and joint
declarations against mutually known major civilizations using these same proposals.
The [agreement lifecycle](Agreement-lifecycle.md) tracks native durations, endings
and remaining relationship effects with durable events and private-memory review.
City-states, mediated peace with human targets and mod-only terms remain outside
this slice. The existing proposal deadline and withdrawal of an unaccepted
proposal are distinct from an enacted deal ending; no native control allows
voluntary early cancellation of these enacted agreements.

## Player tools

| Tool | Purpose |
| --- | --- |
| `trade_options(partner)` | Current trade-table maxima, engine durations, unavailable reasons and active agreements |
| `trade_read(proposal)` | Full canonical terms, consent, status, execution outcome and previous versions |
| `agreement_list()` | Player-local references, status and remaining turns for observed enacted rows and relationships |
| `agreement_read(agreement)` | Current and originally observed terms, native ending history and actual bilateral effects |
| `trade_offer(partner, give, receive, …)` | Publish terms; optionally counteroffer/revise an inspected proposal |
| `friendship_offer(partner)` | Publish consent to the engine's fixed Declaration of Friendship |
| `trade_decide(proposal, decision)` | Accept, decline, or (as author) withdraw the inspected terms |

`give` and `receive` in an offer are from the **proposing player's** perspective.
The host converts them to canonical clauses with explicit `from` and `to` players.
Both parties see the same clauses. Each item contains `type`, `name`, `amount` and
`duration`, using the offered names and durations from `trade_options`.

Treaties must occur identically on both sides. For a Research Agreement, `amount`
is the **gold cost paid by each player**, not an adjustable quantity. Both final
balances must cover that cost after immediate gold transfers; GPT cannot fund it.
The signing cost and durations are rechecked at acceptance. Open Borders and
embassies are directional: giving Open Borders permits the recipient to enter
your territory; giving an embassy reveals your capital area to the recipient.
`unavailableGive`/`unavailableReceive` explain missing prerequisites, while
`relationship` exposes native friendship, embassy, borders, research and pact status.

Shared engine outcomes use `confirmation.relationships`, keyed by civilization
ID. Each entry names `otherCivilizationId` and records that civilization's
`mayEnterOtherTerritory`, `hasEmbassyInOtherCapital`, war state and treaty
durations. For example, Rome granting Greece Open Borders records
`relationships.Greece.mayEnterOtherTerritory=true` with
`otherCivilizationId="Rome"`; the Roman entry can still be false. The same
confirmation appears in acceptance receipts, `trade_read`, communication state
and engine events. It remains the settlement snapshot through retries, later
agreements and checkpoint restarts. These shared records contain no
reader-relative "you" fields. Current `trade_options.relationship` and
`player_turn` views describe the player making the query.
Start a fresh match after updating: these typed confirmations use checkpoint
format version 4.

City terms use shared short trade IDs such as `tc1`, stable through ownership
changes and restart. These differ from an owned city's gameplay handle. The trade
dialog exposes the city's name and population, not its private production,
buildings or citizen assignments. Both participants see the same frozen city
description in a proposal. Acceptance rechecks ownership, capital/resistance rules
and pending city decisions. A sale removes the seller's gameplay authority and
updates territory and visibility immediately. The buyer gets the ordinary owned
city view. A resulting `CityTraded` alert exposes the full KeepCity/Liberate choice
through `RESOLVE_CAPTURED_CITY`; the inherited original-owner choice does not add
third-party negotiation clauses.

Friendship has concrete engine-owned terms (`friendship`, with the native 30-turn
duration), empty economic clauses, and the same `trade_read`/`trade_decide` consent
and retry rules. It cannot be counteroffered into different terms. A social chat
promise does not sign it. An incoming native friendship popup is also readable:
`RESPOND_FRIENDSHIP` takes its alert handle and `Accept` or `Decline`, after reading
the full decision. Accepting either path records a `DECLARE_FRIENDSHIP` engine
outcome and adds advisory notices to relevant private assessments.

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
cannot be amended or withdrawn retroactively. Each negotiation allows three
counteroffers shared by both players, excluding the initial offer. Each published
replacement counts, including an author's revision. At the limit, the latest terms
can still be accepted, declined or withdrawn. New proposal IDs cannot bypass an
open negotiation. There is no negotiation-specific token budget.

The default wall-clock deadline is five minutes after the first offer. Neither
counters nor restarts reset it. The host closes expired proposals, records a
terminal event and releases gameplay even if no player is connected. This deadline
is separate from the duration of an enacted treaty or resource agreement.

Configure subsequent negotiations at host launch:

```bash
uv run civ-agent host start --state-dir /tmp/civ-trade-cycle-1 --scenario trade-cycle \
  --trade-counteroffers 3 --trade-timeout 300
```

Settings persist in the checkpoint; omitted flags on resume retain them. Zero
counteroffers allows acceptance or rejection of the initial terms only. An open
negotiation retains the settings with which it began.

## Execution and recovery

Shared human/agent trade computation and messages remain asynchronous, while gameplay waits for the
negotiation to close. A gameplay offer call returns the settled canonical proposal.
The service waits through HTTPS long-polling outside its dispatch lock and memory
actor; separate communication jobs, including the initiating civilization's job,
handle counteroffers and decisions. Communication tool calls return immediately
after their own command. No model polls or repeatedly restarts to wait for replies.
Ordinary gameplay timeout accounting excludes this service wait; heartbeat leases,
the negotiation deadline and whole-match time limits still apply.

Routine communication prepares `chat_read(includeContext=true)`. Its top-level
`turn` and compact `context` contain that player's scenario role, own economic totals and known
diplomatic relations. Exact quantities and terms come from the trade tools. The
full `player_turn` view remains available on demand for questions that need map
or city detail; negotiation does not require fetching its action menus.
Gameplay startup omits this extra context because it already prepares the turn
overview. Both use current offers, pending trade IDs and active agreement summaries
by default. `includeHistory=true` explicitly requests the retained ledger;
selected old messages/terms remain available through history and detail tools.
Native AI offers belong to the gameplay agent. Their proposal and completion
events are recorded without communication wake-ups, and native offers are omitted
from communication inboxes. Native agreement endings also wait for gameplay.
Both new native offers and responses to existing ones require a gameplay job;
the gateway enforces this even when a communication job is already running.
Gameplay's prepared `chat_read` retains their pending IDs, alongside the turn
overview's native decision blockers. No native trade terms or legality rules change.

If another job changes a required command observation before dispatch,
the gateway returns `STALE_ASSUMPTIONS` with `applied: false`, `recoverable: true`,
the changed record keys and `recovery.reads`. No command was sent. The agent reads
those records, reconsiders the affected action and either makes a fresh decision
or skips work already completed by the other job. A second stale rejection of the
same operation stops the run with `REPEATED_STALE_ASSUMPTIONS` and durable
diagnostics; it cannot be deferred to continue the benchmark. Private assessments
and their review notices do not independently block commands. Replaying the
rejected call returns the same rejection even after a refresh;
it cannot execute an obsolete decision. Uncertain transmitted requests still
stop the job and require receipt reconciliation.

If gameplay and communication both accept the same shared proposal, a stale acceptance
first checks its current status with the host. When that exact proposal version
is already `ENACTED`, it reaches the native handler, which returns the existing
outcome without repeating transfers. Pending offers, changed versions and other
decisions still use the stale-state recovery checks above.

The non-current participant can counteroffer or consent without switching Unciv's
active player. A new negotiation requires the current player and running gameplay.
Operator communication barriers fence decisions alongside chat. An interrupted
gameplay job resumes from durable receipts and the existing deadline; it does not
reopen the negotiation. Emergency rollback restores the whole shared checkpoint.

The host reuses `TradeLogic` for the desktop's offer table and transfer effects,
and `TradeEvaluation` for engine validity. It additionally requires the supported
categories, exact current maxima and durations, positive nonduplicate quantities,
valid participants and safe integer balances. Acceptance still rechecks legality
under the executor lock as a final integrity check. Normal gameplay cannot capture
the offered city, pillage its improvements or spend promised gold during negotiation.
Acceptance or closure releases the hold atomically with the receipt and checkpoint.
Gameplay must re-observe after the hold; a command prepared before waiting is
returned for reconsideration, never automatically executed against new state.
New friendship does the same. Accepted peace or friendship also invalidates stale
hostile actions and gameplay observations.

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
their notes. Rewinding an interrupted turn restores these host records, the engine
and each player's memory together under a new epoch. Completed turns stay committed.

## Playable verification scenario

[`--scenario trade-cycle`](../scenarios/Trade-cycle-scenario.md) is the only gameplay scenario.
It covers eleven staged negotiations, every supported bilateral item, intentional
decline/withdrawal/expiry, three counters, funded research and a two-way city
exchange. Its runbook includes launch commands, the checkpoint verifier and
scripted recovery checks.

Human commands include `civ-agent trade list`, `options`, `read`, `offer`,
`friendship`, `accept`, `decline` and `withdraw`. `offer` takes JSON arrays through
`--give-json` and `--receive-json`, with optional `--proposal` for a counteroffer.
Human acceptance displays the current concrete terms and pins that version
before asking for confirmation.

## Verification

`BilateralTradeProbe` checks exact treaty shape/cost, prerequisite ordering,
directional embassies, off-turn friendship and trade acceptance, city ownership
and fog changes, stale seller commands, funded and underfunded research,
duplicate/forged/excluded terms, restart replay and whole-round restoration.
Engine tests cover owned friendship popup responses and traded-city disposition
authority, including stale decisions and original-owner war restrictions.

`NegotiationProbe` verifies the shared counteroffer cap, blocked game commands and
player commands, private pause metadata, terminal decisions, persistent deadlines,
unattended expiry and resumed gameplay. `EngineTradeProbe` performs authenticated
HTTPS negotiation while Rome retains the same gameplay activation. Greece counters
out of turn, and Rome accepts through communication. It checks exact canonical
clauses, immediate balances, GPT rates, imported/exported resources, engine deal
durations, subsequent handoffs, pending-offer restart and exact retries. It also
covers stale counteroffers and gameplay observations, decline/withdrawal, invalid
durations/duplicates, spent gold, lost resources, expiry, war and round restoration.
Injected checkpoint failures before and after file replacement verify consistent
native state, ledger and receipts after restart.

The three-player communication probe checks hidden proposal access, private
streams/outcomes and unchanged unrelated player revisions. Python tests cover
independent jobs' inspected versions, policy invalidation, narrow tools, exact
receipt recovery, immutable evidence and human confirmation races. The same HTTPS scheduling test also negotiates peace terms off turn. These are
scripted checks; live-model runs are separate acceptance exercises, and full
trade/UI parity remains incomplete.
