"""Advisory delivery is optional, current and separate from private evidence capture."""

import asyncio
import copy
import json
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest
from pydantic import ValidationError
from sqlalchemy import select

from civ_agent import memory_schema as tables
from civ_agent.advisory_answers import failed_answers
from civ_agent.advisory_runtime import AdvisoryRuntime
from civ_agent.advisory_store import material_facts
from civ_agent.agent_service import AgentService
from civ_agent.civilization_state import CivilizationState
from civ_agent.commands import agent_server, game
from civ_agent.decisions_broker import RoutingClient
from civ_agent.decisions_ledger import fingerprint
from civ_agent.decisions_policy import DecisionsSettings, decisions_values
from civ_agent.decisions_tasks import parse_answers, request_body, request_id, request_metadata
from civ_agent.game_config import GameConfig, load_config
from civ_agent.memory_snapshots import load_snapshot
from civ_agent.memory_store import RecordStore
from civ_agent.proposal_gateway import ProposalGateway
from civ_agent.review_store import ReviewStore
from civ_agent.service_io import ServiceIO
from civ_agent.startup_context import prepare_startup, startup_prompt
from tests.memory_helpers import stored_records
from tests.test_agent_service import StartupHost


class AdvisoryHost(StartupHost):
    def __init__(self):
        self.record = {"epoch": "g1", "gameTurn": 0}
        super().__init__(SimpleNamespace(service=SimpleNamespace(active_id="job", records={"job": self.record})))
        self.captures = []
        self.available = True

    async def tool(self, name, args):
        if name == "native_advice":
            self.captures.append(copy.deepcopy(args))
            return {
                "epoch": args["epoch"],
                "revision": args["revision"],
                "turn": self.record["gameTurn"],
                "available": self.available,
                "recommendations": [
                    {
                        "id": f"native-{args['revision']}-{i}",
                        "category": "Construction",
                        "subject": "city1",
                        "explanation": "Original native explanation",
                        "command": {
                            "tool": "player_command",
                            "arguments": {"kind": "QUEUE_CONSTRUCTION", "target": "city1", "selection": name},
                        },
                        "costs": {"production": 40},
                        "prerequisites": ["Agriculture"],
                        "conflicts": ["city1"],
                    }
                    for i, name in enumerate(["Market", "Granary"] * 5)
                ],
                "limitations": ["No simulated discoveries."],
            }
        return await super().tool(name, args)


class Classifier:
    def __init__(self, settings):
        self.settings, self.calls, self.mode = settings, [], "success"

    async def batch(self, evidence, *, timeout):
        self.calls.append(copy.deepcopy(evidence))
        if self.mode == "timeout":
            raise TimeoutError()
        if self.mode == "malformed":
            return {"requestId": request_id(evidence), "answers": "not an answer list"}
        body = request_body(evidence, self.settings)
        answers = []
        values = {
            "retrieval_include": "include",
            "retrieval_priority": "high",
            "preservation": "consider",
            "beliefs": "reconsider",
            "early_review": "review",
        }
        for q in body["questions"]:
            prefix = q["name"].split(":")[0]
            answer = {
                "type": "choice",
                "name": q["name"],
                "choice": values.get(prefix, q["choices"][0]["value"]),
                "confidence": 0.99,
            }
            if self.mode == "low":
                answer["confidence"] = 0.1
            elif self.mode == "refusal":
                answer = {"name": q["name"], "type": "refusal"}
            elif self.mode == "unknown":
                answer["choice"] = "invented-choice"
            answers.append(answer)
        return {
            **request_metadata(body, evidence),
            **parse_answers(body, {"answers": answers}, self.settings.decisions_acceptance_probability),
            "usage": {"input_tokens": 100},
        }

    async def lookup(self, identifier, *, timeout):
        evidence = next(e for e in self.calls if request_id(e) == identifier)
        body = request_body(evidence, self.settings)
        return {
            **request_metadata(body, evidence),
            **failed_answers(body, self.settings.decisions_acceptance_probability, "completion_unknown"),
        }


