"""Same-thread corrections retain early events and fail closed on history errors."""

import asyncio
import json

from tests.test_review_responses import complete, message, published, reviewing
from tests.test_strategic_adviser import ReviewCodex
from tests.test_strategic_review import assessment


def test_repair_report_before_start_response_with_reused_item_id_is_durable(tmp_path, monkeypatch):
    async def run():
        original_request = ReviewCodex.request
        turns = []
        corrected = assessment()
        corrected["assessment"] = "Corrected report from the second turn."
        corrected_text = json.dumps(corrected)

        async def request(process, method, payload):
            result = await original_request(process, method, payload)
            if method == "turn/start" and "outputSchema" in payload:
                turns.append(result["turn"]["id"])
                if len(turns) == 2:
                    common = {"threadId": process.thread_id, "turnId": process.turn_id}
                    await process.notification(
                        "item/started",
                        {**common, "item": {"id": "report", "type": "agentMessage", "phase": "final_answer"}},
                    )
                    await process.notification(
                        "item/agentMessage/delta", {**common, "itemId": "report", "delta": "SECOND_TURN_DRAFT"}
                    )
                    await message(process, corrected_text, phase="final_answer", item_id="report")
                    await complete(process)
            return result

        monkeypatch.setattr(ReviewCodex, "request", request)
        async with reviewing(tmp_path, attempts=2) as (service, process):
            await message(process, "FIRST_INVALID_REPORT", phase="final_answer", item_id="report")
            await complete(process)
            await published(service, corrected)
            assert len(turns) == 2 and turns[0] != turns[1]
            assert len(process.factory.instances) == 1
            record = next(row for row in service.records.values() if row.get("purpose") == "review")
            assert record["turnId"] == turns[1] and record["phase"] == "COMPLETED"
            history = service.history.logs[record["activationId"]]
            events = [json.loads(line) for line in history.path.read_text().splitlines()]
            reports = [event["data"] for event in events if event["kind"] == "assistant_message"]
            assert [(row["turnId"], row["itemId"], row["text"]) for row in reports] == [
                (turns[0], "review:report", "FIRST_INVALID_REPORT"),
                (turns[1], "review:report", corrected_text),
            ]
            drafts = [event["data"] for event in events if event["kind"] == "assistant_delta"]
            assert any(row["turnId"] == turns[1] and row["text"] == "SECOND_TURN_DRAFT" for row in drafts)
            task = service.reviews.store.at_turn(0)
            assert task is not None and task["status"] == "COMPLETED"

    asyncio.run(run())


def test_correction_history_write_error_is_fatal_and_does_not_skip_review(tmp_path, monkeypatch):
    async def run():
        async with reviewing(tmp_path, attempts=2) as (service, process):

            def failed_write(*args, **kwargs):
                raise OSError("History could not be saved")

            monkeypatch.setattr(service.history, "startup_context", failed_write)
            await message(process, "FIRST_INVALID_REPORT", phase="final_answer", item_id="report")
            await complete(process)
            assert service.reviews.worker is not None
            await asyncio.wait_for(service.reviews.worker, 3)
            assert service.reviews.failure is not None
            assert service.reviews.failure["error"] == "REVIEW_RECORD_FAILED"
            task = service.reviews.store.at_turn(0)
            assert task is not None and task["status"] == "PENDING"
            assert service.reviews.store.pending() is not None
            assert service.memory.snapshot("gameplay", 0)["strategicAdvice"] is None
            starts = [payload for method, payload in process.factory.calls if method == "turn/start"]
            assert len(starts) == 1
            assert process.closed

    asyncio.run(run())
