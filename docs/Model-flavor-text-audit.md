# Model-facing flavor text audit

Audited the player observations, communication context, native popup producers,
detail descriptions, receipts, private evidence, and startup context against the
pinned Vanilla and Gods & Kings rules on 2026-09-21. Mods are out of scope.

## Findings and treatment

| Surface | Finding | Model view |
| --- | --- | --- |
| Native demands | `native.demands` contains actions the reader could send. Agents mistook its stock dialogue for received demands, then saved that interpretation in plans. | `availableDemandsToSend`, with demand type, recipient and availability; no dialogue. Actual received messages remain owned alerts with a sender and event type. |
| Native responses | Button labels and leader speeches are theatrical. Refusing the military-presence demand declares war; some incidents identify a city-state only in the speech. | Keep response IDs, availability, consequences, explicit war effect, and visible incident identity. Remove stock speech and button dialogue. |
| Native promises/opinion | Promises have explicit promiser, promisee, type and remaining turns. Opinion descriptions explain real modifiers. | Remove redundant promise text; retain opinion explanations and amounts. |
| Technology popups | Quotes are separate from effects. `TechnologyDescriptions` also prepends Civilopedia prose (Future Tech in both supported rulesets). | Remove quotes and the source-identified Civilopedia prefix; retain effects, unlocks, requirements and the technology name. Apply the same treatment to named research details. |
| Wonder popups | Separate quotation, mechanical short description and Close label. | Keep wonder identity and mechanical description; omit quote and acknowledgement label. |
| Introduction/first contact/golden age | Opening narrative, greetings and celebratory sentences. | Keep event type, player-visible identities and action availability. |
| War/defeat/friendship | Leader speeches; friendship duration appears in a button label. | Keep event, participants, friendship duration and legal choices. |
| Native trade requests | Leader's invitation accompanies structured trade rows. | Omit invitation; retain exact terms, direction, proposal and availability. |
| Victory/defeat results | Narrative accompanies structured winner, victory type and turn. | Keep structured result and permitted combat reports; omit result narrative. Final popup messages follow the same typed projection as ordinary alerts. |
| Research/policy/construction/unit descriptions | Generally gameplay effects, costs, prerequisites, yields, promotions and unlocks, not flavor. Building/unit replacement descriptions substitute for rule explanations. | Retain, except source-identified technology Civilopedia prose above. |
| City-state quests | Descriptions mix target names and bonuses with threats, pleas and praise. Give Gold/Pledge to Protect name a recent bully, not the recipient of the requested action. | Supply structured quest type, issuer, visible target, gift bonus, reward, deadline and contest standing. Omit description when these facts are supplied; label the recent bully explicitly. Unknown/legacy quest descriptions remain until they have structured replacements. |
| Capture choices, notifications, combat reports | Prose can be the only representation of events, consequences or player-chosen names. | Retain. Do not guess from wording or discard every field called `text`/`description`. |
| Player chat, negotiated promises, private plans | Player-authored wording and ambiguity are part of play. | Preserve verbatim. Never reinterpret or adjudicate a promise. |

## Boundary and persistence

The Python model projection filters copies of permission-filtered observations.
Native desktop text and raw host observations/receipts remain available for human
display and audit. The host supplies additional structured facts where the old
dialogue was the only carrier; it does not change engine rules or visibility.

The projection covers overviews before state deltas are calculated, focused
reads, receipts, replayed calls, history and evidence reads, and historical
evidence preloaded at startup, and generated workspace copies of summaries,
receipts and tool events. The trusted archive retains the original records;
legacy MCP replies with duplicate JSON text get the same projection in both forms.
The projection is idempotent and does not reinterpret player-authored strings.
Unknown popup content is retained until audited rather
than risking loss of a decision's meaning.

Existing agent-authored false beliefs are not automatically rewritten. Fresh
instructions distinguish available actions, received requests, and recorded
promises and require reconsidering memories that confuse them. Updating code
does not hot-reload a running game or change its saved memories.

## Validation

- 151 affected Python tests and 11 subtests passed: model projection, native
  diplomacy, city states, observations, notifications, state updates, startup,
  final results, gateway/replay, memory, MCP payloads, archives and service history.
- 16 native integration tests passed: informational popup contents and recovery,
  mixed diplomacy (including war-producing refusals and incident identity),
  city-state interactions/privacy/affordability/recovery, and victory decisions.
- Ruff checks, formatting, Pyrefly and `git diff --check` passed.

These checks validate the delivered data and preserved engine behavior. No new
live LLM playthrough was run to measure strategic behavior after the change.
