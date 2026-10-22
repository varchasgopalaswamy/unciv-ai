"""Independent advisory clocks, current delivery, and bounded receipt recovery."""

import asyncio
import copy

import pytest

from civ_agent.advisory_answers import failed_answers
from civ_agent.decisions_tasks import TASK_VERSION, parse_answers, request_body, request_id, request_metadata
from civ_agent.memory_store import RecordStore
from civ_agent.startup_context import prepare_startup
from tests.test_city_decisions import add_cities, choose_candidates
from tests.test_decisions_advisories import setup


def incoming(memory, identifier=1, turn=0):
    memory.ingest(
        "g1",
        [
            {
                "id": identifier,
                "kind": "message",
                "turn": turn,
                "body": {
                    "message": f"msg{identifier}",
                    "author": "Greece",
                    "text": f"My coastal scouts observed another enemy unit near the border, report {identifier}.",
                    "participants": ["Rome", "Greece"],
                },
            }
        ],
        identifier,
    )


def test_communication_memory_attempt_does_not_block_same_turn_city_advice(tmp_path):
    async def run():
        async with setup(tmp_path, native_enabled=False) as (memory, gateway, host, classifier):
            assert gateway.advisories is not None
            add_cities(host, 1)
            choose_candidates(classifier)
            incoming(memory)
            communication = await gateway.open("communication", "communication", "g1", host, {}, 0)
            await prepare_startup(gateway, communication)
            state = await gateway.advisories.state()
            assert state["memoryAttemptTurn"] == 0 and "cityAttemptTurn" not in state
            assert classifier.calls[0]["preservation"] and not classifier.calls[0].get("cities")
            await gateway.close(communication, True)

            gameplay = await gateway.open("gameplay", "gameplay", "g1", host, {}, 0)
            prepared = await prepare_startup(gateway, gameplay)
            assert len(classifier.calls) == 2
            assert classifier.calls[1]["cities"] and classifier.calls[1]["preservation"] == []
            assert prepared["advisories"]["cityRecommendations"]
            state = await gateway.advisories.state()
            assert state["memoryAttemptTurn"] == state["cityAttemptTurn"] == 0

    asyncio.run(run())


def test_city_attempt_does_not_block_new_communication_memory_work_same_turn(tmp_path):
    async def run():
        async with setup(tmp_path, native_enabled=False) as (memory, gateway, host, classifier):
            assert gateway.advisories is not None
            add_cities(host, 1)
            choose_candidates(classifier)
            gameplay = await gateway.open("gameplay", "gameplay", "g1", host, {}, 0)
            await prepare_startup(gateway, gameplay)
            state = await gateway.advisories.state()
            assert state["cityAttemptTurn"] == 0 and "memoryAttemptTurn" not in state
            await gateway.close(gameplay, True)

            incoming(memory)
            communication = await gateway.open("communication", "communication", "g1", host, {}, 0)
            prepared = await prepare_startup(gateway, communication)
            assert len(classifier.calls) == 2
            assert classifier.calls[1]["preservation"] and not classifier.calls[1].get("cities")
            assert prepared["advisories"]["preservationCandidates"]
            state = await gateway.advisories.state()
            assert state["memoryAttemptTurn"] == state["cityAttemptTurn"] == 0

    asyncio.run(run())


def test_zero_allowance_never_dispatches_or_captures_native_city_previews(tmp_path):
    async def run():
        async with setup(
            tmp_path, native_enabled=False, decisions_backend="luna", additional_decisions_budget_usd=0
        ) as (
            memory,
            gateway,
            host,
            classifier,
        ):
            assert gateway.advisories is not None
            add_cities(host, 1)
            incoming(memory)
            job = await gateway.open("gameplay", "gameplay", "g1", host, {}, 0)
            prepared = await prepare_startup(gateway, job)
            assert classifier.calls == [] and host.__dict__["focus_reads"] == []
            assert prepared["advisories"]["cityRecommendations"] == []
            assert prepared["advisories"]["cityAdviceStatus"]["reason"] == "disabled_budget"
            state = await gateway.advisories.state()
            assert "cityAttemptTurn" not in state
            assert all(
                result["fallback"] == "budget_exhausted" and result["rawAnswers"] == []
                for result in state["attempts"].values()
            )

    asyncio.run(run())


