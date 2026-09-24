# Private notebook redesign — September 23, 2026

The notebook now stores an agent's assessments without treating them as command
preconditions. Gameplay owns revisions; communication appends contributions for
the next gameplay activation. Observation changes produce separate advisory
notices, so they cannot race a notebook edit by changing its version.

**Start a fresh match.** Memory format 3 rejects previous databases and checkpoints
without modifying them. There is no migration or old API adapter. Current-format
restart, exact tool-call replay and whole-match rollback remain supported.

## Changes

- `Notebook` owns `create`, `amend` and `retire`. The `revise` and `replace`
  operations and their compatibility paths are removed.
- Citations are optional historical references. Unavailable references produce
  warnings on accepted assessments; they do not grant access to unseen evidence
  or certify the assessment. Code records the source job, purpose and turn.
- A plan's retirement, age or review notices cannot independently veto a legal
  command. Authority, game-state freshness, native legality, receipts and
  duplicate-execution protection still apply.
- One gameplay conversation owns notebook writes through its background memory
  phase. Communication contributions are immutable; arrivals after the
  activation's snapshot become available next time.
- Notices have their own revisions and do not propagate through a dependency
  graph. Edits acknowledge notices actually delivered to that conversation.
  Background reads cannot pull in later observations, chat or contributions.
- Each accepted edit returns its new record and version. Search-cache updates
  use the delivered result instead of silently reading newer state.
- Conflicts with an unread notebook entry are local `MEMORY_CONFLICT` outcomes.
  Independent valid edits still commit. Repeated command-state conflicts retain
  their terminal diagnostics. Telemetry counts rejected edits independently of
  API/transport success.

The current API and durability contract is in
[private notebook and observation history](../../architecture/Structured-memory.md).

## Deterministic validation

The full Python suite passed **710 tests and 90 subtests**, with two opt-in
installed-Codex tests skipped. After the final handoff/storage cleanup, 23 focused
tests passed. Ruff lint, formatting, Pyrefly and diff checks passed. This change
does not modify the engine and requires no engine or package build.

Regression coverage includes local repeated conflicts and partial batches,
write ownership, contribution visibility at activation boundaries, immutable
historical evidence, unavailable references, exact notice delivery across handoff,
restart fencing, rollback's discarded future, monotonically allocated IDs,
current-format checkpoint verification, old-format rejection without mutation,
and retained command-state failure diagnostics.

## Saved-position comparison

The runs are retained under `build/notebook-evaluation-20260923/`.
France completed turns 39–41. Rome completed turns 35–37, then stopped during
turn 38 following the combat issue below. **Six of eight scheduled turns completed;
the suite correctly reports Rome's case as incomplete.** There were no API errors,
memory rejections, unresolved requests or service crashes.

The cases were `civilian-exposure` and `stalled-city`, which produced the previous
evaluation's memory failures. Both used the same
prepared save, engine revision, GPT-6 Luna at max reasoning, fast priority,
optional strategy guide, fresh private memory and native-AI opponents as the
[previous evaluation](Tactical-memory-evaluation-2026-09-23.md).

### Comparable completed turns

Compare only France 39–41 and Rome 35–37 in each run. These counts include the
gameplay conversations' background memory work; separate communication jobs and
Rome's unfinished turn 38 are excluded from this comparison.

| Metric | Previous implementation | Notebook |
| --- | ---: | ---: |
| Completed player turns | 6 | 6 |
| Model requests | 87 | 76 |
| Gameplay seconds, summed | 1,100.0 | 1,142.7 |
| Finalization seconds, summed | 376.9 | 163.3 |
| Accepted / rejected memory edits | 3 / 3 | 5 / 0 |
| Input tokens, including cached | 4,240,868 | 3,809,406 |
| Uncached input tokens | 404,196 | 353,406 |
| Output tokens, including reasoning | 101,601 | 89,316 |

Finalization includes memory work and service bookkeeping. Intervals from parallel
runs must not be added to estimate wall time. The observed reduction is concentrated
after handoff; gameplay itself did not become faster in this sample.

### Notebook and accounting results

Across all jobs there were **seven accepted edits and zero rejected edits**: five
gameplay notebook edits and two communication contributions. Direct inspection of
the databases confirms that communication never wrote a notebook entry and notices
never appeared in stored notebook content. The contributions arrived during
France's last activation and remained outside that writer's captured inbox, ready
for a subsequent activation. Incorporation on a later turn is covered by the
deterministic boundary tests, rather than this bounded live run.

Every finalized job's provider, agent and host token counts reconcile, including
the three communication jobs and Rome's unfinished turn 38. All jobs together
used 95 model requests, 4,303,326 input tokens (3,860,736 cached), and 109,950 output
tokens (99,138 reasoning). These larger totals include work excluded from the
six-turn comparison above. The incomplete native turn remains marked active and
is not reported as completed accounting for a finished turn.

### Gameplay and the independent stop

France again lost Settler 58 to Barbarians on the first opponent turn. The final
native checkpoint still records Barbarian ownership; the Scout survived at 37 HP
and both starting Workers survived. France queued a replacement Settler.
Antium remained population 1 with zero food surplus and a Library queued at the
Rome stop. These decisions do not establish strategic improvement.

On Rome's turn 38, a combat preview for its 1-HP Warrior against a 12-HP Brute
reported `minDamageToDefender == maxDamageToDefender == 12` and both units'
projected remaining health as zero. The agent chose a sacrificial attack expecting
to remove the Brute. The confirmed receipt instead reported:

```json
{"resolution":"Completed","attackerDestroyed":true,"attackerHealth":0,"damageToAttacker":1,"damageToDefender":1,"defenderDestroyed":false,"defenderHealth":null}
```

The receipt was accurate. The operator checkpoint shows the Brute at 11 HP. Its
health became unavailable to Rome after the Warrior died and visibility changed.
The returned state update removed the Warrior. The agent nevertheless stopped,
describing the defender's status as unclear, without ending its turn. There was
no uncertain dispatch, repeated-stale stop or notebook failure.

Two follow-ups are needed before repeating this full case:

1. Make combat projections account for an attack ending when either combatant
   dies. `PlayerCombatOperations.preview` exposes potential damage estimates;
   `Battle.takeDamage` resolves melee damage in individual hits and stops at the
   first defeat. `TacticalConsequences.kt` currently turns the potential estimates
   into remaining-health ranges that look certain in this case. Preserve native
   rules and visibility while correcting that representation.
2. Clarify the agent's stopping guidance: unavailable enemy information after a
   confirmed action is different from uncertainty about whether a request ran.
   A confirmed loss and a successful state update permit continued gameplay.

The turn-38 round checkpoint and `combat-stop.json` retain the reproduction.
Neither follow-up is implemented by this memory change; the stopped case was
not automatically resumed or counted as a successful five-turn evaluation.

These are matched starting positions with one run per implementation. Opponent
actions, provider latency and model choices can differ; a timing difference alone
does not establish a causal speedup or better strategic judgment.

## Artifacts

`build/notebook-evaluation-20260923/` contains `results.json`, per-case reports,
`comparison.json`, `notebook-audit.json`, `validation.json`, `combat-stop.json`,
the baseline's first-three-Rome-turn report, logs, transcripts and checkpoints.
The original Vanilla match and prior evaluation files were unchanged.

Live source fingerprint:
`5bb76e1be49fbce403d768993a159a744fc9275a3077c584c92556433f6a270f`.
Engine revision: `5f014834c8a697c1c54943ffe545dfe95bd040d8`.
