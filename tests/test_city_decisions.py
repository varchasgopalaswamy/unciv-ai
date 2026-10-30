"""Independent city questions share a budgeted batch without changing action authority."""

import asyncio
import copy
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from civ_agent import memory_schema as tables
from civ_agent.city_decisions import capture, change_key, recommendations, semantic_key
from civ_agent.decisions_tasks import (
    CITY_LIMIT,
    TASK_VERSION,
    parse_answers,
    request_body,
    request_id,
    request_metadata,
)
from civ_agent.memory_snapshots import load_snapshot
from civ_agent.memory_store import RecordStore
from civ_agent.startup_context import prepare_startup, startup_prompt
from tests.memory_helpers import stored_records
from tests.test_decisions_advisories import setup


def add_cities(host, count=2):
    original = host.tool
    host.focus_reads = []
    host.view["phase"] = "ACTIVE"
    host.view["canEndTurn"] = True
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
                "appendProduction": {"cost": 40, "stored": 0, "overflow": 0, "perTurn": 4, "turnsIfStartedNow": 10},
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
        result = await original(name, args)
        if name == "player_turn":
            result["phase"] = host.view["phase"]
        return result

    host.tool = tool


def choose_candidates(classifier, host=None, *, focus_mode="success"):
    original = classifier.batch

    async def batch(evidence, *, timeout):
        result = await original(evidence, timeout=timeout)
        if not isinstance(result.get("answers"), list):
            return result
        raw = copy.deepcopy(result.get("rawAnswers", result["answers"]))
        for answer in raw:
            if (
                answer["name"].startswith(("city_focus_change:", "city_construction_change:"))
                and answer.get("type") == "choice"
            ):
                answer["choice"] = "change"
            if answer["name"].startswith("city_focus:") and answer.get("type") == "choice":
                answer["choice"] = "focus1"
                if focus_mode == "low":
                    answer["confidence"] = 0.2
                elif focus_mode == "unknown":
                    answer["choice"] = "manufactured-focus"
            if answer["name"].startswith("city_construction:") and answer.get("type") == "choice":
                index = int(answer["name"].split(":")[1])
                answer["choice"] = evidence["cities"][index]["constructions"][0]["id"]
        if host is not None:
            host.revision = "changed-during-classification"
        body = request_body(evidence, classifier.settings)
        return {
            **result,
            **request_metadata(body, evidence),
            **parse_answers(body, {"answers": raw}, classifier.settings.decisions_acceptance_probability),
        }

    classifier.batch = batch


@pytest.mark.parametrize("can_end_turn", [False, True])
def test_native_active_phase_captures_cities_even_with_mandatory_choices(tmp_path, can_end_turn):
    async def run():
        async with setup(tmp_path) as (_, gateway, host, _):
            add_cities(host, 1)
            host.view["canEndTurn"] = can_end_turn
            assert "isYourTurn" not in host.view
            job = await gateway.open("job", "gameplay", "g1", host, {}, 0)
            await gateway.call(job, "player_turn", {}, "initial")
            cities, _, status = await capture(gateway, job, [])
            assert len(cities) == status["capturedCities"] == 1
            assert host.__dict__["focus_reads"]
            assert host.executed == []

    asyncio.run(run())


@pytest.mark.parametrize("phase", ["WAITING", "PAUSED", "DEFEATED", "FINISHED"])
def test_inactive_native_phase_never_reads_city_previews(tmp_path, phase):
    async def run():
        async with setup(tmp_path) as (_, gateway, host, _):
            add_cities(host, 1)
            host.view["phase"] = phase
            job = await gateway.open("job", "gameplay", "g1", host, {}, 0)
            await gateway.call(job, "player_turn", {}, "initial")
            assert await capture(gateway, job, []) == ([], {}, {"reason": "not_active_gameplay", "capturedCities": 0})
            assert host.__dict__["focus_reads"] == []

    asyncio.run(run())


def test_missing_native_phase_is_ineligible_without_a_host_read():
    job = SimpleNamespace(observation={}, purpose="gameplay")
    assert asyncio.run(capture(None, job, [])) == ([], {}, {"reason": "not_active_gameplay", "capturedCities": 0})


