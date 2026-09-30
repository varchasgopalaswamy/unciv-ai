# Agent-facing native flavor audit

Date: 2026-09-29

Greece returned France's recovered Settler after reading the native claim that
France would be grateful. That sentence describes anticipated player behavior;
it is not a negotiated commitment. The capture prompt now exposes the choice
and its actual unit outcome without that claim.

## Surfaces reviewed

| Surface | Treatment |
| --- | --- |
| Recaptured civilian | Remove gratitude narrative; keep original-owner identity, return/keep unit outcomes, availability and commands. |
| Conquered/traded city and city management | Replace the liberation option's “massive relationship boost” with neutral native opinion/influence effects; keep annexation, puppet, razing and availability explanations. |
| Built-in AI diplomacy | Remove opinion-modifier speeches; retain modifier names and amounts. Existing projection already removes stock demand/response, promise and friendship dialogue while keeping mechanical consequences. |
| Introductions, contact, war and defeat popups | Existing projection removes stock speeches and acknowledgement wording. Identities and event kinds remain. |
| Research | Existing projection removes quotations and the Civilopedia narrative prefix; retain unlocks, requirements and effects. |
| Wonders and construction | Wonder quotations are already omitted. Building/unit descriptions and wonder benefits use mechanical stats, uniques, requirements and estimates. |
| Policies, promotions, improvements, Great People | Audited the native description methods: effects, requirements and action consequences remain useful gameplay information. |
| City-state quests | Existing structured objectives replace quest narrative, preserving targets, deadlines and influence rewards. Unknown objectives retain their description to avoid losing instructions. |
| City-state notifications | Replace gratitude, outrage and pleas with factual rewards, quest requirements, requests and native diplomatic effects. Preserve unknown-civilization filtering. |
| Ancient-ruin notifications | Replace artifacts, tribes, prophecies and map stories with the actual reward/event; preserve target technology/city and Faith amount. Culture's fixed narrative amount is identified as the base reward subject to game speed. |
| Other ordinary notifications | Clarify religious-unit destruction and Long Count rewards. Retain factual combat, construction, economic, diplomatic and city events. |
| Victory status and results | Victory requirements and milestone progress are mechanical and remain. Remove result titles as well as paragraphs: generic defeat stored its narrative in the title. Outcome, winner, victory type and turn remain. |
| Agreements and trades | Native invitations are already filtered. Terms, durations, expiry/cancellation facts and results remain. Agreement lifecycle reads raw notifications internally but exposes structured ending reasons. |
| Player-authored communication and memory | Preserve messages, negotiated promises and private assessments exactly, including ambiguous or emotional language. |

## Boundaries

Python's `model_view` matches owned response shapes, without a global filter on
words such as “grateful” or keys such as `text`. It applies to fresh tool results,
preloads, receipts, replay, history, evidence and model workspace exports. Original
host evidence remains intact.

Ordinary notification replacements live in `NotificationGameplayText.kt`. They
match audited native templates before translation, then apply the existing
visibility filter. Unknown templates pass through rather than losing potentially
important mechanics. The replacements do not change native notification storage,
quests, influence, AI opinion or any gameplay rules. Numbers follow the pinned
engine and bundled rulesets; new engine wording needs a corresponding audit.

Existing agent-authored beliefs are not rewritten. No engine changes, database
migration or compatibility layer are introduced by this presentation change.

## Validation

`tests/test_model_view.py` covers the recovered French Settler choice, city
liberation/control effects, AI opinion, result speeches, player-authored text,
idempotence and unmodified audit records. It also checks projection through
startup, details, receipts, replay, historical evidence and workspace archives.

`NotificationsIntegrationTest` exercises real native city-state barbarian and
military-assistance events, checking their influence changes against the factual
notifications. It verifies hidden opponent names, event locations, untouched
native audit data and bundled ruin templates. The existing notification
dismissal/restart and informational-alert integration checks remain in use.
