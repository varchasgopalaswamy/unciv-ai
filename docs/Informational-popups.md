# Informational popup content and acknowledgement

Ordinary in-game informational popups are available in `player_turn.alerts`:

| Kind | Content supplied by the shared desktop query |
| --- | --- |
| `StartIntro` | Displayed leader caption and both civilization introduction paragraphs |
| `TechResearched` | Technology name, quote and the description for the observing civilization, including its applicable unlocks |
| `WonderBuilt` | Wonder name, optional quote and short description |
| `GoldenAge` | Heading and the Golden Age explanation |
| `WarDeclaration` | Declaring leader, declaration header and nonempty national declaration; both equivalent acknowledgement labels |
| `FirstContact` | Previously supported leader introduction and acknowledgement, also exposed in the common content format |

Each supported alert has `content` containing `title`, ordered `paragraphs`,
nullable `quote`, `acknowledgement`, and any `additionalAcknowledgements`. These are the same text sources used by
the desktop popup, translated as its labels are. Descriptions are not shortened
or replaced by identifiers. Images, voice playback and Civilopedia navigation
remain separate presentation surfaces.

The agent must read all of this content, write a brief public note about what the
popup says and any useful gameplay information, then submit `ACKNOWLEDGE_ALERT`
for the offered handle when `canAcknowledge` is true. Both interactive and scheduled
player prompts give these instructions. The existing service/scheduler histories
retain the full sanitized tool response and the agent's public note.

The host requires that `player_turn` has returned the current revision before a
new acknowledgement. A revision returned only in a command receipt does not
qualify: the command fails with `OBSERVATION_REQUIRED` until the player fetches
and reads `player_turn`. Popup text participates in the player revision, so a
changed description makes an earlier request stale. Public-query tracking resets
on process restart; a pending acknowledgement needs another read, while an exact
retry of a committed acknowledgement still returns its durable receipt.

The gate records snapshot generation for the bound player, shared across that
player's sessions. Reading and summarizing remain agent instructions; the host
cannot verify network delivery or comprehension. The public note and full tool
history make the agent's handling of each popup reviewable.

Reading is available while paused or waiting, with acknowledgement disabled.
Acknowledgement removes only the selected player-owned message. Research,
construction, Golden Age and encounter effects have already happened and are
never applied again by dismissal. Missing technology/wonder references and
unsupported alert types have no offered acknowledgement. An informational alert
in a mixed queue cannot dismiss a consequential choice alongside it.

War and peace are described in [their implementation slice](War-and-peace.md).
Broader diplomacy, trade, combat/captured-city, religion and espionage decisions retain
their respective implementation slices. Ruleset `Event` dialogs can have gameplay
choices and are not generic informational popups. End-of-game victory/defeat
screens also remain a separate lifecycle: scheduling stops on game completion,
and simply adding a victory alert to the acknowledgement list would not deliver
the result to both players. See the [encounter inventory](Encounter-and-diplomacy.md)
for those outstanding surfaces.

Validation includes shared engine content/authority tests and the HTTPS
`InformationalAlertsIntegrationTest`: complete popup content, civilization-specific
technology descriptions, stale/foreign/invalid requests, observation before
acknowledgement, restart with pending messages, and exact receipt replay.
