"""Compact displayed command replies while preserving their full program data."""

import copy

import pytest
from pydantic_ai.messages import ToolReturnPart

from civ_agent.runtime.command_output import compact_command_output
from civ_agent.state_updates import apply_overview_update, overview_update, stamp


def command(*, tool="execute_commands", status="confirmed", update="delta", recoveries=None):
    before = stamp(
        {"turn": 1, "revision": "rev1", "cities": [], "settlement": {"facts": "sites " * 2000}, "pending": []},
        "job",
        "epoch",
        "rev1",
    )
    after = stamp({**before, "revision": "rev2", "pending": ["Research"]}, "job", "epoch", "rev2")
    delta = overview_update(before if update != "full" else None, after, [])
    raw = {
        "kind": "ACKNOWLEDGE_ALERT",
        "actionSummary": "Acknowledged once.",
        "hostExtension": {"keep": [False, 0, None]},
        "stateUpdate": delta,
    }
    full = {**after, **{key: value for key, value in raw.items() if key != "stateUpdate"}}
    row = {"operationId": "operation", "tool": "player_command", "status": status, "result": full}
    packet = {"decisionId": "decision", "receipts": [row], "unexecuted": []}
    returned = ToolReturnPart(
        tool_name=tool,
        tool_call_id="child",
        content=packet,
        metadata={
            "raw_player_observation": {**packet, "receipts": [{**row, "result": raw}]},
            "observation_recoveries": recoveries or [],
        },
    )
    return before, after, packet, returned


def test_packet_and_unwrapped_result_preserve_receipts_changes_and_original_data():
    before, after, packet, returned = command()
    original = copy.deepcopy(returned)
    full = packet["receipts"][0]["result"]
    compact = compact_command_output({"packet": packet, "unwrapped": full}, [returned])
    result = compact["packet"]["receipts"][0]["result"]
    assert "settlement" not in result
    assert result == compact["unwrapped"]
    assert result["actionSummary"] == "Acknowledged once."
    assert result["hostExtension"] == {"keep": [False, 0, None]}
    assert result["pending"] == ["Research"]
    assert apply_overview_update(before, result["stateUpdate"]) == after
    assert compact["packet"]["receipts"][0]["operationId"] == "operation"
    assert returned == original and "settlement" in full


@pytest.mark.parametrize(
    "options",
    [{"tool": "player_turn"}, {"status": "rejected"}, {"update": "full"}, {"recoveries": [{"fresh": True}]}],
)
def test_reads_rejections_full_snapshots_and_recoveries_remain_complete(options):
    _, _, packet, returned = command(**options)
    assert compact_command_output(packet, [returned]) == packet


def test_partial_batch_keeps_rejections_and_unexecuted_work():
    _, _, packet, returned = command()
    rejected = {
        "operationId": "rejected",
        "tool": "player_command",
        "status": "rejected",
        "result": {"error": "ILLEGAL"},
    }
    pending = {"operationId": "pending", "status": "unexecuted", "reason": "PRIOR_COMMAND_REJECTED"}
    packet["receipts"].extend([rejected, pending])
    packet["unexecuted"].append("pending")
    compact = compact_command_output(packet, [returned])
    assert compact["receipts"][1:] == [rejected, pending]
    assert compact["unexecuted"] == ["pending"]
    assert compact["receipts"][0]["result"]["stateUpdate"]["kind"] == "delta"


def test_authored_projections_and_printed_text_are_preserved_without_guessing_field_names():
    _, _, packet, returned = command()
    full = packet["receipts"][0]["result"]
    selected = {"settlement": full["settlement"], "annotation": "Keep these exact facts."}
    assert compact_command_output(selected, [returned]) == selected
    assert compact_command_output(str(full), [returned]) == str(full)
    assert compact_command_output(packet, []) == packet


def test_ambiguous_sources_and_mismatched_receipt_identities_are_preserved():
    _, _, packet, returned = command()
    changed = copy.deepcopy(returned)
    changed.metadata["raw_player_observation"]["receipts"][0]["result"]["stateUpdate"]["baseStateId"] = "other"
    assert compact_command_output(packet, [returned, changed]) == packet
    changed = copy.deepcopy(returned)
    changed.metadata["raw_player_observation"]["receipts"][0]["operationId"] = "other"
    assert compact_command_output(packet, [changed]) == packet


@pytest.mark.parametrize(
    "operation",
    [
        "trade_offer",
        "trade_decide",
        "friendship_offer",
        "commitment_offer",
        "commitment_decide",
        "city_override",
        "escort_assignment",
        "construction_intent",
    ],
)
def test_other_mutations_use_the_same_update_compaction_and_keep_their_native_fields(operation):
    _, _, packet, returned = command(tool="construction_intent" if operation == "construction_intent" else "act")
    for row in (packet["receipts"][0], returned.metadata["raw_player_observation"]["receipts"][0]):
        row["tool"] = "player_command" if operation == "construction_intent" else operation
        result = row["result"]
        assert isinstance(result, dict)
        result["nativeExtension"] = {"proposal": "p1", "accepted": True}
    displayed = compact_command_output(packet, [returned])
    result = displayed["receipts"][0]["result"]
    assert "settlement" not in result and "stateUpdate" in result
    assert result["nativeExtension"] == {"proposal": "p1", "accepted": True}


def test_coordination_updates_do_not_repeat_the_complete_coordinator_snapshot():
    _, _, packet, returned = command(tool="act")
    full = packet["receipts"][0]["result"]
    full["economyCoordination"] = {"history": "old " * 2000, "rolesFinished": {"economy": False}}
    raw = returned.metadata["raw_player_observation"]["receipts"][0]["result"]
    raw["coordinationUpdate"] = {"kind": "delta", "set": {"rolesFinished": {"economy": False}}}
    displayed = compact_command_output(packet, [returned])["receipts"][0]["result"]
    assert displayed["coordinationUpdate"] == raw["coordinationUpdate"]
    assert "economyCoordination" not in displayed
