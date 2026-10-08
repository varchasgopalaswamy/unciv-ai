import asyncio
import copy
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from jsonschema import Draft202012Validator

from civ_agent.civilization_state import CivilizationState, StateConflict
from civ_agent.game_commands import command_arguments
from civ_agent.player_prompts import job_instructions
from civ_agent.proposal_gateway import ProposalGateway
from civ_agent.protocol import JsonObject
from civ_agent.review_store import ReviewStore
from civ_agent.service_io import ServiceIO
from civ_agent.startup_context import (
    core_definitions,
    memory_topics,
    prepare_startup,
    startup_instructions,
    startup_prompt,
    startup_text,
)
from civ_agent.state_updates import apply_overview_update
from civ_agent.strategy_guide import strategy_reference
from civ_agent.telemetry import JobAccounting
from tests.test_agent_service import StartupHost
from tests.test_strategic_review import assessment
from tests.test_structured_memory import create, strategy


def test_victory_comparison_is_preloaded_once_and_chart_queries_pass_through(tmp_path):
    comparison = {
        "turn": 95,
        "sections": ["rankings"],
        "victories": [],
        "rivals": [],
        "metrics": [{"name": "Production", "meaning": "Empire production per turn"}],
        "rankings": {
            "comparedCivilizations": 2,
            "rows": [
                {"civilization": "Egypt", "values": [108], "ranks": [1]},
                {
                    "civilization": "Rome",
                    "values": [48],
                    "ranks": [2],
                    "trend": {"fromTurn": 84, "toTurn": 94, "changes": [13]},
                },
            ],
        },
        "charts": None,
    }
    query = {"sections": ["charts"], "metrics": ["Production"], "civilizations": ["Rome"], "fromTurn": 80, "toTurn": 94}
    chart = {"turn": 95, "charts": {"turns": [80], "nextFromTurn": 81, "requestedToTurn": 94}}

    class VictoryHost(StartupHost):
        def __init__(self, factory):
            super().__init__(factory)
            self.victory_calls = []

        async def list_tools(self):
            tools = await super().list_tools()
            schema_path = (
                Path(__file__).resolve().parents[1] / "src/main/resources/uncivai/schemas/victory-progress.json"
            )
            for tool in tools:
                if tool["name"] == "victory_progress":
                    tool["inputSchema"] = json.loads(schema_path.read_text())
            return tools

        async def tool(self, name, args):
            if name == "victory_progress":
                self.victory_calls.append(copy.deepcopy(args))
                return copy.deepcopy(chart if args == query else comparison)
            return await super().tool(name, args)

    async def run():
        io = ServiceIO()
        memory = CivilizationState(tmp_path / "memory", "Rome")
        gateway = ProposalGateway(memory, io, tmp_path / "workspace")
        host = VictoryHost(
            SimpleNamespace(service=SimpleNamespace(active_id="job", records={"job": {"epoch": "g1", "gameTurn": 95}}))
        )
        try:
            job = await gateway.open("job", "gameplay", "g1", host, {}, 95)
            prepared = await prepare_startup(gateway, job)
            startup_query = {"sections": ["rankings"]}
            comparison_query = {"tool": "victory_progress", "arguments": startup_query}
            assert prepared["reads"]["victory_progress"] == {**comparison, "query": comparison_query}
            prompt = startup_prompt(prepared, "gameplay", "Play this turn")
            reads = json.JSONDecoder().raw_decode(prompt.user.split("Prepared player-visible reads:\n", 1)[1])[0]
            assert reads["victory_progress"] == {**comparison, "query": comparison_query}
            assert "score" not in reads["victory_progress"]
            assert host.victory_calls == [startup_query]
            assert (await gateway.call(job, "victory_progress", query, "history")) == {
                **chart,
                "query": {"tool": "victory_progress", "arguments": query},
            }
            assert host.victory_calls == [startup_query, query]
        finally:
            await io.close()

    asyncio.run(run())


