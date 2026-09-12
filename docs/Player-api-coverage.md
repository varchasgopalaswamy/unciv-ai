# Player API coverage ledger

The following is a subsystem inventory, not a claim of exhaustive parity. Before a
row is complete, enumerate its individual UI controls, tooltips, notifications,
previews, shortcuts, and ordinary settings-dependent behavior. Each item needs its
API operation, UI source, engine rule, positive test, negative authority/privacy
test, and UI comparison evidence. Unsupported items remain explicit.

| Surface | Required information and actions | Current coverage |
| --- | --- | --- |
| Player identity | Fixed civilization, session lifetime, turn ownership, elimination | Authenticated fixed-civilization sessions, player-scoped revisions, turn fencing and revocable activation credentials; broader controller lifecycle incomplete |
| Turn flow | Pending choices, actionable options, optional prompts, end turn, automation timing | Shared requirements, choices and end turn tested for both bundled rulesets; automation timing and broader decisions incomplete |
| Map | Exploration, fog, remembered improvements, terrain, rivers, resources, ownership, yields | Explored tiles, visibility, terrain/features, rivers, revealed resources, ownership, remembered improvement names and visible units; live yields on visible tiles; full tile breakdowns outstanding |
| Tile inspection | Yield breakdowns, resource reveal, city and unit details, work status | Visible map yields, owned city workable-tile yields/work/lock state, worker progress/options; full tooltip coverage outstanding |
| Unit movement | Own units, paths, movement costs, movement plans, embarkation, transport | Own units with durable handles, displayed movement, current-turn previews and validated movement; UI single-turn paths share operations. Future orders, aircraft/paradrops and complete transport coverage outstanding |
| Unit actions | Found city, worker jobs, pillage, fortify, sleep, skip, explore, automate, upgrade, disband, gift, rename | Shared settlement with promise confirmation and ordinary worker jobs/repair/cancellation, fortify/heal/sleep/skip/wake; land-unit paid upgrades, pillage and disband with requirements, confirmations and actual outcomes; automation, route orders, instant improvements, gifting, naming and broader domains outstanding |
| Combat | Legal targets, human-visible previews and precision, ranged/melee/city attacks, interception, nuclear actions | Shared land melee/ranged and city attacks with visible targets, UI damage ranges, speculative routes and actual receipts; naval, air and nuclear commands missing |
| Promotions | Available promotions, prerequisites, descriptions, choice and naming | Shared promotion eligibility, prerequisites, descriptions, experience, selection and instant healing; naming missing |
| City overview | Population, food, production, happiness, culture, borders, yields and breakdowns | Owned-city population, food/growth, displayed yields/breakdowns, built buildings and construction progress; culture/border and full overview details outstanding |
| City management | Rename, citizens, specialists, focus, growth, tile purchase, tile reassignment | Shared rename, worked-tile assignment, locks and visible tile purchases with prices/reasons; shared focus/growth controls, manual/automatic specialists, reset citizens and shared worked-tile transfers; full city screen inventory still incomplete |
| Construction | Available/disabled entries and reasons, progress, queues, reorder/remove, purchases, placement | Queue discovery/append, indexed remove/reorder, stored production/cost/turn estimates; gold purchases with descriptions, costs, resource/technology requirements and unavailable reasons; shared building sales with eligibility, confirmation and actual refunds; other currencies, placement choices and full construction detail missing |
| Captured cities | Annex, puppet, raze/stop, liberate, occupation, resistance | Shared conquest disposition, one-city destruction, later annex/raze/stop and civilian return choices; detailed occupation overview missing |
| Research | Tree, descriptions, progress, costs, prerequisites, queues, free choices, repeatable tech | Shared queue validation/free choices, displayed science and technology costs/progress/turn estimates; tree descriptions/prerequisite detail incomplete |
| Policies | Branches, requirements, costs, adoption, free choices, postponement | Shared adoption and picker dismissal; detailed observations incomplete |
| Great people | Points and thresholds, free choices, placement failure, special actions, Maya restrictions | Shared free choices with revision and durable receipts; city specialist yields, points per turn, empire accumulated points, thresholds and source rows; special unit actions and remaining overview details incomplete |
| Diplomacy | Known civilizations, relations, modifiers, war/peace, treaties, denunciations, promises, demands | Met major identities/captions, status and peace treaty turns, legal war/peace actions and unavailable reasons, shared war confirmations and declarations; friendship, demands, denunciation, modifiers and broader information missing |
| Trade | Offers, counteroffers, validity, resource/gold/city transfers, acceptance/rejection, expiry | Peace treaty/gold/GPT/luxury/strategic-resource proposals, current offer inspection, acceptance/rejection/counteroffers/retraction and durable receipts; city transfers, third-party and other trades missing |
| City-states | Influence, quests, gifts, protection, bullying, war/peace, allies | Not implemented |
| Religion | Pantheon, founding/enhancing/reforming, beliefs, pressure, conversion, religious units | Not implemented |
| Espionage | Spy status, assignment, promotion, stealing technologies, elections, coups, reports | Not implemented |
| Economy | Treasury, income and expense breakdowns, resources, happiness, connections | Own displayed treasury, gold per turn, maintenance and breakdowns, happiness, unit supply, resource balances/imports/exports/offers and own city connections; complete tooltips and economy controls outstanding |
| Overviews | Cities, units, trades, resources, religion, demographics, rankings, wonders, victory progress | Own economy/resource overview rows, resource reveal and luxury demands/celebrations; remaining overviews and full information parity outstanding |
| Notifications | Per-player alerts, transient combat/movement history, popups, choices, navigation targets | StartIntro, TechResearched, WonderBuilt, GoldenAge, FirstContact and WarDeclaration expose shared popup prose/quotes and require observation before acknowledgement; peace requests expose full leader text/terms and validated choices; other consequential alerts fail closed; filtered durable combat reports and terminal result delivery; other notifications and visual/navigation surfaces incomplete |
| Communication | Player-addressed chat, participant identity, transcript visibility | Not implemented |
| Civilopedia | Public bundled rules, searchable entries, descriptions and links, no hidden game data | Not implemented |
| Victory and defeat | Conditions, results, scores, permitted postgame information, resignation | Shared victory/defeat text, player result query and HTTPS delivery after scheduling stops; scores, progress, resignation and postgame exploration missing |
| Lifecycle | Supported setup validation, create/load/save/pause, checkpoints, reconnect, controller replacement | Fixed Vanilla duel creation, load, checkpoint/resume, serialized execution, operator pause, player-scoped references and durable command receipts; general setup and controller management incomplete |
| Agent runner | Codex conversations, private notes, turn scheduling, budgets, isolation | HTTPS supervisor and per-civilization Codex services, explicit conversations, durable progress, authoritative handoffs, leases, pause/resume and turn/invocation/time limits; hard spending caps, isolation and completed live-match acceptance outstanding |
| Desktop integration | Shared execution and player API across all supported controls | Shared requirements, selected pickers, movement/settlement, worker orders, city tile/queue operations, focus/growth, specialists, sales, shared-tile transfers, gold purchases, land-unit upgrades/pillage/disband, ordinary combat, tactical orders, promotions and capture choices; embedded executor and full UI migration outstanding |

