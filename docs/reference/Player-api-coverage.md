# Player API coverage ledger

The following is a subsystem inventory, not a claim of exhaustive parity. Before a
row is complete, enumerate its individual UI controls, tooltips, notifications,
previews, shortcuts, and ordinary settings-dependent behavior. Each item needs its
API operation, UI source, engine rule, positive test, negative authority/privacy
test, and UI comparison evidence. Unsupported items remain explicit.

| Surface | Required information and actions | Current coverage |
| --- | --- | --- |
| Player identity | Fixed civilization, session lifetime, turn ownership, elimination | Authenticated fixed-civilization sessions, player-scoped revisions, turn fencing and revocable activation credentials; broader controller lifecycle incomplete |
| Turn flow | Pending choices, actionable options, optional prompts, end turn, automation timing | Shared ordinary requirements, choices and end turn tested for both bundled rulesets; native destination continuation at turn start; explicit context-menu Next Turn deferral and broader automation controls/decisions incomplete |
| Map | Exploration, fog, remembered improvements, terrain, rivers, resources, ownership, yields | Explored tiles, visibility, terrain/features, rivers, revealed resources, ownership, remembered improvement names and visible units; live yields on visible tiles; full tile breakdowns outstanding |
| Tile inspection | Yield breakdowns, resource reveal, city and unit details, work status | Visible map yields, owned city workable-tile yields/work/lock state, worker progress/options; full tooltip coverage outstanding |
| Unit movement | Own units, paths, movement costs, movement plans, embarkation, transport | Own units with durable handles, current-turn paths and on-demand distant routes with estimated turn stops; native destination orders continue at turn start, survive restart and support cancellation/replacement; embarkation, ocean restrictions and overseas settlement. Desktop routes share operations. Shared aircraft rebasing to native city/carrier slots, carrier transport and paradrops; remaining transport variants and full UI inventory outstanding |
| Unit actions | Found city, worker jobs, pillage, fortify, sleep, skip, explore, automate, upgrade, disband, gift, rename | Shared settlement with promise confirmation and ordinary worker jobs/repair/cancellation, fortify/heal/sleep/skip/wake and persistent native exploration/automation with start/stop controls; land/naval/air paid upgrades and disband, native pillage with requirements, confirmations and actual outcomes; Great Person instant improvements and eligible city-state unit gifts share validated operations; native Work Boat resource improvements and consumption; road-connection orders, other instant improvements, major-player gifts, naming and upgrades requiring relocation outstanding |
| Combat | Legal targets, human-visible previews and precision, ranged/melee/city attacks, interception, nuclear actions | Shared land/naval/air and guided-missile attacks and city bombardment with visible targets, UI damage ranges and native resolution; air sweeps, automatic interception status and nuclear missions with known consequences, filtered reports and explicit launch confirmation; full combat UI/tooltips and unique combinations not exhaustively inventoried |
| Promotions | Available promotions, prerequisites, descriptions, choice and naming | Shared promotion eligibility, prerequisites, descriptions, experience, selection and instant healing; naming missing |
| City overview | Population, food, production, happiness, culture, borders, yields and breakdowns | Owned-city population, food/growth, displayed yields/breakdowns, built buildings and construction progress; culture/border and full overview details outstanding |
| City management | Rename, citizens, specialists, focus, growth, tile purchase, tile reassignment | Shared rename, worked-tile assignment, locks and visible tile purchases with prices/reasons; shared focus/growth controls, manual/automatic specialists, reset citizens and shared worked-tile transfers; full city screen inventory still incomplete |
| Construction | Available/disabled entries and reasons, progress, queues, reorder/remove, purchases, placement | Queue discovery/append, indexed remove/reorder, stored production/cost/turn estimates; city-specific ongoing building yields, maintenance and growth at fixed assignments, with separate queue-switch effects and compact current/new choice summaries; gold purchases with descriptions, costs, resource/technology requirements and unavailable reasons; shared building sales with eligibility, confirmation and actual refunds; completion-trigger valuation, other currencies, placement choices and full construction detail missing |
| Captured cities | Annex, puppet, raze/stop, liberate, occupation, resistance | Shared conquest disposition, one-city destruction, later annex/raze/stop and civilian return choices; detailed occupation overview missing |
| Research | Tree, descriptions, progress, costs, prerequisites, queues, free choices, repeatable tech | Shared queue validation/free choices, displayed science and technology costs/progress/turn estimates; on-demand tree catalogues, selected desktop descriptions, prerequisites, ordered research paths and unavailable reasons; full visual tree parity not established |
| Policies | Branches, requirements, costs, adoption, free choices, postponement | Shared adoption and picker dismissal; on-demand culture/costs, branches, prerequisites, era requirements, selected desktop benefits and completion effects |
| Great people | Points and thresholds, free choices, placement failure, special actions, Maya restrictions | Shared free choices with revision and durable receipts; city specialist yields, points per turn, empire accumulated points, thresholds and source rows; shared research/construction/culture abilities, Golden Ages, instant improvements and native Great Merchant city-state missions with effects, reasons and durable receipts; remaining expansion actions and overview details incomplete |
| Diplomacy | Known civilizations, relations, modifiers, war/peace, treaties, denunciations, promises, demands | Met major identities/captions, status, treaties, native AI opinion modifiers, native promises and denunciation durations; war/peace/friendship, explicit chat denunciations/demands and owned native responses, including major-player city-state incident messages; nonblocking social peace advice and direct city-state operations. Full information parity remains outstanding; see [mixed-player diplomacy](Mixed-player-diplomacy.md) and [city-states](City-state-gameplay.md). |
| Trade | Offers, counteroffers, validity, resource/gold/city transfers, acceptance/rejection, expiry | Canonical gold/GPT/resources, peace, embassies, borders, research, defensive pacts, city transfers, paid/joint wars and native-permitted mediated peace with an AI third party; exact version consent, counters/decisions, expiry, stable enacted agreement references and native ending history. Agent–AI native offers/friendship queue across turns with durable outcomes; human–agent trades use shared negotiation. Unsupported or hidden native terms are decline-only. No native control permits voluntary early cancellation of these enacted deals. |
| City-states | Influence, quests, gifts, protection, bullying, war/peace, allies | Met city-state influence/resting point/decay, allies/protectors, bonuses/resources, owned quests and contest standings, native Gold/improvement/unit gifts, protection/revocation, Gold/Worker tribute, war/peace and conditional diplomatic marriage. Native affordability, cooldowns, privacy, retries and recovery tested; scoped desktop and live acceptance passed. Every quest completion path and civilization-specific bonus are not yet exhaustively verified; expansion espionage/religion remain separate. |
| Religion | Pantheon, founding/enhancing/reforming, beliefs, pressure, conversion, religious units | Not implemented |
| Espionage | Spy status, assignment, promotion, stealing technologies, elections, coups, reports | Not implemented |
| Economy | Treasury, income and expense breakdowns, resources, happiness, connections | Own displayed treasury, gold per turn, maintenance and breakdowns, happiness, unit supply, resource balances/imports/exports/offers and own city connections; complete tooltips and economy controls outstanding |
| Overviews | Cities, units, trades, resources, religion, demographics, rankings, wonders, victory progress | Own economy/resource overview rows, resource reveal and luxury demands/celebrations; permitted Victory Status rankings, recorded trends, paginated charts and enabled demographics; remaining overviews and full information parity outstanding |
| Notifications | Per-player alerts, transient combat/movement history, popups, choices, navigation targets | StartIntro, TechResearched, WonderBuilt, GoldenAge, FirstContact and WarDeclaration expose shared popup prose/quotes and require observation before acknowledgement; peace requests expose full leader text/terms and validated choices; other consequential alerts fail closed; filtered durable combat reports and terminal result delivery; [ordinary notifications](Ordinary-notifications.md) expose full current text, category, icons and event locations with validated batch dismissal and preserved native history; historical notification browsing and UI navigation execution remain incomplete |
| Civilopedia | Public bundled rules, searchable entries, descriptions and links, no hidden game data | Not implemented |
| Victory and defeat | Conditions, results, scores, permitted postgame information, resignation | Public victory types and turn limit, own integer score, native milestone headers and current Vanilla policy/capital/spaceship objectives, permitted rival next milestones, rankings/trends at turn start, selective charts/demographics, native spaceship assembly, world-leader voting/abstention and results acknowledgement, shared victory/defeat text and terminal result delivery with election results; resignation, native One more turn and postgame exploration missing |
| Lifecycle | Supported setup validation, create/load/save/pause, checkpoints, reconnect, controller replacement | Rome–Greece–France Vanilla creation with all five victory types, optional built-in AI majors and a native city-state count, load, checkpoint/resume, serialized execution, operator pause, player-scoped references and durable command receipts; general setup and controller management incomplete |
| Player communication | Public/private channels, permitted history, social proposals and consent | Authenticated public/private chat, mentions, recipient-specific delivery, exact version social consent and binding economic proposals, withdrawal/reconsideration and distinct engine outcomes; human chat/trade CLI available, browser integration and broader trade outstanding |
| Agent runner | Codex conversations, private notes, turn scheduling, budgets, isolation | HTTPS supervisor and responsive per-civilization services, concurrent gameplay/chat jobs, structured private memory, service-owned request bookkeeping, durable receipts, shared invocation limits and coordinated whole-match recovery; host-owned per-turn/job token, API-call and timing accounting with native final winner/all-major-player scores; hard spending caps, OS isolation and Ancient-to-victory live acceptance outstanding |
| Desktop integration | Shared execution and player API across all supported controls | Shared requirements, selected pickers, movement/settlement, worker orders, city tile/queue operations, focus/growth, specialists, sales, shared-tile transfers, gold purchases, land/naval/air upgrades and disband, pillage and Work Boat improvements, ordinary combat, air rebasing/sweeps, paradrops and nuclear actions, tactical orders, promotions and capture choices; embedded executor and full UI migration outstanding |