def test_startup_selects_old_knowledge_from_fresh_facts_without_admitting_late_contributions(tmp_path):
    async def run():
        memory, io = CivilizationState(tmp_path / "memory", "Rome"), ServiceIO()
        gateway = ProposalGateway(memory, io, tmp_path / "workspace")
        memory.bind("g1")
        memory.start_job("previous", "gameplay", {}, game_turn=0)
        war = create(
            memory,
            {
                "type": "note",
                "text": "Egypt broke an earlier withdrawal promise.",
                "subjects": ["egypt"],
                "priority": 5,
            },
            "previous",
        )
        city = create(
            memory, {"type": "note", "text": "Rome's food tiles need improvement.", "subjects": ["city1"]}, "previous"
        )
        unrelated = create(
            memory,
            {"type": "note", "text": "An old Greek trade price.", "subjects": ["Greece"], "priority": 5},
            "previous",
        )
        memory.finish_job("previous", True)
        host = StartupHost(
            SimpleNamespace(service=SimpleNamespace(active_id="job", records={"job": {"epoch": "g1", "gameTurn": 10}}))
        )
        host.view["diplomacy"] = {"knownCivilizations": [{"civilizationId": "Egypt", "atWar": True}]}
        host.view["cities"][0]["development"] = {"foodPerTurn": 0}
        try:
            job = await gateway.open("job", "gameplay", "g1", host, {}, 10)
            assert not job.memory["contextIds"] and not job.memory["events"]
            memory.start_job("late-chat", "communication", {}, game_turn=10)
            late = create(
                memory, {"type": "note", "text": "Contribution after dispatch", "subjects": ["Egypt"]}, "late-chat"
            )
            prepared = await prepare_startup(gateway, job)
            context = json.loads(prepared["memoryContext"].split("\n", 1)[1])
            assert {record["id"] for record in context["records"]} == {war, city}
            assert context["omittedRecords"] == 1
            assert unrelated not in job.memory["contextIds"] and late not in job.memory["records"]
            assert memory.delivered_records(job.id, [war, city]).keys() == {war, city}
            assert not memory.delivered_records(job.id, [unrelated, late])
            assert "Egypt" in job.memory["contextTopics"] and "growth" in job.memory["contextTopics"]
            await gateway.select_startup_memory(job, {"Greece"})
            assert set(job.memory["contextIds"]) == {war, city}  # Pin once the opening context is prepared.
        finally:
            await io.close()

    asyncio.run(run())


def test_startup_reselection_keeps_budget_and_does_not_deliver_displaced_records(tmp_path):
    async def run():
        memory, io = CivilizationState(tmp_path / "memory", "Rome", 1000), ServiceIO()
        gateway = ProposalGateway(memory, io, tmp_path / "workspace")
        memory.bind("g1")
        memory.start_job("old", "gameplay", {}, game_turn=0)
        war = create(memory, {"type": "note", "text": "Old war knowledge. " * 27, "subjects": ["Egypt"]}, "old")
        memory.finish_job("old", True)
        memory.start_job("recent", "gameplay", {}, game_turn=9)
        recent = create(
            memory, {"type": "note", "text": "Recent unrelated note. " * 25, "subjects": ["Greece"]}, "recent"
        )
        memory.finish_job("recent", True)
        host = StartupHost(
            SimpleNamespace(service=SimpleNamespace(active_id="job", records={"job": {"epoch": "g1", "gameTurn": 10}}))
        )
        host.view["diplomacy"] = {"knownCivilizations": [{"civilizationId": "Egypt", "atWar": True}]}
        try:
            job = await gateway.open("job", "gameplay", "g1", host, {}, 10)
            assert job.memory["contextIds"] == [recent]
            prepared = await prepare_startup(gateway, job)
            context = json.loads(prepared["memoryContext"].split("\n", 1)[1])
            assert [record["id"] for record in context["records"]] == [war]
            assert (
                len(json.dumps(context["records"], ensure_ascii=False, separators=(",", ":"))) <= memory.context_limit
            )
            assert not memory.delivered_records(job.id, [recent])
            result = memory.patch(job.id, [{"op": "amend", "id": recent, "fields": {"text": "Unread edit"}}])
            assert not result["changes"][0]["accepted"] and result["changes"][0]["reason"] == "MEMORY_CONFLICT"
        finally:
            await io.close()

    asyncio.run(run())


