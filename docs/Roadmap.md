# Current roadmap

Updated September 23, 2026. This is the current work summary; completed sprint
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

- **Memory semantics.** Historical citations and note-to-plan links no longer
  require active, reviewed sources. Revisions retain citations, can drop obsolete
  assumptions, and use short versioned detail-evidence handles. Concurrent edits,
  retained assumptions, and newer review evidence remain checked.
- **Memory telemetry.** Accepted/rejected edits, affected batches, and rejection
  reasons now come from the durable call journal and appear in host exports,
  console summaries, and the TUI separately from API success.
- **Small memory edits and recovery.** `amend` changes selected fields while
  preserving existing content and evidence checks. Errors explain citation limits
  and sources unavailable to the frozen memory job. Five amendments committed in
  the saved-position evaluation; the final recovery clarification has deterministic
  and retained-failure validation, without another live model run.
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

1. **Improve decisions from existing information.** France still lost a Settler
   after treating an adjacent Scout as protection, and Antium stayed at zero food
   surplus without citizen or focus changes. Greece preserved its Settler and
   reached a guarded site but did not found within four turns. Use the retained
   cases to assess civilian protection, growth intervention and settlement delays;
   the successful survival cases do not establish overall strategic improvement.
2. **Investigate recurring communication-read failures if they return.** The last
   review found eleven recoverable background MCP errors before a restart, with
   no established server-side cause.

Current efficiency behavior is documented in
[agent context](architecture/Agent-context.md),
[command updates](architecture/Command-state-updates.md),
[prompt caching](architecture/Prompt-caching.md) and
[background memory](architecture/Background-turn-memory.md).
The original numbered efficiency tasks and synchronous finalization cleanup are
implemented; do not reopen them from an archived checklist.

## Broader coverage

The [player API coverage ledger](reference/Player-api-coverage.md) is the detailed
inventory. Remaining areas include broader city/unit controls and information,
religion/espionage, embedded desktop-human play, and agent filesystem/network
isolation. Ordinary native turn-menu deferral also needs separate coverage from
the current turn-button flow. These are not all required for the bounded Vanilla
acceptance run and should receive their own scope and evidence.

Across this work, the host enforces authority and visibility; Unciv owns gameplay
rules. Social promises can inform decisions without prohibiting legal actions.
Keep automated regression checks, live model acceptance and desktop comparison
as separate evidence, and record which was actually performed.
