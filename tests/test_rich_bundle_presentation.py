"""One materialization for every projection of a rich inspection batch."""

import copy
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

from pydantic_ai.messages import ToolReturn

from civ_agent.runtime.player_observations import PlayerObservations
from tests.domain_fixtures import run_async
from tests.test_player_observations import context, detail


def bundle(data) -> dict[str, Any]:
    row = {"operationId": "one", "status": "confirmed", "tool": "city_details", "result": data}
    return {
        "receiptVersion": 1,
        "decisionId": "read",
        "remaining": 0,
        "unexecuted": [],
        "results": [{"tool": "city_details", "arguments": {"target": "city"}, "result": data}],
        "feedback": {"decisionId": "read", "receipts": [row], "unexecuted": []},
        "receiptEntries": [
            {
                "queryId": "q0",
                "queryIndex": 0,
                "tool": "city_details",
                "arguments": {"target": "city"},
                "status": "confirmed",
                "receipt": row,
            }
        ],
    }


@run_async
async def test_delta_is_materialized_once_and_all_receipt_views_match():
    cap = PlayerObservations(invoke=AsyncMock())
    ctx = context()
    await cap.materialize(ctx, detail(value=1), call_id="base")
    previous = copy.deepcopy(next(iter(cap.details.values())))
    response = detail(sequence=2, previous=previous, value=2)
    assert response["detailUpdate"]["kind"] == "delta"
    value = bundle(response)
    result = await cap.after_tool_execute(
        ctx,
        call=SimpleNamespace(tool_name="inspect_bundle", tool_call_id="batch"),
        tool_def=SimpleNamespace(metadata={}),
        args=None,
        result=value,
    )
    assert isinstance(result, ToolReturn)
    result = result.return_value
    assert isinstance(result, dict)
    views = [
        result["results"][0]["result"],
        result["feedback"]["receipts"][0]["result"],
        result["receiptEntries"][0]["receipt"]["result"],
    ]
    assert all(view["data"]["value"] == 2 and view["detailUpdate"]["kind"] == "full" for view in views)
    assert result["receiptEntries"][0]["queryId"] == "q0"
    cap.invoke.assert_not_called()


@run_async
async def test_partial_batch_preserves_unexecuted_and_missing_query_identity():
    cap = PlayerObservations(invoke=AsyncMock())
    ctx = context()
    value = bundle({"value": 1})
    missing = {
        "queryId": "q2",
        "queryIndex": 2,
        "tool": "city_details",
        "arguments": {"target": "other"},
        "status": "not_returned",
        "receipt": None,
    }
    rejected = {"operationId": "two", "status": "unexecuted", "reason": "read-limit", "tool": "city_details"}
    value["receiptEntries"] += [
        {**missing, "queryId": "q1", "queryIndex": 1, "status": "unexecuted", "receipt": rejected},
        missing,
    ]
    value["feedback"]["receipts"].append(rejected)
    value["feedback"]["unexecuted"] = ["two"]
    value["remaining"] = 1
    result = await cap.materialize(ctx, value, call_id="partial", rich_bundle=True)
    assert len(result["results"]) == 1
    assert [entry["status"] for entry in result["receiptEntries"]] == ["confirmed", "unexecuted", "not_returned"]
    assert result["receiptEntries"][1]["receipt"]["reason"] == "read-limit"
    assert result["feedback"]["unexecuted"] == ["two"]
    cap.invoke.assert_not_called()