def test_startup_memory_budget_uses_frozen_versions_despite_concurrent_edits(tmp_path):
    async def run():
        memory, io = CivilizationState(tmp_path / "memory", "Rome"), ServiceIO()
        gateway = ProposalGateway(memory, io, tmp_path / "workspace")
        memory.bind("g1")
        memory.start_job("writer", "gameplay", {}, game_turn=4)
        keys = [
            create(memory, {"type": "note", "text": "Short report", "subjects": ["Egypt"]}, "writer") for _ in range(8)
        ]
        host = StartupHost(
            SimpleNamespace(service=SimpleNamespace(active_id="chat", records={"chat": {"epoch": "g1", "gameTurn": 4}}))
        )
        original = gateway.select_startup_memory

        async def select_then_edit(job, topics):
            await original(job, topics)
            changed = memory.patch(
                "writer",
                [{"op": "amend", "id": key, "fields": {"text": "Updated border report. " * 80}} for key in keys],
            )
            assert all(change["accepted"] for change in changed["changes"])

        gateway.select_startup_memory = select_then_edit
        try:
            job = await gateway.open("chat", "communication", "g1", host, {}, 4)
            prepared = await prepare_startup(gateway, job)
            context = json.loads(prepared["memoryContext"].split("\n", 1)[1])
            assert (
                len(json.dumps(context["records"], ensure_ascii=False, separators=(",", ":"))) <= memory.context_limit
            )
            assert {r["id"] for r in context["records"]} == set(keys)
            assert all(r["text"] == "Short report" for r in context["records"])
            latest = await gateway.call(job, "memory_read", {"ids": keys}, "explicit-read")
            assert all(r["text"].startswith("Updated border report.") for r in latest["records"])
        finally:
            await io.close()

    asyncio.run(run())


def test_memory_topics_use_only_structured_delivered_facts_and_current_social_records():
    reads = {
        "player_turn": {
            "researchQueue": ["Mining"],
            "cities": [{"handle": "city1", "name": "Rome", "foodPerTurn": -1, "constructionQueue": ["Granary"]}],
            "economy": {"happiness": -1, "resources": [{"name": "Iron", "available": 0, "used": 2}]},
            "changesSincePreviousTurn": {"resourcesNoLongerAvailable": ["Silk"]},
            "diplomacy": [{"civilizationId": "Greece", "atWar": False}],
        },
        "chat_read": {
            "agreements": [{"agreement": "a1", "partner": "France"}],
            "offers": [{"proposal": "p1", "participants": ["Egypt", "Rome"], "terms": "unstructured-text-sentinel"}],
            "messages": [{"kind": "message", "body": {"author": "China", "text": "Greece is plotting."}}],
        },
    }
    topics = memory_topics(reads)
    assert {
        "Mining",
        "city1",
        "Rome",
        "Granary",
        "growth",
        "economy",
        "Iron",
        "Silk",
        "France",
        "Egypt",
        "China",
    } <= topics
    assert not {"Greece", "unstructured-text-sentinel", "science"} & topics
    assert memory_topics(
        {"chat_read": {"context": {"knownCivilizations": [{"civilizationId": "Egypt", "atWar": True}]}}}
    ) == {"Egypt", "war", "defense"}


