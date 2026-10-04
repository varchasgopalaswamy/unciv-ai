"""Review replies honor optional phases without accepting drafts or failed turns."""

import asyncio
import json
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock

import pytest

from civ_agent import service_recovery
from civ_agent.agent_service import AgentService
from tests.test_opening_review import OpeningHost
from tests.test_strategic_adviser import ReviewCodex, ReviewFactory
from tests.test_strategic_review import VICTORIES, assessment

MISSING_PHASE = object()


@asynccontextmanager
async def reviewing(tmp_path):
    factory = ReviewFactory()
    service = AgentService(
        "Rome",
        tmp_path / "workspace",
        tmp_path / "service",
        "S" * 43,
        "https://localhost:8765/mcp",
        None,
        process_factory=factory,
        connection_factory=factory.connection,
        review_attempts=1,
    )
    factory.service = service
    service.chat.epoch = "epoch-one"
    service.chat.settings = {"provider": "openrouter", "model": "test/reasoner", "reasoningEffort": "medium"}
    service.chat.social = AsyncMock(return_value={"granted": True})
    service.chat.connection = OpeningHost(factory)
    try:
        await service.review_phase({"epoch": "epoch-one", "gameTurn": 0, "stage": "hold"})
        assert (await service_recovery.barrier(service, True))["ready"]
        await service.review_phase({"epoch": "epoch-one", "gameTurn": 0, "stage": "start"})
        await asyncio.wait_for(factory.review_started.wait(), 3)
        assert factory.review_process is not None
        yield service, factory.review_process
    finally:
        await service.close()


async def message(process, text, *, phase=MISSING_PHASE, item_id="report", method="item/completed", **scope):
    item = {"id": item_id, "type": "agentMessage", "text": text}
    if phase is not MISSING_PHASE:
        item["phase"] = phase
    await process.notification(
        method, {"threadId": process.thread_id, "turnId": process.turn_id, "item": item, **scope}
    )


async def complete(process, status="completed"):
    await process.notification(
        "turn/completed", {"threadId": process.thread_id, "turn": {"id": process.turn_id, "status": status}}
    )


async def published(service, advice):
    async with asyncio.timeout(3):
        while service.reviews.store.pending() is not None or service.reviews.active:
            assert service.reviews.failure is None, service.reviews.failure
            await asyncio.sleep(0.01)
    assert service.memory.snapshot("gameplay", 0)["strategicAdvice"] == {"asOfTurn": 0, **advice}
    record = next(row for row in service.records.values() if row.get("purpose") == "review")
    assert record["phase"] == "COMPLETED"


async def rejected(service, error):
    assert service.reviews.worker is not None
    await asyncio.wait_for(service.reviews.worker, 3)
    assert service.reviews.failure is not None
    assert service.reviews.failure["error"] == "STRATEGIC_REVIEW_ATTEMPTS_EXHAUSTED"
    pending = service.reviews.store.pending()
    assert pending is not None and pending["inputs"] and pending["attempts"] == 1
    assert error in pending["error"]
    assert service.memory.snapshot("gameplay", 0)["strategicAdvice"] is None


@pytest.mark.parametrize("phase", [MISSING_PHASE, None, "final_answer"], ids=["omitted", "null", "final"])
def test_completed_review_accepts_optional_phase_and_waits_for_successful_turn(tmp_path, phase):
    async def run():
        async with reviewing(tmp_path) as (service, process):
            await message(process, "Still considering the position.", item_id="update")
            advice = assessment()
            await message(process, json.dumps(advice), phase=phase)
            # A completed message alone must not publish a report.
            assert service.reviews.active
            assert service.memory.snapshot("gameplay", 0)["strategicAdvice"] is None
            await complete(process)
            await published(service, advice)

    asyncio.run(run())


def test_last_unlabeled_completed_message_is_used(tmp_path):
    async def run():
        async with reviewing(tmp_path) as (service, process):
            first, last = assessment(), assessment()
            last["assessment"] = "The most recent completed report."
            await message(process, json.dumps(first), phase=None, item_id="first")
            await message(process, json.dumps(last), item_id="last")
            await complete(process)
            await published(service, last)

    asyncio.run(run())


