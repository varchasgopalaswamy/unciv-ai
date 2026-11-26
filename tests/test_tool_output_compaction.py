"""Lossless sharing and focused checkpoint presentation for unused tool paths."""

import copy

import pytest
from pydantic_ai.messages import ToolCallPart, ToolReturnPart

from civ_agent.runtime.code_mode_results import bundle_query, movement_options, receipt_result
from civ_agent.runtime.tool_output import compact_tool_output, expand_output_values
from civ_agent.state_updates import same_json
from tests.test_code_mode_results import native
from tests.test_investment_workflow import choice
from tests.test_rich_bundle_presentation import bundle


def movement_packet():
    packet = native(
        {
            "data": {
                "entity": {
                    "handle": "unit1",
                    "movement": 2,
                    "moves": [
                        {
                            "destination": {"x": i, "y": 0},
                            "command": {"kind": "MOVE_UNIT", "target": "unit1"},
                            "context": {"confirmation": "Exact native context " * 20},
                            "canMove": bool(i % 2),
                        }
                        for i in range(12)
                    ],
                }
            }
        }
    )
    packet["receipts"][0]["tool"] = "unit_details"
    return packet


@pytest.mark.parametrize("shape", ["bundle", "bundle_query", "movement_options", "receipt_result"])
def test_shared_payloads_round_trip_without_losing_native_values_or_selected_fields(shape):
    packet = movement_packet()
    if shape == "bundle":
        output = bundle(packet["receipts"][0]["result"])
    elif shape == "bundle_query":
        output = bundle_query(bundle(packet["receipts"][0]["result"]), query_id="q0")
    elif shape == "movement_options":
        output = movement_options(packet)
    else:
        output = receipt_result(packet, operation_id="operation1")
    before = copy.deepcopy(output)
    displayed = compact_tool_output(output, [], {})
    assert "sharedValues" in displayed
    assert len(str(displayed)) < len(str(output)) * 0.75
    assert same_json(expand_output_values(displayed), output)
    assert output == before


def test_partial_bundles_keep_rejected_unexecuted_and_missing_query_receipts():
    output = bundle({"large": "detail " * 1000, "zero": 0, "false": False, "missing": None})
    row = {"operationId": "rejected", "tool": "city_details", "status": "rejected", "result": {"error": "DENIED"}}
    pending = {"operationId": "pending", "tool": "city_details", "status": "unexecuted", "reason": "REASSESS"}
    output["feedback"]["receipts"] += [row, pending]
    output["feedback"]["unexecuted"] = ["pending"]
    output["unexecuted"] = ["pending"]
    for index, receipt in enumerate([row, pending, None], 1):
        output["receiptEntries"].append(
            {
                "queryId": f"q{index}",
                "queryIndex": index,
                "tool": "city_details",
                "arguments": {"target": str(index)},
                "status": receipt["status"] if receipt else "not_returned",
                "receipt": receipt,
            }
        )
    output["remaining"] = 2
    displayed = compact_tool_output(output, [], {})
    assert displayed["remaining"] == 2 and displayed["unexecuted"] == ["pending"]
    assert [entry["status"] for entry in displayed["receiptEntries"]] == [
        "confirmed",
        "rejected",
        "unexecuted",
        "not_returned",
    ]
    assert same_json(expand_output_values(displayed), output)


def test_declined_peace_keeps_complete_reports_and_rejection_once_each():
    overview = {"assessment": "Preserve the exact ambiguous promise? " * 100, "unknowns": ["Unconfirmed intent"]}
    approval = {"approve": False, "reason": "Declined for this exact reason. " * 100}
    output = {
        "error": "MAIN_DECLINED_PEACE",
        "applied": False,
        "consultation": {
            "scope": {"game": "game", "player": "p0"},
            "basis": "basis",
            "status": "COMPLETED",
            "overview": overview,
            "approval": approval,
            "phases": [{"phase": "military", "report": overview}, {"phase": "gameplay", "report": approval}],
        },
    }
    displayed = compact_tool_output(output, [], {})
    assert displayed["error"] == "MAIN_DECLINED_PEACE" and displayed["applied"] is False
    assert "sharedValues" in displayed
    assert same_json(expand_output_values(displayed), output)


def test_checkpoint_retains_selected_decision_updated_task_and_receipt_evidence():
    selected = choice().decision().model_dump(mode="json")
    workspace = {
        "id": "strategy.md",
        "version": "5",
        "content": {"decisions": [selected, {"id": "other", "assessment": "other " * 2000}]},
        "planning": {"develop": {"task": {"status": "in_progress"}}},
        "progress": {"receipts": ["historical " * 100]},
    }
    full = {
        "strategy": workspace,
        "planning": {"applied": True, "task": {"id": "investment-develop", "status": "completed"}},
        "receiptEvidence": [{"receipt": "r1", "commandStatus": "confirmed"}],
        "nativeObjectiveVerified": False,
        "notice": "Authored task completion is not native objective proof.",
    }
    returned = ToolReturnPart(tool_name="track_investment", tool_call_id="checkpoint", content=full)
    call = ToolCallPart("track_investment", {"decisionId": "develop"}, "checkpoint")
    before = copy.deepcopy(returned)
    displayed = compact_tool_output(full, [returned], {"checkpoint": call})
    assert displayed["strategy"]["decision"] == selected
    assert displayed["strategy"]["id"] == "strategy.md" and displayed["strategy"]["version"] == "5"
    assert displayed["planning"] == full["planning"]
    assert displayed["receiptEvidence"] == full["receiptEvidence"]
    assert displayed["nativeObjectiveVerified"] is False
    assert displayed["notice"] == full["notice"]
    assert returned == before
    # An explicit read/projection still returns the complete workspace.
    assert compact_tool_output(workspace, [returned], {"checkpoint": call}) == workspace
    assert compact_tool_output(full, [returned], {}) == full


def test_authored_registry_collisions_literal_refs_and_printed_text_stay_unchanged():
    output = bundle({"large": "data " * 1000})
    collision = {**output, "sharedValues": {"authored": "Preserve this literal dictionary"}}
    assert compact_tool_output(collision, [], {}) == collision
    literal = bundle({"moves": {"sharedValueRef": "authored-literal"}})
    assert compact_tool_output(literal, [], {}) == literal
    literal_in_unknown_field = bundle({"large": "facts " * 1000, "authored": {"sharedValueRef": "literal"}})
    assert compact_tool_output(literal_in_unknown_field, [], {}) == literal_in_unknown_field
    non_json = bundle({"authored": {1, 2}})
    assert compact_tool_output(non_json, [], {}) == non_json
    assert compact_tool_output(str(output), [], {}) == str(output)