@pytest.mark.parametrize("purpose", ["gameplay", "communication"])
def test_diplomatic_advice_selects_private_notes_without_automatically_sending_them(tmp_path, purpose):
    async def run():
        memory, io = CivilizationState(tmp_path / "memory", "Rome"), ServiceIO()
        gateway = ProposalGateway(memory, io, tmp_path / "workspace")
        memory.bind("g1")
        memory.start_job("previous", "gameplay", {}, game_turn=0)
        private = "PRIVATE: We cannot afford a prolonged war."
        relevant = create(memory, {"type": "note", "text": private, "subjects": ["France"]}, "previous")
        unrelated = create(memory, {"type": "note", "text": "Egypt old price", "subjects": ["Egypt"]}, "previous")
        memory.finish_job("previous", True)
        store = ReviewStore(memory)
        store.prepare("review", {"turn": 5, "observation": {"turn": 5}}, {}, 60)
        store.complete("review", assessment())

        class RecordingHost(StartupHost):
            def __init__(self):
                super().__init__(
                    SimpleNamespace(
                        service=SimpleNamespace(active_id="job", records={"job": {"epoch": "g1", "gameTurn": 10}})
                    )
                )
                self.sent = []

            async def tool(self, name, args):
                if name == "player_communication" and args["op"] == "send":
                    self.sent.append(copy.deepcopy(args))
                return await super().tool(name, args)

        host = RecordingHost()
        try:
            job = await gateway.open("job", purpose, "g1", host, {}, 10)
            assert not job.memory["events"]
            prepared = await prepare_startup(gateway, job)
            assert set(job.memory["contextIds"]) == {relevant}
            assert unrelated not in job.memory["delivered"]
            prompt = startup_prompt(prepared, purpose, "Consider diplomacy")
            assert private in prompt.user
            assert prompt.user.count(assessment()["review"]["diplomacy"]["initiatives"][0]["approach"]) == 1
            for plan in assessment()["review"]["plans"]:
                for field in ("objective", "investment", "relativeOutcome", "risks"):
                    assert prompt.user.count(plan[field]) == 1
                for field in ("expectedObservations", "firstActions"):
                    for entry in plan[field]:
                        assert prompt.user.count(entry) == 1
            assert prompt.user.count(assessment()["review"]["reconsiderWhen"]) == 1
            assert "PRIVATE" not in prompt.instructions and host.sent == []
            result = await gateway.call(
                job,
                "chat_send",
                {"to": "France", "text": "Would you share scouting of Greece?", "intent": {"reason": private}},
                "contact",
            )
            assert "error" not in result
            assert len(host.sent) == 1 and host.sent[0]["text"] == "Would you share scouting of Greece?"
            assert private not in json.dumps(host.sent) and "troop shortage" not in json.dumps(host.sent)
        finally:
            await io.close()
            memory.engine.dispose()

    asyncio.run(run())