@asynccontextmanager
async def setup(tmp_path, *, native_enabled=True, **options):
    settings = DecisionsSettings(decisions_enabled=True, **options)
    memory = CivilizationState(tmp_path / "memory", "Rome", settings.memory_context_chars)
    io = ServiceIO()
    gateway = ProposalGateway(memory, io, tmp_path / "workspace")
    gateway.native_advice_enabled = native_enabled
    classifier = Classifier(settings)
    gateway.advisories = AdvisoryRuntime(gateway, classifier, settings)
    host = AdvisoryHost()
    memory.bind("g1")
    try:
        yield memory, gateway, host, classifier
    finally:
        await io.close()
        memory.engine.dispose()


def test_defaults_validation_retired_flags_and_service_container_arguments(tmp_path, capsys):
    config_file = tmp_path / "game.toml"
    config_file.write_text("[agents]\ndecisions_enabled = true\nadditional_decisions_budget_usd = 0.02\n")
    config = load_config(config_file).resolved()
    assert decisions_values(GameConfig().agents) == DecisionsSettings().model_dump()
    settings = config.settings(config.agent_players[0])
    arguments = ["Rome"]
    for key, value in settings.items():
        if isinstance(value, bool):
            if value:
                arguments.append("--" + key.replace("_", "-"))
        else:
            arguments.extend(
                ["--" + key.replace("_", "-"), json.dumps(value) if isinstance(value, (dict, list)) else str(value)]
            )
    assert decisions_values(agent_server.parser().parse_args(arguments)) == decisions_values(config.agents)
    overridden = game.resolve_options(
        game.parser().parse_args(
            ["--additional-decisions-budget-usd", "0", "--decisions-enabled", "--memory-context-chars", "4000"]
        )
    )
    assert overridden.agents.decisions_enabled and overridden.agents.memory_context_chars == 4000
    assert overridden.agents.additional_decisions_budget_usd == 0
    for value in (-1, float("nan"), float("inf")):
        with pytest.raises(ValidationError):
            GameConfig(agents={"additional_decisions_budget_usd": value})
    with pytest.raises(ValidationError, match="Extra inputs"):
        GameConfig(agents={"diplomacy_routing_enabled": False})
    for parser, args in ((game.parser(), []), (agent_server.parser(), ["Rome"])):
        with pytest.raises(SystemExit):
            parser.parse_args([*args, "--diplomacy-routing-enabled"])
        assert "unrecognized arguments" in capsys.readouterr().err
    for retired in ("decisions_native_candidate_limit", "decisions_gameplay_group_limit"):
        with pytest.raises(ValidationError):
            GameConfig(agents={retired: 4})


@pytest.mark.parametrize("decisions_enabled", [False, True])
@pytest.mark.parametrize("native_enabled", [False, True])
def test_service_enables_native_and_decisions_independently(tmp_path, monkeypatch, native_enabled, decisions_enabled):
    def broker():
        assert decisions_enabled  # Disabled operation needs neither a key nor a broker.
        return RoutingClient(tmp_path / "broker.sock", tmp_path / "token.json")

    monkeypatch.setattr(RoutingClient, "from_environment", broker)

    async def run():
        service = AgentService(
            "Rome",
            tmp_path / "workspace",
            tmp_path / "service",
            "S" * 43,
            "https://localhost:8765/mcp",
            None,
            native_advice_enabled=native_enabled,
            decisions_enabled=decisions_enabled,
        )
        try:
            assert service.native_advice_enabled == native_enabled
            assert service.gateway.native_advice_enabled == native_enabled
            assert (service.gateway.advisories is not None) == decisions_enabled
            assert (service.gateway.routing is not None) == decisions_enabled
            service.chat.epoch = "g1"
            service.gateway.request_checkpoint()
            triage = await service.review_triage(
                {"epoch": "g1", "gameTurn": 4, "scheduledSoon": False, "nextReviewTurn": 10}
            )
            assert not triage["earlyReview"] and triage["reason"] == ("barrier" if decisions_enabled else "disabled")
        finally:
            await service.close()

    asyncio.run(run())