The [development slice](City-development.md) and [automatic scheduler](Automatic-turns.md)
add worker/city controls and repeated turns over authenticated HTTPS. The desktop
inspection launcher loads exported snapshots for operator verification; it is not
an embedded human player connected to the authoritative host.

The [encounter inventory](Encounter-and-diplomacy.md) records the first-contact
implementation, initial diplomacy observations, scheduled encounter/restart tests,
and each remaining popup's UI behavior and coverage gap. First contact and war messages can be
acknowledged. The [war and peace slice](War-and-peace.md) supports negotiated peace;
remaining unsupported diplomatic decisions still block turn advancement.

## Known architectural gaps

- View wrappers return raw cities, units, civilizations, counters, and tile fields.
  Serialize explicit allowlisted DTOs; never serialize Views or engine state wholesale.
- Turn-button choice rules now use shared requirements. Mandatory alerts and trades
  still need complete API implementations before unrestricted games can run.
- Global revisions or visible sequence gaps can disclose hidden opponent actions.
  Player-facing revision tokens must depend only on that player's observable state.
- Legacy prototype mutations retain their old receipt semantics and are disabled
  in durable mode. The new command API persists receipts and references with the game.
- The desktop currently clones a game while advancing turns. An embedded host must
  coordinate authoritative game replacement; keeping a stale game reference is unsafe.
- Agent filesystem isolation and completed match acceptance have not been established.

The [exploration slice inventory](Exploration-and-settlement.md) records implemented
fields, source callers, tests and remaining limits. Its scripted MCP scenario reaches
the first turn handoff; this is not a completed duel or live-model acceptance run.

The [combat inventory](Combat.md) records the bounded land-combat implementation,
capture decisions, historical report privacy, terminal delivery and saved-state tests.

The [economy inventory](Economy-and-unit-actions.md) records own finances/resources,
gold purchases and land-unit economy actions, their desktop callers, and scripted
HTTPS saved-state, rollback and restart checks. It does not establish visual
desktop acceptance or a completed live-model match for this slice.

The [city management inventory](City-management.md) records citizen controls,
specialists and progress, building sales and shared-tile transfers, including
UI delegation and two-player HTTPS saved-state, growth, rollback and restart checks.
