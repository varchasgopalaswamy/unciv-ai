"""Regression cases from the model-facing API consistency audit."""

import asyncio
import copy
import json
from contextlib import asynccontextmanager

import pytest

from civ_agent.civilization_state import CivilizationState
from civ_agent.observations import DetailQuery, overview, project_detail
from civ_agent.proposal_gateway import READ_TOOLS, ProposalGateway, public_result
from civ_agent.protocol import JsonObject
from civ_agent.service_io import ServiceIO
from civ_agent.social_context import social_context
from civ_agent.startup_context import prepare_startup, startup_prompt
from civ_agent.state_updates import apply_overview_update
from civ_agent.tactical_review import tactical_review
from tests.test_construction_estimates import estimate
from tests.test_proposal_gateway import Host
from tests.test_tactical_review import reviewed


class ConsistencyHost(Host):
    def __init__(self):
        super().__init__()
        self.calls = []
        self.view["researchQueue"] = ["Pottery"]
        self.view["cities"][0]["purchases"] = {
            "constructions": [
                {
                    "name": "Granary",
                    "available": True,
                    "goldCost": 250,
                    "confirmation": "Spend 250 gold",
                }
            ]
        }

    async def list_tools(self) -> list[JsonObject]:
        return [
            *await super().list_tools(),
            *[
                {"name": name, "description": name, "inputSchema": {"type": "object"}}
                for name in sorted(READ_TOOLS | {"player_summary", "owned_cities", "free_great_person_choice"})
            ],
        ]

    async def tool(self, name, args):
        self.calls.append((name, copy.deepcopy(args)))
        if name == "player_capabilities":
            return {
                "queries": ["player_summary", "owned_cities", "free_great_person_choice", "player_turn"],
                "commands": ["END_TURN"],
                "protocolVersion": 1,
                "completeGameplay": False,
                "durableRecovery": True,
            }
        if name == "research_details":
            return {
                "epoch": "g1",
                "revision": self.revision,
                "turn": 0,
                "technologies": [],
                "researchQueue": self.view["researchQueue"][:],
            }
        if name == "policy_details":
            return {"epoch": "g1", "revision": self.revision, "turn": 0, "policies": []}
        if name == "victory_progress":
            return {"turn": 0, "victories": []}
        if name == "player_result":
            return {"finished": False}
        if name == "player_command" and args.get("kind") == "SET_RESEARCH_QUEUE":
            self.view["researchQueue"] = args["queue"][:]
        if name == "player_communication":
            if args["op"] == "trade_options":
                return {"partner": args["partner"], "mode": "SHARED", "give": [], "receive": []}
            if args["op"] == "trade_read":
                return {"trade": {"proposal": args["proposal"], "version": 1, "mode": "SHARED", "status": "PENDING"}}
            if args["op"] == "agreement_list":
                return {"agreements": []}
            if args["op"] == "agreement_read":
                return {"agreement": {"agreement": args["agreement"], "version": 1, "status": "ACTIVE"}}
            if args["op"] == "offer":
                self.offers.append(
                    {
                        "proposal": "p1",
                        "version": 1,
                        "participants": args["participants"],
                        "kind": args["kind"],
                        "terms": args["terms"],
                        "effective": False,
                    }
                )
                return {"offer": copy.deepcopy(self.offers[-1])}
        result = await super().tool(name, args)
        if name == "player_communication" and args["op"] == "state":
            result["context"] = {"turn": 0, "economy": {"treasury": 777}, "scenario": {"instructions": "SCENARIO"}}
        return result


@asynccontextmanager
async def opened(tmp_path, *, purpose="gameplay", message=False):
    io = ServiceIO()
    memory = CivilizationState(tmp_path / "memory", "Rome")
    memory.bind("g1")
    if message:
        memory.ingest(
            "g1",
            [
                {
                    "id": 1,
                    "kind": "message",
                    "wake": True,
                    "body": {"message": "m1", "author": "Greece", "text": "UNIQUE_INCOMING_CHAT"},
                }
            ],
            1,
        )
    host = ConsistencyHost()
    gateway = ProposalGateway(memory, io, tmp_path / "workspace")
    job = await gateway.open("audit", purpose, "g1", host, {})
    try:
        yield gateway, job, host
    finally:
        await gateway.close(job, True)
        await io.close()


@pytest.mark.parametrize("purpose", ["gameplay", "communication"])
def test_startup_delivers_messages_once_and_context_only_where_needed(tmp_path, purpose):
    async def run():
        async with opened(tmp_path, purpose=purpose, message=True) as (gateway, job, host):
            prepared = await prepare_startup(gateway, job)
            prompt = startup_prompt(prepared, purpose, "")
            assert prompt.user.count("UNIQUE_INCOMING_CHAT") == 1
            assert "newEvents" not in prepared["memoryContext"]
            chat = prepared["reads"]["chat_read"]
            assert all("wake" not in event for event in chat["messages"])
            assert "negotiation" not in chat
            assert ("context" in chat) == (purpose == "communication")
            if purpose == "communication":
                assert chat["context"]["scenario"]["instructions"] == "SCENARIO"
                assert "turn" not in chat["context"]
                assert gateway.memory.summary(job=job.id)["turn"] == chat["turn"]
            else:
                assert "communication" not in prepared["reads"]["player_turn"]
                assert "negotiation" not in prepared["reads"]["player_turn"]
                capabilities = prepared["reads"]["player_capabilities"]
                assert capabilities["commands"] == ["END_TURN"]
                assert (
                    not {
                        "protocolVersion",
                        "completeGameplay",
                        "durableRecovery",
                        "version",
                    }
                    & capabilities.keys()
                )

    asyncio.run(run())


