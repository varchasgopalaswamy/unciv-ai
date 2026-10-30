"""Audit delivery changes using preserved live evidence; makes no model calls."""

import argparse
import json
import sqlite3
from collections import Counter, defaultdict
from pathlib import Path
from statistics import median

from civ_agent.observations import DetailQuery
from civ_agent.payload_updates import encoded
from civ_agent.response_compaction import compact_response, expand_response
from civ_agent.state_updates import apply_player_turn, overview_update, same_json
from tests.report_role_contexts import live_report


def replay_action_delivery(path):
    """Reserialize identical saved specialist outcomes with a retained baseline."""
    previous = {}
    totals = defaultdict(Counter)
    for file in path.glob("agent-services/*/civilization/memory.sqlite3"):
        with sqlite3.connect(file.resolve().as_uri() + "?mode=ro", uri=True) as db:
            rows = db.execute(
                "SELECT j.purpose,c.job,c.id,c.name,c.result FROM calls c "
                "JOIN jobs j ON j.id=c.job WHERE c.result IS NOT NULL ORDER BY c.rowid"
            )
            for role, job, call_id, name, serialized in rows:
                if role not in {"economy", "military"} or not (
                    call_id.isdecimal() or call_id.startswith(("startup:", "resume:"))
                ):
                    continue
                raw = json.loads(serialized)
                result = expand_response(raw)
                responses = [r["result"] for r in result["results"]] if name == "inspect_bundle" else [result]
                for response in responses:
                    if "stateId" not in response and "stateUpdate" not in response:
                        continue
                    before = previous.get(job)
                    try:
                        current = apply_player_turn(before, response)
                    except ValueError:
                        totals[role]["unreconstructedUpdates"] += 1
                        previous.pop(job, None)
                        continue
                    if (
                        name == "player_command"
                        and result.get("actionSummary")
                        and not result.get("error")
                        and result.get("stateUpdate", {}).get("kind") == "full"
                    ):
                        if before is None:
                            totals[role]["fullActionsWithoutBaseline"] += 1
                        else:
                            delta = overview_update(
                                before,
                                current,
                                [DetailQuery.model_validate(q) for q in result["stateUpdate"]["invalidatedDetails"]],
                                compact_nested=True,
                            )
                            candidate = compact_response({**result, "stateUpdate": delta})
                            assert same_json(apply_player_turn(before, candidate), current)
                            totals[role]["identicalOutcomesVerified"] += 1
                            totals[role]["originalReceiptCharacters"] += len(encoded(raw))
                            totals[role]["retainedBaselineReceiptCharacters"] += len(encoded(candidate))
                    previous[job] = current
    return {
        "roles": {role: dict(counts) for role, counts in sorted(totals.items())},
        "limits": "Offline serialization of the same archived specialist outcomes and invalidation lists. Only full successful action receipts with a reconstructable model-delivered baseline are compared. Includes startup, resume and outer tool results; excludes host-only and duplicate internal reads. Exact reconstructed overviews are asserted. This isolates receipt serialization, not model tokens, request savings or changed decisions.",
    }


def delivery_audit(path):
    roles = defaultdict(lambda: {"counts": Counter(), "actionCharacters": [], "toolErrors": Counter()})
    peace = []
    for file in path.glob("agent-services/*/civilization/memory.sqlite3"):
        with sqlite3.connect(file.resolve().as_uri() + "?mode=ro", uri=True) as db:
            rows = db.execute(
                "SELECT j.purpose,c.id,c.name,c.arguments,c.result FROM calls c "
                "JOIN jobs j ON j.id=c.job WHERE c.result IS NOT NULL ORDER BY c.rowid"
            )
            for role, call_id, name, arguments, serialized in rows:
                arguments, result = json.loads(arguments), expand_response(json.loads(serialized))
                data = roles[role]
                counts = data["counts"]
                if name == "trade_decide" and not result.get("error"):
                    outcome = result.get("outcome") or {}
                    clauses = outcome.get("clauses", [])
                    if any(clause.get("item", {}).get("name") == "Peace Treaty" for clause in clauses):
                        peace.append(
                            {
                                "turn": outcome["turn"],
                                "participants": outcome["participants"],
                                "clauses": clauses,
                                "intent": arguments.get("intent"),
                            }
                        )
                if call_id.startswith("resume:") and name == "player_turn":
                    counts["resume:" + arguments.get("delivery", "auto")] += 1
                if call_id.startswith("startup:") and name == "city_details":
                    counts["preloadedCityDetails"] += 1
                # Children of inspection bundles are durable separately. Count
                # their read once and do not count the enclosing bundle's error
                # wrapper a second time. Host reads are not model decisions.
                if not (call_id.isdecimal() or call_id.startswith("inspection:")) or name == "inspect_bundle":
                    continue
                error = result.get("error")
                code = error if isinstance(error, str) else (error or {}).get("code")
                if code:
                    data["toolErrors"][code] += 1
                if name == "military_review" and not code:
                    counts["review:bundled" if call_id.startswith("inspection:") else "review:separate"] += 1
                if name in {"city_details", "unit_details", "construction_details", "player_turn"}:
                    counts["read:" + name] += 1
                if name == "finish_role" and result.get("militaryCompletion"):
                    counts["review:completionRecovery"] += 1
                if name == "player_command":
                    if arguments.get("reviewAfter"):
                        counts["reviewAfter:requested"] += 1
                    if not code and result.get("actionSummary"):
                        counts["confirmedCommands"] += 1
                        counts["actionUpdate:" + result.get("stateUpdate", {}).get("kind", "missing")] += 1
                        if result.get("militaryCompletion"):
                            counts["reviewAfter:returned"] += 1
                        data["actionCharacters"].append(len(serialized))
    return {
        "nativePeaceTreaties": peace,
        "roles": {
            role: {
                "counts": dict(data["counts"]),
                "toolErrors": dict(data["toolErrors"]),
                "medianConfirmedActionCharacters": median(data["actionCharacters"])
                if data["actionCharacters"]
                else None,
            }
            for role, data in sorted(roles.items())
        },
        "limits": "Durable calls counted once, including inspection children but excluding their outer wrappers, host reads and internal refreshes. These counts are not model requests. Action characters are serialized stored replies, not token counts; differing actions affect their size. Numeric call IDs identify model tool calls in this harness.",
    }


def report(root, paired):
    live = live_report(root, paired)
    for model, data in live["models"].items():
        for trial in data["trials"]:
            if not trial.get("notStarted"):
                path = Path(trial["sourceSuite"]) / model / trial.get("sourceTrialId", trial["id"])
                trial["deliveryAudit"] = delivery_audit(path)
                if trial.get("reused"):
                    trial["identicalActionReplay"] = replay_action_delivery(path)
    return {
        "baseline": json.loads((root / "baseline.json").read_text()),
        "candidate": json.loads((root / "candidate.json").read_text()),
        "replay": json.loads((root / "replay-summary.json").read_text()),
        "live": live,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", type=Path)
    parser.add_argument("--paired", type=Path)
    args = parser.parse_args()
    root = args.root.resolve()
    result = report(root, args.paired or root / "live")
    (root / "evaluation-report.json").write_text(json.dumps(result, indent=2) + "\n")
    for model, data in result["live"]["models"].items():
        print(model, json.dumps(data["comparisonSummary"], indent=2))


if __name__ == "__main__":
    main()
