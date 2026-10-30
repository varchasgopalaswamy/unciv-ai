"""Replay retained communication payloads without contacting an engine or model."""

import argparse
import copy
import json
import sqlite3
from collections import Counter
from pathlib import Path
from statistics import median

from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError

from civ_agent.observations import TurnOverview
from civ_agent.payload_updates import encoded
from civ_agent.proposal_gateway import ProposalGateway
from civ_agent.response_compaction import compact_response, expand_response
from civ_agent.role_view import role_overview
from civ_agent.startup_context import communication_trade_queries

# Delivery metadata is attached after the ordinary gateway projection. Replay
# the same envelopes around either view, without manufacturing new evidence.
ENVELOPE_FIELDS = (
    "stateId",
    "invalidatedDetails",
    "memoryUpdates",
    "economyCoordination",
    "coordinationUpdate",
    "strategyReview",
    "strategicReview",
)


def project_captured_overview(value):
    projected = role_overview(value, "communication", purpose="communication")
    for key in ENVELOPE_FIELDS:
        if key in value:
            projected[key] = copy.deepcopy(value[key])
            if key in projected["roleBrief"]["omitted"]:
                projected["roleBrief"]["omitted"].remove(key)
    return projected


def assert_preserved(before, after, allowed_tools):
    for key in (
        "pending",
        "alerts",
        "notifications",
        "combatReports",
        "rules",
        "scenario",
        "result",
        "diplomacy",
        "socialAdvisories",
        "tacticalReview",
        "changesSincePreviousTurn",
        "outputSummary",
        "evidence",
        *ENVELOPE_FIELDS,
    ):
        if key in before:
            assert encoded(before[key]) == encoded(after[key]), key
    assert encoded(after["economy"]) == encoded({k: v for k, v in before["economy"].items() if k != "development"})
    for collection in ("cities", "units"):
        original = {r["handle"]: r for r in before[collection]}
        assert set(original) == {r["handle"] for r in after[collection]}
        for entity in after[collection]:
            for field, value in entity.items():
                if field != "owner":
                    assert encoded(original[entity["handle"]][field]) == encoded(value), (collection, field)
    for field, value in after.get("militaryReadiness", {}).items():
        assert encoded(before["militaryReadiness"][field]) == encoded(value), field
    for read in after["roleBrief"]["focusedReads"].values():
        assert read["tool"] in allowed_tools
    full = role_overview(before, "full", purpose="communication")
    for field in after["roleBrief"]["omitted"]:
        # Nested omission descriptions use a root field followed by punctuation.
        root = field.split(":", 1)[0].split(".", 1)[0].split(" ", 1)[0]
        if root not in {"endTurn", "turnControls", "roleCompletion"}:
            assert root in full, field


