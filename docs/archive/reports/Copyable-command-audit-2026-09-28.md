# Copyable commands and civilian protection — September 28, 2026

## Result

The Python player API and Kotlin host advertise the same **54 gameplay command
types**. **44 have copyable templates; 10 do not.** The missing templates are
inconsistent with the model-facing instruction to "Copy only an inspected
option's command."

This is an audit, not an implementation change. Gameplay, prompts and the running
match were not changed for this audit.

### Follow-up implemented

Capture choices now include a copyable `RESOLVE_CAPTURED_CITY` command on each
available option, including return/keep civilian decisions. That brings coverage
to **45 of 54 command types**; the other nine gaps below remain.
The turn-start tactical overview also shows the resulting unit type if a visible
civilian is captured and kept. The return/keep popup explains the conversion and
names the recipient and resulting unit type for each option. See
[tactical review](../../reference/Tactical-review.md). Native capture/privacy
integration tests and Python projection tests cover these changes; no live model
replay has been performed for this follow-up.

## Missing gameplay templates

| Command | Current decision surface | Recommended placement |
| --- | --- | --- |
| `RESOLVE_CAPTURED_CITY` | `player_turn.alerts[].capture.options[]` exposes names and availability, but no command. Covers conquered cities, traded-city decisions and returned civilians. | Attach an exact command to every available option, using the **alert** handle. A name such as `RESOLVE_CAPTURE_DECISION` would describe its scope more clearly. |
| `CHOOSE_FREE_GREAT_PERSON` | The overview supplies `freeGreatPeople`, `mayaLimited` and `greatPersonOptions` as names. | Supply an offered choice with `selection` for each currently available native option. |
| `CAST_DIPLOMATIC_VOTE` | `victory.ballot` supplies `canVote` and candidates. | Attach a vote command to each candidate when voting is available. |
| `ABSTAIN_DIPLOMATIC_VOTE` | Ballot availability exists; arguments are described only in tool prose. | Add an abstain command to the available ballot. |
| `ACKNOWLEDGE_DIPLOMATIC_RESULTS` | `victory.votingResults.canAcknowledge` exists; arguments are described only in tool prose. | Attach acknowledgement to the results when available. |
| `CITY_STATE_ACTION` | `player_details(section="diplomacy")` returns named actions, availability and consequences. | Attach a command using the city-state ID and action name; retain any required explicit confirmation. |
| `DECLARE_WAR` | The diplomacy detail exposes `declareWar.available` and declaration consequences for majors and city-states. | Attach target/kind and identify the required `confirmWar` field. Never prefill consent. |
| `DISMISS_POLICY_PICKER` | The overview supplies pending decisions, but no dismissal command. | Expose native dismissal availability and a command. Free policy choices may remain due after dismissing a paid picker. |
| `DISMISS_NOTIFICATIONS` | Notifications supply handles and `canDismiss`; batching is described in tool prose. | Attach per-notification commands or a form accepting the player's selected current handles. |
| `RENAME_CITY` | The command schema accepts a city handle and a new name through `selection`; city status has no form. | Provide target/kind and identify the user-supplied new-name field. Do not invent a name to manufacture a complete command. |

The first five include decisions that can hold up play. Capture decisions are
particularly severe: the host permits only capture resolution and informational
alert acknowledgements while a capture decision is pending.

### Observed failure: Rome, turn 28

Rome recovered a French Settler from barbarians. Its `RecapturedCivilian` popup
offered `ReturnCivilian` and `KeepCivilian`, both available, with
`canAcknowledge=false`. Neither option had a command. Rome repeatedly attempted
`ACKNOWLEDGE_ALERT` and `RESPOND_FRIENDSHIP`, producing argument/availability
errors and `TURN_BLOCKED` responses. The correct existing action was
`RESOLVE_CAPTURED_CITY` with the alert handle and chosen option name.

The detailed host MCP description mentions this action, but the Python adapter
replaces that description with a compact one that omits it. Merely having the
right command in the advertised enum does not make this decision discoverable.

Relevant sources:

- [Command shapes](../../../src/civ_agent/game_commands.py)
- [Template builders](../../../src/civ_agent/action_templates.py)
- [Model-facing command guidance](../../../src/civ_agent/proposal_gateway.py)
- [Native command gating and capture resolution](../../../src/main/kotlin/uncivai/player/TurnSession.kt)

