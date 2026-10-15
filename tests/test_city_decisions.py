"""Independent city questions share a budgeted batch without changing action authority."""

import asyncio
import copy

import pytest
from sqlalchemy import select

from civ_agent import memory_schema as tables
from civ_agent.city_decisions import capture, recommendations, semantic_key
from civ_agent.decisions_tasks import CITY_LIMIT, TASK_VERSION, parse_answers, request_body, request_id
from civ_agent.memory_snapshots import load_snapshot
from civ_agent.memory_store import RecordStore
from civ_agent.startup_context import prepare_startup, startup_prompt
from tests.memory_helpers import stored_records
from tests.test_decisions_advisories import setup


def add_cities(host, count=2):
    original = host.tool
    host.focus_reads = []
    host.view["isYourTurn"] = True
    host.view["economy"] = {"happiness": 4, "goldPerTurn": 2, "treasury": 100}
    prototype = copy.deepcopy(host.view["cities"][0])
    prototype.update(
        constructionOptions=["Market", "Granary", "Warrior"],
        development={
            "population": 3,
            "focus": "NoFocus",
            "avoidGrowth": False,
            "unassignedPopulation": 0,
            "foodStored": 5,
            "foodToNextPopulation": 20,
            "foodPerTurn": 2,
            "turnsToGrowth": 8,
            "yields": [{"stat": "Production", "amount": "4"}],
            "tiles": [],
            "constructions": [],
        },
        management={
            "manualSpecialists": False,
            "focusOptions": [
                {"name": name, "label": name, "action": {"available": True, "unavailableReasons": []}}
                for name in ("NoFocus", "ProductionFocus")
            ],
        },
        constructionEstimates={
            name: {
                "production": {"cost": 40, "stored": 0, "overflow": 0, "perTurn": 4, "turnsIfStartedNow": 10},
                "queueUnavailableReasons": [],
                "whileBuilding": {"change": {}},
                "completion": None,
                "requiredTechnologies": [],
                "resourceRequirements": {},
            }
            for name in ("Market", "Granary", "Warrior")
        },
    )
    host.view["cities"] = [
        {**copy.deepcopy(prototype), "handle": f"city{i + 1}", "name": f"City {i + 1}"} for i in range(count)
    ]

    async def tool(name, args):
        if name == "player_inspect" and args.get("subject") == "CIVILOPEDIA":
            return {
                "epoch": "g1",
                "revision": host.revision,
                "turn": host.record["gameTurn"],
                "data": {"items": [{"name": n, "lines": [{"text": "Native public rules"}]} for n in args["names"]]},
            }
        if name == "player_inspect" and args.get("subject") == "CITY_FOCUS":
            host.focus_reads.append(copy.deepcopy(args))
            return {
                "epoch": "g1",
                "revision": host.revision,
                "turn": host.record["gameTurn"],
                "subject": "CITY_FOCUS",
                "data": {
                    "city": args["target"],
                    "available": True,
                    "limitations": ["Native current-population preview"],
                    "options": [
                        {
                            "name": focus,
                            "label": focus,
                            "command": {"kind": "SET_CITY_FOCUS", "target": args["target"], "selection": focus},
                            "yields": {"Production": "4" if focus == "NoFocus" else "6"},
                            "foodPerTurn": 2,
                            "turnsToGrowth": 8,
                            "constructionTurns": 10,
                        }
                        for focus in ("NoFocus", "ProductionFocus")
                    ],
                },
            }
        return await original(name, args)

    host.tool = tool


def choose_candidates(classifier, host=None, *, focus_mode="success"):
    original = classifier.batch

    async def batch(evidence, *, timeout):
        result = await original(evidence, timeout=timeout)
        if not isinstance(result.get("answers"), list):
            return result
        for answer in result["answers"]:
            if answer["name"].startswith("city_focus:") and answer.get("type") == "choice":
                answer["choice"] = "focus1"
                if focus_mode == "low":
                    answer["confidence"] = 0.2
                elif focus_mode == "unknown":
                    answer["choice"] = "manufactured-focus"
            if answer["name"].startswith("city_construction:") and answer.get("type") == "choice":
                answer["choice"] = "construction0"
        if host is not None:
            host.revision = "changed-during-classification"
        return result

    classifier.batch = batch


