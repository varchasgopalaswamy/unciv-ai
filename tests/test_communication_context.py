"""Focused diplomacy retains exact facts, inspected terms and recoverable history."""

import asyncio
import copy
import json
from contextlib import asynccontextmanager

import pytest
from jsonschema import Draft202012Validator

from civ_agent.civilization_state import CivilizationState
from civ_agent.coordination_context import MAX_COORDINATION_CHARACTERS, working_context
from civ_agent.proposal_gateway import ProposalGateway
from civ_agent.protocol import JsonObject
from civ_agent.role_view import label_commands, role_overview
from civ_agent.service_io import ServiceIO
from civ_agent.startup_context import (
    MAX_PREPARED_TRADES,
    communication_trade_queries,
    prepare_startup,
    startup_prompt,
)
from civ_agent.state_updates import apply_player_turn
from civ_agent.tool_schemas import tool_model
from civ_agent.war_state import WarStore
from tests.domain_fixtures import managed
from tests.test_conversation_context import message
from tests.test_military_roles import GOALS, position
from tests.test_trade_gateway import PendingTradesHost


def test_negotiation_view_keeps_constraints_threats_and_the_whole_force():
    raw = {
        "pending": [{"kind": "research"}],
        "alerts": [{"handle": "alert1"}],
        "economy": {
            "treasury": 100,
            "availableGold": 10,
            "committedGold": 90,
            "happiness": -4,
            "resources": [{"name": "Iron", "available": -1}],
            "unitSupply": {"overSupply": 3},
            "development": {"tileImprovements": ["Farm"]},
        },
        "tacticalReview": {"visibleHostiles": [{"civilization": "Greece"}], "limits": "Unseen forces unknown"},
        "diplomacy": [{"civilizationId": "Greece", "atWar": False, "bilateral": {"peaceTreatyTurns": 10}}],
        "militaryReadiness": {
            "roleCounts": {"melee": 1},
            "visibleForeignCities": [{"name": "Athens", "position": {"x": 1, "y": 0}}],
            "omittedVisibleForeignCities": 2,
            "foreignObservationLimits": "Currently visible only",
            "recruitment": ["Warrior", "Archer"],
        },
        "units": [
            {"handle": "unit1", "military": True, "strength": 14, "health": 50, "inspections": {"combat": {}}},
            {"handle": "unit2", "military": False, "protection": {"nearbyHostiles": 1}},
        ],
        "cities": [{"handle": "city1", "population": 9, "health": 150, "constructionEstimates": {"Granary": 4}}],
        "settlement": {"candidates": ["A distant site"]},
        "wonderOpportunities": [{"name": "Oracle"}],
        "recentProgress": {"previousOutput": [1, 2, 3]},
        "newSurface": {"unknown": True},
    }
    before = copy.deepcopy(raw)
    view = role_overview(raw, "communication", purpose="communication")
    for key in ("pending", "alerts", "diplomacy", "tacticalReview"):
        assert view[key] == raw[key]
    assert view["economy"] == {k: v for k, v in raw["economy"].items() if k != "development"}
    assert view["militaryReadiness"] == {k: v for k, v in raw["militaryReadiness"].items() if k != "recruitment"}
    assert len(view["units"]) == 2 and view["units"][0]["strength"] == 14
    assert view["units"][1]["protection"] == raw["units"][1]["protection"]
    assert view["cities"][0]["population"] == 9
    assert "inspections" not in view["units"][0] and "constructionEstimates" not in view["cities"][0]
    assert {"settlement", "wonderOpportunities", "recentProgress", "newSurface"} <= set(view["roleBrief"]["omitted"])
    assert raw == before and "newSurface" in role_overview(raw, "full")