## Commands with templates

These account for the remaining 44 types. Templates generally live in selective
details, keeping large option lists out of every turn's preload. Broad research,
policy and construction catalogues intentionally lead to named reads; those
named reads supply the relevant commands.

| Surface | Commands |
| --- | --- |
| Unit movement / settlement / orders, and route preview | `MOVE_UNIT`, `SET_UNIT_DESTINATION`, `CANCEL_UNIT_MOVEMENT`, `FOUND_CITY`, `SET_UNIT_ORDER` |
| Unit and city combat details | `ATTACK`, `CITY_ATTACK` |
| Unit promotions and economy details | `PROMOTE_UNIT`, `UPGRADE_UNIT`, `PILLAGE`, `DISBAND_UNIT` |
| Unit work details | `START_WORKER_JOB`, `STOP_WORKER_JOB`, `CREATE_WATER_IMPROVEMENT` |
| Unit abilities details | `ACTIVATE_GREAT_PERSON`, `ADD_SPACESHIP_PART`, `GIFT_UNIT` |
| Mission preview | `REBASE_UNIT`, `PARADROP`, `AIR_SWEEP`, `NUCLEAR_STRIKE` |
| Named construction and city purchase/production details | `QUEUE_CONSTRUCTION`, `BUY_CONSTRUCTION`, `REMOVE_CONSTRUCTION`, `MOVE_CONSTRUCTION` |
| City purchases and citizen details | `BUY_TILE`, `SET_WORKED_TILE`, `SET_TILE_LOCKED`, `REASSIGN_CITY_TILE` |
| City citizen, economy and status details | `SET_CITY_FOCUS`, `SET_AVOID_GROWTH`, `SET_MANUAL_SPECIALISTS`, `ASSIGN_SPECIALIST`, `UNASSIGN_SPECIALIST`, `RESET_CITIZENS`, `SELL_BUILDING`, `MANAGE_CAPTURED_CITY` |
| Named research and policy details | `SET_RESEARCH_QUEUE`, `CHOOSE_FREE_TECHNOLOGY`, `ADOPT_POLICY` |
| Overview alerts | `ACKNOWLEDGE_ALERT`, `RESPOND_FRIENDSHIP`, `RESPOND_CITY_STATE_ALERT` |
| Overview end-turn choice | `END_TURN` |

The action-specific unit/city sections retain their templates after projection.
The model-facing flavor-text projection also preserves these commands. Intent
and explicit confirmations are deliberately left for the player to supply.

## Separate chat, diplomacy and trade tools

These are outside the 54 `player_command` types and were inspected separately.

| Surface | Finding |
| --- | --- |
| Incoming major-player native demand/incident | Already supplies `responseTool` with `chat_send`, recipient and exact `nativeAction` arguments. The player supplies text and intent. |
| Outgoing native denouncement and demand | Availability/type/recipient are shown, but `DENOUNCE` and `ISSUE_DEMAND` lack an equivalent tool-argument template. |
| Friendship proposal | `trade_options.friendship` and initiation availability exist, but no `friendship_offer` call template. |
| Trade/friendship acceptance, decline and withdrawal | `trade_read` supplies proposal identity and state/availability, but no `trade_decide` response templates. Shared offers expose acceptance/counteroffer flags; native AI offers additionally expose decline/withdraw flags. Standardize explicit response availability before attaching templates. |
| Social promise decisions | `commitment_decide` has a discoverable fixed decision enum, but `chat_read` does not attach response arguments to offers. Optional templates should follow the existing consent semantics. |
| Authored chat, trade terms and promises | The player chooses text, participants, amounts and terms. These need authoring schemas or partial forms, not invented complete proposals. Existing trade-item data already supplies names, types, amounts and durations to select from. |

Private notebook authoring and operator commands do not present native gameplay
choices and are outside this menu-response audit.

Relevant sources:
[tool schemas](../../../src/civ_agent/tool_schemas.py),
[shared trade views](../../../src/main/kotlin/uncivai/EngineTrades.kt),
[native AI trade views](../../../src/main/kotlin/uncivai/NativeTradeBridge.kt),
[social context](../../../src/civ_agent/social_context.py).

## Settler losses: advice and information already available

