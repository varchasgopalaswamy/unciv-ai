"""Opt-in selectors run identically on the host and in the pinned Monty worker."""

import copy
from typing import Any

import pytest
from pydantic_ai import ModelRetry
from pydantic_monty import Monty, MontyRuntimeError

from civ_agent.runtime.code_mode_results import (
    LOCAL_HELPER_SOURCE,
    LOCAL_SELECTOR_SOURCE,
    LOCAL_SELECTOR_VERSION,
    RESULT_HELPERS,
    RESULT_SELECTORS,
    bundle_query,
    movement_options,
    receipt_result,
    select_one,
)


def native(*receipts):
    return {
        "decisionId": "decision1",
        "source": "preserved-native-source",
        "receipts": list(receipts),
        "unexecuted": [row["operationId"] for row in receipts if row["status"] == "unexecuted"],
        "extension": {"evidence": ["fact1"]},
    }


def receipt(identifier="op1", tool="unit_details", data=None, status="confirmed"):
    return {"operationId": identifier, "tool": tool, "status": status, "result": {} if data is None else data}


def unit_packet(moves=None):
    return native(receipt(data={"data": {"entity": {"handle": "unit1", "movement": "2/2", "moves": moves or []}}}))


def rich_bundle():
    feedback = native(
        receipt("op0", "player_turn", {"turn": 3}),
        receipt("op1", "unit_details", {"error": "TARGET_CHANGED"}, "rejected"),
        {"operationId": "op2", "status": "unexecuted", "reason": "RECONSIDER"},
    )
    queries = [("player_turn", {}), ("unit_details", {"target": "unit1"}), ("unit_details", {"target": "unit2"})]
    return {
        "receiptVersion": 1,
        "results": [{"tool": row["tool"], "result": row["result"]} for row in feedback["receipts"][:2]],
        "receiptEntries": [
            {
                "queryId": f"call:query:{index}",
                "queryIndex": index,
                "tool": tool,
                "arguments": arguments,
                "status": feedback["receipts"][index]["status"],
                "receipt": feedback["receipts"][index],
            }
            for index, (tool, arguments) in enumerate(queries)
        ],
        "unexecuted": ["op2"],
        "feedback": feedback,
        "remaining": 1,
    }


def test_select_one_preserves_the_entire_canonical_option_without_choosing_or_mutating():
    command = {"kind": "MOVE_UNIT", "target": "unit1", "destination": {"x": 2, "y": 0}}
    rows = [
        {"destination": {"x": 1, "y": 0}, "command": {**command, "destination": {"x": 1, "y": 0}}},
        {
            "destination": {"x": 2, "y": 0},
            "command": command,
            "confirmationField": "confirmAction",
            "afterMove": {"captureRisk": {"threats": ["Brute"]}},
            "context": {"revision": "r1"},
        },
    ]
    before = copy.deepcopy(rows)
    selected = select_one(rows, where={"destination": {"x": 2, "y": 0}})
    assert selected == rows[1]
    assert selected is not None
    selected["command"]["confirmAction"] = True
    assert rows == before
    assert select_one(rows, where={"missing": None}) is None
    assert select_one([], where={"destination": {"x": 2, "y": 0}}) is None
    with pytest.raises(ModelRetry, match="RESULT_SELECTION_AMBIGUOUS"):
        select_one([rows[1], rows[1]], where={"destination": {"x": 2, "y": 0}})


def test_movement_fields_are_explicit_and_keep_feedback_and_risk_separate_from_decisions():
    option = {
        "destination": {"x": 1, "y": 2},
        "command": {"kind": "MOVE_UNIT", "target": "unit1", "destination": {"x": 1, "y": 2}},
        "confirmationField": "confirmAction",
        "afterMove": {"captureRisk": {"threats": ["Barbarian"]}},
    }
    packet = unit_packet([option])
    before = copy.deepcopy(packet)
    projected = movement_options(packet)
    assert projected == {"feedback": packet, "unit": "unit1", "movement": "2/2", "options": [option]}
    assert "confirmAction" not in projected["options"][0]["command"]
    projected["options"].clear()
    projected["feedback"]["extension"].clear()
    assert packet == before
    assert movement_options(unit_packet())["options"] == []
    reviewed = {"source": "review-source", "data": packet["receipts"][0]["result"]}
    assert movement_options(reviewed, envelope="review")["feedback"] == reviewed


