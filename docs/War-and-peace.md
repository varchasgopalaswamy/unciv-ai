# War declarations and peace negotiations

The player API supports major-civilization war declarations and human peace
negotiations over the existing authenticated HTTPS transport. Engine operations
are shared with desktop diplomacy controls. This slice covers mutual peace,
gold, gold per turn, and luxury/strategic resources; city transfers, third-party
agreements, friendship, denunciation and demands remain outside its scope.

## Observations and choices

`player_turn.diplomacy.knownCivilizations` contains met, surviving major
civilizations. Each entry includes `status`, `atWar`, `peaceTreatyTurns`,
`declareWar`, `declarationConfirmation`, `negotiatePeace`, and `peaceOptions`.
`agreements` retains the bilateral current-trade overview rows: terms with positive
duration are ongoing obligations, while one-time consideration remains visible
until the agreement is removed by the engine. Actions report `available` and `unavailableReasons`. Treaty restrictions,
negotiation cooldowns, game rules and the active player's authority are checked
again on execution. Confirmation text includes broken-promise and defensive-pact
warnings; unknown pact partners are unnamed, matching the shared desktop warning.

`peaceOptions.give` and `.receive` are from the authenticated player's perspective.
Each term includes its exact `type`, `name`, current maximum `amount`, fixed
`duration`, and desktop `displayText`. Resource terms include `ownedByYou`.
Quantities are exposed only when the player can inspect the corresponding peace
trade table, including an incoming peace offer. At peace this API does not expose
an opponent's treasury or resource inventory as negotiation options.

`incomingOffers` contains player-owned requests with durable opaque handles,
the requesting leader's full message, both offer columns, labels, and legal
accept/reject/counteroffer choices. `outgoingOffers` shows only the player's own
sent proposals and a retraction choice. Neither view exposes other players'
negotiations with third parties. Unsupported terms retain their type/amount/duration
and an explicit generic description, without raw city or other hidden IDs. Such
requests can be rejected, but acceptance and counteroffers are unavailable.

## Commands

All commands need the usual `requestId`, `epoch`, and current `revision`.

| Command | Additional fields and behavior |
| --- | --- |
| `DECLARE_WAR` | `target`: known `civilizationId`; `confirmWar: true` after reading `declarationConfirmation`. Applies the normal bilateral war effects; receipt `feedback` includes the defender's response text shown by desktop diplomacy. |
| `PROPOSE_PEACE` | `target`: known `civilizationId`; `peaceTerms` as below. Sends a request for the other player's turn. Retract an existing outgoing offer before replacing it. |
| `ACCEPT_PEACE` | `target`: incoming offer handle. Revalidates current terms and applies the trade once. Receipt `feedback` contains the shared desktop acceptance message and farewell label. |
| `REJECT_PEACE` | `target`: incoming offer handle. Applies the normal refusal and removes that request. Also available for unsupported requests from known major civilizations. |
| `COUNTER_PEACE` | `target`: incoming offer handle; `peaceTerms`. Validates the complete replacement before consuming the incoming request and sending the counteroffer. Invalid drafts leave the request intact. |
| `RETRACT_PEACE` | `target`: known `civilizationId`. Removes the player's pending outgoing offer. |

For example, construct this payload using the **current offered** treaty duration
and gold term duration; these numbers are illustrative, not defaults:

```json
{
  "requestId": "rome-peace-1",
  "epoch": "<current epoch>",
  "revision": "<current revision>",
  "kind": "PROPOSE_PEACE",
  "target": "Greece",
  "peaceTerms": {
    "give": [
      {"type": "Treaty", "name": "Peace Treaty", "amount": 1, "duration": 10},
      {"type": "Gold", "name": "Gold", "amount": 25, "duration": -1}
    ],
    "receive": [
      {"type": "Treaty", "name": "Peace Treaty", "amount": 1, "duration": 10}
    ]
  }
}
```

Both sides must contain exactly one Peace Treaty. Amounts must be positive and
within current quantities; durations must match the speed-adjusted engine offers.
Duplicate terms, asymmetric treaties, arbitrary durations and unsupported terms
are rejected. The engine revalidates affordability when an offer is accepted.
An offer does not reserve gold or resources; acceptance can become unavailable.

The opponent receives `WarDeclaration` with the declaring leader's caption,
announcement and nation-specific declaration text. Both equivalent desktop
responses are included in `content`; `ACKNOWLEDGE_ALERT` dismisses the message
without changing the war a second time.

## Reading, scheduling and recovery

The agent must fetch and read the current `player_turn` before declaring war or
responding to an incoming offer, just as for informational popup acknowledgement.
A command receipt's revision alone fails with `OBSERVATION_REQUIRED`. War also
requires `confirmWar: true`. This gate establishes that the player snapshot was
generated; the prompt and recorded transcript provide evidence of reading, not a
proof of comprehension or network delivery.

Incoming requests block end turn until resolved. Outgoing proposals do not: the
player should finish its turn and hand off so the opponent can decide. The runner
continues to schedule by the host's committed turn changes and never chooses a
diplomatic response itself. New gameplay prompts explain this workflow.

The checkpoint stores incoming handles with their exact request identities and
successful receipts with game effects. Same-version restart preserves handles,
epoch and exact replay. Unfinished decisions require another observation after
restart. Replaying a committed acceptance returns the original receipt, even on
a later turn, without repeating gold/resource transfer or peace effects. Start a
new save for this version; migration from older checkpoint schemas is not included.

Verification uses engine `PlayerDiplomacyOperationsTest` and
`PlayerPeaceOperationsTest`, host privacy/authority probes, and
`WarPeaceRunIntegrationTest`. Its HTTPS players meet through legal movement,
declare war, read the opponent's declaration, wait through normal cooldown turns,
propose/retract/reject/counteroffer peace, restart before and after acceptance,
retry the exact accepted command and continue scheduled rounds. These are scripted
clients; live model play and a visual desktop acceptance run remain separate.
