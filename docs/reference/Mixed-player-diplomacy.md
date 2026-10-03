# Mixed-player diplomacy

## Scope

Player-to-player diplomacy supports games containing human players, external
agents, and Unciv's built-in AI. Direct city-state interactions remain a separate
slice; decisions addressed by another major civilization about a protected or
allied city-state are included.

- Human–human, human–agent and agent–agent trades use one shared negotiation service
  with native UI and structured API adapters.
- Only agent–built-in-AI trade and friendship requests use the native bridge.
- Native requests await the recipient's native turn without pausing gameplay.
- The gameplay agent owns offers, responses, counteroffers, withdrawals and
  friendship requests involving the built-in AI. These proposals, outcomes and
  agreement endings remain recorded without waking a communication job. Turn-start
  context includes pending native offers; communication inboxes include shared
  human/agent offers. Communication jobs can inspect native terms, but their action
  flags are unavailable and the gateway rejects attempts to act on them.
- Trade-menu terms, including peace and permitted mediated peace, stay in trade
  negotiation. They are never chat native actions.
- Chat can carry explicit denouncement, demand, and owned diplomatic-decision
  actions. Ordinary text and social promises never acquire engine effects.
- Shared engine operations own legality, decision text, responses, and effects.
  The host owns visibility, authority, exact request receipts, and delivery.
- Native AI decisions use existing engine logic; the host never supplies consent
  on behalf of a player or invents penalties for breaking social promises.

## Delivery and verification

1. Shared native denouncement, demand, incident-decision and observation operations,
   reused by desktop controls, with independent engine tests.
2. Host diplomacy inbox and explicit chat actions, with observation/turn checks,
   recipient-private results, atomic receipts and restart-safe references.
3. Native trade/friendship bridge integrated into the existing proposal history,
   including incoming offers, decline, counteroffers, withdrawal, invalidation,
   native outcomes and permitted third-party peace.
4. Python tool schemas, prompts, memory refresh and human interface support.
5. Five-major mixed-controller acceptance fixture, recovery checks, desktop
   comparison, and a live agent exercise. Record actual results and limits here.

## Interfaces and timing

Read `player_turn` to discover permitted known-major actions, native AI opinion
modifiers, native promises and their durations, and each owned alert's
`nativeDiplomacy` event, sender, recipient and options. The model view lists outgoing
options under `native.availableDemandsToSend`, with a `type`, `recipient`,
availability and unavailable reasons. These are actions the reader may send;
they are not received requests or promises. Native stock dialogue is omitted
from the model view; see the [flavor text audit](../archive/reports/Model-flavor-text-audit.md).
War-producing responses carry `declaresWar`, native warnings
and any applicable nonblocking social peace advice. Native consequences remain
Unciv's: a rejection described as “You'll pay for this!” need not declare war.

Agent-facing examples, after observing the current turn:

```json
{"tool":"chat_send","arguments":{"to":"Greece","text":"Rome formally denounces your conduct.","nativeAction":{"kind":"DENOUNCE","target":"Greece"}}}
{"tool":"chat_send","arguments":{"to":"China","text":"Please stop settling near us.","nativeAction":{"kind":"ISSUE_DEMAND","target":"China","selection":"DoNotSettleNearUs"}}}
{"tool":"chat_send","arguments":{"to":"Greece","text":"Acknowledged.","nativeAction":{"kind":"RESPOND_DIPLOMACY","target":"<owned alert handle>","selection":"Acknowledge"}}}
```

Use the observed outgoing demand `type` or incoming choice `name` for `selection`;
unavailable choices stay unavailable. The
agent gateway supplies the exact observed revision and durable request identity.
Direct `player_communication` clients supply `epoch`, `requestId`, and
`nativeAction.revision`. A private message's recipient must match the native
action's actual target. Failed validation produces neither the message nor an
effect; retries recover the original committed result. Native results are separate
`diplomacy` events, and only those authenticated results become memory evidence.
Plain chat remains a statement even when it uses identical words.

