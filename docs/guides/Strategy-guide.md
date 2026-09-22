# Optional player strategy guide

The [strategy reference](../../src/civ_agent/strategy_guide.md) is about 1,640 words,
roughly three to four pages. It covers exploration and expansion, city economics,
research and policies, diplomacy and combat, and completing a victory. It explicitly
labels itself as suggestions that players may ignore or adapt. It sets no build
order, city count, mandatory diplomatic response, or gameplay restrictions.

## Enable or disable

The guide is **off by default**. To supply it to Rome, Greece, and France:

```sh
.venv/bin/python scripts/run_vanilla_game.py --strategy-guide
```

The full-game launcher and `python -m tests.live_vanilla` both accept
`--strategy-guide` and `--no-strategy-guide`. For one civilization's service:

```sh
.venv/bin/civ-agent agent-server start Rome --strategy-guide
```

The setting applies to that service's scheduled gameplay, communication jobs,
and interactive sessions. It does not affect built-in AI players. Pass the same
flag again when restarting services or using `--resume`; the default for each
launch is off. Changing it changes subsequent startup prompts; it cannot erase
text already delivered in a conversation being recovered.

## Prompt size and caching

The service loads the guide as a package resource, independent of its working
directory. There are no extra model requests, player-tool reads, or runtime web
lookups. When enabled, the text appears once at the beginning of user context,
labeled as optional reference suggestions, before private memory and observations.
The developer message contains the operational contract and tool definitions.
Interactive startup likewise places
the reference before changing context. When disabled, the preexisting prompt
text is unchanged.

The reference is 10,867 characters after trimming its trailing newline.
Allow roughly **2,500–3,000 additional tokens** as an estimate, not a tokenizer or
provider measurement. This adds about 1.0–1.2 percentage points of the effective
context window observed below. Prefix placement permits cache reuse, but does
not guarantee cache hits; cached tokens still occupy context. See
[prompt caching](../architecture/Prompt-caching.md) for context layout and the measured client limitations.

Each scheduled job's `startup.strategyGuide` records `enabled`, `characters`,
and a SHA-256 fingerprint of the exact reference. Existing instruction/prompt
character counts include the added text. Full-game `launch.json` and checkpoint
comparison `live-run.json` also record `strategyGuide` as a boolean. Histories
retain the complete delivered prompt, but normal console output still omits
preloads unless `--verbose` is given.

### Context audit, 2026-09-21 local time

Published model specifications list a **1,050,000-token** context window and
128,000 maximum output tokens for the current GPT-5.6 family and GPT-6 Astra.
These are model limits, not a guarantee that a client enables the entire window.
Sources: [GPT-5.6 Luna](https://developers.openai.com/api/docs/models/gpt-5.6-luna),
[GPT-5.6 Terra](https://developers.openai.com/api/docs/models/gpt-5.6-terra),
[GPT-5.6 Sol](https://developers.openai.com/api/docs/models/gpt-5.6-sol),
[GPT-6 Astra](https://developers.openai.com/api/docs/models/gpt-6-astra).

The installed Codex catalogue lists 272,000 tokens with a 95% effective window
for Luna, Terra, Sol, and Astra. The game agents' provider traces actually report
**258,400** as `model_context_window`, so that is the relevant comparison for
this run. No context-window override was present in the inspected local config.
Codex exposes separate context-window and compaction settings in its
[configuration reference](https://learn.chatgpt.com/docs/config-file/config-reference).

The audit sampled the 16 completed gameplay activations then available in
`~/.local/state/unciv-host/vanilla-26_09_21`: all three players' turns 0–4 and
Rome's turn 5, spanning 129 model requests. It matched activation `threadId`s
to Codex session traces and read per-request `last_token_usage`, deduplicating
unchanged cumulative usage notifications. The concurrently starting job was
excluded. These figures describe that snapshot, not later turns of the live run.

| Measurement, guide disabled | Observed size | Share of 258,400 |
| --- | ---: | ---: |
| Full opening request, including Codex/tool overhead | 15,839–16,594 tokens | 6.1–6.4% |
| Largest input after tool exchanges | 36,232 tokens | 14.0% |
| Largest request including reported output | 36,470 tokens | 14.1% |
| Compactions recorded | 0 | — |

Service-supplied preloads were 37,101–40,131 characters: 29,609 stable instruction
characters plus 7,492–10,522 changing characters. Character counts are useful
for comparing payloads; the provider counts above measure the complete request.
With the guide, an opening request would be approximately 18,300–19,600 tokens,
or 7–8% of the observed effective window. This is an estimate pending a live run
with the option enabled.

The large token totals reported at turn end add together repeated model requests;
they do not describe one context window. Ordinary turns start fresh conversations
with selected durable memory, rather than retaining the entire game's transcript.
There is ample capacity in this opening sample. Larger empires, wars, long tool
exchanges, and recovery within a turn may increase context later. Recheck actual
per-request usage when inspecting those stages; this audit does not establish
late-game limits or strategic quality.

## Source selection and scope

The following CivFanatics articles were written by players in 2011 and are marked
Vanilla where applicable; some resource pages were rehosted later. They cover
movement, happiness, roads and harbors, social policies, research agreements, and
ranged combat. The reference summarizes general tradeoffs in original prose.
It omits fixed opening scripts, patch-specific formulas, and exploit advice.
Victory descriptions were also checked against the selected engine's Vanilla
`VictoryTypes.json`; old guides are not authoritative about engine behavior.

- [Unit Movement in Civilization V](https://forums.civfanatics.com/resources/unit-movement-in-civilization-v-vanilla.25661/)
- [Complete Guide to Happiness](https://forums.civfanatics.com/resources/complete-guide-to-happiness-vanilla.25584/)
- [Social Policy: Liberty](https://forums.civfanatics.com/resources/social-policy-liberty-vanilla.25656/)
- [Social Policy: Tradition](https://forums.civfanatics.com/resources/social-policy-tradition.25644/)
- [Trade routes, roads, railroads, and harbors](https://forums.civfanatics.com/resources/how-do-trade-routes-roads-railroads-harbours-work-vanilla.25620/)
- [Social Policy: Rationalism](https://forums.civfanatics.com/resources/social-policy-rationalism-vanilla.25662/)
- [Research Agreements](https://forums.civfanatics.com/resources/research-agreements-vanilla.25574/)
- [Social Policy: Patronage](https://forums.civfanatics.com/resources/social-policy-patronage-vanilla.25663/)
- [The Mechanics of Combat](https://forums.civfanatics.com/resources/the-mechanics-of-combat-vanilla.25621/)
- [Ranged Combat](https://forums.civfanatics.com/threads/ranged-combat.437132/)

Source links stay in this operator document and are not sent to the agent.

The current full-game setup uses Vanilla. The guide does not import Brave New
World tourism, international trade-unit systems, or ideologies. It may still
contain useful general advice for another ruleset. It directs attention to the
current position, unit capabilities, and victory requirements. No claim of
improved play is made until enabled and disabled live runs can be compared.

## Validation

The focused startup, service, communication, CLI, full-game launcher, and interactive
launcher suites pass: 90 tests plus 22 subtests. They cover optional inclusion,
unchanged prompts when disabled, one copy in the stable prefix, both job types,
resource access outside the checkout, and forwarding the option to all three
agent services. Ruff lint/format and Pyrefly also pass. No live game was launched
for this change; the context audit reads an existing run.