The [development slice](City-development.md) and [automatic scheduler](../guides/Automatic-turns.md)
add worker/city controls and repeated turns over authenticated HTTPS. The desktop
inspection launcher loads exported snapshots for operator verification; it is not
an embedded human player connected to the authoritative host.

The [encounter inventory](Encounter-and-diplomacy.md) records the first-contact
implementation, initial diplomacy observations, scheduled encounter/restart tests,
and each remaining popup's UI behavior and coverage gap. First contact and war messages can be
acknowledged. The [war and peace slice](War-and-peace.md) supports negotiated peace.
The [mixed-player slice](Mixed-player-diplomacy.md) adds native major-player
demands, denunciation and incident responses, plus the agent–AI offer bridge.
Unsupported decisions still block turn advancement; these slices do not establish
full city-state, mod or embedded-human gameplay coverage.

The [city-state slice](City-state-gameplay.md) adds shared native interactions,
Great Merchant missions, assigned quest observations and incident integration.
Its [guided live acceptance](../archive/reports/City-state-live-results.md) covers alliance competition,
quest rewards, gifts, protection and tribute across a restart. Prepared milestones
and scoped desktop comparison do not establish completion of every native quest
or unrestricted full-game coverage.

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
- The native [Next Turn context menu](../../vendor/Unciv/core/src/com/unciv/ui/screens/worldscreen/status/NextTurnMenu.kt)
  exposes explicit advancement past ordinary research/construction/policy picker
  prompts. The API currently implements the ordinary turn-button flow only.
  Adding deferral must respect native menu accessibility and modal capture/alert
  blockers, rather than blindly skipping every pending decision.

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