def test_two_city_questions_coalesce_with_memory_and_preserve_native_delivery(tmp_path):
    async def run():
        async with setup(tmp_path) as (memory, gateway, host, classifier):
            add_cities(host)
            choose_candidates(classifier)
            memory.ingest(
                "g1",
                [
                    {
                        "id": 1,
                        "kind": "message",
                        "turn": 0,
                        "body": {
                            "message": "msg1",
                            "author": "Greece",
                            "text": "Could you help me defend the coast?",
                            "participants": ["Rome", "Greece"],
                        },
                    }
                ],
                1,
            )
            job = await gateway.open("job", "gameplay", "g1", host, {}, 0)
            prepared = await prepare_startup(gateway, job)
            assert len(classifier.calls) == 1
            evidence = classifier.calls[0]
            questions = request_body(evidence, classifier.settings)["questions"]
            assert [q["name"] for q in questions[:4]] == [
                "city_focus:0",
                "city_construction:0",
                "city_focus:1",
                "city_construction:1",
            ]
            assert any(q["name"].startswith("preservation:") for q in questions)
            assert all("independent" in q["instructions"] for q in questions[:4])
            rows = prepared["advisories"]["cityRecommendations"]
            assert len(rows) == 2
            assert rows[0]["focus"]["native"]["command"] == {
                "kind": "SET_CITY_FOCUS",
                "target": "city1",
                "selection": "ProductionFocus",
            }
            assert rows[0]["construction"]["native"]["command"]["target"] == "city1"
            assert (
                prepared["reads"]["native_advice"]["recommendations"][0]["explanation"] == "Original native explanation"
            )
            assert "Optional city recommendations" in startup_prompt(prepared, "gameplay", "Play").user
            assert not any(q.tool == "player_inspect" for q in job.details_read)
            with memory.transaction() as db:
                snapshot = load_snapshot(
                    db, db.scalar(select(tables.jobs.c.snapshot).where(tables.jobs.c.id == job.id))
                )
                assert snapshot["advisories"]["cityRecommendations"] == rows
                assert not list(db.execute(select(tables.calls).where(tables.calls.c.name == "player_inspect")))
            assert not any("CITY_FOCUS" in k for k in stored_records(memory))
            await prepare_startup(gateway, job)
            assert len(classifier.calls) == 1
            # A legal action outside either selected candidate needs no override.
            await gateway.call(
                job, "construction_details", {"target": "city1", "names": ["Warrior"]}, "inspect-warrior"
            )
            result = await gateway.call(
                job,
                "player_command",
                {"kind": "QUEUE_CONSTRUCTION", "target": "city1", "selection": "Warrior"},
                "other-action",
            )
            assert result["kind"] == "QUEUE_CONSTRUCTION" and host.executed[-1]["selection"] == "Warrior"
            replay = await prepare_startup(gateway, job)
            assert replay["advisories"]["cityRecommendations"] == []
            assert len(classifier.calls) == 1

    asyncio.run(run())


def test_focus_failure_does_not_discard_construction_answer(tmp_path):
    async def run():
        async with setup(tmp_path, native_enabled=False) as (_, gateway, host, classifier):
            add_cities(host, 1)
            choose_candidates(classifier, focus_mode="low")
            job = await gateway.open("job", "gameplay", "g1", host, {}, 0)
            prepared = await prepare_startup(gateway, job)
            row = prepared["advisories"]["cityRecommendations"][0]
            assert "focus" not in row and "construction" in row
            assert host.captures == []  # Native advisor remains independently disabled.

    asyncio.run(run())


@pytest.mark.parametrize("mode", ["unknown", "timeout", "malformed", "refusal", "low", "zero"])
def test_failure_keeps_ordinary_gameplay_without_fabricated_advice(tmp_path, mode):
    async def run():
        async with setup(tmp_path, additional_decisions_budget_usd=0 if mode == "zero" else 0.1) as (
            _,
            gateway,
            host,
            classifier,
        ):
            add_cities(host, 1)
            classifier.mode = mode
            job = await gateway.open("job", "gameplay", "g1", host, {}, 0)
            prepared = await prepare_startup(gateway, job)
            assert prepared["advisories"]["cityRecommendations"] == []
            assert len(classifier.calls) == int(mode != "zero")
            assert bool(prepared["reads"]["native_advice"]["recommendations"])
            if mode == "zero":
                assert host.__dict__["focus_reads"] == []

    asyncio.run(run())