def test_explicit_final_answer_takes_priority_over_later_unlabeled_reply(tmp_path):
    async def run():
        async with reviewing(tmp_path) as (service, process):
            final, unlabeled = assessment(), assessment()
            unlabeled["assessment"] = "This reply must not override an explicitly final answer."
            await message(process, json.dumps(unlabeled), item_id="before")
            await message(process, json.dumps(final), phase="final_answer", item_id="final")
            await message(process, json.dumps(unlabeled), phase=None, item_id="after")
            await complete(process)
            await published(service, final)

    asyncio.run(run())


@pytest.mark.parametrize("text", ["", "invalid"], ids=["empty", "invalid"])
def test_invalid_explicit_final_does_not_fall_back_to_valid_unlabeled_report(tmp_path, text):
    async def run():
        async with reviewing(tmp_path) as (service, process):
            await message(process, text, phase="final_answer", item_id="final")
            await message(process, json.dumps(assessment()), phase=None, item_id="unlabeled")
            await complete(process)
            await rejected(service, "Invalid JSON")

    asyncio.run(run())


@pytest.mark.parametrize(
    "text,error",
    [
        pytest.param("invalid", "Invalid JSON", id="json"),
        pytest.param(json.dumps({"assessment": "Missing review."}), "Field required", id="schema"),
        pytest.param(json.dumps(assessment(VICTORIES[:-1])), "STRATEGIC_REVIEW_COVERAGE", id="coverage"),
    ],
)
def test_last_unlabeled_report_still_requires_json_schema_and_victory_coverage(tmp_path, text, error):
    async def run():
        async with reviewing(tmp_path) as (service, process):
            # Never search backwards for an earlier valid report or concatenate replies.
            await message(process, json.dumps(assessment()), item_id="earlier-valid")
            await message(process, text, phase=None, item_id="last-invalid")
            await complete(process)
            await rejected(service, error)

    asyncio.run(run())


@pytest.mark.parametrize(
    "fields",
    [
        pytest.param({"phase": "commentary"}, id="commentary"),
        pytest.param({"phase": "unknown_phase"}, id="unknown-phase"),
        pytest.param({"phase": ""}, id="empty-phase"),
        pytest.param({"threadId": "another-thread"}, id="different-thread"),
        pytest.param({"turnId": "another-turn"}, id="different-turn"),
        pytest.param({"method": "item/started"}, id="unfinished-item"),
    ],
)
def test_nonfinal_unfinished_or_unrelated_messages_are_not_review_answers(tmp_path, fields):
    async def run():
        async with reviewing(tmp_path) as (service, process):
            await message(process, json.dumps(assessment()), **fields)
            await process.notification(
                "item/agentMessage/delta",
                {
                    "threadId": process.thread_id,
                    "turnId": process.turn_id,
                    "itemId": "report",
                    "delta": json.dumps(assessment()),
                },
            )
            await complete(process)
            await rejected(service, "Invalid JSON")

    asyncio.run(run())


@pytest.mark.parametrize("status", ["failed", "interrupted"])
def test_unlabeled_report_from_unsuccessful_turn_is_not_published(tmp_path, status):
    async def run():
        async with reviewing(tmp_path) as (service, process):
            await message(process, json.dumps(assessment()), phase=None)
            await complete(process, status)
            await rejected(service, "STRATEGIC_REVIEW_FAILED")

    asyncio.run(run())


@pytest.mark.parametrize("phase", [MISSING_PHASE, None], ids=["omitted", "null"])
def test_optional_phase_report_and_completion_before_turn_start_response_are_retained(tmp_path, monkeypatch, phase):
    request = ReviewCodex.request

    async def early_reply(process, method, payload):
        result = await request(process, method, payload)
        if method == "turn/start" and "outputSchema" in payload:
            await message(process, json.dumps(assessment()), phase=phase)
            await complete(process)
        return result

    monkeypatch.setattr(ReviewCodex, "request", early_reply)

    async def run():
        async with reviewing(tmp_path) as (service, _):
            await published(service, assessment())

    asyncio.run(run())