def test_prepared_reads_establish_evidence_and_update_base_without_model_calls(tmp_path):
    async def run():
        io = ServiceIO()
        memory = CivilizationState(tmp_path / "memory", "Rome")
        gateway = ProposalGateway(memory, io, tmp_path / "workspace")
        gateway.accounting = JobAccounting(tmp_path / "accounting")
        host = StartupHost(
            SimpleNamespace(service=SimpleNamespace(active_id="job", records={"job": {"epoch": "g1", "gameTurn": 0}}))
        )
        host.view["alerts"] = [{"handle": "alert1", "canAcknowledge": True, "content": {"text": "A message"}}]
        host.view["map"]["units"][0]["canFoundCity"] = True
        host.view["map"]["units"][0]["economy"] = {
            "upgrades": [
                {
                    "targetName": "Legion",
                    "goldCost": 50,
                    "additionalResourceRequirements": {"Iron": 1},
                    "available": True,
                    "reasons": [],
                }
            ]
        }
        host.view["cities"][0]["constructionChoice"] = {
            "name": "Science",
            "chosenTurn": 0,
            "observedSinceTurn": 0,
            "turnsObserved": 4,
            "availableSinceChoice": ["Workshop", "Market"],
        }
        host.view["cities"][0]["constructionQueue"] = ["Science"]
        host.view["cities"][0]["productionConversions"] = {
            "Science": {
                "stat": "Science",
                "productionPerTurn": "20",
                "baseYieldPerProduction": 0.25,
                "yieldPerTurn": "9.2",
            }
        }
        host.view["wonderOpportunities"] = [
            {
                "name": "The Oracle",
                "kind": "WORLD",
                "benefits": "+3 Culture; Free Social Policy",
                "greatPersonPoints": {"Great Scientist": 1},
                "specialistSlots": {},
                "cities": [{"city": "city1", "canQueue": True, "turns": 8}],
            }
        ]
        host.view["economy"] = {"development": {"unimprovedResources": [], "unconnectedCities": ["Antium"]}}
        host.view["diplomacyOpportunities"] = {"luxuryTrades": [], "expiringTerms": []}
        host.view["militaryReadiness"] = {
            "roleCounts": {"RECON": 1},
            "unitsExcludingRecon": 0,
            "cities": [{"city": "city1", "garrison": None, "nearbyDefenders": []}],
            "visibleBorderForces": [],
            "recruitment": [{"city": "city1", "units": ["Warrior"]}],
        }
        host.view["settlement"] = {
            "status": "SITES_AVAILABLE",
            "eligibleSites": 4,
            "candidates": [{"position": {"x": i, "y": 4}, "score": 100 - i} for i in range(4)],
            "settlers": [
                {
                    "unit": "unit1",
                    "firstObservedTurn": 0,
                    "turnsObserved": 5,
                    "turnsWithoutMovement": 3,
                    "recentDestinationChanges": [],
                    "currentSite": {"canFoundNow": True, "siteStatus": "ELIGIBLE", "uncertainty": None},
                    "destination": {
                        "position": {"x": 3, "y": 4},
                        "score": 97,
                        "route": {
                            "status": "ROUTE_OFFERED",
                            "estimatedTurns": 2,
                            "unexploredTiles": 0,
                            "tilesOutsideCurrentSight": 4,
                        },
                    },
                }
            ],
        }
        try:
            job = await gateway.open("job", "gameplay", "g1", host, {}, 0)
            await io.call(gateway.accounting.start, "job", "g1", True)
            prepared = await prepare_startup(gateway, job)
            assert set(prepared["reads"]) == {"player_turn", "player_capabilities", "victory_progress", "chat_read"}
            view = prepared["reads"]["player_turn"]
            assert view == job.delivered_overview
            assert job.social_read and job.verified
            assert {q.tool for q in job.details_read} == {"victory_progress", "chat_read", "unit_details"}
            assert len(prepared["decisionDetails"]) == 2
            unit_detail = next(
                detail for detail in prepared["decisionDetails"] if detail["query"]["tool"] == "unit_details"
            )
            assert unit_detail["query"]["arguments"] == {
                "target": "unit1",
                "sections": ["economy", "settlement"],
            }
            construction_detail = next(
                detail for detail in prepared["decisionDetails"] if detail["query"]["tool"] == "construction_details"
            )
            assert construction_detail["query"]["arguments"] == {
                "target": "city1",
                "names": ["Workshop", "Market"],
            }
            assert "observation:overview" in job.memory["records"]
            assert "queries" in prepared["reads"]["player_capabilities"]
            assert "version" not in prepared["reads"]["player_capabilities"]
            assert "revision" not in view and "epoch" not in view
            assert "stateId" in view
            assert view["units"][0]["upgrades"][0]["goldCost"] == 50
            assert view["units"][0]["inspections"]["UPGRADE_UNIT"]["arguments"]["sections"] == ["economy"]
            assert view["cities"][0]["constructionChoice"]["availableSinceChoice"] == ["Workshop", "Market"]
            assert view["economy"]["development"]["unconnectedCities"] == ["Antium"]
            assert view["diplomacyOpportunities"] == host.view["diplomacyOpportunities"]
            assert view["settlement"]["eligibleSites"] == 4 and len(view["settlement"]["candidates"]) == 3
            assert view["settlement"]["settlers"] == host.view["settlement"]["settlers"]
            assert view["units"][0]["settlementStatus"] == "CAN_FOUND_NOW"
            assert view["militaryReadiness"] == host.view["militaryReadiness"]
            assert view["wonderOpportunities"][0]["benefits"] == host.view["wonderOpportunities"][0]["benefits"]
            assert view["wonderOpportunities"][0]["cities"] == [{"city": "city1", "turns": 8}]
            assert view["cities"][0]["constructionConversion"]["yieldPerTurn"] == "9.2"
            assert {tool["name"] for tool in prepared["coreTools"]} == {
                "mcp__civ__player_turn",
                "mcp__civ__player_command",
                "mcp__civ__memory_read",
                "mcp__civ__unit_details",
                "mcp__civ__map_region",
                "mcp__civ__chat_send",
            }
            for tool in prepared["coreTools"]:
                Draft202012Validator.check_schema(tool["inputSchema"])
            assert "social promises" in prepared["readInstructions"]["chat_read"]
            assert (
                json.loads(startup_text(prepared).split("Prepared player-visible reads:\n", 1)[1].split("\n\n", 1)[0])
                == prepared["reads"]
            )
            action = view["alerts"][0]["command"]
            assert action == {"kind": "ACKNOWLEDGE_ALERT", "target": "alert1"}
            result = await gateway.call(job, "player_command", action, "ack")
            assert apply_overview_update(view, result["stateUpdate"]) == job.delivered_overview
            assert "militaryReadiness" not in result["stateUpdate"]["set"]
            assert "settlement" not in result["stateUpdate"]["set"]
            assert "wonderOpportunities" not in result["stateUpdate"]["set"]
            assert len(host.executed) == 1
            assert view["endTurn"]["command"] == {"kind": "END_TURN"}
            assert "deferReason" not in view["endTurn"]["command"]
            assert "deferReason is optional" in view["endTurn"]["instruction"]
            settlement = view["units"][0]["inspections"]["FOUND_CITY"]
            args = {"kind": "FOUND_CITY", "target": "unit1", "intent": {"reason": "Settle here"}}
            assert settlement["arguments"]["sections"] == ["settlement"]
            assert "error" not in await gateway.call(job, "player_command", args, "found")
            assert len(host.executed) == 2
            await io.call(gateway.accounting.sample, {"activationId": "job", "phase": "RUNNING"})
            calls = gateway.accounting.pending()[0]["telemetry"]["apiCalls"]
            assert calls["startup:player_turn"]["calls"] == 1
            assert "tool:player_turn" not in calls
            assert calls["startup:unit_details"]["calls"] == 1
            assert calls["startup:construction_details"]["calls"] == 1
            assert calls["tool:player_command"]["calls"] == 2
            assert memory.call("job", "startup:player_turn", "player_turn", {}) == view
        finally:
            await io.close()

    asyncio.run(run())