def test_communication_view_switch_recovery_replay_and_new_activation(tmp_path):
    async def run():
        async with managed(tmp_path) as m:
            gateway = m.gateway
            job = await gateway.open("chat", "communication", "g1", m.host, {}, 0)
            prepared = await prepare_startup(gateway, job)
            assert set(prepared["reads"]) == {"chat_read"} and job.observation is None
            gateway.commit_startup_coordination(job)
            first = await gateway.call(job, "player_turn", {}, "first-overview")
            assert first["roleBrief"]["view"] == "communication"
            assert "roleCompletion" not in first and "endTurn" not in first
            schema = next(t["outputSchema"] for t in await gateway.tools(job) if t["name"] == "player_turn")
            Draft202012Validator(schema).validate(first)
            m.host.revision = "sibling-change"
            m.host.view.setdefault("economy", {})["treasury"] = 73
            changed = await gateway.call(job, "player_turn", {}, "changed")
            current = apply_player_turn(first, changed)
            assert current == job.delivered_overview and current["economy"]["treasury"] == 73
            assert current["roleBrief"]["view"] == "communication"
            # Replaying an earlier receipt never restores its facts or switches views.
            assert await gateway.call(job, "player_turn", {}, "first-overview") == first
            assert job.delivered_overview == current
            full = await gateway.call(job, "player_turn", {"view": "full"}, "full")
            assert "roleBrief" not in full
            detail = await gateway.call(job, "city_details", {"target": "city1"}, "city")
            assert detail["available"] and detail["data"]["entity"]["handle"] == "city1"
            await gateway.call(job, "player_turn", {}, "still-full")
            assert job.overview_view == "full"
            focused = await gateway.call(job, "player_turn", {"view": "communication"}, "focused")
            assert focused["roleBrief"]["view"] == "communication" and "stateUpdate" not in focused
            gateway.reset_coordination(job)
            recovered = await gateway.call(job, "player_turn", {"delivery": "full"}, "recovered")
            assert recovered["roleBrief"]["view"] == "communication"
            fresh = await gateway.open("next-chat", "communication", "g1", m.host, {}, 0)
            assert fresh.overview_view == "communication" and fresh.delivered_overview is None
            instructions = startup_prompt(prepared, "communication", "Negotiate").instructions
            assert "command_help" not in instructions and "inspect_bundle" not in instructions
            assert "only the host advances" not in instructions and "finish_role" not in instructions
            expanded_instructions = startup_prompt(
                {**prepared, "overviewView": "full"}, "communication", "Negotiate"
            ).instructions
            assert "command_help" not in expanded_instructions and "inspect_bundle" not in expanded_instructions
            offer = label_commands(
                {"command": {"kind": "ADOPT_POLICY", "selection": "Tradition"}}, m.roles, "communication", m.host.view
            )
            assert offer["commandAccess"]["mode"] == "unavailable"
            assert offer["commandAccess"]["owner"] == "gameplay" and "tool" not in offer["commandAccess"]

    asyncio.run(run())


def test_trade_preload_selection_uses_new_events_and_exact_audiences():
    social = {
        "civilization": "Rome",
        "trades": [
            {"proposalId": f"d{i}", "author": author, "partner": "Rome", "status": "PENDING"}
            for i, author in enumerate(["France", "Greece", "Greece", "Greece"])
        ],
        "conversationContext": {"threads": [{"participants": ["Rome", "France"]}]},
        "messages": [],
    }
    assert not communication_trade_queries(social)
    incoming = message(1, ["Rome", "Greece", "France"], "Consider my offer", public=True)
    incoming["body"]["author"] = "Greece"
    social["messages"] = [incoming, {"kind": "trade", "body": {"proposalId": "d3"}}]
    before = copy.deepcopy(social)
    selected = communication_trade_queries(social)
    assert len(selected) == MAX_PREPARED_TRADES
    assert [args["proposalId"] for _, args in selected] == ["d3", "d1"]
    assert social == before
    # Own echoed messages, old excerpts, concluded offers and AI trades add no preload.
    incoming["body"]["author"] = "Rome"
    social["messages"] = [incoming]
    assert not communication_trade_queries(social)
    incoming["body"].update(author="France", public=False)
    social["trades"][0]["mode"] = "NATIVE_AI"
    for trade in social["trades"][1:]:
        trade["status"] = "ENACTED"
    assert not communication_trade_queries(social)


class CommunicationHost(PendingTradesHost):
    def __init__(self, *, changed=False, failed=False):
        super().__init__()
        self.trades: list[JsonObject] = copy.deepcopy(self.trades)
        self.changed, self.failed = changed, failed
        self.trades[1].update(
            version=1,
            acceptAvailable=True,
            declineAvailable=True,
            clauses=[
                {"from": "Greece", "to": "Rome", "item": {"type": "Gold", "name": "Gold", "amount": 20, "duration": -1}}
            ],
        )

    async def tool(self, name, args):
        if name == "player_communication" and args["op"] == "trade_read" and self.failed:
            self.reads.append(args["proposal"])
            return {"error": "UNKNOWN_TRADE", "applied": False, "recoverable": True}
        if name == "player_communication" and args["op"] == "trade_decide" and self.changed:
            self.writes.append(copy.deepcopy(args))
            return {"error": "STALE_REVISION"}
        return await super().tool(name, args)


@asynccontextmanager
async def negotiation(tmp_path, **host_options):
    memory, io = CivilizationState(tmp_path / "memory", "Rome"), ServiceIO()
    host = CommunicationHost(**host_options)
    gateway = ProposalGateway(memory, io, tmp_path / "work")
    job = await gateway.open("chat", "communication", "g1", host, {}, 0)
    memory.ingest("g1", [message(1, ["Rome", "Greece"], "Please consider our terms")], 1)
    try:
        yield gateway, job, host
    finally:
        await io.close()
        memory.engine.dispose()