def test_stale_provider_reply_is_not_delivered(tmp_path):
    async def run():
        async with setup(tmp_path) as (_, gateway, host, classifier):
            add_cities(host, 1)
            choose_candidates(classifier, host)
            job = await gateway.open("job", "gameplay", "g1", host, {}, 0)
            prepared = await prepare_startup(gateway, job)
            assert prepared["advisories"]["cityRecommendations"] == []
            assert prepared["advisories"]["cityAdviceStatus"]["fallback"] == "stale_native_basis"
            assert host.executed == []

    asyncio.run(run())


@pytest.mark.parametrize("change_during_call", [False, True])
def test_changed_objective_discards_city_advice_even_with_current_native_state(tmp_path, change_during_call):
    async def run():
        async with setup(tmp_path) as (memory, gateway, host, classifier):
            add_cities(host, 1)
            choose_candidates(classifier)
            objective = {
                "assessment": "Develop science in City 1",
                "state": "active",
                "createdTurn": 0,
                "updatedTurn": 0,
            }

            def save_objective():
                with memory.transaction() as db:
                    RecordStore(db).write("city-objective", "strategy", objective)

            save_objective()
            original = classifier.batch

            async def batch(evidence, *, timeout):
                result = await original(evidence, timeout=timeout)
                if change_during_call:
                    objective["assessment"] = "Recruit defenders in City 1"
                    save_objective()
                return result

            classifier.batch = batch
            job = await gateway.open("job", "gameplay", "g1", host, {}, 0)
            prepared = await prepare_startup(gateway, job)
            assert classifier.calls[0]["objectives"][0]["key"] == "city-objective"
            if not change_during_call:
                assert prepared["advisories"]["cityRecommendations"]
                objective["assessment"] = "Recruit defenders in City 1"
                save_objective()
                prepared = await prepare_startup(gateway, job)
            assert prepared["advisories"]["cityRecommendations"] == []
            assert len(classifier.calls) == 1
            with memory.transaction() as db:
                snapshot = load_snapshot(
                    db, db.scalar(select(tables.jobs.c.snapshot).where(tables.jobs.c.id == job.id))
                )
                assert snapshot["advisories"]["cityRecommendations"] == []
                assert memory._meta(db, "advisories")["cityDelivery"]["deliveredQuestions"] == 0

    asyncio.run(run())


def test_city_request_replay_ignores_transport_binding_but_not_native_changes(tmp_path):
    async def run():
        async with setup(tmp_path) as (_, gateway, host, classifier):
            add_cities(host, 1)
            job = await gateway.open("job", "gameplay", "g1", host, {}, 0)
            await gateway.call(job, "player_turn", {}, "initial")
            cities, context, _ = await capture(gateway, job, [])
            evidence = {"version": TASK_VERSION, "cities": cities, "cityContext": context}
            restarted = copy.deepcopy(evidence)
            restarted["cities"][0]["basis"].update(epoch="restored-epoch", revision="restored-revision")
            assert request_id(restarted) == request_id(evidence)
            assert request_body(restarted, classifier.settings) == request_body(evidence, classifier.settings)
            restarted["cities"][0]["focuses"][0]["native"]["foodPerTurn"] = -2
            assert request_id(restarted) != request_id(evidence)

    asyncio.run(run())


@pytest.mark.parametrize("question_limit", [1, 2, 3])
def test_question_limit_omits_whole_city_pairs(tmp_path, question_limit):
    async def run():
        async with setup(tmp_path, decisions_max_questions=question_limit) as (_, gateway, host, classifier):
            add_cities(host)
            choose_candidates(classifier)
            job = await gateway.open("job", "gameplay", "g1", host, {}, 0)
            prepared = await prepare_startup(gateway, job)
            expected_cities = question_limit // 2
            assert len(prepared["advisories"]["cityRecommendations"]) == expected_cities
            assert prepared["advisories"]["cityAdviceStatus"]["omittedCities"] == 2 - expected_cities
            assert len(classifier.calls) == bool(expected_cities)
            if classifier.calls:
                assert len(request_body(classifier.calls[0], classifier.settings)["questions"]) == 2 * expected_cities

    asyncio.run(run())