def test_communication_startup_cannot_prepare_gameplay_or_expose_its_commands(tmp_path):
    async def run():
        io = ServiceIO()
        memory = CivilizationState(tmp_path / "memory", "Rome")
        gateway = ProposalGateway(memory, io, tmp_path / "workspace")
        host = StartupHost(
            SimpleNamespace(service=SimpleNamespace(active_id="job", records={"job": {"epoch": "g1", "gameTurn": 0}}))
        )
        try:
            job = await gateway.open("job", "communication", "g1", host, {})
            prepared = await prepare_startup(gateway, job)
            assert set(prepared["reads"]) == {"chat_read"}
            assert job.observation is None
            assert {tool["name"] for tool in prepared["coreTools"]} == {
                "mcp__civ__chat_read",
                "mcp__civ__chat_send",
                "mcp__civ__memory_update",
            }
            assert "player_command" not in json.dumps(prepared)
        finally:
            await io.close()

    asyncio.run(run())


@pytest.mark.parametrize("purpose", ["gameplay", "communication"])
def test_private_startup_context_follows_observation_reviews_and_expiry(tmp_path, purpose):
    async def run():
        memory, io = CivilizationState(tmp_path / "memory", "Rome"), ServiceIO()
        gateway = ProposalGateway(memory, io, tmp_path / "workspace")
        memory.bind("g1")
        memory.start_job("previous", "gameplay", {})
        memory.observe_player({"turn": 0}, "previous")
        offer = {"proposal": "p1", "version": 1, "participants": ["Rome", "Greece"], "effective": False}
        memory.observe("communication", {"context": {"turn": 0}, "offers": [offer]}, "previous")
        key = create(
            memory,
            strategy(assumptions=[{"text": "Diplomatic situation is stable", "source": "observation:communication"}]),
            job="previous",
        )
        expiring = create(memory, {"type": "note", "text": "Temporary report", "expiresTurn": 1}, job="previous")
        memory.finish_job("previous", True)
        host = StartupHost(
            SimpleNamespace(service=SimpleNamespace(active_id="job", records={"job": {"epoch": "g1", "gameTurn": 1}}))
        )
        original = host.tool

        async def at_turn_one(name, args):
            result: JsonObject = await original(name, args)
            if name == "player_communication" and args["op"] == "state":
                result["context"] = {"turn": 1}
                result["offers"] = [{**offer, "effective": True}]
            return result

        host.tool = at_turn_one
        try:
            job = await gateway.open("job", purpose, "g1", host, {}, 1)
            assert not job.memory["records"][key]["body"]["needsReview"]
            prepared = await prepare_startup(gateway, job)
            context = json.loads(prepared["memoryContext"].split("\n", 1)[1])
            records = {record["id"]: record for record in context["records"]}
            assert records[key]["needsReview"]
            assert expiring not in records
            assert memory.snapshot(purpose)["records"][expiring]["body"]["state"] == (
                "retired" if purpose == "gameplay" else "active"
            )
            assert "memoryUpdates" not in prepared["reads"].get("player_turn", {})
            prompt = startup_prompt(prepared, purpose, "Play this turn")
            assert prompt.user.count(prepared["memoryContext"]) == 1
            assert prompt.user.index("Prepared player-visible reads:") < prompt.user.index(prepared["memoryContext"])
            assert "Temporary report" not in prompt.instructions
            assert startup_text(prepared).count(prepared["memoryContext"]) == 1
            saved = memory.call(
                job.id,
                "startup:memory:0",
                "memory_read",
                {"ids": job.memory["contextIds"], "includeRetired": True},
            )
            assert saved is not None and saved["records"] == context["records"]
            result = await gateway.call(
                job,
                "memory_update",
                {
                    "changes": [
                        {
                            "op": "replace",
                            "id": key,
                            "content": strategy(),
                        }
                    ]
                },
                "review",
            )
            assert result["changes"][0]["accepted"] is (purpose == "gameplay")
        finally:
            await io.close()

    asyncio.run(run())