def test_mixed_confidence_keeps_current_memory_and_construction_but_not_uncertain_focus(tmp_path):
    async def run():
        async with setup(tmp_path, native_enabled=False) as (memory, gateway, host, classifier):
            assert gateway.advisories is not None
            add_cities(host, 1)
            choose_candidates(classifier, focus_mode="low")
            incoming(memory)
            job = await gateway.open("gameplay", "gameplay", "g1", host, {}, 0)
            prepared = await prepare_startup(gateway, job)
            notices = prepared["advisories"]
            assert notices["preservationCandidates"]
            assert len(notices["cityRecommendations"]) == 1
            row = notices["cityRecommendations"][0]
            assert "focus" not in row and row["construction"]["choice"] == "append:Granary"
            result = (await gateway.advisories.state())["cityResult"]
            outcomes = {row["name"]: row for row in result["questionResults"]}
            assert outcomes["city_focus:0"]["status"] == "low_confidence"
            assert outcomes["city_focus:0"]["rawAnswer"]["confidence"] == 0.2
            assert outcomes["city_construction:0"]["status"] == "accepted"

    asyncio.run(run())


def test_stale_native_reply_drops_city_advice_without_losing_current_memory_answer(tmp_path):
    async def run():
        async with setup(tmp_path, native_enabled=False) as (memory, gateway, host, classifier):
            add_cities(host, 1)
            choose_candidates(classifier, host)
            incoming(memory)
            job = await gateway.open("gameplay", "gameplay", "g1", host, {}, 0)
            notices = (await prepare_startup(gateway, job))["advisories"]
            assert notices["cityRecommendations"] == []
            assert notices["cityAdviceStatus"]["fallback"] == "stale_native_basis"
            assert notices["preservationCandidates"] and len(classifier.calls) == 1

    asyncio.run(run())


def test_new_active_objective_during_classification_discards_only_city_delivery(tmp_path):
    async def run():
        async with setup(tmp_path, native_enabled=False) as (memory, gateway, host, classifier):
            add_cities(host, 1)
            choose_candidates(classifier)
            incoming(memory)
            original = classifier.batch

            async def changed(evidence, *, timeout):
                result = await original(evidence, timeout=timeout)
                assert evidence["objectives"] == []
                with memory.transaction() as db:
                    RecordStore(db).write(
                        "new-objective", "strategy", {"assessment": "Recruit defenders in Rome", "state": "active"}
                    )
                return result

            classifier.batch = changed
            job = await gateway.open("gameplay", "gameplay", "g1", host, {}, 0)
            notices = (await prepare_startup(gateway, job))["advisories"]
            assert notices["cityRecommendations"] == []
            assert notices["preservationCandidates"]

    asyncio.run(run())


def test_unchanged_city_inputs_keep_original_result_separate_from_new_memory_batch(tmp_path):
    async def run():
        async with setup(tmp_path, native_enabled=False) as (memory, gateway, host, classifier):
            assert gateway.advisories is not None
            add_cities(host, 1)
            choose_candidates(classifier)
            incoming(memory)
            first = await gateway.open("first", "gameplay", "g1", host, {}, 0)
            await prepare_startup(gateway, first)
            old_result = copy.deepcopy((await gateway.advisories.state())["cityResult"])
            await gateway.close(first, True)
            incoming(memory, 2, 5)
            host.record["gameTurn"] = 5
            host.revision = "fresh-transport-revision"
            host.view["economy"]["treasury"] += 10
            second = await gateway.open("second", "gameplay", "g1", host, {}, 5)
            notices = (await prepare_startup(gateway, second))["advisories"]
            assert len(classifier.calls) == 2 and not classifier.calls[1].get("cities")
            assert classifier.calls[1]["preservation"][0]["source"]["text"].endswith("report 2.")
            assert notices["cityAdviceStatus"]["reason"] == "unchanged_city_decision"
            assert notices["cityRecommendations"] == []
            assert len(notices["preservationCandidates"]) == 1
            assert (await gateway.advisories.state())["cityResult"] == old_result

    asyncio.run(run())


