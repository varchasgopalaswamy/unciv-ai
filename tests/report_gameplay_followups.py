"""Report isolated worker ownership and combined gameplay follow-up screens."""

import argparse
import json
import sqlite3
from collections import Counter
from pathlib import Path

from tests.report_role_contexts import cohort_summary, live_report, paired_comparisons


def protocol_audit(path):
    """Count durable calls once; retries do not become additional actions."""
    jobs, holds, errors = Counter(), [], Counter()
    citation_only = rejected_citations = reviews = completion_recoveries = 0
    for file in path.glob("agent-services/*/civilization/memory.sqlite3"):
        with sqlite3.connect(file.resolve().as_uri() + "?mode=ro", uri=True) as db:
            rows = db.execute(
                "SELECT j.purpose,j.game_turn,c.name,c.arguments,c.result FROM calls c "
                "JOIN jobs j ON j.id=c.job WHERE c.result IS NOT NULL"
            )
            for role, turn, name, arguments, result in rows:
                arguments, result = json.loads(arguments), json.loads(result)
                error = result.get("error")
                code = error if isinstance(error, str) else (error or {}).get("code")
                if code:
                    errors[code] += 1
                if name == "player_command":
                    kind = arguments.get("kind")
                    confirmed = bool(result.get("actionSummary")) and not code
                    if kind in {"START_WORKER_JOB", "STOP_WORKER_JOB", "CONNECT_ROAD"} and confirmed:
                        jobs[(role, kind)] += 1
                    intent = arguments.get("intent")
                    # Durable calls also include rejected model arguments.
                    intent = intent if isinstance(intent, dict) else {}
                    if intent.get("cityPlan") and not intent.get("reason"):
                        citation_only += confirmed
                        rejected_citations += code in {"INVALID_TOOL_ARGUMENTS", "INVALID_COMMAND_ARGUMENTS"}
                if role == "military":
                    reviews += name == "military_review" and not code
                    completion_recoveries += code == "MILITARY_COMPLETION_REVIEW_REQUIRED"
                    if name == "finish_role" and result.get("roleFinished"):
                        holds.extend(
                            {"turn": turn, **decision}
                            for decision in (arguments.get("militaryReview") or {}).get("decisions", [])
                        )
    return {
        "confirmedWorkerOrders": [
            {"role": role, "kind": kind, "count": count} for (role, kind), count in sorted(jobs.items())
        ],
        "completionReads": reviews,
        "completionRecoveries": completion_recoveries,
        "acceptedHoldDecisions": holds,
        "confirmedPlanOnlyCommands": citation_only,
        "rejectedPlanOnlyArguments": rejected_citations,
        "toolErrors": dict(errors),
        "limits": "Worker order counts are accepted commands, not distinct completed improvements. Holds record authored intent, not tactical quality. Only explicit native action summaries count as confirmed commands.",
    }


def comparisons(trials):
    result = {}
    for control, treatment in (("baseline", "workers"), ("workers", "candidate"), ("baseline", "candidate")):
        selected = [
            {**trial, "variant": "baseline" if trial["variant"] == control else "candidate"}
            for trial in trials
            if trial["variant"] in {control, treatment}
        ]
        pairs = paired_comparisons(selected)
        result[f"{control}_to_{treatment}"] = {
            "control": control,
            "treatment": treatment,
            "pairs": pairs,
            "summary": cohort_summary(pairs),
        }
    return result


def report(root, paired):
    result = live_report(root, paired)
    for name, model in result["models"].items():
        for trial in model["trials"]:
            if not trial.get("notStarted"):
                directory = Path(trial["sourceSuite"]) / name / trial.get("sourceTrialId", trial["id"])
                trial["protocolAudit"] = protocol_audit(directory)
        model["followupComparisons"] = comparisons(model["trials"])
    return result


def text_report(result):
    lines = [
        "Gameplay follow-up screening",
        "",
        "Baseline: committed role-context change. Workers: worker ownership alone. Candidate: workers, Military completion review and plan-only intent.",
        "One repetition per position/model is screening evidence, not a causal estimate or win-rate evidence. Elapsed time has no weight in conclusions.",
        "",
    ]
    for name, model in result["models"].items():
        lines.append(
            f"{name}: {model['completedTrials']}/{model['scheduledTrials']} trials completed, {model['completedRounds']} rounds"
        )
        for comparison in model["followupComparisons"].values():
            summary = comparison["summary"]
            lines.append(
                f"  {comparison['control']} -> {comparison['treatment']}: {summary['completePairs']} complete pairs; total-token change {summary['candidateUsageChangePercent'].get('totalTokens')}%"
            )
            for row in summary["outcomeComparisons"]:
                before, after = row["baseline"], row["candidate"]
                fields = (
                    "growthInRetainedCities",
                    "newImprovementsOnRetainedLand",
                    "buildingsInRetainedCities",
                    "technologies",
                    "militaryLossReports",
                    "cities",
                )
                lines.append(
                    "    " + row["case"] + ": " + ", ".join(f"{key} {before[key]} -> {after[key]}" for key in fields)
                )
        for trial in model["trials"]:
            audit = trial.get("protocolAudit", {})
            lines.append(
                f"  {trial['id']}: "
                + json.dumps(
                    {key: value for key, value in audit.items() if key not in {"limits", "acceptedHoldDecisions"}},
                    sort_keys=True,
                )
            )
        lines.append("")
    lines.append(
        "See JSON for all-role cached/uncached input, output, requests, dated production/science rates, source/checkpoint provenance, exact holds and limitations."
    )
    return "\n".join(lines) + "\n"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", type=Path)
    parser.add_argument("--paired", type=Path)
    args = parser.parse_args()
    result = report(args.root, args.paired or args.root / "live")
    (args.root / "evaluation-report.json").write_text(json.dumps(result, indent=2) + "\n")
    (args.root / "evaluation-report.txt").write_text(text_report(result))


if __name__ == "__main__":
    main()
