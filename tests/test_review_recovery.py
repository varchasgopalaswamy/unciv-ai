"""Invalid advisory reports are repaired once without stopping ordinary play."""

import asyncio
import json

import pytest

from civ_agent.protocol import JsonObject
from civ_agent.review_store import ReviewStore
from tests.test_review_responses import complete, message, reviewing
from tests.test_strategic_adviser import ReviewCodex
from tests.test_strategic_review import assessment


def review_task(service, turn=0) -> JsonObject:
    task = service.reviews.store.at_turn(turn)
    assert task is not None
    return task


async def settled(service):
    async with asyncio.timeout(3):
        while service.reviews.store.pending() is not None or service.reviews.active:
            assert service.reviews.failure is None, service.reviews.failure
            await asyncio.sleep(0.01)


def test_invalid_opening_advice_becomes_unavailable_and_releases_gameplay(tmp_path):
    async def run():
        async with reviewing(tmp_path) as (service, process):
            await message(process, "invalid")
            await complete(process)
            await settled(service)
            assert service.health()["runFailure"] is None
            assert review_task(service)["status"] == "UNAVAILABLE"
            assert ReviewStore(service.memory).blocking("epoch-one", 0) is None
            result: JsonObject = await service.review_phase({"epoch": "epoch-one", "gameTurn": 0, "stage": "status"})
            assert result["ready"] and result["pendingReview"] is None
            assert result["reviewOutcome"]["status"] == "UNAVAILABLE"
            memory = service.memory.snapshot("gameplay", 0)
            assert memory["strategicAdvice"] is None
            assert memory["strategicReviewStatus"]["status"] == "UNAVAILABLE"
            assert process.closed

    asyncio.run(run())


@pytest.mark.parametrize("method", ["service/modelStatus", "service/requestFailed"])
def test_quota_exhaustion_preserves_pending_review_without_spending_repair_attempts(tmp_path, method):
    async def run():
        async with reviewing(tmp_path, attempts=2) as (service, process):
            notice = {
                "reason": "usageLimitExceeded",
                "state": "failed",
                "threadId": process.thread_id,
                "turnId": process.turn_id,
                "rpcMethod": "turn/start",
            }
            await process.notification(method, notice)
            quota = service.health()["quotaFailure"]
            assert quota is not None and quota["purpose"] == "review"
            await complete(process, "failed")
            async with asyncio.timeout(3):
                while service.reviews.active:
                    await asyncio.sleep(0.01)
            pending = review_task(service)
            assert pending["status"] == "PENDING" and pending["attempts"] == 0
            assert service.health()["runFailure"] is None
            quota = service.health()["quotaFailure"]
            assert quota is not None and quota["reason"] == "usageLimitExceeded"
            assert len([p for m, p in process.factory.calls if m == "turn/start" and "outputSchema" in p]) == 1

    asyncio.run(run())


def test_review_rpc_failure_keeps_private_diagnostics_and_lifecycle_record(tmp_path):
    async def run():
        detail = {"rpcMethod": "turn/start", "rpcId": 4, "rpcCode": -32602, "reason": "invalidParams"}
        async with reviewing(tmp_path) as (service, process):
            await process.notification("service/requestFailed", detail)
            record = next(r for r in service.records.values() if r.get("purpose") == "review")
            assert record["requestFailure"] == detail
            files = [json.loads(p.read_text()) for p in service.records_dir.glob("*.json")]
            assert any(r.get("requestFailure") == detail for r in files)
            await message(process, json.dumps(assessment()))
            await complete(process)
            await settled(service)
        entries = [json.loads(line) for line in service.diagnostics.path.read_text().splitlines()]
        assert any(e["event"] == "codex_request_failed" and e["purpose"] == "review" for e in entries)

    asyncio.run(run())