def test_changed_native_source_during_preview_discards_captured_cities(tmp_path):
    async def run():
        async with setup(tmp_path) as (_, gateway, host, _):
            add_cities(host, 1)
            original = host.tool

            async def changed(name, args):
                result = await original(name, args)
                if name == "player_inspect" and args.get("subject") == "CITY_FOCUS":
                    result["revision"] = "stale-preview"
                return result

            host.tool = changed
            job = await gateway.open("job", "gameplay", "g1", host, {}, 0)
            await gateway.call(job, "player_turn", {}, "initial")
            cities, context, status = await capture(gateway, job, [])
            assert cities == [] and context == {}
            assert status["reason"] == "stale_native_input"

    asyncio.run(run())


def test_city_change_and_alternative_questions_coalesce_with_memory_and_preserve_native_delivery(tmp_path):
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
            assert [q["name"] for q in questions[:8]] == [
                "city_focus_change:0",
                "city_focus:0",
                "city_construction_change:0",
                "city_construction:0",
                "city_focus_change:1",
                "city_focus:1",
                "city_construction_change:1",
                "city_construction:1",
            ]
            assert any(q["name"].startswith("preservation:") for q in questions)
            assert all("independent" in q["instructions"] for q in questions[:4])
            assert all(len(q["choices"]) == 2 for q in questions if "_change:" in q["name"])
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
            assert "City advice." in startup_prompt(prepared, "gameplay", "Play").user
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
        async with setup(
            tmp_path,
            decisions_backend="luna" if mode == "zero" else "jev",
            additional_decisions_budget_usd=0 if mode == "zero" else 0.1,
        ) as (
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


def test_free_jev_captures_and_classifies_city_advice_with_zero_dollar_allowance(tmp_path):
    async def run():
        async with setup(tmp_path, decisions_backend="jev", additional_decisions_budget_usd=0) as (
            _,
            gateway,
            host,
            classifier,
        ):
            add_cities(host, 1)
            job = await gateway.open("job", "gameplay", "g1", host, {}, 0)
            prepared = await prepare_startup(gateway, job)
            assert classifier.calls and host.__dict__["focus_reads"]
            assert prepared["advisories"]["cityRecommendations"]

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
            evidence = {"version": TASK_VERSION, "player": "Rome", "cities": cities, "cityContext": context}
            restarted = copy.deepcopy(evidence)
            restarted["cities"][0]["basis"].update(epoch="restored-epoch", revision="restored-revision")
            assert request_id(restarted) == request_id(evidence)
            assert request_body(restarted, classifier.settings) == request_body(evidence, classifier.settings)
            restarted["cities"][0]["focuses"][0]["native"]["foodPerTurn"] = -2
            assert request_id(restarted) != request_id(evidence)

    asyncio.run(run())


@pytest.mark.parametrize("question_limit", [1, 2, 3, 4, 7])
def test_question_limit_omits_whole_city_question_groups(tmp_path, question_limit):
    async def run():
        async with setup(tmp_path, decisions_max_questions=question_limit) as (_, gateway, host, classifier):
            add_cities(host)
            choose_candidates(classifier)
            job = await gateway.open("job", "gameplay", "g1", host, {}, 0)
            prepared = await prepare_startup(gateway, job)
            expected_cities = question_limit // 4
            assert len(prepared["advisories"]["cityRecommendations"]) == expected_cities
            assert prepared["advisories"]["cityAdviceStatus"]["omittedCities"] == 2 - expected_cities
            assert len(classifier.calls) == bool(expected_cities)
            if classifier.calls:
                assert len(request_body(classifier.calls[0], classifier.settings)["questions"]) == 4 * expected_cities

    asyncio.run(run())


def test_byte_limit_omits_city_pair_and_its_unneeded_construction_rules(tmp_path):
    async def run():
        async with setup(tmp_path, decisions_max_request_bytes=25000) as (_, gateway, host, classifier):
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
            assert len(request_body(evidence, classifier.settings)["questions"]) == 4

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
            assert prepared["advisories"]["cityRecommendations"] == []
            if mode == "success":
                assert len(host.__dict__["focus_reads"]) == 1
                assert prepared["advisories"]["cityAdviceStatus"]["reason"] == "unchanged_city_decision"
            else:
                assert len(host.__dict__["focus_reads"]) == 2
                assert prepared["advisories"]["cityAdviceStatus"]["reused"]  # Receipt lookup, never a second paid call.

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
            evidence = {"version": TASK_VERSION, "player": "Rome", "cities": cities, "cityContext": context}
            body = request_body(evidence, classifier.settings)
            assert len(body["questions"]) == 4 * CITY_LIMIT
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


def test_appending_extra_worker_cannot_reuse_invested_work_and_moves_bind_exact_entries(tmp_path):
    async def run():
        async with setup(tmp_path) as (_, gateway, host, _):
            add_cities(host, 1)
            city = host.view["cities"][0]
            host.view["constructionCatalogue"].append("Worker")
            city["constructionOptions"].append("Worker")
            city["constructionQueue"] = ["Worker", "Warrior", "Worker"]
            city["constructionEstimates"]["Worker"] = {
                **copy.deepcopy(city["constructionEstimates"]["Warrior"]),
                "production": {"cost": 70, "stored": 20, "overflow": 0, "perTurn": 5, "turnsIfStartedNow": 10},
                "appendProduction": {"cost": 70, "stored": 0, "overflow": 0, "perTurn": 5, "turnsIfStartedNow": 14},
            }
            city["development"]["constructions"] = [
                {
                    "index": index,
                    "name": name,
                    "perpetual": False,
                    "productionCost": 70 if name == "Worker" else 40,
                    "productionStored": 20 if index == 0 else 0,
                    "productionPerTurn": 5 if name == "Worker" else 4,
                    "turns": 10 if index < 2 else 14,
                    "canRemove": True,
                    "moveToIndices": [i for i in range(3) if i != index],
                }
                for index, name in enumerate(city["constructionQueue"])
            ]
            job = await gateway.open("job", "gameplay", "g1", host, {}, 0)
            await gateway.call(job, "player_turn", {}, "initial")
            cities, _, _ = await capture(gateway, job, [])
            choices = {option["id"]: option["native"] for option in cities[0]["constructions"]}
            extra = choices["append:Worker"]
            assert extra["additionalCopy"] and extra["queueOperation"] == "append"
            assert extra["production"]["stored"] == 0 and extra["production"]["turnsIfStartedNow"] == 14
            assert extra["queueDelayTurns"] is extra["completionAfterQueueTurns"] is None
            assert cities[0]["facts"]["currentConstructionEstimate"]["production"]["stored"] == 20
            later = choices["move:2"]
            assert later["queueIndex"] == later["command"]["index"] == 2
            assert later["previousQueueProduction"]["stored"] == 0
            assert later["production"]["stored"] == 20  # The first copy after the native move owns stored work.
            assert choices["move:1"]["command"]["index"] == 1
            assert "move:0" not in choices  # Keep already continues the exact head.
            host.view["cities"][0]["constructionOptions"].remove("Worker")
            await gateway.close(job, True)
            second = await gateway.open("blocked", "gameplay", "g1", host, {}, 0)
            await gateway.call(second, "player_turn", {}, "next")
            cities, _, _ = await capture(gateway, second, [])
            assert not any(option["id"] == "append:Worker" for option in cities[0]["constructions"])

    asyncio.run(run())


@pytest.mark.parametrize(
    ("gate", "gate_confidence", "alternative_confidence", "expected"),
    [
        ("change", 0.99, 0.99, "focus1"),
        ("change", 0.2, 0.99, None),
        ("change", 0.99, 0.2, None),
        ("keep", 0.99, 0.2, "keep"),
        ("keep", 0.99, 0.99, "keep"),
        ("keep", 0.2, 0.99, None),
        ("defer", 0.99, 0.99, None),  # Not a value in the current binary gate.
    ],
)
def test_city_change_gate_is_independent_and_both_answers_are_required_for_an_action(
    tmp_path, gate, gate_confidence, alternative_confidence, expected
):
    async def run():
        async with setup(tmp_path) as (_, gateway, host, classifier):
            add_cities(host, 1)
            job = await gateway.open("job", "gameplay", "g1", host, {}, 0)
            await gateway.call(job, "player_turn", {}, "initial")
            cities, context, _ = await capture(gateway, job, [])
            body = request_body({"player": "Rome", "cities": cities, "cityContext": context}, classifier.settings)
            result = parse_answers(
                body,
                {
                    "answers": [
                        {
                            "name": "city_focus_change:0",
                            "type": "choice",
                            "choice": gate,
                            "confidence": gate_confidence,
                        },
                        {
                            "name": "city_focus:0",
                            "type": "choice",
                            "choice": "focus1",
                            "confidence": alternative_confidence,
                        },
                    ]
                },
                0.9,
            )
            rows = recommendations(gateway, job, cities, result, context)
            if expected is None:
                assert rows == []
            else:
                assert rows[0]["focus"]["choice"] == expected
                assert ("native" in rows[0]["focus"]) == (expected == "focus1")

    asyncio.run(run())


def test_city_trigger_ignores_routine_countdowns_but_tracks_growth_queue_and_adopted_goals():
    job = SimpleNamespace(
        observation={
            "cities": [
                {
                    "handle": "city1",
                    "constructionQueue": ["Worker"],
                    "constructionChoice": {"name": "Worker", "chosenTurn": 3, "turnsObserved": 1},
                    "development": {"population": 2, "foodStored": 4, "productionStored": 20, "turnsToGrowth": 4},
                }
            ]
        },
        memory={},
    )
    before = change_key(job, [])
    city = job.observation["cities"][0]
    city["constructionChoice"]["turnsObserved"] += 1
    city["development"].update(foodStored=6, productionStored=25, turnsToGrowth=3)
    assert change_key(job, []) == before
    for field, value in (("population", 3), ("foodPerTurn", -2)):
        changed = copy.deepcopy(job)
        changed.observation["cities"][0]["development"][field] = value
        assert change_key(changed, []) != before
    city["constructionQueue"] = []
    assert change_key(job, []) != before
    assert change_key(job, [{"key": "role", "version": 1}]) != change_key(job, [])


def test_meaningful_city_change_is_evaluated_before_normal_memory_cooldown(tmp_path):
    async def run():
        async with setup(tmp_path, decisions_startup_min_gap_turns=5) as (_, gateway, host, classifier):
            add_cities(host, 1)
            choose_candidates(classifier)
            first = await gateway.open("first", "gameplay", "g1", host, {}, 0)
            await prepare_startup(gateway, first)
            await gateway.close(first, True)
            host.record["gameTurn"] = 1
            host.view["cities"][0]["development"]["population"] += 1
            second = await gateway.open("growth", "gameplay", "g1", host, {}, 1)
            await prepare_startup(gateway, second)
            assert len(classifier.calls) == 2
            assert len(host.__dict__["focus_reads"]) == 2

    asyncio.run(run())


def test_adopted_city_intent_is_distinct_from_optional_review_and_equal_yield_policies_remain_choices(tmp_path):
    async def run():
        async with setup(tmp_path) as (_, gateway, host, _):
            add_cities(host, 1)
            original = host.tool

            async def tool(name, args):
                result = await original(name, args)
                if name == "player_inspect" and args.get("subject") == "CITY_FOCUS":
                    for option in result["data"]["options"]:
                        option["yields"] = {"Production": "4"}
                return result

            host.tool = tool
            job = await gateway.open("job", "gameplay", "g1", host, {}, 0)
            job.memory["strategicAdvice"] = {"asOfTurn": 0, "assessment": "Consider a production plan"}
            await gateway.call(job, "player_turn", {}, "initial")
            objectives = [
                {
                    "key": "chosen",
                    "version": 2,
                    "record": {
                        "decisions": [
                            {
                                "id": "capital-growth",
                                "objective": "Grow capital",
                                "investment": "Finish Granary",
                                "reconsiderWhen": "Happiness becomes negative",
                                "subjects": ["city1"],
                            }
                        ]
                    },
                }
            ]
            cities, context, _ = await capture(gateway, job, objectives)
            assert job.delivered_overview is not None
            assert context["reviewAdvice"] == job.delivered_overview["strategicReview"]
            assert context["reviewAdvice"]["status"] == "BASELINE_UNAVAILABLE"
            adopted = context["adoptedDecisions"][0]
            assert adopted["source"] == {"key": "chosen", "version": 2}
            assert adopted["objective"] == "Grow capital"
            assert cities[0]["facts"]["adoptedDecisions"] == [adopted]
            assert [option["native"]["name"] for option in cities[0]["focuses"]] == ["NoFocus", "ProductionFocus"]

    asyncio.run(run())
