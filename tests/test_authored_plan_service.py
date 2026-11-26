"""Structured plans use typed input, delivered CAS and native Memory receipts."""

import asyncio
import json
from copy import deepcopy

import pytest
from pydantic import ValidationError

from civ_agent.memory_records import CityPlan
from civ_agent.orchestration.authored_memory import AuthoredMemoryService
from civ_agent.orchestration.authored_plan_service import (
    DeleteAuthoredDocument,
    DocumentOperation,
    ReadAuthoredDocument,
    SaveAuthoredDocument,
    invoke_document,
)
from civ_agent.orchestration.contracts import digest
from civ_agent.orchestration.gateway_registry import GatewayRegistry
from civ_agent.orchestration.store import JournalConflict
from tests.test_authored_memory import operation, setup


def save(name, *, assessment="Grow before expanding", city=None, model_step=2):
    content = (
        {"type": "city_plan", "city": city, "reasons": {"construction": assessment}}
        if city is not None
        else {"type": "strategy", "assessment": assessment}
    )
    return SaveAuthoredDocument.model_validate(
        {
            "content": content,
            "model_step": model_step,
            "operation": {"id": digest(name), "fingerprint": digest([name, content])},
        }
    )


def read_document(**values):
    return ReadAuthoredDocument.model_validate({"model_step": 1, **values})


def test_city_plan_replacement_has_no_model_paths_json_or_version_copying(tmp_path):
    async def run():
        async with setup(tmp_path, purpose="economy") as fixture:
            read = read_document(document="city_plan", city="city2")
            first = await invoke_document(fixture.service, fixture.identity, read)
            assert first.content is None and first.version is None and not first.existed
            assert first.id == "city-city2.md"
            prepared = await invoke_document(fixture.service, fixture.identity, save("create", city="city2"))
            assert prepared.applied and prepared.id == "city-city2.md"
            first_version = prepared.version
            delivered = await invoke_document(
                fixture.service, fixture.identity, read.model_copy(update={"model_step": 3})
            )
            assert isinstance(delivered.content, CityPlan)
            assert delivered.content.city == "city2" and delivered.version == first_version
            saved = fixture.registry.load(fixture.identity)
            assert saved.state.data["memory"]["authoredDocumentObservations"]["city-city2.md"] == {
                "version": first_version,
                "model_step": 3,
            }
            assert "city-city2.md" not in saved.state.data["memory"].get("delivered", {})
            assert "city-city2.md" not in fixture.job.memory.get("records", {})
            replaced = await invoke_document(
                fixture.service,
                fixture.identity,
                save("replace", city="city2", assessment="Build a Granary", model_step=4),
            )
            assert replaced.applied and replaced.error is None
            actual = await fixture.service.store.read(fixture.prefix + "city-city2.md", max_chars=8000)
            assert json.loads(actual.content)["reasons"] == {"construction": "Build a Granary"}
            assert actual.metadata["job"] == fixture.identity.job
            assert actual.metadata["purpose"] == "economy"

    asyncio.run(run())


def test_missing_read_is_definite_nonmutating_rejection(tmp_path):
    async def run():
        async with setup(tmp_path) as fixture:
            prepared = await invoke_document(fixture.service, fixture.identity, save("unread"))
            assert prepared.error == "read_required" and not prepared.applied
            assert await fixture.service.store.read(fixture.prefix + "strategy.md", max_chars=8000) is None

    asyncio.run(run())


def test_cas_observation_never_grants_domain_source_credit_or_same_response_mutation(tmp_path):
    async def run():
        async with setup(tmp_path) as fixture:
            before = deepcopy(fixture.registry.load(fixture.identity).state.data["memory"])
            live_before = deepcopy(fixture.job.memory)
            await invoke_document(fixture.service, fixture.identity, read_document(document="strategy", model_step=3))
            after = deepcopy(fixture.registry.load(fixture.identity).state.data["memory"])
            assert after.pop("authoredDocumentObservations") == {"strategy.md": {"version": None, "model_step": 3}}
            assert after == before
            live_after = deepcopy(fixture.job.memory)
            live_after.pop("authoredDocumentObservations", None)
            assert live_after == live_before
            sibling = await invoke_document(fixture.service, fixture.identity, save("sibling", model_step=3))
            assert sibling.error == "read_required" and not sibling.applied
            later = await invoke_document(fixture.service, fixture.identity, save("later", model_step=4))
            assert later.applied and later.version == "1"

    asyncio.run(run())


