"""Resolve the immutable request, then bind its judgment to equivalent current facts."""

import asyncio
import copy
from unittest.mock import AsyncMock

import pytest

from civ_agent.decisions_tasks import parse_answers, request_body, request_id, request_metadata
from civ_agent.memory_store import RecordStore
from civ_agent.protocol import JsonObject
from tests.test_advisory_review_delivery import harness as harness


def record_overview(harness, turn=4, *, revision="new-version", deficit=True):
    body: JsonObject = {**copy.deepcopy(harness.native), "turn": turn, "revision": revision}
    body["economy"]["goldPerTurn"] = -3 if deficit else 3
    with harness.memory.transaction() as db:
        return RecordStore(db).write("observation:overview", "observation", body)


def accepted(evidence, settings):
    body = request_body(evidence, settings)
    raw = {"name": "early_review", "type": "choice", "choice": "review", "confidence": 0.99}
    return {
        **request_metadata(body, evidence),
        **parse_answers(body, {"answers": [raw]}, settings.decisions_acceptance_probability),
    }


async def lose_first_reply(harness):
    original = []

    async def lost(evidence, *, timeout):
        original.append(copy.deepcopy(evidence))
        raise TimeoutError()

    harness.client.batch = AsyncMock(side_effect=lost)
    first = await harness.advisory.triage(3, scheduled_soon=False, next_review_turn=20)
    assert not first["earlyReview"] and first["reason"] == "broker_unavailable"
    return original[0]


@pytest.mark.parametrize("churn_during_lookup", [False, True])
def test_receipt_after_routine_churn_schedules_current_turn_with_original_request_identity(
    harness, churn_during_lookup
):
    async def run():
        original = await lose_first_reply(harness)
        identifier = request_id(original)
        version = record_overview(harness)

        async def receipt(request, *, timeout):
            nonlocal version
            assert request == identifier
            if churn_during_lookup:
                version = record_overview(harness, revision="another-routine-version")
            return accepted(original, harness.settings)

        harness.client.lookup = AsyncMock(side_effect=receipt)
        result = await harness.advisory.triage(4, scheduled_soon=False, next_review_turn=20)
        assert result["earlyReview"]
        state = await harness.advisory.state()
        assert state["reviewEvidence"] == original
        binding = state["reviewDeliveryEvidence"]
        assert binding["earlyReview"]["completedTurn"] == 4
        assert binding["earlyReview"]["current"]["source"]["version"] == version
        assert request_id(binding) != identifier
        assert state["earlyRequestTurn"] == 4 and state["reconciliations"][identifier] == 1
        # The actual scheduler acknowledgment is for the later prepared task.
        harness.service.records["parent"]["gameTurn"] = 4
        harness.native["turn"] = 5
        await harness.runtime.phase("g1", 5, "start", True, early_review=True)
        deliveries = [row for row in harness.accounting.classifier_reports() if row["direction"] == "advisory_delivery"]
        assert len(deliveries) == 1 and deliveries[0]["requestId"] == identifier
        assert deliveries[0]["deliveryResults"][0]["exposure"] == "scheduler"
        state = await harness.advisory.state()
        assert state["reviewDelivery"][0]["source"]["version"] == version
        assert state["reviewDeliveryTurn"] == 4
        harness.client.batch.assert_awaited_once()
        harness.client.lookup.assert_awaited_once()

    asyncio.run(run())


@pytest.mark.parametrize("change", ["material", "objective", "statement"])
def test_receipt_cannot_schedule_after_semantic_context_changes_during_lookup(harness, change):
    if change == "statement":
        with harness.memory.transaction() as db:
            RecordStore(db).write("objective", "strategy", {"assessment": "Defend the coast", "state": "active"})

    async def run():
        original = await lose_first_reply(harness)
        record_overview(harness)

        async def receipt(identifier, *, timeout):
            if change == "material":
                record_overview(harness, revision="material-change", deficit=False)
            elif change == "objective":
                with harness.memory.transaction() as db:
                    RecordStore(db).write(
                        "new-objective", "strategy", {"assessment": "Recruit defenders", "state": "active"}
                    )
            else:
                with harness.memory.transaction() as db:
                    RecordStore(db).write(
                        "event:4",
                        "statement",
                        {
                            "turn": 4,
                            "kind": "message",
                            "body": {
                                "author": "Greece",
                                "text": "The coast now has an enemy fleet approaching our city.",
                            },
                        },
                    )
            return accepted(original, harness.settings)

        harness.client.lookup = AsyncMock(side_effect=receipt)
        result = await harness.advisory.triage(4, scheduled_soon=False, next_review_turn=20)
        assert result == {"earlyReview": False, "reason": "stale_evidence"}
        state = await harness.advisory.state()
        assert state["earlyRequestTurn"] is None and state["reviewDeliveryEvidence"] is None
        assert state["reviewEvidence"] == original
        await harness.advisory.review_scheduled(4)
        assert not any(row["direction"] == "advisory_delivery" for row in harness.accounting.classifier_reports())
        harness.client.batch.assert_awaited_once()
        harness.client.lookup.assert_awaited_once()

    asyncio.run(run())


def test_accepted_current_binding_cannot_be_acknowledged_for_a_different_turn(harness):
    async def run():
        original = await lose_first_reply(harness)
        record_overview(harness)
        harness.client.lookup = AsyncMock(return_value=accepted(original, harness.settings))
        assert (await harness.advisory.triage(4, scheduled_soon=False, next_review_turn=20))["earlyReview"]
        await harness.advisory.review_scheduled(3)
        assert "reviewDelivery" not in await harness.advisory.state()
        assert not any(row["direction"] == "advisory_delivery" for row in harness.accounting.classifier_reports())

    asyncio.run(run())
