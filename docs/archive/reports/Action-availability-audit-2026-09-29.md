# Action availability audit — September 29, 2026

## Trigger and implemented correction

Greece declared war on France on turn 21 to capture a Settler it described as
adjacent to its Scout. The Scout was at `(-7,1)` with `1/2` movement; the Settler
was at `(-6,0)`, two hexes away. Native movement and attack offers did not include
the capture, either before or after the declaration. The old
`tacticalReview.capturableCivilians` list indicated capture-compatible unit types
and outcomes, without testing an owned unit's ability to reach them.

The summary now uses `potentialCaptureTargets` and distinguishes:

- What keeping the captured unit would produce.
- A required declaration of war, with post-declaration reachability explicitly
  unevaluated.
- Visible guards and cities that require combat.
- Current native direct-capture offers, with commands and required inspections.
- The two nearest non-air military units, their hex distances and movement.

Distance does not establish reachability. One native direct-capture order per
eligible unit is retained regardless of distance or the nearest-two limit,
preferring movement over an attack. The host neither simulates a declaration
nor restricts a legal one. No alias for the old summary field is retained.

Implementation: [tactical_review.py](../../../src/civ_agent/tactical_review.py).
Current contract: [Tactical review](../../reference/Tactical-review.md).

## Additional findings and implemented fixes

These are source-level findings in related summaries. They are separate from the
observed Greece incident; that incident does not establish that these fields have
caused another bad decision.

### Combat capture eligibility — substantive mismatch

[`AttackPreview.consequences`](../../../src/main/kotlin/uncivai/player/TacticalConsequences.kt)
previously set `capturePossible` for any melee attack on a civilian, or a melee
attack whose city damage projection reached one HP. That omitted native exceptions:

- Uncapturable civilians, such as Great Scientists, are destroyed instead of
  captured. The map's capture metadata already excludes these units.
- The Vanilla Helicopter Gunship has `Unable to capture cities`, which native
  city-conquest execution checks. The old consequence calculation omitted it.

Relevant native execution is in `Battle.handleCityDefeated` and
`BattleUnitCapture.captureCivilianUnit`; rules are in the selected engine's
Vanilla unit definitions.

**Implemented:** engine execution and previews share `Battle.cityDefeatAction`
and `BattleUnitCapture.destroysCivilianOnCapture`. The attack preview provides
`civilianOrCityCapturePossible` and `civilianCaptureOutcome`; map observations
use the same civilian restrictions. The model receives
`civilianOrCityCapturePossible`, explicitly scoped to the direct civilian/city
target, and `civilianDestroyedOnCapture` for destruction that bypasses damage.
It no longer receives the ambiguously scoped `capturePossible` field. Guarded
civilians and military prize captures remain separate from this flag.

The new direct-capture list is limited to civilians with native capture metadata,
without a guard or city, so it does not expose these city/uncapturable exceptions
as capture commands.

### Combat advance destination — ambiguous conditional context

The old consequence calculation populated `mayAdvanceTo` for every melee
attack. Even the injured-Scout test had positive minimum defender HP and a
populated advance destination. The field supplied the context of the target
tile without verifying that the attack could clear that tile and advance.
Native post-battle movement also checks movement and entry permission.

**Implemented:** this field is now `targetTileContext`. It preserves the target
tile's visible dangers for melee planning without asserting that the attack
will clear the tile or advance into it. No advance prediction is manufactured.

### Trade inventory — wording suggests an offer was made

The former [`missingLuxuriesOffered`](../../../src/main/kotlin/uncivai/player/DiplomacyOpportunities.kt)
field contained missing luxuries in a partner's permitted trade-menu inventory.
It did not mean the partner proposed those items, accepted particular terms, or
wanted to trade. Availability and the peace-treaty requirement were checked, but
the field name blurred inventory and intent.

**Implemented:** renamed to `missingLuxuriesAvailableForTrade`, with a shared
`luxuryTradeBasis` explaining that terms require negotiation and both players'
consent. The model projection and consumers use the new name. Actual proposals
and consent remain in the trade flow. No aliases or migrations were added.

## Other surfaces inspected

| Surface | Existing distinction |
| --- | --- |
| Settlement sites | `ELIGIBLE` describes the site; current founding readiness and unit blockers are separate. Movement previews recheck arrival conditions. |
| Settler escort candidates | Hex proximity is separate from native route availability, remaining movement and pairing orders. |
| Exploration and nearby defenders | Geographic distances explicitly do not assert routes, safety or response times. |
| Resource improvements | Builder type, missing technologies, site availability and preparatory jobs are separate; worker commands use native current-job availability. |
| Buildings and wonders | Benefits and completion estimates coexist with queueability, prerequisites and rejection reasons. Estimates do not create construction commands. |
| Purchases, upgrades and city-state actions | Current action availability comes from native operations, including resource/currency constraints and active-turn gating. |
| Research and policy choices | Immediate choices, prerequisites and research queues have distinct availability fields. |
| Air missions | Bases and missions use native previews; proximity or remaining capacity alone does not create a rebase command. |
| Copyable command templates | Templates attach to current offers or explicit availability flags; inspect-before-action and refresh behavior remain in place. |

This was an audit of opportunity/availability semantics, not a claim of complete
player API coverage or a new privacy certification.

## Validation

- 108 Python tests passed across tactical review, model projection, state updates,
  startup context and selective actions. They cover copyable capture commands,
  the Greece geometry, visibility, wrapped distances, renamed fields and trade
  inventories without invented proposal responses.
- 125 engine tests passed across `CapturePreviewTest`, `PlayerCombatOperationsTest`,
  `PlayerCaptureOperationsTest`, `AttackOutcomeTest` and `BattleTest`. New scenarios
  compare previews with actual ordinary capture, Settler conversion/recovery,
  uncapturable destruction, helicopters, ranged attacks and guarded civilians.
  They also verify the shared city-state destruction rule, city destruction and
  barbarian raids. Queries are checked for native-state mutation.
- 31 host integration tests passed across tactical consequences, tactical review
  and observation opportunities. They cover serialized field names, consistent
  map/attack capture metadata, actual capture receipts and trade inventory wording.
  The earlier native two-hex/one-movement Greece regression remains covered.
- Validation used the selected `vendor/Unciv` working tree, based on
  `e8a842721328abc94dab6ccbb713e5f5a280a231`, including these engine changes.
  Python lint, formatting and Pyrefly passed. Gradle and pytest ran through
  `codex-log`.