def test_all_independent_reads_expire_without_requerying_them_after_action(tmp_path):
    async def run():
        async with opened(tmp_path) as (gateway, job, host):
            base = await gateway.call(job, "player_turn", {}, "start")
            await gateway.call(job, "city_details", {"target": "city1", "sections": ["purchases"]}, "local")
            queries = [
                ("research_details", {"names": ["Mining"]}),
                ("policy_details", {"names": ["Tradition"]}),
                ("victory_progress", {"sections": ["rankings"]}),
                ("player_result", {}),
                ("trade_options", {"partner": "Greece"}),
                ("trade_read", {"proposalId": "d1"}),
                ("agreement_list", {}),
                ("agreement_read", {"agreement": "a1"}),
                ("chat_read", {}),
            ]
            for name, args in queries:
                value = await gateway.call(job, name, args, "read-" + name)
                assert value["query"] == {"tool": name, "arguments": args}
            before = len(host.calls)
            result = await gateway.call(
                job,
                "player_command",
                {
                    "kind": "SET_RESEARCH_QUEUE",
                    "queue": ["Mining"],
                    "intent": {"reason": "Improve production"},
                },
                "research",
            )
            assert apply_overview_update(base, result["stateUpdate"])["researchQueue"] == ["Mining"]
            invalidated = result["stateUpdate"]["invalidatedDetails"]
            assert {q["tool"] for q in invalidated} == {name for name, _ in queries}
            assert [name for name, _ in host.calls[before:]] == [
                "player_command",
                "player_turn",
                "player_communication",
            ]
            assert [q.tool for q in job.details_read] == ["city_details"]
            # Board-dependent availability expired, but the inspected terms
            # still select this exact version for native consent validation.
            assert job.trades["d1"]["version"] == 1
            fresh = await gateway.call(job, "research_details", {"names": ["Mining"]}, "reread")
            assert fresh["researchQueue"] == ["Mining"] and fresh["turn"] == 0
            host.revision = "external-change"
            full = await gateway.call(job, "player_turn", {}, "resync")
            assert fresh["query"] in full["invalidatedDetails"]
            assert job.delivered_overview is not None and "invalidatedDetails" not in job.delivered_overview

    asyncio.run(run())


def test_social_mutation_invalidates_current_chat_in_gameplay_and_communication(tmp_path):
    async def run(purpose):
        async with opened(tmp_path / purpose, purpose=purpose) as (gateway, job, host):
            if purpose == "gameplay":
                await gateway.call(job, "player_turn", {}, "start")
            chat = await gateway.call(job, "chat_read", {}, "read")
            result = await gateway.call(
                job,
                "commitment_offer",
                {
                    "participants": ["Rome", "Greece"],
                    "kind": "other",
                    "terms": "Maybe support each other",
                    "conditions": "When practical",
                    "intent": {"reason": "Explore cooperation"},
                },
                "offer",
            )
            update = result["stateUpdate"] if purpose == "gameplay" else result
            assert chat["query"] in update["invalidatedDetails"]
            fresh = await gateway.call(job, "chat_read", {}, "reread")
            assert fresh["offers"][0]["version"] == 1

    for purpose in ("gameplay", "communication"):
        asyncio.run(run(purpose))


@pytest.mark.parametrize("inspection", ["purchases", "named"])
def test_purchase_commands_succeed_after_reading_the_offered_costs(tmp_path, inspection):
    async def run():
        async with opened(tmp_path) as (gateway, job, host):
            await gateway.call(job, "player_turn", {}, "start")
            if inspection == "purchases":
                read = await gateway.call(
                    job, "city_details", {"target": "city1", "sections": ["purchases"]}, "inspect"
                )
                option = read["data"]["entity"]["purchases"]["constructions"][0]
            else:
                read = await gateway.call(
                    job, "construction_details", {"target": "city1", "names": ["Granary"]}, "inspect"
                )
                option = read["data"]["constructions"][0]["purchase"]
            result = await gateway.call(
                job,
                "player_command",
                {
                    **option["command"],
                    "confirmAction": True,
                    "intent": {"reason": "Grow this city"},
                },
                "buy",
            )
            assert result["done"] and host.executed[-1]["kind"] == "BUY_CONSTRUCTION"

    asyncio.run(run())