def test_receipt_selection_retains_partial_effects_and_never_promotes_status_or_replays():
    packet = native(
        receipt("first", "player_command", {"applied": True}),
        receipt("second", "player_command", {"error": "STALE"}, "rejected"),
        {"operationId": "third", "status": "unexecuted", "reason": "RECONSIDER"},
    )
    before = copy.deepcopy(packet)
    for row in packet["receipts"]:
        selected = receipt_result(packet, operation_id=row["operationId"])
        assert selected == {"feedback": packet, "receipt": row}
    assert receipt_result(packet, operation_id="absent") == {"feedback": packet, "receipt": None}
    receipt_result(packet, operation_id="first")["receipt"]["result"].clear()
    assert packet == before


def test_bundle_selects_explicit_queries_and_preserves_every_status_source_and_pending_id():
    bundle = rich_bundle()
    before = copy.deepcopy(bundle)
    for index in range(3):
        row = bundle["receiptEntries"][index]
        selected = bundle_query(bundle, query_id=row["queryId"])
        assert selected == {"bundle": bundle, "query": row}
        assert selected["query"]["status"] == row["receipt"]["status"]
        assert bundle_query(bundle, tool=row["tool"], arguments=row["arguments"]) == selected
    assert bundle_query(bundle, query_id="absent") == {"bundle": bundle, "query": None}
    bundle_query(bundle, query_id="call:query:0")["bundle"]["feedback"]["extension"].clear()
    assert bundle == before
    missing = copy.deepcopy(bundle)
    missing["receiptEntries"].append(
        {
            "queryId": "call:query:3",
            "queryIndex": 3,
            "tool": "player_turn",
            "arguments": {},
            "status": "not_returned",
            "receipt": None,
        }
    )
    assert bundle_query(missing, query_id="call:query:3")["query"]["receipt"] is None
    with pytest.raises(ModelRetry, match="RESULT_SELECTION_AMBIGUOUS"):
        bundle_query(missing, tool="player_turn", arguments={})


def parity_cases():
    packet = unit_packet()
    wrong_tool = unit_packet()
    wrong_tool["receipts"][0]["tool"] = "city_details"
    missing_moves = unit_packet()
    del missing_moves["receipts"][0]["result"]["data"]["entity"]["moves"]
    nested_movement = unit_packet()
    nested_movement["receipts"][0]["result"]["data"]["entity"]["movement"] = {"moves": []}
    partial = native(receipt(), {"operationId": "pending", "status": "unexecuted", "reason": "RECONSIDER"})
    duplicate_receipt = native(receipt(), receipt())
    duplicate_bundle = rich_bundle()
    duplicate_bundle["receiptEntries"][1]["queryId"] = "call:query:0"
    changed_receipt = rich_bundle()
    changed_receipt["receiptEntries"][1] = copy.deepcopy(changed_receipt["receiptEntries"][1])
    changed_receipt["receiptEntries"][1]["receipt"]["result"] = {"applied": True}
    changed_status = rich_bundle()
    changed_status["receiptEntries"][1]["status"] = "confirmed"
    changed_pending = rich_bundle()
    changed_pending["unexecuted"] = []
    return [
        ("select_one", {"rows": [], "where": {"x": 1}}),
        ("select_one", {"rows": [{"x": 1}], "where": {"missing": None}}),
        ("select_one", {"rows": [{"x": True}, {"x": 1}, {"x": 1.0}], "where": {"x": 1}}),
        ("select_one", {"rows": [{"x": {"y": [True]}}], "where": {"x": {"y": [1]}}}),
        ("select_one", {"rows": [{"x": 1}, {"x": 1}], "where": {"x": 1}}),
        ("select_one", {"rows": [{"x": 1}], "where": {}}),
        ("select_one", {"rows": [None], "where": {"x": 1}}),
        ("movement_options", {"result": packet}),
        ("movement_options", {"result": wrong_tool}),
        ("movement_options", {"result": missing_moves}),
        ("movement_options", {"result": nested_movement}),
        ("movement_options", {"result": partial}),
        ("movement_options", {"result": native(receipt(), receipt("op2"))}),
        ("movement_options", {"result": native(receipt(status="rejected"))}),
        (
            "movement_options",
            {"result": {"source": "frozen", "data": packet["receipts"][0]["result"]}, "envelope": "review"},
        ),
        ("receipt_result", {"result": partial, "operation_id": "pending"}),
        ("receipt_result", {"result": partial, "operation_id": "op1"}),
        ("receipt_result", {"result": partial, "operation_id": "absent"}),
        ("receipt_result", {"result": partial, "operation_id": ""}),
        ("receipt_result", {"result": duplicate_receipt, "operation_id": "op1"}),
        ("bundle_query", {"result": rich_bundle(), "query_id": "call:query:1"}),
        ("bundle_query", {"result": rich_bundle(), "tool": "unit_details", "arguments": {"target": "unit2"}}),
        ("bundle_query", {"result": rich_bundle(), "query_id": "absent"}),
        ("bundle_query", {"result": rich_bundle(), "query_id": "call:query:0", "tool": "player_turn"}),
        ("bundle_query", {"result": rich_bundle(), "tool": "unit_details"}),
        ("bundle_query", {"result": {"results": []}, "tool": "player_turn", "arguments": {}}),
        ("bundle_query", {"result": duplicate_bundle, "query_id": "call:query:0"}),
        ("bundle_query", {"result": changed_receipt, "query_id": "call:query:1"}),
        ("bundle_query", {"result": changed_status, "query_id": "call:query:1"}),
        ("bundle_query", {"result": changed_pending, "query_id": "call:query:1"}),
    ]