def test_unresolved_city_reuse_looks_up_original_mixed_request_before_new_memory_work(tmp_path):
    async def run():
        async with setup(tmp_path, native_enabled=False) as (memory, gateway, host, classifier):
            assert gateway.advisories is not None
            add_cities(host, 1)
            choose_candidates(classifier)
            incoming(memory)
            original = classifier.batch
            completed, lookups = {}, []

            async def batch(evidence, *, timeout):
                result = await original(evidence, timeout=timeout)
                completed[request_id(evidence)] = result
                if len(classifier.calls) == 1:
                    raise TimeoutError()  # The receipt exists, but delivery was lost.
                return result

            async def lookup(identifier, *, timeout):
                lookups.append(identifier)
                return completed[identifier]

            classifier.batch = batch
            classifier.__dict__["lookup"] = lookup
            first = await gateway.open("first", "gameplay", "g1", host, {}, 0)
            assert (await prepare_startup(gateway, first))["advisories"]["cityRecommendations"] == []
            original_identifier = request_id(classifier.calls[0])
            await gateway.close(first, True)
            incoming(memory, 2, 5)
            host.record["gameTurn"] = 5
            host.revision = "rebound-native-revision"
            second = await gateway.open("second", "gameplay", "g1", host, {}, 5)
            notices = (await prepare_startup(gateway, second))["advisories"]
            assert lookups == [original_identifier]
            assert len(classifier.calls) == 2 and not classifier.calls[1].get("cities")
            assert [source["key"] for source in classifier.calls[1]["preservation"]] == ["event:2"]
            assert len(notices["preservationCandidates"]) == 2
            assert notices["cityAdviceStatus"]["reused"]
            assert notices["cityRecommendations"][0]["basis"]["revision"] == host.revision
            state = await gateway.advisories.state()
            assert request_id(state["cityEvidence"]) == original_identifier
            assert state["cityResult"]["requestId"] == original_identifier

    asyncio.run(run())


def simple_evidence():
    return {
        "version": TASK_VERSION,
        "player": "Rome",
        "preservation": [{"key": "source", "version": 1, "record": {"body": {"text": "Greece reports enemy ships."}}}],
    }


def completed_result(evidence, settings):
    body = request_body(evidence, settings)
    raw = [
        {"name": q["name"], "type": "choice", "choice": q["choices"][0]["value"], "confidence": 0.99}
        for q in body["questions"]
    ]
    return {
        **request_metadata(body, evidence),
        **parse_answers(body, {"answers": raw}, settings.decisions_acceptance_probability),
    }


@pytest.mark.parametrize("receipt", ["completed", "unknown", "unavailable", "not_submitted", "wrong_metadata"])
def test_lost_reply_gets_one_receipt_lookup_and_never_redispatches_unknown_completion(tmp_path, receipt):
    async def run():
        async with setup(tmp_path, native_enabled=False) as (_, gateway, _, classifier):
            evidence = simple_evidence()
            seen = []

            async def batch(evidence, *, timeout):
                seen.append(("batch", request_id(evidence)))
                if len(seen) == 1:
                    raise TimeoutError()
                return completed_result(evidence, classifier.settings)

            async def lookup(identifier, *, timeout):
                seen.append(("lookup", identifier))
                if receipt == "unavailable":
                    raise TimeoutError()
                if receipt == "not_submitted":
                    return {"requestId": identifier, "fallback": "not_submitted"}
                result = completed_result(evidence, classifier.settings)
                if receipt == "unknown":
                    body = request_body(evidence, classifier.settings)
                    return {
                        **result,
                        **failed_answers(
                            body, classifier.settings.decisions_acceptance_probability, "completion_unknown"
                        ),
                    }
                if receipt == "wrong_metadata":
                    result["promptHash"] = "0" * 64
                return result

            classifier.batch = batch
            classifier.__dict__["lookup"] = lookup
            runtime = gateway.advisories
            assert runtime is not None
            first = await runtime.evaluate(evidence)
            assert first["fallback"] == "broker_unavailable"
            second = await runtime.evaluate(evidence)
            third = await runtime.evaluate(evidence)
            assert third == second
            assert [kind for kind, _ in seen] == (
                ["batch", "lookup", "batch"] if receipt == "not_submitted" else ["batch", "lookup"]
            )
            assert {identifier for _, identifier in seen} == {request_id(evidence)}
            if receipt in {"completed", "not_submitted"}:
                assert second["answers"] and second["brokerReconciliation"]
            else:
                assert second["answers"] == []
            assert (await runtime.state())["reconciliations"][request_id(evidence)] == 1

    asyncio.run(run())