def test_current_social_context_preserves_effective_and_proposed_versions_and_history():
    def offer(id, version=1, **extra):
        return {"proposal": id, "version": version, "effective": False, "terms": "Perhaps help when feasible", **extra}

    raw = {
        "context": {"turn": 20},
        "offers": [
            offer("p1", effective=True),
            offer("p1", 2),
            offer("expired", expiresTurn=20),
            offer("withdrawn"),
            offer("superseded"),
            offer("superseded", 2),
        ],
        "consents": [{"proposal": "withdrawn", "version": 1, "author": "Rome", "decision": "withdraw"}],
        "outcomes": [{"old": "outcome"}],
        "trades": [{"proposal": "d1", "status": "PENDING"}, {"proposal": "d2", "status": "ENACTED"}],
        "agreements": [{"agreement": "a1", "status": "ACTIVE"}, {"agreement": "a2", "status": "ENDED"}],
    }
    before = copy.deepcopy(raw)
    current = social_context(raw, include_context=False, include_history=False, include_native_trades=True)
    assert [(o["proposal"], o["version"]) for o in current["offers"]] == [("p1", 1), ("p1", 2), ("superseded", 2)]
    assert all(o["terms"] == "Perhaps help when feasible" for o in current["offers"])
    assert current["consents"] == [] and "outcomes" not in current
    assert len(current["trades"]) == len(current["agreements"]) == 1
    history = social_context(raw, include_context=True, include_history=True, include_native_trades=True)
    assert history == {**raw, "context": {}, "turn": 20, "chatRecipients": []}
    assert raw == before


def test_overview_has_one_unit_roster_and_reports_current_settler_production():
    snapshot = reviewed()
    city = snapshot["cities"][0]
    city["constructionQueue"] = ["Library", "Settler"]
    snapshot["tacticalReview"] = tactical_review(snapshot).model_dump()
    view = overview(snapshot)
    assert city["name"] not in view["tacticalReview"]["citiesProducingSettlers"]
    assert "units" not in view["turnReview"] and "military" not in view["tacticalReview"]
    assert not {"treasury", "goldPerTurn", "happiness"} & view["tacticalReview"].keys()
    soldier = next(u for u in view["units"] if u["military"])
    assert {"strength", "rangedStrength", "health", "position"} <= soldier.keys()
    injured = view["tacticalReview"]["militaryNearHostiles"]
    assert injured and not any(u["otherMilitaryOnSameTile"] for u in injured)
    city["constructionQueue"] = ["Settler", "Library"]
    assert city["name"] in tactical_review(snapshot).citiesProducingSettlers


def test_construction_accounting_keeps_later_copies_distinct_without_repeating_first_copy():
    host = Host()
    host.view["phase"] = "ACTIVE"
    city = host.view["cities"][0]
    city.update(constructionQueue=["Warrior", "Warrior"], constructionEstimates={"Warrior": estimate()})
    city["development"] = {
        "constructions": [
            {
                "index": index,
                "name": "Warrior",
                "canRemove": True,
                "moveToIndices": [1 - index],
                "productionCost": 60,
                "productionStored": 10 if index == 0 else 0,
                "productionPerTurn": 10,
                "turns": 5 if index == 0 else 6,
            }
            for index in (0, 1)
        ]
    }
    query = DetailQuery(tool="construction_details", arguments={"target": "city1", "names": ["Warrior"]})
    entry = project_detail(host.view, query)["data"]["constructions"][0]
    assert entry["production"]["stored"] == 10
    first, later = entry["progress"]
    assert "production" not in first
    assert later["production"]["stored"] == 0 and later["production"]["turnsIfStartedNow"] == 6
    details = project_detail(host.view, DetailQuery(tool="city_details", arguments={"target": "city1"}))["data"][
        "entity"
    ]
    assert "production" not in details["constructionEstimates"]["Warrior"]
    assert details["development"]["constructions"][0]["production"]["stored"] == 10
    assert "constructionTurns" not in overview(host.view)["cities"][0]


def test_legacy_tools_are_absent_and_review_recovery_uses_public_arguments(tmp_path):
    async def run():
        async with opened(tmp_path) as (gateway, job, host):
            tools = {t["name"]: t for t in await gateway.tools(job)}
            old = {"player_summary", "owned_cities", "free_great_person_choice"}
            assert not old & tools.keys()
            capabilities = await gateway.call(job, "player_capabilities", {}, "capabilities")
            assert not old & set(capabilities["queries"])
            assert set(tools["trade_decide"]["inputSchema"]["required"]) == {"proposalId", "decision"}
            assert "Supply only" not in tools["trade_decide"]["description"]
            for name in old:
                with pytest.raises(ValueError, match="TOOL_NOT_PERMITTED"):
                    await gateway.call(job, name, {}, name)

    asyncio.run(run())
    raw = {"error": {"code": "TURN_REVIEW_REQUIRED", "message": "Supply reviewId"}}
    value = public_result(raw)
    assert value["recoverable"] and value["applied"] is False
    assert value["recovery"]["reads"] == [{"tool": "player_turn", "arguments": {}}]
    assert "reviewId" not in json.dumps(value)
    assert public_result(value) == value and raw["error"]["message"] == "Supply reviewId"
