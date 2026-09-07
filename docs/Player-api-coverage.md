# Player API coverage ledger

The following is a subsystem inventory, not a claim of exhaustive parity. Before a
row is complete, enumerate its individual UI controls, tooltips, notifications,
previews, shortcuts, and ordinary settings-dependent behavior. Each item needs its
API operation, UI source, engine rule, positive test, negative authority/privacy
test, and UI comparison evidence. Unsupported items remain explicit.

| Surface | Required information and actions | Current coverage |
| --- | --- | --- |
| Player identity | Fixed civilization, session lifetime, turn ownership, elimination | Prototype binding and summary only |
| Turn flow | Pending choices, actionable options, optional prompts, end turn, automation timing | Shared requirements, choices and end turn tested for both bundled rulesets; automation timing and broader decisions incomplete |
| Map | Exploration, fog, remembered improvements, terrain, rivers, resources, ownership, yields | Not implemented |
| Tile inspection | Yield breakdowns, resource reveal, city and unit details, work status | Not implemented |
| Unit movement | Own units, paths, movement costs, movement plans, embarkation, transport | Not implemented |
| Unit actions | Found city, worker jobs, pillage, fortify, sleep, skip, explore, automate, upgrade, disband, gift, rename | Not implemented |
| Combat | Legal targets, human-visible previews and precision, ranged/melee/city attacks, interception, nuclear actions | Not implemented |
| Promotions | Available promotions, prerequisites, descriptions, choice and naming | Not implemented |
| City overview | Population, food, production, happiness, culture, borders, yields and breakdowns | Names only |
| City management | Rename, citizens, specialists, focus, growth, tile purchase, tile reassignment | Shared validated rename; other controls missing |
| Construction | Available/disabled entries and reasons, progress, queues, reorder/remove, purchases, placement | Queue discovery and append without tile placement; detailed reasons, progress and other controls missing |
| Captured cities | Annex, puppet, raze/stop, liberate, occupation, resistance | Not implemented |
| Research | Tree, descriptions, progress, costs, prerequisites, queues, free choices, repeatable tech | Shared queue validation and free choices; detailed observations incomplete |
| Policies | Branches, requirements, costs, adoption, free choices, postponement | Shared adoption and picker dismissal; detailed observations incomplete |
| Great people | Points and thresholds, free choices, placement failure, special actions, Maya restrictions | Shared free choices with revision and durable receipts; other actions and detailed observations incomplete |
| Diplomacy | Known civilizations, relations, modifiers, war/peace, treaties, denunciations, promises, demands | Not implemented |
| Trade | Offers, counteroffers, validity, resource/gold/city transfers, acceptance/rejection, expiry | Not implemented |
| City-states | Influence, quests, gifts, protection, bullying, war/peace, allies | Not implemented |
| Religion | Pantheon, founding/enhancing/reforming, beliefs, pressure, conversion, religious units | Not implemented |
| Espionage | Spy status, assignment, promotion, stealing technologies, elections, coups, reports | Not implemented |
| Economy | Treasury, income and expense breakdowns, resources, happiness, connections | Prototype treasury only |
| Overviews | Cities, units, trades, resources, religion, demographics, rankings, wonders, victory progress | Not implemented |
| Notifications | Per-player alerts, transient combat/movement history, popups, choices, navigation targets | Informational alert acknowledgement; consequential alerts fail closed; detailed observations incomplete |
| Communication | Player-addressed chat, participant identity, transcript visibility | Not implemented |
| Civilopedia | Public bundled rules, searchable entries, descriptions and links, no hidden game data | Not implemented |
| Victory and defeat | Conditions, results, scores, permitted postgame information, resignation | Not implemented |
| Lifecycle | Supported setup validation, create/load/save/pause, checkpoints, reconnect, controller replacement | Load, checkpoint/resume, serialized execution, operator pause, player-scoped references and durable command receipts; setup and controller management incomplete |
| Agent runner | Codex conversations, private notes, turn scheduling, budgets, isolation | Not implemented |
| Desktop integration | Shared execution and player API across all supported controls | Shared requirements and selected picker operations; embedded executor and full UI migration outstanding |

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