@pytest.mark.parametrize("outcome", ["completed", "unknown", "cancelled"])
def test_memory_recovery_keeps_original_request_across_transport_only_observation_changes(tmp_path, outcome):
    async def run():
        async with setup(tmp_path, native_enabled=False) as (memory, gateway, host, classifier):
            # Handling the first chat must not remove Greece from the current
            # relevance topics: this case changes transport identity only.
            host.view["diplomacy"] = {
                "knownCivilizations": [{"civilizationId": "Greece", "civilizationName": "Greece", "atWar": True}]
            }
            incoming(memory, turn=20)
            with memory.transaction() as db:
                RecordStore(db).write(
                    "belief",
                    "belief",
                    {
                        "claim": "Coastal reinforcements may be needed",
                        "uncertainty": "high",
                        "state": "active",
                        "createdTurn": 0,
                        "updatedTurn": 0,
                    },
                )
            host.record["gameTurn"] = 20
            original = classifier.batch
            receipts, lookups = {}, []

            async def lost(evidence, *, timeout):
                result = await original(evidence, timeout=timeout)
                receipts[request_id(evidence)] = result
                if len(classifier.calls) == 1:
                    if outcome == "cancelled":
                        raise asyncio.CancelledError()
                    raise TimeoutError()
                return result

            async def lookup(identifier, *, timeout):
                lookups.append(identifier)
                if outcome == "unknown":
                    evidence = classifier.calls[0]
                    body = request_body(evidence, classifier.settings)
                    return {**request_metadata(body, evidence), **failed_answers(body, 0.9, "completion_unknown")}
                return receipts[identifier]

            classifier.batch, classifier.lookup = lost, lookup
            first = await gateway.open("first", "gameplay", "g1", host, {}, 20)
            if outcome == "cancelled":
                with pytest.raises(asyncio.CancelledError):
                    await prepare_startup(gateway, first)
            else:
                assert (await prepare_startup(gateway, first))["advisories"]["preservationCandidates"] == []
            await gateway.close(first, outcome != "cancelled")
            original_id = request_id(classifier.calls[0])
            host.record["gameTurn"], host.revision = 25, "new-transport-only-version"
            second = await gateway.open("second", "gameplay", "g1", host, {}, 25)
            prepared = await prepare_startup(gateway, second)
            assert len(classifier.calls) == 1 and lookups == [original_id]
            if outcome != "unknown":
                assert [source["key"] for source in prepared["advisories"]["preservationCandidates"]] == ["event:1"]
                assert any(
                    row["requestId"] == original_id and row["name"] == "preservation:0" and row["status"] == "delivered"
                    for row in second.memory["advisoryDelivery"]
                )
            else:
                assert prepared["advisories"]["preservationCandidates"] == []

    asyncio.run(run())


def test_cached_retrieval_records_later_exposure_when_handoff_expires(tmp_path):
    async def run():
        async with setup(tmp_path, native_enabled=False, memory_context_chars=1000) as (
            memory,
            gateway,
            host,
            classifier,
        ):
            with memory.transaction() as db:
                records = RecordStore(db)
                records.write(
                    "old-note",
                    "note",
                    {"text": "Old observation " * 30, "state": "active", "createdTurn": 0, "updatedTurn": 0},
                )
                records.write(
                    "handoff",
                    "handoff",
                    {
                        "text": "Continue the current task. " * 25,
                        "state": "active",
                        "basis": {"turn": 19},
                        "createdTurn": 19,
                        "updatedTurn": 19,
                    },
                )
            host.record["gameTurn"] = 20
            first = await gateway.open("first", "gameplay", "g1", host, {}, 20)
            await prepare_startup(gateway, first)
            assert "handoff" in first.memory["contextIds"] and "old-note" not in first.memory["contextIds"]
            identifier = request_id(classifier.calls[0])
            assert all(row["status"] == "not_selected" for row in first.memory["advisoryDelivery"])
            await gateway.close(first, True)
            host.record["gameTurn"], host.revision = 25, "later"
            second = await gateway.open("second", "gameplay", "g1", host, {}, 25)
            await prepare_startup(gateway, second)
            assert len(classifier.calls) == 1 and "old-note" in second.memory["contextIds"]
            assert any(
                row["requestId"] == identifier and row["status"] == "delivered" and row["exposure"] == "context"
                for row in second.memory["advisoryDelivery"]
            )

    asyncio.run(run())