def test_native_suggestions_are_delivered_directly_without_paid_gameplay_classification(tmp_path):
    async def run():
        async with setup(tmp_path) as (memory, gateway, host, classifier):
            job = await gateway.open("job", "gameplay", "g1", host, {}, 0)
            prepared = await prepare_startup(gateway, job)
            advice = prepared["reads"]["native_advice"]
            assert advice["available"] and len(advice["recommendations"]) == 8
            assert advice["remainingRecommendations"] == 2
            assert advice["limitations"] == ["No simulated discoveries."]
            first = advice["recommendations"][0]
            assert first["explanation"] == "Original native explanation"
            assert first["costs"] == {"production": 40}
            assert first["prerequisites"] == ["Agriculture"] and first["conflicts"] == ["city1"]
            catalogue = {t["name"] for t in await gateway.tools(job)}
            assert "native_advice" in catalogue and "gameplay_advice" not in catalogue
            assert "gameplay_advice" not in prepared["reads"]
            assert classifier.calls == []
            records = stored_records(memory)
            assert "observation:native_advice" in records
            exposure = [r for r in records.values() if r["category"] == "guidance"]
            assert len(exposure) == 1 and len(exposure[0]["body"]["recommendations"]) == 8
            prompt = startup_prompt(prepared, "gameplay", "Play").user
            assert "Original native explanation" in prompt
            assert (await prepare_startup(gateway, job))["reads"]["native_advice"] == advice
            # Choosing a legal alternative requires only the normal native inspections.
            await gateway.call(job, "city_details", {"target": "city1"}, "inspect")
            result = await gateway.call(
                job,
                "player_command",
                {"kind": "QUEUE_CONSTRUCTION", "target": "city1", "selection": "Warrior"},
                "alternative",
            )
            assert result["kind"] == "QUEUE_CONSTRUCTION" and host.executed[0]["selection"] == "Warrior"
            fresh = await gateway.call(job, "native_advice", {}, "refresh")
            assert fresh["revision"] == host.revision and len(fresh["recommendations"]) == 10
            assert classifier.calls == []  # Actions and native reads cannot initiate paid work.

    asyncio.run(run())


@pytest.mark.parametrize("mode", ["success", "low", "refusal", "unknown", "timeout", "malformed", "zero"])
def test_memory_triage_cannot_filter_native_options_or_add_gameplay_questions(tmp_path, mode):
    async def run():
        async with setup(
            tmp_path,
            decisions_backend="luna" if mode == "zero" else "jev",
            additional_decisions_budget_usd=0 if mode == "zero" else 0.1,
        ) as (
            memory,
            gateway,
            host,
            classifier,
        ):
            with memory.transaction() as db:
                RecordStore(db).write(
                    "old",
                    "note",
                    {
                        "text": "A historical observation to reconsider",
                        "state": "active",
                        "createdTurn": 0,
                        "updatedTurn": 0,
                    },
                )
            host.record["gameTurn"] = 20
            job = await gateway.open("job", "gameplay", "g1", host, {}, 20)
            classifier.mode = mode
            prepared = await prepare_startup(gateway, job)
            advice = prepared["reads"]["native_advice"]
            assert advice["available"] and len(advice["recommendations"]) == 8
            assert advice["recommendations"][0]["command"]["arguments"]["selection"] == "Market"
            assert advice["recommendations"][1]["command"]["arguments"]["selection"] == "Granary"
            assert len(classifier.calls) == (0 if mode == "zero" else 1)
            for evidence in classifier.calls:
                assert "native" not in evidence and "gameplay" not in evidence
                assert "Original native explanation" not in json.dumps(evidence)
                questions = request_body(evidence, classifier.settings)["questions"]
                assert not any(q["name"].startswith(("gameplay:", "priority:")) for q in questions)

    asyncio.run(run())