def test_same_version_keeps_prior_observation_but_new_version_requires_next_response(tmp_path):
    async def run():
        async with setup(tmp_path) as fixture:
            await invoke_document(fixture.service, fixture.identity, read_document(document="strategy", model_step=1))
            await invoke_document(fixture.service, fixture.identity, read_document(document="strategy", model_step=2))
            unchanged = await invoke_document(fixture.service, fixture.identity, save("same-version", model_step=2))
            assert unchanged.applied
            await fixture.proxy.write(
                fixture.prefix + "strategy.md",
                json.dumps({"type": "strategy", "assessment": "Concurrent plan"}),
                expected_version=unchanged.version,
                operation=operation("other-author"),
            )
            await invoke_document(fixture.service, fixture.identity, read_document(document="strategy", model_step=3))
            sibling = await invoke_document(fixture.service, fixture.identity, save("new-version", model_step=3))
            assert sibling.error == "read_required" and not sibling.applied
            later = await invoke_document(fixture.service, fixture.identity, save("reconsidered", model_step=4))
            assert later.applied and later.version == "3"

    asyncio.run(run())


def test_observed_version_does_not_follow_a_concurrent_edit(tmp_path):
    async def run():
        async with setup(tmp_path) as fixture:
            path = fixture.prefix + "strategy.md"
            old = json.dumps({"type": "strategy", "assessment": "Initial"})
            first = await fixture.proxy.write(path, old, expected_version=None, operation=operation("seed"))
            await invoke_document(fixture.service, fixture.identity, read_document(document="strategy"))
            newer = json.dumps({"type": "strategy", "assessment": "Concurrent decision"})
            changed = await fixture.proxy.write(
                path, newer, expected_version=first.version, operation=operation("concurrent")
            )
            reply = await invoke_document(fixture.service, fixture.identity, save("candidate"))
            assert reply.error == "version_conflict" and not reply.applied
            saved = await fixture.service.store.read(path, max_chars=8000)
            assert saved.content == newer and saved.version == changed.version

    asyncio.run(run())


def test_observed_absence_is_persisted_not_a_process_cache(tmp_path):
    async def run():
        async with setup(tmp_path) as fixture:
            await invoke_document(fixture.service, fixture.identity, read_document(document="strategy"))
            await fixture.registry.close()
            registry = GatewayRegistry(
                fixture.domain.gateway, game="game", player="p0", resolve=fixture.registry.resolve
            )
            try:
                reopened = AuthoredMemoryService(registry, fixture.path)
                prepared = await invoke_document(reopened, fixture.identity, save("after-restart"))
                assert prepared.applied and prepared.version == "1"
            finally:
                await registry.close()

    asyncio.run(run())


@pytest.mark.parametrize("deleting", [False, True])
def test_native_mutation_receipt_replays_after_role_retirement(tmp_path, deleting):
    async def run():
        async with setup(tmp_path) as fixture:
            read = read_document(document="strategy")
            await invoke_document(fixture.service, fixture.identity, read)
            original = save("record")
            frozen = await invoke_document(fixture.service, fixture.identity, original)
            assert frozen.applied
            if deleting:
                await invoke_document(fixture.service, fixture.identity, read.model_copy(update={"model_step": 3}))
                original = DeleteAuthoredDocument(
                    document="strategy",
                    model_step=4,
                    operation=DocumentOperation(id=digest("delete"), fingerprint=digest("delete-input")),
                )
                frozen = await invoke_document(fixture.service, fixture.identity, original)
                assert frozen.applied
            await fixture.registry.retire(fixture.identity)
            replay = await invoke_document(fixture.service, fixture.identity, original)
            assert replay.error is None and replay.replayed
            with pytest.raises(JournalConflict, match="RETIRED"):
                await invoke_document(fixture.service, fixture.identity, save("fresh-after-retirement"))

    asyncio.run(run())