@pytest.mark.parametrize("changed", [False, True])
def test_preloaded_trade_is_inspected_with_exact_version_and_replay(tmp_path, changed):
    async def run():
        async with negotiation(tmp_path, changed=changed) as (gateway, job, host):
            prepared = await prepare_startup(gateway, job)
            assert host.reads == ["ddef34"] and not host.writes
            detail = prepared["decisionDetails"][0]
            assert detail["trade"]["clauses"] == host.trades[1]["clauses"]
            assert job.trades["ddef34"]["version"] == 1
            assert gateway.memory.delivered_records(job.id, ["observation:trade/ddef34"])
            core = {t["name"] for t in prepared["coreTools"]}
            assert {"mcp__civ__trade_read", "mcp__civ__trade_decide"} <= core
            gateway.commit_startup_coordination(job)
            response = next(r for r in detail["trade"]["responses"] if r["arguments"]["decision"] == "decline")
            if changed:
                host.trades[1].update(version=2)
                host.trades[1]["clauses"][0]["item"]["amount"] = 40
            result = await gateway.call(job, response["tool"], response["arguments"], "decision")
            assert host.writes[-1]["version"] == 1
            assert result.get("error") == ("STALE_REVISION" if changed else None)
            assert await gateway.call(job, response["tool"], response["arguments"], "decision") == result
            assert len(host.writes) == 1

    asyncio.run(run())


def test_failed_preload_keeps_pending_inspection_and_never_authorizes_a_decision(tmp_path):
    async def run():
        async with negotiation(tmp_path, failed=True) as (gateway, job, host):
            prepared = await prepare_startup(gateway, job)
            assert prepared["decisionDetails"][0]["error"] == "UNKNOWN_TRADE"
            assert not job.trades
            pending = prepared["reads"]["chat_read"]["trades"][0]
            assert pending["inspection"] == {"tool": "trade_read", "arguments": {"proposalId": "ddef34"}}
            host.failed = False
            rejected = await gateway.call(
                job, "trade_decide", {"proposalId": "ddef34", "decision": "decline"}, "unread"
            )
            assert rejected["error"] == "PROPOSAL_NOT_OBSERVED" and not host.writes

    asyncio.run(run())


def test_accepted_goals_preload_and_omitted_records_remain_retrievable(tmp_path):
    async def run():
        async with negotiation(tmp_path) as (gateway, job, _):
            gateway.wars.apply(
                "g1",
                "gameplay",
                "war_goals",
                {"action": "initial", "opponent": "Greece", "goals": GOALS, "reason": "Defend"},
                "initial",
            )
            state = gateway.wars.synchronize("g1", position())
            episode = next(iter(state["episodes"]))
            proposed_goals = [{**GOALS[0], "outcome": "Advance toward Athens"}]
            gateway.wars.apply(
                "g1",
                "military",
                "war_goals",
                {
                    "action": "propose",
                    "opponent": "Greece",
                    "version": 1,
                    "goals": proposed_goals,
                    "reason": "New opportunity",
                },
                "propose",
            )
            state = gateway.wars.state()
            before = copy.deepcopy(state)
            prepared = await prepare_startup(gateway, job)
            coordination = prepared["economyCoordination"]
            assert coordination["warState"]["episodes"][episode]["goals"] == GOALS
            pending = coordination["warState"]["episodes"][episode]["pendingGoalRevisions"]
            assert pending["count"] == 1 and "proposals" not in coordination["warState"]["episodes"][episode]
            exact = await gateway.call(job, pending["read"]["tool"], pending["read"]["arguments"], "proposals")
            assert next(iter(exact["items"][0]["proposals"].values()))["goals"] == proposed_goals
            gateway.commit_startup_coordination(job)
            assert job.war_versions[episode] == 1
            assert gateway.wars.state() == before
            current = await gateway.call(job, "war_goals", {}, "read")
            assert current["episodes"][episode]["goals"] == GOALS
            schema = tool_model("war_goals", "communication").model_json_schema()
            assert Draft202012Validator(schema).is_valid({"action": "read"})
            assert not Draft202012Validator(schema).is_valid(
                {"action": "initial", "opponent": "Greece", "goals": GOALS, "reason": "Override"}
            )
            # Whole records exceeding the working budget remain available in exact history.
            state["episodes"][episode]["goals"][0]["outcome"] = "unchanged authored goal " * 1000
            bounded = working_context({"warState": state}, "communication")
            assert len(json.dumps(bounded)) < MAX_COORDINATION_CHARACTERS
            assert bounded["history"]["omitted"]["episodes"] == 1
            retrieved = WarStore._read(state, "communication", {"section": "episodes", "ids": [episode]})
            assert retrieved["items"][0]["goals"] == state["episodes"][episode]["goals"]
            assert retrieved["oversizedRecord"]
            # Peace changes the native relationship without rewriting the authored goal.
            ended = gateway.wars.synchronize("g1", position(war=False))
            assert not working_context({"warState": ended}, "communication")["warState"]["episodes"]
            archived = await gateway.call(job, "war_goals", {"section": "episodes", "ids": [episode]}, "history")
            assert archived["items"][0]["goals"] == GOALS and archived["items"][0]["active"] is False

    asyncio.run(run())