@pytest.mark.parametrize("native_enabled", [False, True])
def test_native_advice_is_an_independent_option_even_with_decisions_enabled(tmp_path, native_enabled):
    async def run():
        async with setup(tmp_path, native_enabled=native_enabled) as (_, gateway, host, classifier):
            job = await gateway.open("job", "gameplay", "g1", host, {}, 0)
            prepared = await prepare_startup(gateway, job)
            catalogue = {t["name"] for t in await gateway.tools(job)}
            assert ("native_advice" in prepared["reads"]) == native_enabled
            assert ("native_advice" in catalogue) == native_enabled
            assert len(host.captures) == int(native_enabled)
            assert classifier.calls == []

    asyncio.run(run())


def test_old_active_retrieval_frozen_versions_citations_belief_notice_and_no_cascade(tmp_path):
    async def run():
        async with setup(tmp_path) as (memory, gateway, host, classifier):
            with memory.transaction() as db:
                store = RecordStore(db)
                for key, category, body in (
                    (
                        "old",
                        "belief",
                        {
                            "claim": "Greece may reinforce the coast",
                            "uncertainty": "high",
                            "sources": ["event:1"],
                            "contradictingSources": [],
                            "subjects": ["coast"],
                            "priority": 5,
                        },
                    ),
                    ("retired", "note", {"text": "retired sentinel", "state": "retired"}),
                    ("expired", "note", {"text": "expired sentinel", "expiresTurn": 2}),
                ):
                    store.write(key, category, {"state": "active", "createdTurn": 0, "updatedTurn": 0, **body})
            memory.ingest(
                "g1",
                [
                    {
                        "id": 1,
                        "kind": "message",
                        "turn": 20,
                        "body": {
                            "message": "msg1",
                            "author": "Greece",
                            "participants": ["Rome", "Greece"],
                            "text": "I might reinforce the coast; can you send ships?",
                            "turn": 20,
                        },
                    }
                ],
                1,
            )
            host.record["gameTurn"] = 20
            host.view["diplomacy"] = {"knownCivilizations": [{"civilizationId": "Greece", "atWar": True}]}
            original = stored_records(memory)["old"]
            job = await gateway.open("job", "gameplay", "g1", host, {}, 20)
            assert "old" not in job.memory["contextIds"]
            prepared = await prepare_startup(gateway, job)
            evidence = classifier.calls[0]
            assert [r["key"] for r in evidence["retrieval"]] == ["old"]
            assert "old" in job.memory["contextIds"]
            assert stored_records(memory)["old"] == original
            assert len(prepared["advisories"]["beliefNotices"]) == 1
            assert prepared["advisories"]["preservationCandidates"][0]["key"] == "event:1"
            assert 'uncertainty":"high' in prepared["memoryContext"]
            assert '"sources":["event:1"]' in prepared["memoryContext"]
            with memory.transaction() as db:
                snapshot = load_snapshot(
                    db, db.scalar(select(tables.jobs.c.snapshot).where(tables.jobs.c.id == job.id))
                )
                assert snapshot["delivered"]["old"] == original["version"]
                calls = list(db.execute(select(tables.calls.c.name, tables.calls.c.arguments)))
                assert any(name == "memory_read" and "old" in args for name, args in calls)
            await gateway.close(job, True)
            host.record["gameTurn"] = 25
            host.revision = "transport-only"
            next_job = await gateway.open("later", "gameplay", "g1", host, {}, 25)
            later = await prepare_startup(gateway, next_job)
            assert len(classifier.calls) == 1
            assert later["advisories"]["beliefNotices"] == []
            assert later["advisories"]["preservationCandidates"] == []

    asyncio.run(run())