def test_role_city_and_epoch_fences_remain_distinct(tmp_path):
    async def run():
        async with setup(tmp_path, purpose="economy") as fixture:
            strategy = await invoke_document(fixture.service, fixture.identity, save("forbidden-strategy"))
            assert strategy.error == "policy_rejected"
            fixture.domain.roles.overrides["city2"] = "Main took this city"
            other = await invoke_document(fixture.service, fixture.identity, save("other-city", city="city2"))
            assert other.error == "policy_rejected"
            forged = fixture.identity.model_copy(
                update={"scope": fixture.identity.scope.model_copy(update={"epoch": "g2"})}
            )
            with pytest.raises(JournalConflict, match="EPOCH"):
                await invoke_document(fixture.service, forged, read_document(document="strategy"))
            assert await fixture.service.store.list_paths(fixture.prefix, limit=100) == []

    asyncio.run(run())


def test_checkpoint_and_corrupt_storage_are_integrity_failures(tmp_path):
    async def run():
        async with setup(tmp_path) as fixture:
            await invoke_document(fixture.service, fixture.identity, read_document(document="strategy"))
            fixture.domain.gateway.checkpoint_requested = True
            with pytest.raises(JournalConflict, match="CHECKPOINT"):
                await invoke_document(fixture.service, fixture.identity, save("during-checkpoint"))
            fixture.domain.gateway.checkpoint_requested = False
            # The underlying native store can represent arbitrary notes. A
            # malformed reserved document is storage corruption, not input repair.
            await fixture.service.store.write_with_metadata(
                fixture.prefix + "strategy.md", "broken JSON", expected_version=None, metadata={}
            )
            with pytest.raises(JournalConflict, match="CORRUPT"):
                await invoke_document(fixture.service, fixture.identity, read_document(document="strategy"))

    asyncio.run(run())


def test_semantic_models_reject_wrong_target_and_unknown_metadata():
    with pytest.raises(ValidationError):
        read_document(document="city_plan")
    with pytest.raises(ValidationError):
        read_document(document="strategy", city="city2")
    payload = save("valid").model_dump(mode="json")
    payload["expected_version"] = "invented"
    with pytest.raises(ValidationError):
        SaveAuthoredDocument.model_validate(payload)


def test_valid_semantic_document_exceeding_storage_size_is_definite_rejection(tmp_path):
    async def run():
        async with setup(tmp_path) as fixture:
            await invoke_document(fixture.service, fixture.identity, read_document(document="strategy"))
            payload = save("large").model_dump(mode="json")
            payload["content"]["assumptions"] = [{"text": "x" * 2000} for _ in range(5)]
            call = SaveAuthoredDocument.model_validate(payload)
            assert len(call.content.model_dump_json()) > 8000
            prepared = await invoke_document(fixture.service, fixture.identity, call)
            assert prepared.error == "policy_rejected" and not prepared.applied
            assert await fixture.service.store.read(fixture.prefix + "strategy.md", max_chars=8000) is None

    asyncio.run(run())


def test_same_native_operation_id_rejects_changed_semantics_and_replays_after_reread(tmp_path):
    async def run():
        async with setup(tmp_path) as fixture:
            await invoke_document(fixture.service, fixture.identity, read_document(document="strategy"))
            original = save("same-operation")
            first = await invoke_document(fixture.service, fixture.identity, original)
            await invoke_document(fixture.service, fixture.identity, read_document(document="strategy", model_step=3))
            replay = await invoke_document(
                fixture.service, fixture.identity, original.model_copy(update={"model_step": 4})
            )
            assert replay.replayed and replay.version == first.version
            changed = save("same-operation", assessment="Different plan", model_step=4)
            with pytest.raises(JournalConflict, match="OPERATION_CONFLICT"):
                await invoke_document(fixture.service, fixture.identity, changed)
            actual = await fixture.service.store.read(fixture.prefix + "strategy.md", max_chars=8000)
            assert (
                actual.version == first.version and json.loads(actual.content)["assessment"] == "Grow before expanding"
            )

    asyncio.run(run())