def test_pinned_monty_parity_for_valid_empty_ambiguous_partial_and_rejected_results():
    functions: dict[str, Any] = {
        function.__name__: function for function in (select_one, movement_options, receipt_result, bundle_query)
    }
    with Monty(max_processes=1, request_timeout=5) as pool:
        with pool.checkout(limits={"max_memory": 64 * 1024 * 1024}) as session:
            session.feed_run(LOCAL_HELPER_SOURCE)
            session.feed_run(LOCAL_SELECTOR_SOURCE)
            for name, inputs in parity_cases():
                expression = name + "(" + ", ".join(key + "=" + key for key in inputs) + ")"
                before = copy.deepcopy(inputs)
                try:
                    expected = functions[name](**inputs)
                except ModelRetry as error:
                    marker = str(error).split(":", 1)[0]
                    with pytest.raises(MontyRuntimeError, match=marker):
                        session.feed_run(expression, inputs=inputs)
                else:
                    assert session.feed_run(expression, inputs=inputs) == expected
                assert inputs == before


def test_real_monty_selects_a_move_without_generators_or_host_callbacks():
    packet = unit_packet(
        [
            {
                "destination": {"x": 2, "y": 0},
                "command": {"kind": "MOVE_UNIT", "target": "unit1", "destination": {"x": 2, "y": 0}},
            }
        ]
    )
    source = """movement = movement_options(result=packet)
assert movement['movement'] == '2/2'
option = select_one(rows=movement['options'], where={'destination': {'x': 2, 'y': 0}})
assert option is not None
option['command']
"""
    with Monty(max_processes=1, request_timeout=5) as pool:
        with pool.checkout(limits={"max_memory": 64 * 1024 * 1024}) as session:
            session.feed_run(LOCAL_HELPER_SOURCE)
            session.feed_run(LOCAL_SELECTOR_SOURCE)
            assert (
                session.feed_run(source, inputs={"packet": packet})
                == packet["receipts"][0]["result"]["data"]["entity"]["moves"][0]["command"]
            )


def test_legacy_helpers_and_opt_in_selectors_have_disjoint_versioned_names():
    assert RESULT_HELPERS == {"result_data", "result_feedback", "result_view", "required_result_field", "result_entity"}
    assert RESULT_SELECTORS == {"select_one", "movement_options", "receipt_result", "bundle_query"}
    assert RESULT_SELECTORS.isdisjoint(RESULT_HELPERS)
    assert LOCAL_SELECTOR_VERSION == "result-selectors-v1"
    assert "from civ_agent" not in LOCAL_SELECTOR_SOURCE
    assert "result-selectors" not in LOCAL_HELPER_SOURCE
