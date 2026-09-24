# Tactical and memory evaluation — September 23, 2026

All 15 scheduled player turns completed across four isolated saved positions.
Provider, agent and host token totals reconcile. No gameplay API errors or service
crashes occurred. The new interfaces work, but civilian protection and stalled
city growth remain weaknesses in model decisions.

## Changes tested

- Combat options place current HP, estimated remaining HP, possible destruction
  or capture, attacker consumption and visible position context beside the action.
- Movement previews show remaining movement, attack/founding availability,
  currently visible nearby hostiles, nearby own military and unseen tile counts.
  These describe current information; they do not simulate an opponent's next turn
  or guarantee protection. Native-legal risky actions remain available.
- Memory supports `amend` for changing selected content fields while retaining
  the rest. Evidence and version checks still apply. Source-limit errors explain
  the maximum of 20 citations and how to repair the submitted edit.
- Four historical failures are now reusable saves. Reports compare initial/final
  native positions, preserve commands, count partial memory failures and reconcile
  token accounting.

See [tactical fields](../../reference/Tactical-review.md),
[memory semantics](../../architecture/Structured-memory.md) and
[fixture preparation and launch commands](../../scenarios/Strategic-positions.md).

## Setup

The four new cases come from seed `1283182` in the retained Vanilla game. Each
used `gpt-6-luna`, max reasoning, fast priority and the optional strategy guide.
Other major civilizations used native AI. Each subject started with fresh private
memory and a fresh social ledger. The original match was read and copied only.

Agents received the normal objective to win, with no prescribed solution. Control
changes and any advance to the subject's turn are recorded during preparation;
the fixtures do not author changes to terrain, units, technology or treasury.
These are single runs, not exact replays of the historical agents or opponents.

## Outcomes

| Position | Player turns | Result |
| --- | ---: | --- |
| Scout survival | France 41–43 | All five starting units survived. Scout withdrew and healed from 24 to 64 HP. |
| Settler escort | Greece 33–36 | All four starting units survived; a camp was cleared. Settler reached `(2,-5)` beside Warrior `(2,-4)`. No city founded within four turns; arrival exhausted its movement. |
| Civilian exposure | France 39–41 | Settler was captured on the first opponent turn and remained with Barbarians. Both starting Workers survived. Scout survived at 37 HP after a failed recovery attempt and retreat. |
| Stalled city | Rome 35–39 | Antium remained population 1 with zero food surplus and a Library queued. Rome recovered a Worker, and its injured Warrior withdrew and survived at 27 HP. |

France's civilian move preview showed zero movement remaining, founding
unavailable that turn, six unseen nearby tiles and no nearby own military. France
then moved a Scout adjacent and treated it as protection. That did not prevent
capture. Native unit identity and ownership confirm the loss; it is not inferred
solely from a missing roster entry.

Antium's stored food rose from 2 to 4, but both its last handoff observation and a
native reload of the final checkpoint show zero surplus and no projected growth.
No command changed its citizens or focus. Rome researched Trapping to enable a
future Deer improvement, but the five-turn evaluation did not resolve growth.

## Usage and memory reliability

| Position | Model requests | Gameplay seconds | Finalization seconds | Accepted / rejected memory edits |
| --- | ---: | ---: | ---: | ---: |
| Scout survival | 37 | 697.8 | 97.5 | 2 / 0 |
| Settler escort | 55 | 761.6 | 201.7 | 4 / 0 |
| Civilian exposure | 51 | 758.7 | 212.4 | 1 / 2 |
| Stalled city | 68 | 679.1 | 371.7 | 4 / 2 |

Total: 211 model requests; 10,458,177 input tokens, including 9,464,832 cached;
993,345 uncached input; 266,152 output, including 243,016 reasoning. Summed
gameplay took 48.3 minutes and finalization 14.7 minutes. Games ran in parallel,
so these sums are not elapsed wall time.

Five amendments committed successfully. Of four rejected edits, Rome recovered
from one version conflict and one undelivered-source rejection. France repeated
its undelivered-source rejection and did not commit its proposed final plan
revision. The previous record and review flags remained intact.

The rejected French citations concerned an Open Borders transaction handled by a
separate communication job, outside the gameplay job's frozen evidence. Following
the run, the recovery response was clarified to list `unavailableSources`, explain
that rereading a plan does not deliver those sources, and advise against retrying
an unchanged rejected edit. Evidence checks remain enforced. This final change
was verified against both retained rejections using read-only databases and 55
focused tests; the live metrics above precede it.

## Validation and limits

- Full Python suite: 704 passed, 2 opt-in skips, 90 subtests passed. After the final
  memory-recovery clarification, 55 focused tests passed. Ruff, formatting and
  type checks passed.
- Full native suite: 115 passed and one stale city-test expectation failed.
  Research can reassign citizens; the test now rereads worked tiles before its
  next assertion. All eight selected city, tactical and air checks then passed.
- All nine saved fixtures validated and reloaded. Tactical checks cover lethal
  projections, city capture, missile consumption, movement exhaustion, founding,
  future-route uncertainty, read-only previews and hidden-enemy privacy.
- Compact turn overviews are unchanged by the new action-detail fields. Detailed
  action reads are larger. This unpaired evaluation establishes no reduction in
  calls, tokens or latency and no causal improvement in strategy or win rate.

The remaining decision problems are reproducible: an adjacent Scout was treated
as sufficient civilian protection, and a zero-surplus city received no direct
growth intervention. Longer or repeated paired evaluations are needed to assess
reliability. The successful survival cases do not close those issues or full-game
acceptance.

## Artifacts

Local artifacts are retained under `build/tactical-evaluation-20260923/`:
`results.json`, `summary.json`, four `<case>-report.json` files, the detailed
`report.md`, `memory-recovery-replay.json`, logs, checkpoints, transcripts and
read-only analysis inputs. Final Antium observations are retained under
`build/tactical-final-facts/observed/stalled-city-final/initial-turn.json`.

The live source-tree fingerprint is
`c84d58800251da8949b7e6480a1dbc1cb70c1116c5a49b94ea8e80581c7fdefc`.
After the recovery clarification it is
`1144d39d60a7dd2be9ab68605d316e0d17ba6994209a65b793251032ea25f164`.
Fingerprints include tracked and untracked, nonignored files beneath `src/`.