France's two losses were preceded by visible danger:

- **Turn 22:** the turn overview identified an unescorted Settler at `(-1,-5)`
  with a Brute one hex away. The capture was reported on turn 23.
- **Turn 26:** the movement preview for `(8,5)` reported zero movement after
  arrival and `foundCity=NO_MOVEMENT`. It showed a full-health Brute at `(9,5)`
  and France's Spearman at `(8,6)`, both one hex from the destination. France
  nevertheless described the Settler as protected and ended there. The capture
  was reported on turn 27.

These cases are not explained by absent threat observations. The distinction
between nearby military and actual protection needs to be clearer. Existing
movement guidance already says proximity is not guaranteed protection, while
the optional guide discusses safe routes without explicitly describing land
escorts or advance scouting.

Suggested concise advisory:

> Scout the route and destination ahead of a Settler. On exposed turns, a
> military escort on the same tile protects the civilian while the escort
> survives; a nearby unit does not prevent capture. Compare the risk and delay
> before committing to a destination where the Settler cannot found immediately.

This should remain advice, not an escort requirement or an engine-legal movement
restriction. Safe routes through controlled territory may justify travelling
without an escort. Scouting reduces uncertainty; it does not guarantee safety
on later turns. The operational question is where both units finish the turn,
not whether an escort appears somewhere nearby.

Use one short guide paragraph and a concise factual explanation beside civilian
endpoint context. The existing tactical overview already reports
`otherMilitaryOnSameTile`, and route previews expose nearby own military and
hostiles. Clarify those facts instead of adding a mandatory planning checklist
or requiring repeated broad reads.

### Related omission: what keeping a rescued Settler does

The native return/keep popup supplies empty consequence paragraphs for both
options. For Rome's rescued **foreign** Settler, `KeepCivilian` calls native
`captureOrConvertToWorker`: Rome receives a **Worker**, not a new Settler.
Recapturing one's own Settler preserves it. Rome's stated reason for keeping the
unit—an additional settlement opportunity—therefore relies on an incorrect
outcome.

Expose the native resulting unit type and return consequences with these
choices. This is a mechanics-information fix, separate from escort advice.
Relevant sources: [capture choices and execution](../../../vendor/Unciv/core/src/com/unciv/logic/civilization/PlayerCaptureOperations.kt),
[capture conversion](../../../vendor/Unciv/core/src/com/unciv/logic/battle/BattleUnitCapture.kt),
[tactical overview](../../../src/civ_agent/tactical_review.py).

## Recommended implementation order

1. Fill the capture, Great Person and election response gaps. Put the exact
   action on each available option, including newly surfaced post-action alerts.
2. Fill city-state, war and dismissal gaps; provide an authoring form for rename.
   Confirmations must remain explicit. `DECLARE_WAR` currently requires
   `confirmWar=true` in the execution schema, so its unsigned template needs a
   distinct validation path rather than prefilling consent.
3. Add equivalent fixed-response templates to native chat and trade surfaces.
   Preserve authored text/terms and the existing native availability checks.
4. Replace the blanket copy-only instruction with guidance that distinguishes
   offered actions from freeform inputs. Add coverage checks for every advertised
   command, its intended inspection surface and unavailable alternatives.
5. Clarify civilian endpoint protection and expose capture outcomes. Replay the
   two French failure positions to evaluate whether behavior actually changes.

## Validation and limits

- Matched all 54 command names between the native host enum and Python schema.
- Traced overview, unit/city sections, named reads, mission previews, native chat
  and trade response paths.
- Exercised every existing template builder using synthetic offered-option
  fixtures through the normal projections and model-facing filtering. All 44
  emitted command types validate against the execution schema and omit intent
  and consent. The synthetic fixtures combine options for coverage; they are not
  a legal native saved position.
- Reproduced missing capture and city-state templates in those projections.
- Used read-only saved observations/calls for France's turns 22 and 26 and
  Rome's turn-28 popup. No game action or live model invocation was performed.
- This establishes template coverage, not full human-action API coverage or a
  behavioral improvement. Template presence alone does not prove every native
  state combination or recovery case works.

The temporary probe and its detailed emitted-field paths are in
`/tmp/unciv-copyable-command-audit.py` and
`/tmp/unciv-copyable-command-audit.json`.