def replay(roots):
    schema = Draft202012Validator(TurnOverview.model_json_schema())
    catalogue = ProposalGateway.tool_catalogue(
        [{"name": name} for name in ("player_turn", "player_result", "victory_progress")], "communication"
    )
    allowed_tools = {tool["name"] for tool in catalogue}
    counts, errors, omitted, fields = Counter(), Counter(), Counter(), Counter()
    sizes, records = [], []
    for root in roots:
        for file in sorted(root.glob("g*/agent-services/*/civilization/memory.sqlite3")):
            selected, later_reads = {}, set()
            with sqlite3.connect(file.resolve().as_uri() + "?mode=ro", uri=True) as db:
                db.execute("BEGIN")
                rows = db.execute(
                    "SELECT c.job,c.id,c.name,c.arguments,c.result FROM calls c "
                    "JOIN jobs j ON j.id=c.job WHERE j.purpose='communication' AND c.result IS NOT NULL ORDER BY c.rowid"
                )
                for job, call_id, name, arguments, serialized in rows:
                    if not call_id.isdecimal() and not call_id.startswith("startup:chat_read"):
                        continue
                    raw = json.loads(serialized)
                    value = expand_response(raw)
                    if call_id.isdecimal():
                        counts["modelToolResults"] += 1
                        counts["recordedToolCharacters"] += len(serialized)
                    if name == "trade_read" and call_id.isdecimal() and "error" not in value:
                        proposal = json.loads(arguments).get("proposalId")
                        if isinstance(proposal, str):
                            later_reads.add((job, proposal))
                    if name == "chat_read" and call_id.startswith("startup:"):
                        counts["capturedStartups"] += 1
                        queries = communication_trade_queries(value)
                        for _, args in queries:
                            selected[job, args["proposalId"]] = True
                        counts["startupsWithSelectedPreloads"] += bool(queries)
                        counts["selectedPreloads"] += len(queries)
                        counts["pendingTradesWithInspection"] += sum(
                            "inspection" in trade for trade in value.get("trades", [])
                        )
                        continue
                    if name != "player_turn" or not call_id.isdecimal():
                        continue
                    counts["overviewReads"] += 1
                    if "error" in value or "stateUpdate" in value or "stateId" not in value:
                        counts["nonFullOverviewReadsExcluded"] += 1
                        continue
                    if "economyBrief" in value or "roleBrief" in value:
                        # Older economy views contain identity-only unit rows.
                        # A model's explicit scoped reads are not full baselines
                        # and would keep their selection with this change.
                        counts["alreadyFocusedOverviewReadsExcluded"] += 1
                        continue
                    projected = project_captured_overview(value)
                    # Both views use identical current serialization. Recorded
                    # byte counts remain separate from this projection comparison.
                    baseline = compact_response(value, share=True)
                    candidate = compact_response(projected, share=True)
                    before_size, after_size = len(encoded(baseline)), len(encoded(candidate))
                    counts["projectedOverviews"] += 1
                    counts["baselineOverviewCharacters"] += before_size
                    counts["candidateOverviewCharacters"] += after_size
                    sizes.append((before_size, after_size))
                    omitted.update(projected["roleBrief"]["omitted"])
                    fields.update({k: len(encoded(v)) for k, v in value.items()})
                    try:
                        schema.validate(projected)
                        assert_preserved(value, projected, allowed_tools)
                        assert encoded(expand_response(candidate)) == encoded(projected)
                    except (AssertionError, ValueError, ValidationError) as error:
                        # Schema failures should be reported alongside the rest
                        # of the corpus instead of hiding later failures.
                        errors[type(error).__name__ + ": " + str(error)[:160]] += 1
                    records.append(
                        {"source": str(file), "job": job, "call": call_id, "before": before_size, "after": after_size}
                    )
            counts["selectedPreloadsWithLaterExplicitRead"] += len(selected.keys() & later_reads)
    return {
        "counts": dict(counts),
        "medianOverviewCharacters": {"before": median(a for a, _ in sizes), "after": median(b for _, b in sizes)}
        if sizes
        else None,
        "medianPerReadReductionPercent": median(100 * (1 - b / a) for a, b in sizes) if sizes else None,
        "aggregateOverviewReductionPercent": 100
        * (1 - counts["candidateOverviewCharacters"] / counts["baselineOverviewCharacters"])
        if sizes
        else None,
        "errors": dict(errors),
        "omitted": dict(omitted),
        "baselineFieldCharacters": dict(fields.most_common()),
        "records": records,
        "limits": "Offline projection of captured full communication overviews, serialized identically with the current compactor. No engine or model calls. Exact preserved fields, owned identities, reachable tool names, schemas and lossless compaction are checked. Conditional preloads are selected only from startup-visible identities; later matching reads are overlap, not proof that a request can be avoided. No later trade terms are inserted into startup. Active-goal preloads are covered by gateway regression tests, not reconstructed from future saved state. This measures characters, not model tokens, gameplay quality or total inference savings.",
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("roots", type=Path, nargs="+")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    report = replay(args.roots)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2) + "\n")
    print(
        json.dumps(
            {k: v for k, v in report.items() if k not in {"records", "omitted", "baselineFieldCharacters"}}, indent=2
        )
    )
    if report["errors"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