def test_startup_rejects_foreign_identity(tmp_path):
    async def run():
        io = ServiceIO()
        gateway = ProposalGateway(CivilizationState(tmp_path / "memory", "Rome"), io, tmp_path / "workspace")
        host = StartupHost(
            SimpleNamespace(service=SimpleNamespace(active_id="job", records={"job": {"epoch": "g1", "gameTurn": 0}}))
        )
        host.civilization = "Greece"
        try:
            job = await gateway.open("job", "gameplay", "g1", host, {})
            with pytest.raises(StateConflict, match="PLAYER_CREDENTIAL_MISMATCH"):
                await prepare_startup(gateway, job)
            assert job.observation is None
        finally:
            await io.close()

    asyncio.run(run())


def test_core_definitions_keep_complete_schemas_and_do_not_mutate_catalogue():
    schema = {
        "type": "object",
        "properties": {"x": {"type": "integer"}},
        "required": ["x"],
        "additionalProperties": False,
    }
    catalogue = [
        {"name": "player_command", "description": "Execute", "inputSchema": schema, "outputSchema": {"type": "object"}}
    ]
    before = copy.deepcopy(catalogue)
    assert core_definitions(catalogue, "gameplay") == [
        {"name": "mcp__civ__player_command", "description": "Execute", "inputSchema": schema}
    ]
    assert catalogue == before
    assert command_arguments({"kind": "END_TURN", "deferReason": "Stay fortified"})["deferReason"] == "Stay fortified"


