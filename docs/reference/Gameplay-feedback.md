# Gameplay feedback improvements

Implementation following the October 10 Luna gameplay audit. Factual history and
context assembly use Python/Kotlin. The existing player model interprets evidence
and chooses actions; no additional summarizing model or automatic adjudication is
introduced.

1. Preserve a bounded, checkpointed history of permitted observations and combine
   it with confirmed receipts. Show repeated orders alongside observed unit,
   exploration and construction progress, including actions without plan links.
2. Compare continuing output, building then selecting an available conversion, and
   purchasing then selecting that conversion. Use native previews, expose queue
   operations separately, and retain end-of-horizon assets and incomplete progress.
3. Bring changed facts into plan reviews: observed unhappiness, food, imports,
   access, resource ownership and interrupted development. Preview empire-wide
   happiness and food effects without projecting future population or citizen choices.
4. Supply recent conversation excerpts grouped by exact audience, with source IDs
   and dates, plus existing player-authored notes. Clarify victory milestones as
   requirements; the player interprets replies, commitments and strategic relevance.
5. Group explicitly linked investment receipts with observed results and current
   upkeep. Keep unlinked expenditure visible separately. Invite reconsideration
   when spending or renewing peace; never require aggression or enforce promises.

Validation: native preview equivalence and non-mutation, host integration,
history replay/restart/rewind, audience separation, pending-versus-enacted trades,
and regressions based on the audited stalled Scout, delayed Library, unhappy
economy and upgrades during peace. Behavioral gains require subsequent controlled
model runs; passing software tests alone does not establish stronger play.

Validated October 11, 2026: the full host build passed (162 tests), the native
`BuildingYieldPreviewTest` suite passed (9 tests), and the Python suite passed
(1,333 tests and 76 subtests; 10 optional environment-dependent tests skipped).
Ruff formatting/lint, Pyrefly, and both repositories' whitespace checks passed.
A read-only replay of the original audit's player observations recovered France's
17 repeated Scout orders with zero exploration, Persia's unchanged Library work,
Inca's 13 observed unhappy turns, China's 680-gold upgrades, and Arabia's missing
Settler. See the retained [replay artifacts](../../build/luna-feedback-replay-20261011/summary.json).

The additional operator-requested policy blocks Gold, Science and Culture
production for API-controlled players before the selected ruleset's final era
(Future era in the bundled Vanilla rules). Other native prerequisites still apply
after that era. The host removes blocked saved/automated queue entries while
preserving ordinary construction work, exposes the restriction in
`productionPolicy`, rejects explicit queue requests, and excludes blocked
conversions from investment alternatives. Native AI control is unchanged.

`recentProgress` retains a 20-turn window of permitted samples (up to 21 turn observations) in the existing
checkpoint metadata. The overview presents context as of its first delivery that
turn, keeping command updates concise; receipts and live state carry subsequent
changes. Strategic reviews recompute progress from the latest observation.
Reports distinguish repeated accepted commands, sampled
positions, empire exploration, retained construction production, unhappy
observations, imports, border agreements and newly observed resource ownership.
`decisionFeedback` adds confirmed receipt spending and matching current units to
explicitly linked choices. The history also reports unlinked spending separately;
it does not infer a plan association from prose or label missing units destroyed.

`chat_read.conversationContext` retrieves up to six exact-audience threads with up
to six messages each within a 12,000-character message budget, including confirmed sends awaiting their event echo. Sources
are delivered as evidence and replay/rewind follows the existing journal. Existing
private notes remain the player's place for unanswered questions and follow-up
conditions. No model is asked to summarize conversations automatically.
