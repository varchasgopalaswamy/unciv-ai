"""Confirmed native effects refresh domain state without Python reply journals."""

import copy

import pytest

from civ_agent.civilization_state import StateConflict
from civ_agent.response_compaction import expand_response
from tests.domain_fixtures import Host, managed, run_async
from tests.test_action_authority import MOVE, session
from tests.test_prepare_host_effect import counts, forbid_journal


@run_async
async def test_native_apply_updates_real_domain_and_retains_the_exact_detached_host_receipt(tmp_path, monkeypatch):
    async with managed(tmp_path) as m:
        await m.gateway.call(m.economy, "player_turn", {}, "initial")
        arguments = {"kind": "SET_CITY_FOCUS", "target": "city1", "selection": "FoodFocus"}
        prepared = await m.gateway.prepare_host_effect(m.economy, "player_command", arguments, "native")
        raw = await m.host.tool(prepared.transport, prepared.payload)
        raw["hostExtension"] = {"exact": ["  Original\n", 0, False]}
        original, baseline = copy.deepcopy(raw), counts(m.memory)
        with monkeypatch.context() as patch:
            for method in ("call", "queue", "receipt", "uncertain", "fail_run", "_call_record"):
                patch.setattr(m.memory, method, forbid_journal)
            first = await m.gateway.apply_host_effect(m.economy, prepared, "native", raw)
            second = await m.gateway.apply_host_effect(m.economy, prepared, "native", raw, replayed=True)
        assert len(m.host.executed) == 1 and counts(m.memory) == baseline
        for reply in (first, second):
            expanded = expand_response(reply)
            assert all(expanded[key] == value for key, value in original.items())
            assert "stateUpdate" in expanded and m.economy.observation["revision"] == raw["revision"]
        assert m.main.observation is None and m.main.role_refresh_required
        assert m.roles.economy_status["cities"] == ["city1", "city2"]
        first["hostExtension"]["exact"].append("changed reply")
        assert raw == original


@pytest.mark.parametrize(
    "failure", [OSError("offline"), ValueError("bad refresh"), StateConflict("INVALID_HOST_REVISION")]
)
@run_async
async def test_native_apply_preserves_confirmed_receipt_on_refresh_failure_without_failing_run(
    tmp_path, monkeypatch, failure
):
    host = Host()
    async with session(tmp_path, host) as (gateway, memory, job, _):
        prepared = await gateway.prepare_host_effect(job, "player_command", MOVE, "native")
        raw = await host.tool(prepared.transport, prepared.payload)
        original, baseline = copy.deepcopy(raw), counts(memory)

        async def unavailable(name, arguments):
            raise failure

        with monkeypatch.context() as patch:
            for method in ("call", "queue", "receipt", "uncertain", "fail_run", "_call_record"):
                patch.setattr(memory, method, forbid_journal)
            patch.setattr(host, "tool", unavailable)
            result = await gateway.apply_host_effect(job, prepared, "native", raw)
        assert all(result[key] == value for key, value in original.items())
        assert result["stateUpdate"]["kind"] == "unavailable"
        assert counts(memory) == baseline and raw == original and job.observation is None
        assert len(host.executed) == 1 and gateway.run_failure is None
