"""Early-review exposure acknowledges prepared work, independently of acceptance."""

import asyncio
import copy
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from civ_agent.advisory_review import review_input
from civ_agent.advisory_runtime import AdvisoryRuntime
from civ_agent.civilization_state import CivilizationState
from civ_agent.decisions_policy import DecisionsSettings
from civ_agent.decisions_tasks import parse_answers, request_body, request_id, request_metadata
from civ_agent.memory_store import RecordStore
from civ_agent.review_runtime import ReviewRuntime
from civ_agent.review_store import ReviewStore
from civ_agent.telemetry import JobAccounting


class InlineIO:
    async def call(self, function, *args, **kwargs):
        return function(*args, **kwargs)


@pytest.fixture
def harness(tmp_path, monkeypatch):
    memory = CivilizationState(tmp_path / "memory", "Rome")
    memory.bind("g1")
    accounting = JobAccounting(tmp_path / "telemetry")
    settings = DecisionsSettings(decisions_enabled=True)
    client = SimpleNamespace(batch=AsyncMock(side_effect=AssertionError("Scheduling cannot make a paid call")))
    io = InlineIO()
    gateway = SimpleNamespace(
        memory=memory,
        io=io,
        accounting=accounting,
        paused=True,
        reviewing=False,
        tool_catalogue=lambda tools, purpose: tools,
    )
    advisory = AdvisoryRuntime(gateway, client, settings)
    gateway.advisories = advisory
    native = {
        "epoch": "g1",
        "turn": 4,
        "phase": "PAUSED",
        "cities": [],
        "economy": {"happiness": -2, "goldPerTurn": -3},
    }
    connection = SimpleNamespace(
        tool=AsyncMock(side_effect=lambda name, arguments: copy.deepcopy(native)),
        list_tools=AsyncMock(return_value=[]),
    )
    service = SimpleNamespace(
        memory=memory,
        io=io,
        gateway=gateway,
        civilization="Rome",
        diagnostics=Mock(),
        chat=SimpleNamespace(
            epoch="g1",
            held=True,
            dispatching=False,
            connection=connection,
            gameplay_settings={},
            settings={},
            timeout=60,
        ),
        records={
            "parent": {
                "activationId": "parent",
                "purpose": "gameplay",
                "epoch": "g1",
                "gameTurn": 3,
                "confirmedHandoff": True,
                "settings": {},
            }
        },
    )
    runtime = ReviewRuntime(service, interval=20)

    async def capture(snapshot, read, tools, turn, epoch):
        return {"turn": turn, "observation": snapshot, "reads": {"player_turn": snapshot}}

    captured = AsyncMock(side_effect=capture)
    monkeypatch.setattr(runtime, "capture", captured)
    started = Mock()
    monkeypatch.setattr(runtime, "start", started)
    opening = {"turn": 0, "cities": [], "economy": {"happiness": 2, "goldPerTurn": 3}}
    store = ReviewStore(memory)
    store.prepare("opening", {"turn": 0, "observation": opening, "reads": {"player_turn": opening}}, {}, 60)
    store.complete("opening", {})
    with memory.transaction() as db:
        RecordStore(db).write("observation:overview", "observation", {**native, "turn": 3})
    yield SimpleNamespace(
        memory=memory,
        accounting=accounting,
        advisory=advisory,
        settings=settings,
        client=client,
        service=service,
        runtime=runtime,
        native=native,
        connection=connection,
        captured=captured,
        started=started,
    )
    accounting.engine.dispose()
    memory.engine.dispose()


