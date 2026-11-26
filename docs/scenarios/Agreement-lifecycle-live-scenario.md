# Live scenario: agreements under pressure

This guided Rome–Greece–France exercise checks whether agents inspect enacted
agreements, use them as evidence for private plans, and reconsider those plans
after native expiry or resource interruption. Agents negotiate the new trade's
price. The engine controls its duration and effects.

See the [implemented agreement contract](../reference/Agreement-lifecycle.md).

## Milestones

| Stage | Agent objectives | Required evidence |
| --- | --- | --- |
| Opening | Rome offers one Iron to Greece for 2–4 GPT. Both negotiate, inspect the accepted terms, and create a private plan supported by an agreement read. | One enacted proposal at the native duration; matching native rows; actual `agreement_list` and `agreement_read` calls; sourced plans. |
| Ordinary expiry | An existing Rome–France GPT/Open Borders trade starts with one native turn remaining. Its preparation represents prior elapsed time, not a newly shortened deal. | Both owner-local rows expire through scheduled end turns. Parties inspect their notices and changed income/access. No immediate payment is refunded or repeated. |
| Restart | Pause after round one with Rome next, archive the checkpoint and all three private memory databases, then stop and restart the host and services. | Process logs plus preserved agreement IDs, terms, histories, receipts and private memory. |
| Resource interruption | From turn one, France declares a limited war against Rome and raids a visible Roman Iron mine using ordinary movement and pillaging. | Successful native commands and a pillaged mine. The resource trade ends through native resource checking; affected players receive the ending event. |
| Plan review | Rome and Greece refresh the affected agreement, revise their existing sourced plans, and choose how to proceed without the income or imported resource. | Separate advisory notices followed by agent-authored amendments or retirement in this guided exercise; ordinary gameplay does not require this review. |
| Continuation | Finish two complete rounds after the restart. | Native turn three, Rome next, nine scheduled handoffs; no replacement deals or duplicated settlements. |

The opening trade is prepared background, not a claim of live negotiation. Its
countdown is aged through native diplomacy ticks before play. New live deals use
the normal native duration. There is no early-cancellation command or change to
Civ V rules.

France's instructions use its own visible military objective and game turn. They
must not reveal whether Rome and Greece have a deal, its price, IDs, or private
plans. Any voluntary disclosure in chat is a separate legitimate source. A generic
resource-cut event must not disclose the partner's private resource balance or the
location and cause of its loss.

Keep ordinary decisions small: retain productive city queues and research, hold
units except the French raider, and avoid captures, extra treaties and replacement
offers. A refused or expired negotiation is an unmet objective; preserve its
evidence rather than silently starting over.

## Run limits and evidence

Use the configured player models and reasoning settings. The intended run has
three complete rounds, at most 64 model invocations, 90 minutes including the
planned restart, and 600 seconds per activation. Start with a one-round scheduler
limit, then raise it to three after the restart. Report an unmet milestone or
exhausted limit before extending the exercise.

The operator verifier checks checkpoint state, native effects, committed receipts,
agreement history and private memory. Actual process restarts and the quality of
the agents' plan revisions also require process-log and transcript review. These
checks complement the scripted lifecycle regressions; they do not establish full
player-API coverage.

## Launch

Validate the prepared gameplay path without model calls:

```sh
./gradlew verifyAgreementLifecycleScenario \
  -PagreementScenarioDir="$PWD/build/verification/agreement-scenario-new"
uv run pytest tests/test_verify_agreement_scenario.py tests/test_launch_host.py
```

Use a fresh output directory for the scripted probe. It exercises the production
opening, native expiry, a counteroffer and acceptance, an exact retry across a
restart, all nine scheduled handoffs, and authenticated French war, movement and
pillaging. France's complete compact context remains identical before and after
the private resource negotiation. Its final checkpoint can pass the verifier's
native checks, but cannot pass the live memory checks because it uses no models.

Use a fresh absolute directory. Run the host and each agent service in separate
terminals, with the same directory in each:

```sh
export AGREEMENT_MATCH="$HOME/.local/state/unciv-host/agreement-lifecycle"
uv run civ-agent host start --state-dir "$AGREEMENT_MATCH" --new \
  --scenario agreement-lifecycle --trade-timeout 600
uv run civ-agent agent-server start Rome --state-dir "$AGREEMENT_MATCH"
uv run civ-agent agent-server start Greece --state-dir "$AGREEMENT_MATCH"
uv run civ-agent agent-server start France --state-dir "$AGREEMENT_MATCH"
```

After all four services report ready:

```sh
uv run civ-agent match start --state-dir "$AGREEMENT_MATCH" \
  --max-turns 1 --max-invocations 64 --max-seconds 5400 --turn-timeout 600 --verbose
uv run civ-agent match pause --state-dir "$AGREEMENT_MATCH"
```

At completed round one, preserve `exploration.json` as `before-restart.json` and
take consistent SQLite backups of each
`agent-services/CIVILIZATION/civilization/memory.sqlite3` as
`before-restart-memory/CIVILIZATION.sqlite3`. Keep these files owner-only. Record
service shutdown/startup logs. Restart the same four commands, omitting `--new`,
then continue:

```sh
uv run civ-agent match resume --state-dir "$AGREEMENT_MATCH" --max-turns 3 --verbose
uv run civ-agent match verify-agreement-scenario --state-dir "$AGREEMENT_MATCH" \
  --before-restart "$AGREEMENT_MATCH/before-restart.json"
```

Limits and usage remain cumulative across resume. A nonzero verifier result means
one or more required milestones or pieces of evidence are missing. Inspect its
report before deciding how to continue. No package-distribution build is required.