def test_invalid_report_gets_one_same_thread_correction(tmp_path, monkeypatch):
    async def run():
        repaired = asyncio.Event()
        original = ReviewCodex.request
        starts = []

        async def request(process, method, payload):
            result = await original(process, method, payload)
            if method == "turn/start" and "outputSchema" in payload:
                starts.append(payload)
                if len(starts) == 2:
                    repaired.set()
            return result

        monkeypatch.setattr(ReviewCodex, "request", request)
        async with reviewing(tmp_path, attempts=2) as (service, process):
            await message(process, '{"assessment":"unfinished"}')
            await complete(process)
            await asyncio.wait_for(repaired.wait(), 3)
            assert len(process.factory.instances) == 1
            assert starts[0]["threadId"] == starts[1]["threadId"]
            assert "review" in starts[1]["input"][0]["text"].lower()
            assert "correct" in starts[1]["input"][0]["text"].lower()
            assert starts[0]["input"] != starts[1]["input"]
            await message(process, json.dumps(assessment()))
            await complete(process)
            await settled(service)
            assert review_task(service)["attempts"] == 2
            assert review_task(service)["status"] == "COMPLETED"
            assert service.memory.snapshot("gameplay", 0)["strategicAdvice"] is not None

    asyncio.run(run())


def test_failed_correction_is_unavailable_without_third_attempt(tmp_path, monkeypatch):
    async def run():
        repaired = asyncio.Event()
        original = ReviewCodex.request
        starts = []

        async def request(process, method, payload):
            result = await original(process, method, payload)
            if method == "turn/start" and "outputSchema" in payload:
                starts.append(payload)
                if len(starts) == 2:
                    repaired.set()
            return result

        monkeypatch.setattr(ReviewCodex, "request", request)
        async with reviewing(tmp_path, attempts=3) as (service, process):
            await message(process, "invalid")
            await complete(process)
            await asyncio.wait_for(repaired.wait(), 3)
            await message(process, "still invalid")
            await complete(process)
            await settled(service)
            assert len(starts) == 2
            assert len(process.factory.instances) == 1
            assert review_task(service)["status"] == "UNAVAILABLE"
            assert service.reviews.failure is None
            assert service.memory.snapshot("gameplay", 0)["strategicAdvice"] is None

    asyncio.run(run())


def test_stalled_output_restarts_once_with_specific_feedback(tmp_path):
    async def run():
        async with reviewing(tmp_path, attempts=2) as (service, process):
            factory = process.factory
            factory.review_started.clear()
            await process.notification("service/transportFailure", {"reason": "REVIEW_OUTPUT_STALLED"})
            await asyncio.wait_for(factory.review_started.wait(), 3)
            replacement = factory.review_process
            assert replacement is not process and process.closed
            starts = [payload for method, payload in factory.calls if method == "turn/start"]
            assert len(starts) == 2
            feedback = starts[1]["input"][0]["text"]
            assert "stopped making progress" in feedback
            assert "REVIEW_OUTPUT_STALLED" not in feedback
            await message(replacement, json.dumps(assessment()))
            await complete(replacement)
            await settled(service)
            assert review_task(service)["status"] == "COMPLETED"
            assert len(factory.instances) == 2

    asyncio.run(run())


def test_history_callback_failure_is_fatal_instead_of_skipped_advice(tmp_path):
    async def run():
        async with reviewing(tmp_path, attempts=2) as (service, process):
            await process.notification("service/transportFailure", {"reason": "CODEX_NOTIFICATION_FAILED"})
            assert service.reviews.worker is not None
            await asyncio.wait_for(service.reviews.worker, 3)
            assert service.reviews.failure is not None
            assert service.reviews.failure["error"] == "REVIEW_RECORD_FAILED"
            pending = service.reviews.store.pending()
            assert pending is not None and pending["attempts"] == 1
            assert review_task(service)["status"] == "PENDING"
            assert len(process.factory.instances) == 1

    asyncio.run(run())


def test_confirmed_exit_with_incomplete_history_has_accurate_fatal_error(tmp_path, monkeypatch):
    from civ_agent.agent_service import ProcessDrainError

    close = ReviewCodex.close
    count = 0

    async def delayed_close(process):
        nonlocal count
        await close(process)
        count += 1
        if count == 1:
            raise ProcessDrainError()

    monkeypatch.setattr(ReviewCodex, "close", delayed_close)

    async def run():
        async with reviewing(tmp_path) as (service, process):
            await message(process, json.dumps(assessment()))
            await complete(process)
            assert service.reviews.worker is not None
            await asyncio.wait_for(service.reviews.worker, 3)
            assert process.closed
            assert service.reviews.failure is not None
            assert service.reviews.failure["error"] == "REVIEW_HISTORY_DRAIN_FAILED"
            assert service.reviews.unconfirmed_process is process
            assert review_task(service)["status"] == "PENDING"
            assert service.memory.snapshot("gameplay", 0)["strategicAdvice"] is None

    asyncio.run(run())