| Pair / operation | Route | When effects occur |
| --- | --- | --- |
| Human–human, human–agent or agent–agent trade/friendship | Shared proposal and exact consent | Both participants accept the same version; shared negotiation pauses gameplay |
| Agent proposes trade/friendship to built-in AI | Same tools, proposal `mode: NATIVE_AI` | Native AI decides on its turn; `PENDING` returns immediately, without a gameplay pause or wall-clock deadline |
| Built-in AI offers/counters to agent | Native request imported into the proposal history | Agent accepts/declines/counters on its turn; a counter sent back waits for the AI's turn |
| Denounce, issue demand, respond to a diplomatic alert | Explicit `chat_send.nativeAction` | On the acting player's turn, with a fresh observation and engine validation |
| Peace and mediated peace | Trade tools | Existing native trade rules; no chat peace action and no consent invented for human third parties |

Native history preserves withdrawal, decline, actual counters, invalidation, and
enactment separately. Restart reattaches result observers using persisted request
IDs. A disappeared request never implies acceptance. One native trade and one
native friendship request can be pending concurrently for a pair. Unsupported or
hidden native terms are presented without those terms and can only be declined.

Native friendship offers *received* from the built-in AI continue to use the
existing owned friendship decision. Direct city-state commands remain separate;
major-civilization messages about attacking/bullying a protected or allied
city-state use native incident choices and influence effects.

## Human controls

The trusted `players.json` configuration accepts `controller: "HUMAN"` or
`"AGENT"`; omission retains the existing `AGENT` default. This host-side setting
selects native-bridge eligibility and is checked against the checkpoint on resume.
Do not issue a player credential to the built-in AI. The mixed launcher creates
agent grants for Rome/Greece and human grants for France/Egypt.

One local human can use native desktop controls and the integrated Chat/Trades/
Promises/Agreements panel via `game start --config examples/human-game.toml`.
Shared human–agent negotiations have no response deadline, preserve exact consent,
and suspend gameplay inference while waiting. Trade and Negotiate Peace open
Unciv’s native four-column trade table. Chat → Trades opens that same table and
links proposal history; friendship remains in the shared panel. Native item
controls prepare drafts, and the shared service publishes, counters, accepts,
declines or withdraws them. The custom trade editor has been removed. Built-in AI
and city-state diplomacy keep their native queued controls. Public/private messages and social promises use the
same player-bound communication API; no operator data is loaded into the panel.

The existing `civ-agent chat` and `civ-agent trade` commands remain useful for
scripted scenarios. An explicit native action prints the current
options/consequences and asks the human to confirm it:

```sh
civ-agent chat send France --state-dir /tmp/mixed-diplomacy \
  --to Greece --text 'France formally denounces your conduct.' \
  --native-action DENOUNCE --target Greece
```

