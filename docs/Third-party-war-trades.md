# Third-party war trades

Players can pay a known major civilization to declare war on another major, join
an existing war, or agree to a joint declaration. Both negotiators must know the
target, which must still be alive. The engine's trade table and peace-treaty
restrictions remain authoritative. Turn ownership stays with the current player
while the existing negotiation hold permits the other player's consent.

This slice uses the existing `TradeLogic`, `TradeEvaluation` and
`PlayerDiplomacyOperations` from the pinned engine. The desktop trade screen already
uses these operations. No additional engine revision is required.

## Terms and observations

`trade_options(partner)` exposes native `WarDeclaration` entries in `give` and
`receive`. Copy the target civilization ID, amount **1** and duration **-1** from
the option. The duration denotes an immediate declaration; it is not a commitment
to remain at war for a negotiated number of turns.

For example, Rome can propose:

```json
{
  "partner": "Greece",
  "give": [{"type": "Gold", "name": "Gold", "amount": 30, "duration": -1}],
  "receive": [{"type": "WarDeclaration", "name": "France", "amount": 1, "duration": -1}],
  "intent": {"reason": "Recruit Greece into the war against France"}
}
```

Acceptance makes **Greece** declare on France and transfers the offered gold.
Adding the same declaration to `give` commits Rome too. Canonical clauses retain
explicit `from` and `to` directions through counteroffers; the declarer is the
clause's `from` civilization, and the target is `item.name`.

Each war option includes the native display text. Own options include
`war.yourDeclarationConfirmation`; `trade_read` includes `yourWarDeclarations` for
the reader's own declarations. These show the shared diplomacy warning about
native promises and defensive pacts. Unknown pact partners remain unnamed, as in
the desktop. A reader cannot inspect another negotiator's private warnings.
These are current per-player warnings, separate from the immutable canonical terms.
Imported UI requests still hide war targets the reader has never met, even though
the `WarDeclaration` category is now supported.

The shared `ENACT_TRADE` outcome adds `wars`, containing the explicit declarer,
target and actual `atWar` result. Full target finances, military state and other
agreements are absent. Native declarations perform their usual treaty cancellation,
defensive-pact entry, diplomatic modifiers, war cooldown and alert/notification effects.

## Consent, privacy and recovery

Publishing and counteroffering record the author's consent. Acceptance still pins
the version inspected by that job. Invalid targets, quantities, durations,
duplicates, active peace treaties, defeated targets and stale engine eligibility
transfer nothing. Acceptance revalidates before any native effect.

An accepted private peace commitment blocks both directly declaring and sponsoring
a war against that player until the signer deliberately reconsiders it. The host
checks the author before publication and the other signer on acceptance. A failed
private check does not publish that signer's promise or alter the pending offer.
The author's consent also expires if their peace commitments change while waiting;
the other signer receives only a generic request for renewed consent. Private
commitment counters stay in the checkpoint and never appear in player responses.

The target receives native war popups and notifications plus a durable engine
event concerning the declaring civilization and the target. This event contains no
private proposal ID, payment, or sponsor. Other observers receive only their native
permitted information. All players may see the global negotiation hold open/close,
without private participants or terms. Defensive-pact allies retain their native
war alerts and notifications.

Engine state, trade consent, outcome, target event and request receipt share one
atomic checkpoint. Exact retries cannot declare or pay twice. Native war popups can
be read and acknowledged through the existing player commands before subsequent
scheduled turns. Whole-round rollback restores the war, money and private records
together and fences the abandoned epoch.

The Python gateway checks observed target relations before dispatching war consent.
Confirmed trade-war events flag plans about the target for review, including the
sponsor's plans when the sponsor does not itself enter the war. Chat claims alone
do not establish an engine war result.

## Inventory and verification

| Surface | Shared source | Regression evidence |
| --- | --- | --- |
| Tradable declarations and limits | `TradeLogic.getAvailableOffers`, `TradeEvaluation.isTradeValid`, `DiplomacyManager.canDeclareWar` | Exact offer-table comparison; forged, duplicate, unknown, one-sided-contact, defeated and treaty-bound target rejection |
| Declaration warnings | `PlayerDiplomacyOperations.declarationConfirmation` | Own warnings; another signer's known pact partner hidden from the reader |
| Paid, joint and joining wars | Desktop `TradeLogic.acceptTrade`, `DeclareWar` | Gold, full diplomacy state, popups, notifications and visibility compared with the native trade operation on cloned fixtures; successful reversed-author counteroffer |
| Human authority and privacy | Fixed player sessions, `EngineTrades`, `Communication` | Off-turn initiation rejected; off-turn acceptance; target cannot read private proposals; private research/production orders and treasury do not affect permitted observations |
| Consent and memory | `ProposalGateway`, `CivilizationState`, `Communication` | Inspected-version pinning, changed-target observations, private peace checks for both signers, sponsorship and target-plan review |
| Persistence and turn continuation | Checkpoints, operator scheduling, receipts | Pending-offer restart, settled restart, four-player turn handoffs, exact retries, rollback and storage failure before/after replacement |

`ThirdPartyTradeProbe` exercises these cases through authenticated HTTPS and native
save assertions. It includes Vanilla paid/joint/joining wars and defensive-pact
effects, plus a Gods & Kings joint-war case. `CommunicationIntegrationTest` runs
the probe. Python regressions cover trade schemas, consent, stale observations and
memory. The four-player fixture is internal; the existing `trade-cycle` scenario
continues to exercise bilateral items.

Validated on September 14, 2026 against engine revision
`0bad73285ffa306334b89bcecbe395dc3b3b0bda`:

- `./gradlew spotlessApply check`: 74 tests passed, none skipped.
- `uv run pytest -q`: 329 tests and 86 subtests passed.
- Ruff lint and format checks, Pyrefly, and `git diff --check`: passed.

The full suite also exposed a conquered-save notification recovery defect. The
[notification restore fix](Ordinary-notifications.md) preserves saved handles while
allowing native loading to append a city-disconnection notice.

These are scripted checks and comparison with the desktop's underlying operations.
A live model exercise, graphical desktop acceptance and full trade parity remain
separate gates. [Three-Cornered War](War-trades-live-scenario.md) provides a guided live
Rome/Greece/France exercise with prepared launchers and a checkpoint verifier.
The [September 18 live results](War-trades-live-results.md) confirm all three
declaration forms, restart preservation and two continuation rounds. Both saved
runs completed after resuming from their initial wall-time limits.
[Desktop acceptance](War-trades-desktop-acceptance.md) also passed for the three
Vanilla war-trade forms, native acceptance/notification screens and the shared
declaration warning, with 16 inspected screenshots and exact settlement checks.
Introductions require a mod-only option in the bundled rulesets.
The native mediated-peace offer explicitly excludes human targets because their
consent is unknown; that action remains unsupported. See the
[next sprint](Sprints.md#next-sprint-enacted-agreement-lifecycle) for agreement endings.