def test_startup_skips_paid_ranking_when_current_topics_already_select_all_optional_memories(tmp_path):
    async def run():
        async with setup(tmp_path) as (memory, gateway, host, classifier):
            with memory.transaction() as db:
                RecordStore(db).write(
                    "old",
                    "note",
                    {
                        "text": "A dated note about the owned warrior",
                        "subjects": ["Warrior"],
                        "state": "active",
                        "createdTurn": 0,
                        "updatedTurn": 0,
                    },
                )
            host.record["gameTurn"] = 20
            job = await gateway.open("job", "gameplay", "g1", host, {}, 20)
            assert "old" not in job.memory["contextIds"]
            await prepare_startup(gateway, job)
            assert "old" in job.memory["contextIds"] and classifier.calls == []

    asyncio.run(run())


def test_material_signatures_ignore_routine_counters():
    before = {
        "turn": 10,
        "revision": "one",
        "economy": {"treasury": 10, "goldPerTurn": 5},
        "cities": [{"handle": "c1", "constructionChoice": {"name": "Market", "turnsObserved": 1}}],
    }
    after = copy.deepcopy(before)
    after.update(turn=11, revision="two")
    after["economy"]["treasury"] = 15
    after["cities"][0]["constructionChoice"]["turnsObserved"] = 2
    assert fingerprint(material_facts(before)) == fingerprint(material_facts(after))


def test_early_review_uses_recorded_changes_cooldowns_and_no_full_capture(tmp_path):
    async def run():
        async with setup(tmp_path) as (memory, gateway, _, classifier):
            opening = {"turn": 0, "cities": []}
            reviews = ReviewStore(memory)
            reviews.prepare("opening", {"turn": 0, "observation": opening}, {}, 60)
            reviews.complete("opening", {})
            with memory.transaction() as db:
                RecordStore(db).write(
                    "observation:overview", "observation", {"turn": 10, "cities": [{"handle": "new"}]}
                )
            runtime = gateway.advisories
            assert runtime is not None
            assert not (await runtime.triage(9, scheduled_soon=False, next_review_turn=40))["earlyReview"]
            assert not (await runtime.triage(10, scheduled_soon=True, next_review_turn=40))["earlyReview"]
            assert classifier.calls == []
            assert (await runtime.triage(10, scheduled_soon=False, next_review_turn=40))["earlyReview"]
            assert (await runtime.triage(10, scheduled_soon=False, next_review_turn=40))["earlyReview"]
            assert len(classifier.calls) == 1
            await runtime.save(lastEarlyTurn=10)
            assert not (await runtime.triage(19, scheduled_soon=False, next_review_turn=40))["earlyReview"]
            # Same previously classified facts do not generate another call after cooldown.
            assert not (await runtime.triage(20, scheduled_soon=False, next_review_turn=40))["earlyReview"]
            assert len(classifier.calls) == 1

    asyncio.run(run())


def test_early_review_rejects_stale_recorded_evidence(tmp_path):
    async def run():
        async with setup(tmp_path) as (memory, gateway, _, classifier):
            opening = {"turn": 0, "cities": []}
            reviews = ReviewStore(memory)
            reviews.prepare("opening", {"turn": 0, "observation": opening}, {}, 60)
            reviews.complete("opening", {})
            with memory.transaction() as db:
                RecordStore(db).write(
                    "observation:overview", "observation", {"turn": 10, "cities": [{"handle": "new"}]}
                )
            original = classifier.batch

            async def changed(evidence, *, timeout):
                result = await original(evidence, timeout=timeout)
                with memory.transaction() as db:
                    RecordStore(db).write("observation:overview", "observation", {"turn": 10, "cities": []})
                return result

            classifier.batch = changed
            assert gateway.advisories is not None
            assert (await gateway.advisories.triage(10, scheduled_soon=False, next_review_turn=20)) == {
                "earlyReview": False,
                "reason": "stale_evidence",
            }

    asyncio.run(run())