The scheduler activates the human seat without starting a model or applying a
model turn timeout. Opening/periodic reviews remain agent-only quiet phases;
human text messages queue privately until all reviews finish. The desktop labels
queued messages and preserves unsent drafts per conversation across map navigation.
Background actions leave chat focus and cursor position intact. Drafts are local
to the desktop session and cleared on rewind.
Closing the desktop stops the whole match; interrupted-turn resume rewinds native
actions, communication and agent state together. See the
[launcher guide](../guides/Full-vanilla-run.md#play-a-civilization-yourself).

## Verification

### Human chat during reviews and background turns — October 2, 2026

- **22 host checks passed**: communication, operator-control and input-schema suites.
  `HumanChatReviewProbe` covers private/public queueing, frozen game state, recipient
  privacy and wakeups, ordered delivery, duplicate requests, checkpoint restart,
  and discarded-attempt rewind. Native actions, trades and agent sends remain
  blocked during review; ordinary pauses do not admit new queued chat.
- The rendered `ManagedDesktopProbe` exercises keyboard input after authority
  changes and other players' turns, preserving focus, selection and caret. Public,
  Roman and Greek drafts survive switching recipients and leaving/reopening chat.
  Human review-time chat is displayed as queued, hidden from recipients until
  release, then delivered to the intended recipient. Rewind clears unsent drafts.
  Evidence: `/tmp/unciv-human-chat-02/COMPLETE` and adjacent screenshots.

### Unified native trading — October 2, 2026

The shared engine/service architecture is documented in
[Kotlin architecture](../architecture/Kotlin-architecture.md#shared-trade-execution).

- `UnifiedTradeProbe` passed all four ordered human/agent pairings. Every Vanilla
  item went through preparation, proposal, counteroffer and acceptance: cash/GPT,
  luxury/strategic resources, embassies, open borders, both city directions, funded
  research agreements, defensive pacts, peace, third-party war and mediated peace.
  Native `TradeView` drafts and structured drafts produced identical canonical
  items. Checks compare engine effects, controller-independent results, durations,
  city ownership, stale consent, duplicate acceptance and nonmutating preparation.
- The rendered production desktop/HTTPS scenario passed in
  `/tmp/unciv-unified-trade-desktop-04/` (`COMPLETE` and screenshots). It used native
  numeric/item controls and lifecycle buttons for publishing, human counteroffers,
  withdrawal, decline and off-turn acceptance. An open acceptance popup rejected
  consent after a newer counteroffer arrived. Turn rewind restored gameplay and
  communication together, followed by a complete round and the quiet-review barrier.
- The focused suites passed **33 host tests, 44 engine tests and 44 Python tests**.
  Independent engine regressions exercise detached edits, paired treaties, exact
  combined research affordability, explicit funding, duplicate/overflow/stale
  terms, native queued acceptance and trade effects. The host regression suite
  covers private terms, lost responses, checkpoint failures, restart and rewind.
- Python draft-tool checks pass for gameplay and communication jobs: returned final
  terms remain explicit, publish arguments validate, and preparation sends no offer.

This establishes the tested Vanilla transactions, not mod support or remote human
clients. The rendered run uses one local human and scripted peers; it spends no
model tokens. The earlier paid one-round check below tested the local-human
scheduler, before this native-editor replacement.

### Authoritative local desktop — October 2, 2026

- The scripted production desktop/HTTPS check passed native founding and research,
  private-message isolation, human offers and exact counteroffer consent, off-turn
  acceptance, the quiet-review barrier, and a complete France → Rome → Greece →
  built-in Egypt round. Rewinding restored native actions and messages together;
  credential rotation preserved the human seat. Rendered captures are retained in
  `/tmp/unciv-managed-desktop-10/`. This also verifies that another conversation
  stays unread, ordinary refreshes keep unsent drafts, and rewind discards them.
- A live round in `/tmp/unciv-managed-live-02/` passed with scripted human France,
  Rome/Greece using `gpt-6-luna` at medium reasoning, built-in Egypt and one city-state.
  Both opening reviews and both agent turns completed. It stopped at the requested
  one-round limit (turn 1, handoff 3). All three turn accounting records completed;
  France recorded native actions and no model job.
- 21 host communication/recovery/launch tests and 10 engine operation tests passed. Python
  checks cover launcher roles, controller-preserving recovery, human waits longer
  than model timeouts, and stopping or halting during human input without starting
  a model for that seat.

Repeat the rendered check with a **new** absolute output directory:

```sh
./gradlew runManagedDesktop -PmanagedDesktopDir=/tmp/unciv-desktop-check
```

Add `-PmanagedLive=true` for the opt-in paid one-round model test. These checks need
a desktop display. Long matches, exhaustive native UI actions, and remote human
clients remain outside this acceptance result.

### Earlier mixed-controller coverage

September 19, 2026:

- Full automated baseline passed: 79 host tests, 166 related engine player-operation
  tests, and 421 Python tests with 90 subtests. Subsequent focused runs passed
  18 host tests, eight engine tests and 64 Python tests with 26 subtests, including
  the added controller-authority and diplomatic memory regressions.
- The retained scripted HTTPS scenario passed in
  `build/verification/mixed-diplomacy-20260919-5/`: five majors plus a city-state;
  shared human negotiation; native demand/reply and denounce/replay; pending native
  trade/friendship through restart; native incoming offers/counters/withdrawal;
  invalidated terms; hidden target redaction; a full native turn cycle; and
  native-permitted mediated peace while a human third party remains at war.
  Denunciation and military-ultimatum war responses display an accepted social
  peace promise, permit native-legal war, and leave that social promise recorded.
- The same scenario checks failures before and after atomic checkpoint replacement:
  an uncertain write halts the runtime, reloading preserves the correct side of
  the boundary, and receipt replay cannot duplicate the message or native effect.
- Desktop acceptance passed with eight inspected rendered screenshots and native
  before/after saves in `build/verification/mixed-desktop-20260919/`. Actual UI
  buttons exercised a settlement demand, acceptance, denouncement, acknowledgement,
  and a queued AI gift. Its native AI resolution uses an isolated automation call;
  the scripted HTTPS scenario separately covers actual turn advancement.
- Ruff, Pyrefly and host formatting checks passed. No Python distribution build
  is required or used.

### Live result

The live exercise passed all 19 read-only verification checks. The retained state
is `/tmp/unciv-mixed-live-20260919-2/`, with `mixed-live.json`,
`mixed-live-verification.json`, player receipts, service histories and private
memory databases. It reached native turn 2, handoff 8, with Rome next. All four
gameplay and seven communication invocations completed using `gpt-5.6-luna` with
medium reasoning. The final checkpoint is paused and all three services are stopped.

- Rome issued the native settlement demand to China. China refused through native
  automation, and Rome read and acknowledged the reply without war.
- Rome explicitly denounced Greece once. Greece acknowledged the native message
  and discussed it in ordinary chat; the players remained at peace.
- France accepted Rome's exact 10-Gold gift through the shared human trade CLI.
  This live offer was accepted without a counter; counters are covered by the
  scripted scenario.
- Rome's 30-Gold gift to China returned `PENDING` without pausing gameplay, then
  became `ENACTED` on China's native turn. Greece's friendship request likewise
  returned immediately and was natively `DECLINED`, with the corresponding native
  flag and no friendship. Neither result was selected by the test driver.
- Both agents revised their private diplomatic plans after observing the results.
  Private native AI proposal references were absent from France/Egypt's inboxes.

The first attempted run is retained at `/tmp/unciv-mixed-live-20260919-1/`; its
driver incorrectly assumed the opening gift's direction. The successful game was
paused while correcting driver turn-archive finalization and resume handling,
then continued from the same checkpoint. Those interruptions are recorded in
`mixed-live.json`; they did not roll back the native outcomes or supply agent
consent. A prepared city-state capital also replaced an invalid starting-settler
position discovered by the earlier scripted fixture run.

Limits: France/Egypt were scripted human-controller stand-ins, not people at the
desktop. Live process restart, fault injection, mediated peace, native counteroffers
and direct city-state actions were not exercised by these models. Recovery,
mediated peace and counters have separate scripted evidence; eight desktop captures
provide scoped visual acceptance, not full UI/API parity.

Status: implemented and accepted for this scope. The engine revision is recorded
by the `vendor/Unciv` submodule; the implementation and acceptance tooling live in
their respective repositories.

### Reproduce

Scripted native/recovery coverage, using the selected engine checkout and assets:

```sh
./gradlew verifyMixedDiplomacyScenario \
  -PmixedScenarioDir="$PWD/build/verification/mixed-diplomacy" --console=plain
./gradlew runMixedDiplomacyDesktop \
  -PmixedDesktopDir="$PWD/build/verification/mixed-desktop" --console=plain
```

Live exercise, in separate terminals with a fresh state directory:

```sh
civ-agent host start --state-dir /tmp/mixed-diplomacy \
  --scenario mixed-diplomacy --port 8791 --trade-timeout 600
civ-agent agent-server start Rome --state-dir /tmp/mixed-diplomacy \
  --port 8891 --model gpt-5.6-luna --reasoning-effort medium
civ-agent agent-server start Greece --state-dir /tmp/mixed-diplomacy \
  --port 8892 --model gpt-5.6-luna --reasoning-effort medium
.venv/bin/python tests/live_mixed_diplomacy.py --state-dir /tmp/mixed-diplomacy \
  --model gpt-5.6-luna --reasoning-effort medium
.venv/bin/python tests/verify_mixed_diplomacy_live.py --state-dir /tmp/mixed-diplomacy
```

Wait for the host and both services to report ready before starting the driver.
Rome/Greece make model decisions; France/Egypt are scripted human-controller
stand-ins, and China runs native automation. The driver pauses the match and
disconnects agent communication at exit; stop all three server processes afterward.
Use the driver's `--resume` option after a driver interruption to continue the
same saved exercise; do not start a second driver for the same match.
Never publish the state directory: it contains credentials and private player data.