def test_byte_limit_omits_city_pair_and_its_unneeded_construction_rules(tmp_path):
    async def run():
        async with setup(tmp_path, decisions_max_request_bytes=16000) as (_, gateway, host, classifier):
            add_cities(host)
            host.view["cities"][0]["constructionOptions"] = ["Market"]
            host.view["cities"][1]["constructionOptions"] = ["Granary"]
            original = host.tool

            async def tool(name, args):
                result = await original(name, args)
                if name == "player_inspect" and args.get("subject") == "CIVILOPEDIA":
                    for item in result["data"]["items"]:
                        item["lines"] = [{"text": "Native public rules. " * 400}]
                return result

            host.tool = tool
            choose_candidates(classifier)
            job = await gateway.open("job", "gameplay", "g1", host, {}, 0)
            prepared = await prepare_startup(gateway, job)
            assert len(classifier.calls) == 1
            assert len(prepared["advisories"]["cityRecommendations"]) == 1
            assert prepared["advisories"]["cityAdviceStatus"]["omittedCities"] == 1
            evidence = classifier.calls[0]
            assert [r["name"] for r in evidence["cityContext"]["constructionRules"]] == ["Market"]
            assert len(request_body(evidence, classifier.settings)["questions"]) == 2

    asyncio.run(run())


@pytest.mark.parametrize("mode", ["success", "timeout"])
def test_stable_city_inputs_do_not_repeat_paid_calls_across_turns(tmp_path, mode):
    async def run():
        async with setup(tmp_path, native_enabled=False) as (_, gateway, host, classifier):
            add_cities(host, 1)
            choose_candidates(classifier)
            classifier.mode = mode
            first = await gateway.open("first", "gameplay", "g1", host, {}, 0)
            await prepare_startup(gateway, first)
            await gateway.close(first, True)
            host.record["gameTurn"] = 5
            host.view["economy"]["treasury"] += 10
            host.revision = "new-transport-revision"
            second = await gateway.open("second", "gameplay", "g1", host, {}, 5)
            prepared = await prepare_startup(gateway, second)
            assert len(classifier.calls) == 1
            if mode == "success":
                assert prepared["advisories"]["cityRecommendations"][0]["basis"]["revision"] == host.revision
            else:
                assert prepared["advisories"]["cityRecommendations"] == []

    asyncio.run(run())


def test_capture_limits_omissions_unknown_ids_and_broker_controls(tmp_path):
    async def run():
        async with setup(tmp_path) as (_, gateway, host, classifier):
            add_cities(host, CITY_LIMIT + 1)
            job = await gateway.open("job", "gameplay", "g1", host, {}, 0)
            await gateway.call(job, "player_turn", {}, "initial")
            cities, context, status = await capture(gateway, job, [])
            assert len(cities) == CITY_LIMIT and status["omittedCities"] == 1
            assert (
                recommendations(
                    gateway, job, cities, {"answers": [{"name": "city_focus:0", "choice": "unknown", "confidence": 1}]}
                )
                == []
            )
            evidence = {"version": TASK_VERSION, "cities": cities, "cityContext": context}
            body = request_body(evidence, classifier.settings)
            assert len(body["questions"]) == 2 * CITY_LIMIT
            for key in ("instructions", "model", "budgetExempt"):
                with pytest.raises(ValueError):
                    request_body({**evidence, key: "injected"}, classifier.settings)
            unknown = {
                "answers": [
                    {"name": q["name"], "type": "choice", "choice": "invented", "confidence": 1}
                    for q in body["questions"]
                ]
            }
            assert parse_answers(body, unknown, 0.9)["fallback"] == "malformed"
            original = semantic_key(cities, context, [])
            cities[0]["focuses"][0]["native"]["constructionTurns"] = 67
            assert semantic_key(cities, context, []) != original
            assert request_id(evidence) != request_id({**evidence, "cityContext": {}})

    asyncio.run(run())
