"""Idle worker skips are explicit, current, role-owned and replayable."""

import copy
import json

import pytest
from jsonschema import ValidationError as SchemaError
from pydantic import ValidationError

from civ_agent.decision_contracts import DecisionContract
from civ_agent.economy_completion import worker_completion
from civ_agent.tool_schemas import EconomyFinishRole
from tests.domain_fixtures import managed, run_async


def worker(handle="worker1", **changes):
    return {
        "handle": handle,
        "name": "Worker",
        "military": False,
        "health": 100,
        "position": {"x": 0, "y": 0},
        "movement": "2/2",
        "movementSupported": True,
        "idle": True,
        "canFoundCity": False,
        "worker": {"currentJob": None, "queue": []},
        **changes,
        "combat": {"attacksRemaining": 0, **changes.get("combat", {})},
    }


def install_workers(m, count=2):
    m.host.view["map"]["units"].extend(worker(f"worker{index + 1}") for index in range(count))
    m.host.revision = "workers-added"


def skip(*targets):
    return {"targets": list(targets), "reason": "Wait for the escort before entering the exposed area."}


def schedule_snapshot(m):
    with m.memory.transaction() as db:
        return m.memory._meta(db, "economy:schedule")


def test_remaining_workers_exclude_jobs_travel_standing_orders_exhaustion_and_other_roles():
    units = [
        worker(),
        worker("fractional", movement="0.3/2"),
        worker("working", worker={"currentJob": "Farm", "queue": [{"name": "Farm"}]}),
        worker("traveling", movementOrder={"destination": {"x": 1, "y": 0}}),
        worker("sleeping", combat={"order": {"action": "Sleep"}}),
        worker("skipped", combat={"order": {"action": "Skip"}}),
        worker("spent", movement="0/2"),
        worker("military", military=True),
        worker("assigned"),
        worker("native-escort", escort={"active": True, "partner": "guard2"}),
    ]
    state = {"escorts": {"guard": {"civilian": "assigned"}}}
    assert worker_completion({"map": {"units": units}}, state)["idleWorkers"] == ["fractional", "worker1"]


def test_worker_skip_contract_is_strict_and_economy_only():
    contract = DecisionContract("economy", [])
    arguments = {"workerSkips": [skip("worker1", "worker2")]}
    decision = {"kind": "finish", "arguments": arguments}
    assert contract.parse(contract.encode(decision)) == decision
    # Wire null retains the ordinary omitted/empty default.
    assert contract.parse(contract.encode({"kind": "finish", "arguments": {}}))["arguments"] == {}
    for reason in ("", "  \t\n", "x" * 501):
        with pytest.raises(ValidationError):
            EconomyFinishRole.model_validate({"workerSkips": [{**skip("worker1"), "reason": reason}]})
    with pytest.raises(ValidationError):
        EconomyFinishRole.model_validate({"workerSkips": [{"targets": [], "reason": "Hold"}]})
    for role in ("gameplay", "military", "communication"):
        other = DecisionContract(role, [])
        wire = json.loads(other.encode({"kind": "finish", "arguments": {}}))
        wire["decision"]["arguments"]["workerSkips"] = arguments["workerSkips"]
        with pytest.raises(SchemaError):
            other.parse(json.dumps(wire))


@pytest.mark.parametrize("targets", [("worker1",), ("worker1", "worker1", "worker2"), ("worker1", "worker2", "unit1")])
@run_async
async def test_skips_must_cover_exactly_the_remaining_owned_idle_workers(tmp_path, targets):
    async with managed(tmp_path) as m:
        install_workers(m)
        await m.gateway.call(m.economy, "player_turn", {}, "read")
        before = copy.deepcopy(schedule_snapshot(m))
        result = await m.gateway.call(m.economy, "finish_role", {"workerSkips": [skip(*targets)]}, "finish")
        assert result["error"] == "ECONOMY_WORKER_SKIPS_REQUIRED"
        assert schedule_snapshot(m) == before and not m.roles.ready["economy"]


@run_async
async def test_new_worker_requires_fresh_observation_before_an_explicit_skip_can_finish(tmp_path):
    async with managed(tmp_path) as m:
        install_workers(m, 1)
        await m.gateway.call(m.economy, "player_turn", {}, "read")
        m.host.view["map"]["units"].append(worker("worker2"))
        m.host.revision = "new-worker"
        before = copy.deepcopy(schedule_snapshot(m))
        missing = await m.gateway.call(m.economy, "finish_role", {"workerSkips": [skip("worker1")]}, "old-skips")
        assert missing["error"] == "ECONOMY_WORKER_SKIPS_REQUIRED"
        stale = await m.gateway.call(
            m.economy, "finish_role", {"workerSkips": [skip("worker1", "worker2")]}, "unseen-worker"
        )
        assert stale["error"] == "ECONOMY_OBSERVATION_CHANGED"
        assert schedule_snapshot(m) == before
        await m.gateway.call(m.economy, "player_turn", {}, "refresh")
        accepted = await m.gateway.call(
            m.economy, "finish_role", {"workerSkips": [skip("worker1", "worker2")]}, "fresh-skips"
        )
        assert accepted["roleFinished"]


@pytest.mark.parametrize("change", ["job", "destination", "order", "escort"])
@run_async
async def test_worker_actions_or_handoffs_remove_the_skip_requirement(tmp_path, change):
    async with managed(tmp_path) as m:
        install_workers(m, 1)
        await m.gateway.call(m.economy, "player_turn", {}, "read")
        unit = m.host.view["map"]["units"][-1]
        if change == "job":
            unit["worker"]["currentJob"] = "Farm"
        elif change == "destination":
            unit["movementOrder"] = {"destination": {"x": 1, "y": 0}}
        elif change == "order":
            unit["combat"]["order"] = {"action": "Skip"}
        else:
            m.roles.war_state["escorts"] = {"unit1": {"civilian": "worker1"}}
        m.host.revision = "worker-resolved"
        refreshed = await m.gateway.call(m.economy, "player_turn", {}, "refresh")
        assert refreshed["roleCompletion"]["idleWorkers"] == []
        outdated = await m.gateway.call(m.economy, "finish_role", {"workerSkips": [skip("worker1")]}, "old-skip")
        assert outdated["error"] == "ECONOMY_WORKER_SKIPS_REQUIRED"
        done = await m.gateway.call(m.economy, "finish_role", {}, "finish")
        assert done["roleFinished"]
