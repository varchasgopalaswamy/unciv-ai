# Structured memory and decision history

Approved implementation scope, September 2026. This replaces the freeform memory format; existing matches must start fresh.

## Records and lifecycle

The civilization service owns SQLite records and immutable revisions. Agents create typed plans (objective, approach, success criteria, assumptions), beliefs (claim, uncertainty and evidence), miscellaneous notes, and long-term SOUL principles. Code assigns short handles, turn metadata and versions. Observations, attributed statements, social commitments and confirmed engine outcomes retain their separate provenance.

Agents read and search records with 20-record pages, then create, revise, replace or retire them. Retirement removes a record from active context and current Markdown, preserving audit history. Reasons are completed, abandoned, superseded, retracted, expired or irrelevant. Replacement is atomic. Retired handles cannot be reactivated by ordinary edits. There is no permanent purge in this iteration. Removing a private note never cancels a public promise.

Explicit deadlines expire on trusted game turns. Agents assess completion and incorrect beliefs. Relevant diplomatic changes, loss of owned cities/units, and retracted or replaced supporting beliefs flag dependent plans for review. Historical evidence is distinct from ongoing assumptions. Timestamp changes and unrelated greetings do not invalidate plans.

## Decisions and summaries

Consequential gameplay and diplomatic mutations require an intent: a brief reason or an observed, active plan. Optional expected outcomes and assumptions explain the decision. The service pins evidence and plan revisions, validates them at transactional approval, records intent before dispatch, and links the exact request and receipt. Intent never reaches the Kotlin wire. Missing or stale intent rejects before dispatch. Pending, uncertain, rejected and confirmed results remain distinct; a queued building is not a completed building and a requested destination is not a confirmed arrival.

`turn_N/summary.json` contains confirmed actions, reasons, observations, memory assessments and unresolved work. A deterministic `summary.md` has one game-turn heading and at most 200 words. It survives interruption without a final model response and is regenerated after reconciliation. Diagnostic attempts and final model prose remain in detailed history. Communication jobs have their own structured histories; they do not rewrite completed gameplay assessments.

`SOUL.md`, `STRATEGY.md`, `economy.md`, relationship files, notes and an index are generated views. Fresh jobs receive relevant active records ordered by subject and priority, review notices and new events, with a configurable 12,000-character private-memory limit. Whole records only; omitted records remain searchable. Old summaries are not concatenated. No vector store, model polling or housekeeping model jobs are introduced.

## Concurrency and recovery

Evidence must have been delivered to the job; code attaches its revision. Independent valid edits survive conflicts in a batch. A plan needing review cannot authorize an action until revised; the agent may instead supply a fresh decision. Retirement before approval blocks stale reuse. Retirement after approval cannot undo an in-flight action. Model and network work never monopolize SQLite transactions or mailbox processing.

Round snapshots include lifecycle, decisions, pinned evidence and revision ancestry. History queries follow the restored branch only. Handles remain monotonic across rollback, abandoned jobs stay fenced, and generated files are rebuilt with obsolete projections removed. Ordinary restarts retain committed work; whole-match rollback restores all players and the engine together.

## Validation

Exercise typed validation, source provenance, intent enforcement and exact receipts; retirement, expiry, replacement and partial conflicts; context limits and export cleanup; relevant versus harmless dependency changes; interrupted/reconciled summaries; and restart/whole-match rollback with inaccessible abandoned history. Keep existing responsiveness checks. Run Python checks and host regression tests, then a bounded three-player Vanilla usability run using database and native-save evidence. No additional launchable scenario or engine changes are planned.
