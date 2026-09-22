# Current roadmap

Updated September 22, 2026. This is the current work summary; completed sprint
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
implemented. Those results do not close full-game acceptance. The
[latest reviewed run](archive/reports/Live-game-review-2026-09-22.md) had reached
turn 30 without a completed game.

## Observed follow-ups

These remain recommendations from the live review, not completed changes:

1. **Reduce memory repair work.** Avoid pre-handoff revision loops and unnecessary
   rereads of old plan dependencies while preserving evidence provenance.
2. **Expose partial memory rejections in telemetry.** Count accepted/rejected edits
   and reasons separately from transport-level API success.
3. **Improve decisions from existing information.** Review counterattack risk,
   delayed military replacement, scouting before settlement and stalled city
   growth. Consider showing the visible settlement status of a standing movement
   order; preserve uncertainty and player visibility.
4. **Investigate recurring communication-read failures if they return.** The last
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
