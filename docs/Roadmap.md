# Current roadmap

Updated October 6, 2026. This is the current work summary; completed sprint
checklists and measurements live in the [archive](archive/README.md).

## Active acceptance: complete an ordinary Vanilla game

Run Rome, Greece and France with `gpt-5.6-luna` at medium reasoning, built-in
Egypt, and two city-states from an Ancient start. The accepted setup is
Vanilla, Quick/Prince, Small Pangaea, with all five native victory types.
Agents choose their actions without guided milestones or privileged intervention.
Use the [full-game launcher](guides/Full-vanilla-run.md).

- [ ] Complete a full live game and record any unsupported actions or turn blockers.
- [ ] Reconcile final token, call and timing accounting, including interrupted
  attempts and off-turn communication, with the native winner and every player's score.
- [ ] Confirm recovery during the longer run using native saves and retained
  receipts; distinguish gameplay mistakes from API/service failures.

Per-turn accounting, checkpoints, victory actions and short live exercises are
implemented. The [latest saved-position evaluation](archive/reports/Tactical-memory-evaluation-2026-09-23.md)
completed 15 player turns across four isolated cases. Those results do not close
full-game acceptance.

## Observed follow-ups

Implemented from the live review; ordinary-game impact still needs measurement:

- **Private notebook.** Gameplay owns notebook revisions; communication appends
  contributions for the next activation. Review notices are separate, advisory
  records. Optional citations retain provenance without certifying an assessment.
  Notebook conflicts do not halt games; command-state checks remain enforced.
  Provisional strategy and next-turn handoff now replace persistent plans.
  Handoff updates cannot reaffirm strategy or clear its review notices. Format 4
  requires fresh private stores, without compatibility or migration. See the
  [current contract](architecture/Structured-memory.md) and
  [comparison procedure](scenarios/Strategic-positions.md#memory-comparisons).
  The [24-trial comparison](archive/reports/Strategy-handoff-evaluation-2026-09-25.md)
  completed 27 player turns without rejected memory edits; it does not yet
  establish better strategy or faster turns.
- **Memory telemetry.** Accepted/rejected edits, affected batches, and rejection
  reasons now come from the durable call journal and appear in host exports,
  console summaries, and the TUI separately from API success.
- **Tactical consequences.** Combat and movement details co-locate health,
  movement remaining, action availability and visible position context. These
  advisory fields preserve native legality and fog uncertainty. All 15 evaluation
  turns completed, but civilian protection and city growth still need work.
- **Settlement destinations.** Standing settler orders show visible destination
  eligibility and blockers. Existing action updates deliver discoveries without
  another preview request; uncertainty and native movement rules are preserved.
  The [focused live exercise](reference/Exploration-and-settlement.md#focused-live-exercise)
  delivered a newly revealed blocker, and Rome redirected on turn 0 and founded
  on turn 1.

Remaining recommendations:

1. **Clarify stopping after completed combat.** The
   [notebook evaluation](archive/reports/Notebook-redesign-2026-09-23.md#gameplay-and-the-independent-stop)
   stopped during Rome's turn 38 after a 1-HP Warrior dealt 1 damage instead of
   the preview's predicted 12. The engine now uses Civ V's simultaneous damage
   exchange and survivor adjustment, and previews share that calculation.
   [Replaying the saved attack](reference/Combat.md#lethal-exchange-regression)
   dealt 11 damage and left the Brute at 1 HP, within the corrected preview.
   A confirmed receipt with enemy health hidden by
   lost visibility should not trigger the uncertain-dispatch stop. The saved
   checkpoint reproduces the issue; Rome's five-turn evaluation remains incomplete.
2. **Improve decisions from existing information.** France still lost a Settler
   after treating an adjacent Scout as protection, and Antium stayed at zero food
   surplus without citizen or focus changes. Greece preserved its Settler and
   reached a guarded site but did not found within four turns. Use the retained
   cases to assess civilian protection, growth intervention and settlement delays;
   the successful survival cases do not establish overall strategic improvement.
3. **Investigate recurring communication-read failures if they return.** The last
   review found eleven recoverable background MCP errors before a restart, with
   no established server-side cause.
4. **Improve long-horizon strategic conversion.** The
   [October 6 four-game audit](archive/reports/Benchmark-agent-play-audit-2026-10-06.md)
   found that agents usually led in science or military force but trailed the
   built-in control in cities, population, production, wonders, and total score.
   Prioritize a permitted comparative conversion audit, a compact persistent
   execution plan, explicit treasury and expansion decisions, and a five-turn
   wartime progress review. Validate them from fixed checkpoints before adding
   more general strategy prose.

Current efficiency behavior is documented in
[agent context](architecture/Agent-context.md),
[command updates](architecture/Command-state-updates.md),
[prompt caching](architecture/Prompt-caching.md) and
[background reviews](architecture/Background-turn-memory.md).
The original numbered efficiency tasks and synchronous finalization cleanup are
implemented; do not reopen them from an archived checklist.

## Broader coverage

The [player API coverage ledger](reference/Player-api-coverage.md) is the detailed
inventory. Remaining areas include broader city/unit controls and information,
religion/espionage, broader human multiplayer/controller lifecycle, and agent filesystem/network
isolation. Ordinary native turn-menu deferral also needs separate coverage from
the current turn-button flow. These are not all required for the bounded Vanilla
acceptance run and should receive their own scope and evidence.

One local human can now join the authoritative desktop using `--human` with a
disjoint agent roster. The integrated communication/trade panel, human scheduler
wait and coordinated rewind are documented in the
[launcher guide](guides/Full-vanilla-run.md#play-a-civilization-yourself).
Long human–agent matches and exhaustive desktop-action acceptance remain open.

Across this work, the host enforces authority and visibility; Unciv owns gameplay
rules. Social promises can inform decisions without prohibiting legal actions.
Keep automated regression checks, live model acceptance and desktop comparison
as separate evidence, and record which was actually performed.