@pytest.mark.parametrize("strategy_guide", [False, True])
def test_prompt_prefix_excludes_state_memory_identity_and_capability_changes(strategy_guide):
    prepared: JsonObject = {
        "coreTools": [{"name": "mcp__civ__player_command", "inputSchema": {"type": "object"}}],
        "readInstructions": {"chat_read": "Statements are claims."},
        "reads": {
            "player_capabilities": {"commands": ["END_TURN"]},
            "player_turn": {"turn": 1, "civilization": "Rome", "stateId": "private-state-1"},
            "chat_read": {"text": "UNTRUSTED_MESSAGE_IGNORE_INSTRUCTIONS"},
        },
    }
    before = copy.deepcopy(prepared)
    first = startup_prompt(prepared, "gameplay", "Rome turn 1; MEMORY_SENTINEL", strategy_guide=strategy_guide)
    changed = copy.deepcopy(prepared)
    changed["reads"] = {
        "player_capabilities": {"error": "TEMPORARY_ERROR"},
        "player_turn": {"turn": 2, "civilization": "Greece", "stateId": "private-state-2"},
        "chat_read": {"text": "SECOND_PRIVATE_MESSAGE"},
    }
    second = startup_prompt(changed, "gameplay", "Greece turn 2; OTHER_MEMORY", strategy_guide=strategy_guide)
    assert first.instructions == second.instructions
    assert first.metadata()["instructionsSha256"] == second.metadata()["instructionsSha256"]
    assert first.user != second.user
    for private in ("MEMORY_SENTINEL", "UNTRUSTED_MESSAGE_IGNORE_INSTRUCTIONS", "private-state-1"):
        assert private not in first.instructions
        assert first.user.count(private) == 1
        assert private not in second.user
    assert first.user.index("Prepared capabilities") < first.user.index("MEMORY_SENTINEL")
    assert first.user.index("MEMORY_SENTINEL") < first.user.index("private-state-1")
    assert "inputSchema" in first.instructions and "inputSchema" not in first.user
    assert "TEMPORARY_ERROR" in second.user
    changed["coreTools"][0]["inputSchema"]["required"] = ["kind"]
    third = startup_prompt(changed, "gameplay", "Greece turn 2", strategy_guide=strategy_guide)
    assert third.metadata()["instructionsSha256"] != first.metadata()["instructionsSha256"]
    assert prepared == before


@pytest.mark.parametrize("purpose", ["gameplay", "communication"])
def test_strategy_reference_is_optional_once_and_before_dynamic_context(tmp_path, monkeypatch, purpose):
    # The reference must be available to installed services outside the checkout.
    monkeypatch.chdir(tmp_path)
    reference = strategy_reference()
    prepared = {
        "coreTools": [],
        "readInstructions": {},
        "memoryContext": "PRIVATE_MEMORY_SENTINEL",
        "reads": {"chat_read": {"text": "PRIVATE_CHAT_SENTINEL"}},
    }
    baseline = startup_prompt(prepared, purpose, "Play this turn")
    enabled = startup_prompt(prepared, purpose, "Play this turn", strategy_guide=True)
    assert baseline.instructions == job_instructions(purpose) + "\n\n" + startup_instructions(prepared)
    assert baseline.metadata()["strategyGuide"] == {"enabled": False, "characters": 0, "sha256": None}
    assert enabled.instructions == baseline.instructions
    assert enabled.user.count(reference) == 1
    assert enabled.user.endswith(baseline.user)
    assert enabled.user.index(reference) < enabled.user.index("PRIVATE_MEMORY_SENTINEL")
    assert enabled.metadata()["strategyGuide"]["enabled"]
    assert enabled.metadata()["strategyGuide"]["characters"] == len(reference)
    assert enabled.metadata()["strategyGuide"]["sha256"]
    assert enabled.metadata()["instructionCharacters"] == baseline.metadata()["instructionCharacters"]
    assert enabled.metadata()["instructionsSha256"] == baseline.metadata()["instructionsSha256"]
    interactive = startup_text(prepared, strategy_guide=True)
    assert interactive == reference + "\n\n" + startup_text(prepared)
    assert interactive.count(reference) == 1
    assert interactive.index(reference) < interactive.index("PRIVATE_MEMORY_SENTINEL")
    assert (
        json.loads(interactive.split("Prepared player-visible reads:\n", 1)[1].split("\n\n", 1)[0]) == prepared["reads"]
    )


def test_prefix_is_deterministic_and_communication_has_its_own_workflow():
    first = {"coreTools": [], "readInstructions": {"b": "B", "a": "A"}, "reads": {"chat_read": {}}}
    second = {"reads": {"chat_read": {}}, "readInstructions": {"a": "A", "b": "B"}, "coreTools": []}
    one = startup_prompt(first, "communication", "Rome")
    two = startup_prompt(second, "communication", "Greece")
    assert one.instructions == two.instructions
    assert "Avoid duplicate proposals" in one.instructions
    assert "After END_TURN succeeds" not in one.instructions
    assert one.metadata()["instructionCharacters"] == len(one.instructions)