def seed_triage(harness, mode="review"):
    evidence = review_input(harness.memory, 3, harness.settings, 20)
    assert evidence is not None
    signature = evidence.pop("signature")
    body = request_body(evidence, harness.settings)
    answer = {
        "name": "early_review",
        "type": "choice",
        "choice": "wait" if mode == "wait" else "review",
        "confidence": 0.4 if mode == "low" else 0.99,
    }
    result = {
        **request_metadata(body, evidence),
        **parse_answers(body, {"answers": [answer]}, harness.settings.decisions_acceptance_probability),
    }
    harness.advisory.store.save(
        reviewEvidence=evidence,
        reviewDeliveryEvidence=evidence,
        reviewAttemptSignature=signature,
        earlyRequestTurn=2 if mode == "wrong_turn" else 3,
        attempts={request_id(evidence): result},
    )


def test_phase_reports_scheduler_exposure_only_after_actual_task_preparation_and_deduplicates(harness):
    seed_triage(harness)

    async def run():
        response = await harness.runtime.phase("g1", 4, "start", True, early_review=True)
        assert response["pendingReview"]["job"] == "parent"
        pending = harness.runtime.store.pending()
        assert pending is not None and json.loads(pending["inputs"])["turn"] == 3
        state = await harness.advisory.state()
        assert state["lastEarlyTurn"] == 3
        assert len(state["reviewDelivery"]) == 1
        row = state["reviewDelivery"][0]
        assert row["name"] == "early_review" and row["status"] == "delivered" and row["exposure"] == "scheduler"
        reports = harness.accounting.classifier_reports()
        assert len(reports) == 1 and reports[0]["direction"] == "advisory_delivery"
        assert reports[0]["usage"] is None and reports[0]["accounting"] is None
        await harness.runtime.phase("g1", 4, "start", True, early_review=True)
        assert harness.accounting.classifier_reports() == reports
        harness.connection.tool.assert_awaited_once()
        harness.client.batch.assert_not_awaited()
        # Native snapshot turns and completed-review turns use distinct conventions.
        harness.runtime.store.complete("parent", {})
        state = await harness.advisory.state()
        assert state["reviewMaterialBaseline"]["reviewTurn"] == 3
        assert state["reviewMaterialBaseline"]["sourceTurn"] == 4

    asyncio.run(run())


@pytest.mark.parametrize("mode", ["not_triaged", "manual", "no_parent", "finished", "capture_failed"])
def test_no_classifier_delivery_for_manual_untriaged_or_unprepared_review(harness, mode):
    if mode != "not_triaged":
        seed_triage(harness)
    if mode == "no_parent":
        harness.service.records.clear()
    elif mode == "finished":
        harness.native["result"] = {"winner": "Greece"}
    elif mode == "capture_failed":
        harness.captured.side_effect = ValueError("capture unavailable")

    async def run():
        if mode == "capture_failed":
            with pytest.raises(ValueError, match="capture unavailable"):
                await harness.runtime.phase("g1", 4, "start", True, early_review=True)
        else:
            await harness.runtime.phase("g1", 4, "start", True, early_review=mode != "manual")
        assert harness.accounting.classifier_reports() == []
        state = await harness.advisory.state()
        assert "reviewDelivery" not in state
        if mode in {"no_parent", "finished", "capture_failed"}:
            assert harness.runtime.store.at_turn(3) is None and "lastEarlyTurn" not in state
        else:
            assert harness.runtime.store.at_turn(3) is not None
        if mode == "no_parent":
            harness.connection.tool.assert_not_awaited()
        if mode in {"no_parent", "finished"}:
            harness.captured.assert_not_awaited()
        harness.client.batch.assert_not_awaited()

    asyncio.run(run())


@pytest.mark.parametrize("mode", ["low", "wait", "wrong_turn"])
def test_scheduling_ack_cannot_promote_uncertain_negative_or_other_turn_triage(harness, mode):
    seed_triage(harness, mode)

    async def run():
        await harness.advisory.review_scheduled(3)
        state = await harness.advisory.state()
        assert state["lastEarlyTurn"] == 3 and "reviewDelivery" not in state
        assert harness.accounting.classifier_reports() == []
        harness.client.batch.assert_not_awaited()

    asyncio.run(run())