The [Great People slice](Great-people.md) covers both players' abilities and
on-demand research/policy observations, with native-state, generation, rollback,
restart and retry verification. Live-model and visual desktop acceptance remain separate.

The [third-party war inventory](Third-party-war-trades.md) covers paid/joint
declarations, own warnings, imported-request identity filtering, private peace
commitment advice, canonical outcomes and target events. HTTPS checks compare native
effects, including defensive pacts, through consent, scheduled turns, restart,
rollback and interrupted checkpoint writes. The [historical sprint record](../archive/plans/Sprints.md)
records the completed [enacted agreement lifecycle](Agreement-lifecycle.md),
including its separate automated, live model and desktop acceptance results,
and the completed [social-promise correction](Social-commitments.md) with its
scoped automated and live acceptance.

The [chat, memory and recovery slice](../guides/Chat-memory-and-recovery.md) adds asynchronous
communication, provenance-aware private notes, scoped proposal approval and explicit
whole-match restore. Its live two-player round is checked against host records and
native saves; it does not establish a completed match or full UI parity.

[Compact observations](Compact-observations.md) keep full alert content and tactical warnings in the turn overview, with cached city/unit/construction/map detail reads and explicit evidence delivery. This changes the model-facing presentation; existing engine coverage limits still apply.

The [victory and turn-decision slice](Victory-and-turn-blockers.md) adds shared
spaceship assembly, private world-leader ballots, permitted results, acknowledgement
and repeated-election processing. Victory Status also provides current rankings,
short recorded trends, bounded chart history and enabled demographics, with
visibility and tab settings enforced. Its prepared endgame and observation checks
do not establish an autonomous full-game acceptance run.

The [air warfare slice](Air-gameplay.md) adds shared rebasing, carrier transport,
air strikes and sweeps, guided/nuclear missiles, paradrops and aircraft economy.
Its [live exercise](../archive/reports/Air-live-results.md) and desktop comparison cover prepared
Vanilla milestones across a restart. Full UI parity and unrestricted full-game
acceptance remain separate work.
