"""Correctable model requests produce receipts without hiding lifecycle failures."""

import pytest

from civ_agent.civilization_state import StateConflict
from tests.domain_fixtures import run_async
from tests.test_diplomacy_contracts import opened


@pytest.mark.parametrize(
    "purpose,tool,arguments,code",
    [
        ("gameplay", "native_advice", {}, "NATIVE_ADVICE_DISABLED"),
        ("gameplay", "chat_delivery", {"deliveryId": "missing"}, "DIPLOMACY_ROUTING_DISABLED"),
        ("communication", "research_details", {}, "TOOL_NOT_PERMITTED"),
        ("communication", "player_command", {"kind": "END_TURN"}, "GAMEPLAY_NOT_PERMITTED"),
        ("gameplay", "unknown_tool", {}, "TOOL_NOT_PERMITTED"),
        ("gameplay", "finish_role", {}, "ACTIVE_ROLE_REQUIRED"),
        ("gameplay", "economy_request", {"text": "Build a Worker."}, "ACTIVE_ROLE_REQUIRED"),
        ("gameplay", "city_override", {"city": "city1", "reason": "Handle the city."}, "ACTIVE_ROLE_REQUIRED"),
        ("gameplay", "player_command", {"kind": "END_TURN", "revision": "invented"}, "INVALID_TOOL_ARGUMENTS"),
    ],
)
@run_async
async def test_definite_permission_rejections_are_replayable_without_host_actions(
    tmp_path, purpose, tool, arguments, code
):
    async with opened(tmp_path, purpose=purpose) as (gateway, job, host):
        result = await gateway.call(job, tool, arguments, "rejected")
        assert result["error"] == code and result["applied"] is False
        assert await gateway.call(job, tool, arguments, "rejected") == result
        assert not gateway.memory.pending() and not host.executed and not host.writes
        corrected = await gateway.call(job, "chat_history", {"query": "earlier"}, "corrected")
        assert "error" not in corrected


@pytest.mark.parametrize("code", ["STALE_EPOCH", "STALE_JOB", "RECEIPT_UNCERTAIN", "UNKNOWN_INTERNAL_CONFLICT"])
@run_async
async def test_agreement_read_does_not_hide_lifecycle_or_uncertain_state(tmp_path, code):
    async with opened(tmp_path) as (gateway, job, host):
        native = host.tool

        async def tool(name, args):
            if name == "player_communication" and args["op"] == "agreement_list":
                return {"agreements": []}
            return await native(name, args)

        host.tool = tool
        observe = gateway.memory.observe

        def fail(key, body, job=None):
            if key == "agreements":
                raise StateConflict(code)
            return observe(key, body, job)

        gateway.memory.observe = fail
        with pytest.raises(StateConflict, match=code):
            await gateway.call(job, "agreement_list", {}, "read")
