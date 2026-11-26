"""Rich bundle output is opt-in and preserves native outcomes without replay."""

import asyncio
import copy
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from pydantic_ai import ModelRetry

from civ_agent.inspection_bundle import InspectionBundle
from civ_agent.orchestration.contracts import GatewayDependencies, GatewayReceipt, Scope
from civ_agent.orchestration.gateway_tools import gateway_tools
from civ_agent.runtime.code_mode_results import bundle_query
from civ_agent.tool_schemas import tool_model


def tools(invoke, *, rich=False):
    catalogue = [
        {"name": name, "description": name, "inputSchema": tool_model(name, "military").model_json_schema()}
        for name in ("player_turn", "unit_details")
    ]
    catalogue.append({"name": "inspect_bundle", "inputSchema": InspectionBundle.model_json_schema()})
    return gateway_tools("military", catalogue, invoke, include_controls=True, include_bundle_receipts=rich)


def packet():
    return {
        "decisionId": "decision1",
        "source": "actual-native-source",
        "receipts": [
            {"operationId": "op0", "tool": "player_turn", "status": "confirmed", "result": {"turn": 8}},
            {
                "operationId": "op1",
                "tool": "unit_details",
                "status": "rejected",
                "result": {"error": "TARGET_CHANGED"},
                "reason": "RECONSIDER",
            },
            {"operationId": "op2", "status": "unexecuted", "reason": "PREREQUISITE_CHANGED"},
        ],
        "unexecuted": ["op2"],
        "error": "INSPECTION_STOPPED",
        "extension": {"evidence": ["receipt-proof"]},
    }


def queries():
    return [
        {"tool": "player_turn", "arguments": {}},
        {"tool": "unit_details", "arguments": {"target": "unit1", "sections": ["movement"]}},
        {"tool": "unit_details", "arguments": {"target": "unit2"}},
        {"tool": "unit_details", "arguments": {"target": "unit3"}},
    ]


async def invoke_bundle(result, queries, *, rich=False, call_id="bundle-call"):
    invoke = AsyncMock(return_value=GatewayReceipt(result=result))
    tool = next(tool for tool in tools(invoke, rich=rich) if tool.name == "inspect_bundle")
    request = tool.function.__annotations__["request"].model_validate({"queries": queries})
    deps = GatewayDependencies(
        scope=Scope(game="game", player="p0", epoch="epoch", activation="turn8"),
        job="military8",
        purpose="military",
    )
    ctx = SimpleNamespace(deps=deps, tool_call_id=call_id, run_step=4)
    returned = await tool.function(ctx, request)
    assert invoke.await_count == 1
    assert invoke.call_args.args == (deps, call_id, 4, {"kind": "inspect", "operations": queries})
    return returned, tool


def test_legacy_bundle_output_and_description_remain_exact_by_default():
    result = packet()
    returned, tool = asyncio.run(invoke_bundle(result, queries()))
    expected = {
        "results": [
            {"tool": "player_turn", "arguments": {}, "result": {"turn": 8}},
            {
                "tool": "unit_details",
                "arguments": {"target": "unit1", "sections": ["movement"]},
                "result": {"error": "TARGET_CHANGED"},
            },
        ],
        "remaining": 1,
        "decisionId": "decision1",
        "error": "INSPECTION_STOPPED",
    }
    assert json.dumps(returned, separators=(",", ":")) == json.dumps(expected, separators=(",", ":"))
    assert (
        tool.description
        == "Read independent queries whose arguments are already known. Inner arguments use each canonical read schema; player_inspect uses subject/target directly, without another arguments wrapper. Returns a plain dictionary: results entries have tool/arguments/result, plus remaining/error. Check each entry and remaining before using data; do not call result_data on the whole bundle."
    )


def test_rich_bundle_retains_partial_rejected_pending_and_missing_receipts_with_identity():
    result = packet()
    before = copy.deepcopy(result)
    legacy, _ = asyncio.run(invoke_bundle(result, queries()))
    rich, tool = asyncio.run(invoke_bundle(result, queries(), rich=True))
    assert {key: rich[key] for key in legacy} == legacy
    assert rich["receiptVersion"] == 1
    assert rich["feedback"] == before
    assert rich["unexecuted"] == ["op2"]
    entries = rich["receiptEntries"]
    assert len(entries) == len(queries()) == 4
    assert [row["status"] for row in entries] == ["confirmed", "rejected", "unexecuted", "not_returned"]
    assert len({row["queryId"] for row in entries}) == 4
    assert entries[3]["receipt"] is None
    for index, query in enumerate(queries()):
        assert entries[index]["queryIndex"] == index
        assert entries[index]["tool"] == query["tool"]
        assert entries[index]["arguments"] == query["arguments"]
        assert entries[index]["receipt"] == (result["receipts"][index] if index < 3 else None)
        assert bundle_query(rich, query_id=entries[index]["queryId"])["query"] == entries[index]
    assert tool.description is not None and "receiptVersion=1" in tool.description
    assert result == before


def test_duplicate_queries_require_the_unique_returned_query_id():
    query = {"tool": "unit_details", "arguments": {"target": "unit1"}}
    result = {
        "decisionId": "decision2",
        "receipts": [
            {"operationId": f"op{index}", "tool": "unit_details", "status": "confirmed", "result": {"index": index}}
            for index in range(2)
        ],
        "unexecuted": [],
    }
    rich, _ = asyncio.run(invoke_bundle(result, [query, query], rich=True))
    with pytest.raises(ModelRetry, match="RESULT_SELECTION_AMBIGUOUS"):
        bundle_query(rich, tool="unit_details", arguments={"target": "unit1"})
    for index, entry in enumerate(rich["receiptEntries"]):
        assert bundle_query(rich, query_id=entry["queryId"])["query"]["receipt"]["result"] == {"index": index}


def test_extra_original_receipts_are_retained_without_fabricating_more_requested_queries():
    result = packet()
    rich, _ = asyncio.run(invoke_bundle(result, queries()[:1], rich=True))
    assert len(rich["receiptEntries"]) == 1
    assert rich["feedback"] == result
    assert rich["unexecuted"] == ["op2"]
    assert bundle_query(rich, query_id=rich["receiptEntries"][0]["queryId"])["bundle"]["feedback"] == result
