"""Host receipts and native frozen inputs replace the Python command journal."""

import asyncio
import copy
import hashlib
import json
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

from civ_agent import memory_schema as tables
from civ_agent.civilization_state import StateConflict
from civ_agent.memory_store import RecordStore, encode
from civ_agent.observations import DetailQuery
from civ_agent.orchestration.contracts import GatewayCall, GatewayJob, PlayerRole, Scope
from civ_agent.orchestration.gateway_registry import GatewayRegistry
from civ_agent.orchestration.gateway_state import capture_job, restore_job
from civ_agent.orchestration.host_effect_contracts import (
    FrozenNativeCommand,
    NativeCommandApplication,
    NativeCommandFinish,
    NativeCommandStep,
)
from civ_agent.orchestration.host_effect_service import NativeCommandService
from civ_agent.orchestration.store import JournalConflict
from tests.domain_fixtures import command, managed, run_async
from tests.test_gateway_execution import executor
from tests.test_prepare_host_effect import counts, forbid_journal


def step(identity, *, call_id="native", model_step=1, operations=None, index=0):
    return NativeCommandStep(
        identity=identity,
        call=GatewayCall(
            call_id=call_id,
            model_step=model_step,
            decision={
                "kind": "act",
                "operations": operations or [command("SET_CITY_FOCUS", target="city2", selection="FoodFocus")],
            },
        ),
        index=index,
    )


async def owner(domain, identity, *, restore=False):
    async def resolve(record):
        if restore:
            job = restore_job(
                record.state, domain.host, job_id=identity.job, purpose=identity.purpose, epoch=identity.scope.epoch
            )
            domain.gateway.jobs[job.token] = job
            domain.roles.jobs[job.purpose] = job
            return job
        return domain.roles.jobs[identity.purpose]

    return GatewayRegistry(domain.gateway, game="game", player="p0", resolve=resolve)


@asynccontextmanager
async def native(tmp_path, *, purpose: PlayerRole = "economy", invalidated=False):
    async with managed(tmp_path) as domain:
        job = domain.economy if purpose == "economy" else domain.main
        _, state = await executor(domain, job)
        if invalidated:
            domain.gateway.invalidate_role_observation(job)
            state = capture_job(job, delivered_access=state.delivered_access)
        identity = GatewayJob(
            scope=Scope(game="game", player="p0", epoch="g1", activation="turn1"), job=job.id, purpose=purpose
        )
        original, probes = domain.host.tool, []

        async def host(name, arguments):
            if name == "player_receipt":
                probes.append(copy.deepcopy(arguments))
                return copy.deepcopy(domain.host.seen.get(arguments["requestId"], {"error": "NOT_ENACTED"}))
            result = await original(name, arguments)
            if name == "player_command":
                result["epoch"] = arguments["epoch"]
                result["hostDiagnostic"] = {"opaque": "Exact private host receipt"}
            return result

        domain.host.tool = host
        registry = await owner(domain, identity)
        await registry.register(identity, state, await domain.gateway.tools(job))
        scope = SimpleNamespace(
            **vars(domain), identity=identity, owner=registry, service=NativeCommandService(registry), probes=probes
        )
        try:
            yield scope
        finally:
            await scope.owner.close()


@run_async
async def test_failed_batch_dates_intent_from_retained_observation_without_restoring_credit(tmp_path):
    async with native(tmp_path, invalidated=True) as m:
        request = step(m.identity)
        result = await m.service.execute_local(m.identity, request.call, run_id="offline-invalidated-intent")
        assert result.result["receipts"][0]["reason"] == "FRESH_OBSERVATION_REQUIRED"
        assert not m.host.executed
        attempts = result.state.data["memory"]["unexecutedActionIntents"]
        assert len(attempts) == 1 and attempts[0]["sourceTurn"] == 0
        assert attempts[0]["currentIntent"] == "unverified"
        assert result.state.data["observation"] is None
        assert result.state.data["prior_role_observation"]["turn"] == 0
        assert result.state.data["role_refresh_required"] is True


@run_async
async def test_freeze_execute_apply_finish_uses_host_receipts_and_only_domain_outcome_projection(tmp_path, monkeypatch):
    async with native(tmp_path) as m:
        baseline = counts(m.memory)
        request = step(m.identity)
        with monkeypatch.context() as patch:
            for method in ("call", "queue", "receipt", "uncertain", "fail_run", "_call_record"):
                patch.setattr(m.memory, method, forbid_journal)
            prepared = await m.service.prepare(request)
            assert prepared.effect is not None and not m.host.executed
            frozen = FrozenNativeCommand.model_validate_json(prepared.effect.model_dump_json())
            receipt = await m.service.execute(frozen)
            original = copy.deepcopy(receipt)
            applied = await m.service.apply(NativeCommandApplication(effect=frozen, receipt=receipt))
            assert applied.stop is None and applied.result["done"]
            assert "requestId" not in applied.result and "epoch" not in applied.result
            assert receipt == original
            assert await m.service.execute(frozen) == original
            replay = await m.service.apply(NativeCommandApplication(effect=frozen, receipt=receipt))
            assert replay.result["done"] and len(m.host.executed) == 1
            finish = NativeCommandFinish(identity=m.identity, call=request.call)
            assert await m.service.finish(finish) == {"finished": True}
            assert await m.service.finish(finish) == {"finished": True}
        assert counts(m.memory) == baseline and m.owner.load(m.identity).pending is None
        assert m.owner.load(m.identity).state.pending_access is not None
        with m.memory.transaction() as db:
            decisions = RecordStore(db).all(categories={"decision"}, project=False)
        native_records = [record for record in decisions.values() if record["body"].get("native")]
        assert len(native_records) == 1
        body = native_records[0]["body"]
        assert body["native"]["operationId"] == receipt["requestId"]
        assert (
            body["native"]["requestSha256"]
            == hashlib.sha256(encode(["player_command", frozen.prepared.payload]).encode()).hexdigest()
        )
        assert body["native"]["receiptSha256"] == hashlib.sha256(encode(original).encode()).hexdigest()
        assert body["facts"] == {"actionSummary": receipt["actionSummary"]}
        assert body["outcome"] == "confirmed" and "result" not in body
        assert "Exact private host receipt" not in json.dumps(body)


@run_async
async def test_lost_native_reply_restarts_from_frozen_envelope_and_queries_original_receipt(tmp_path):
    async with native(tmp_path) as m:
        request = step(m.identity)
        preparation = await m.service.prepare(request)
        assert preparation.effect is not None
        frozen = FrozenNativeCommand.model_validate_json(preparation.effect.model_dump_json())
        original_payload = copy.deepcopy(frozen.prepared.payload)
        baseline = counts(m.memory)

        async def crash(effect, receipt):
            raise asyncio.CancelledError()

        m.service.reply_checkpoint = crash
        with pytest.raises(asyncio.CancelledError):
            await m.service.execute(frozen)
        assert len(m.host.executed) == 1
        m.host.revision = "a-new-position-after-the-lost-reply"
        await m.owner.close()
        m.owner = await owner(m, m.identity, restore=True)
        m.service = NativeCommandService(m.owner)
        recovered = await m.service.execute(frozen)
        assert recovered == m.host.seen[original_payload["requestId"]]
        assert m.host.executed == [original_payload]
        assert m.probes == [original_payload, original_payload]
        applied = await m.service.apply(NativeCommandApplication(effect=frozen, receipt=recovered))
        assert applied.result["done"] and counts(m.memory) == baseline
        await m.service.finish(NativeCommandFinish(identity=m.identity, call=request.call))
        assert m.owner.load(m.identity).pending is None


@run_async
async def test_frozen_envelope_and_raw_receipt_are_bound_against_tampering(tmp_path):
    async with native(tmp_path) as m:
        preparation = await m.service.prepare(step(m.identity))
        frozen = preparation.effect
        assert frozen is not None
        for changed in ("revision", "requestId", "epoch", "target"):
            tampered = frozen.model_copy(deep=True)
            tampered.prepared.payload[changed] = "tampered"
            with pytest.raises(JournalConflict, match="BINDING_CONFLICT"):
                await m.service.execute(tampered)
        receipt = await m.service.execute(frozen)
        for changed in ("requestId", "epoch", "kind"):
            with pytest.raises(JournalConflict, match="RECEIPT_BINDING_CONFLICT"):
                await m.service.apply(NativeCommandApplication(effect=frozen, receipt={**receipt, changed: "tampered"}))
        assert len(m.host.executed) == 1


@run_async
async def test_pending_native_batch_blocks_fresh_calls_legacy_mode_and_late_finish(tmp_path):
    async with native(tmp_path) as m:
        request = step(m.identity)
        frozen = (await m.service.prepare(request)).effect
        assert frozen is not None
        newer = step(m.identity, call_id="newer", model_step=2)
        with pytest.raises(JournalConflict, match="RECONCILIATION_REQUIRED"):
            await m.service.prepare(newer)
        with pytest.raises(JournalConflict, match="NATIVE_RECONCILIATION_REQUIRED"):
            await m.owner(m.identity, request.call.call_id, request.call.model_step, request.call.decision)
        with pytest.raises(JournalConflict, match="RECONCILIATION_REQUIRED"):
            await m.owner.retire(m.identity)
        raw = await m.service.execute(frozen)
        await m.service.apply(NativeCommandApplication(effect=frozen, receipt=raw))
        old_finish = NativeCommandFinish(identity=m.identity, call=request.call)
        await m.service.finish(old_finish)
        await m.service.prepare(newer)
        with pytest.raises(JournalConflict, match="RECONCILIATION_REQUIRED"):
            await m.service.finish(old_finish)
        assert m.owner.load(m.identity).pending.call == newer.call


@run_async
async def test_changed_batch_target_stops_remaining_native_commands_without_second_dispatch(tmp_path):
    async with native(tmp_path) as m:
        operations = [
            command("SET_CITY_FOCUS", target="city2", selection=value) for value in ("FoodFocus", "GoldFocus")
        ]
        request = step(m.identity, operations=operations)
        frozen = (await m.service.prepare(request)).effect
        assert frozen is not None
        raw = await m.service.execute(frozen)
        m.host.view["cities"][1]["constructionQueue"] = ["Market"]
        await m.service.apply(NativeCommandApplication(effect=frozen, receipt=raw))
        stopped = await m.service.prepare(request.model_copy(update={"index": 1}))
        assert stopped.effect is None and stopped.stop == "TARGET_CHANGED"
        assert len(m.host.executed) == 1


@run_async
async def test_sibling_source_reads_do_not_grant_native_command_inspection_credit(tmp_path):
    async with native(tmp_path, purpose="gameplay") as m:
        destination = {"x": 3, "y": 1}
        operation = command("SET_UNIT_DESTINATION", target="unit1", destination=destination)
        # A sibling read activity has cached this detail, but its result has not
        # reached another provider response. The registration's delivered set wins.
        m.main.details_read.append(
            DetailQuery(tool="movement_preview", arguments={"target": "unit1", "destination": destination})
        )
        result = await m.service.prepare(step(m.identity, operations=[operation]))
        assert result.effect is None and result.stop == "FRESH_INSPECTION_REQUIRED"
        assert not m.host.executed


@run_async
async def test_sibling_tool_call_cannot_use_the_first_receipts_refreshed_observation(tmp_path):
    async with native(tmp_path) as m:
        first = step(m.identity)
        frozen = (await m.service.prepare(first)).effect
        assert frozen is not None
        raw = await m.service.execute(frozen)
        await m.service.apply(NativeCommandApplication(effect=frozen, receipt=raw))
        await m.service.finish(NativeCommandFinish(identity=m.identity, call=first.call))
        sibling = step(m.identity, call_id="sibling-in-same-provider-response")
        stopped = await m.service.prepare(sibling)
        assert stopped.effect is None and stopped.stop == "FRESH_OBSERVATION_REQUIRED"
        await m.service.finish(NativeCommandFinish(identity=m.identity, call=sibling.call))
        # Only a subsequent provider response proves that the refresh was supplied.
        next_response = step(m.identity, call_id="after-delivery", model_step=2)
        assert (await m.service.prepare(next_response)).effect is not None
        assert len(m.host.executed) == 1


@pytest.mark.parametrize(
    "field,value", [("player", "another"), ("game", "another"), ("epoch", "g2"), ("activation", "another")]
)
@run_async
async def test_registration_scope_cannot_be_changed_before_native_dispatch(tmp_path, field, value):
    async with native(tmp_path) as m:
        wrong = m.identity.model_copy(update={"scope": m.identity.scope.model_copy(update={field: value})})
        with pytest.raises(JournalConflict):
            await m.service.prepare(step(wrong))
        assert not m.host.executed


@run_async
async def test_restored_native_connection_verifies_player_before_receipt_lookup_or_dispatch(tmp_path):
    async with native(tmp_path) as m:
        frozen = (await m.service.prepare(step(m.identity))).effect
        assert frozen is not None
        await m.owner.close()
        m.owner = await owner(m, m.identity, restore=True)
        m.service = NativeCommandService(m.owner)
        m.host.civilization = "Wrong player"
        with pytest.raises(StateConflict, match="PLAYER_CREDENTIAL_MISMATCH"):
            await m.service.execute(frozen)
        assert not m.host.executed and not m.probes


@run_async
async def test_raw_receipt_survives_failed_refresh_without_call_journal_or_effect_retry(tmp_path, monkeypatch):
    async with native(tmp_path) as m:
        frozen = (await m.service.prepare(step(m.identity))).effect
        assert frozen is not None
        receipt = await m.service.execute(frozen)
        original, baseline, host = copy.deepcopy(receipt), counts(m.memory), m.host.tool

        async def offline(name, arguments):
            if name == "player_turn":
                raise ValueError("malformed refresh")
            return await host(name, arguments)

        monkeypatch.setattr(m.host, "tool", offline)
        for method in ("call", "queue", "receipt", "uncertain", "fail_run", "_call_record"):
            monkeypatch.setattr(m.memory, method, forbid_journal)
        reply = await m.service.apply(NativeCommandApplication(effect=frozen, receipt=receipt))
        assert reply.stop == "FRESH_OBSERVATION_REQUIRED"
        assert reply.result["done"] and reply.result["stateUpdate"]["kind"] == "unavailable"
        assert receipt == original and len(m.host.executed) == 1 and counts(m.memory) == baseline
        assert m.owner.load(m.identity).state.data["observation"] is None


@run_async
async def test_domain_apply_ack_loss_replays_projection_once_and_reapplies_role_state(tmp_path, monkeypatch):
    async with native(tmp_path) as m:
        request = step(m.identity)
        frozen = (await m.service.prepare(request)).effect
        assert frozen is not None
        receipt = await m.service.execute(frozen)
        original_save = m.service._save_context

        def lost(*args, **kwargs):
            raise OSError("Lost apply acknowledgement before context commit")

        monkeypatch.setattr(m.service, "_save_context", lost)
        with pytest.raises(OSError):
            await m.service.apply(NativeCommandApplication(effect=frozen, receipt=receipt))
        monkeypatch.setattr(m.service, "_save_context", original_save)
        applied = await m.service.apply(NativeCommandApplication(effect=frozen, receipt=receipt))
        assert applied.result["done"] and len(m.host.executed) == 1
        with m.memory.transaction() as db:
            records = RecordStore(db).all(categories={"decision"}, project=False)
            pending = db.execute(tables.outbox.select()).all()
        assert len([r for r in records.values() if r["body"].get("native")]) == 1 and not pending


@run_async
async def test_existing_legacy_batch_fence_cannot_be_reinterpreted_as_native_dispatch(tmp_path):
    async with native(tmp_path) as m:
        request = step(m.identity)
        m.owner._claim(m.identity, request.call)
        with pytest.raises(JournalConflict, match="RECONCILIATION_REQUIRED"):
            await m.service.prepare(request)
        assert m.owner.load(m.identity).pending.execution == "gateway-v1" and not m.host.executed


@run_async
async def test_frozen_command_is_not_replayed_or_applied_after_epoch_changes(tmp_path):
    async with native(tmp_path) as m:
        request = step(m.identity)
        frozen = (await m.service.prepare(request)).effect
        assert frozen is not None
        with m.memory.transaction() as db:
            m.memory._set(db, "epoch", "g2")
        with pytest.raises(JournalConflict, match="EPOCH_CONFLICT"):
            await m.service.execute(frozen)
        with pytest.raises(JournalConflict, match="EPOCH_CONFLICT"):
            await m.service.apply(NativeCommandApplication(effect=frozen, receipt={"error": "REJECTED"}))
        with pytest.raises(JournalConflict, match="EPOCH_CONFLICT"):
            await m.service.finish(NativeCommandFinish(identity=m.identity, call=request.call))
        assert not m.host.executed and not m.probes


@run_async
async def test_uncertain_host_lookup_never_authorizes_native_command_dispatch(tmp_path, monkeypatch):
    async with native(tmp_path) as m:
        frozen = (await m.service.prepare(step(m.identity))).effect
        assert frozen is not None
        host = m.host.tool

        async def uncertain(name, arguments):
            return {"error": "COMMUNICATION_UNAVAILABLE"} if name == "player_receipt" else await host(name, arguments)

        monkeypatch.setattr(m.host, "tool", uncertain)
        with pytest.raises(JournalConflict, match="NATIVE_RECEIPT_UNCERTAIN"):
            await m.service.execute(frozen)
        assert not m.host.executed and m.owner.load(m.identity).pending is not None


@run_async
async def test_local_command_saves_exact_envelope_before_send_and_clears_after_apply(tmp_path, monkeypatch):
    async with native(tmp_path) as m:
        request = step(m.identity)
        execute = m.service.execute
        saved = []

        async def inspect(effect):
            with m.memory.transaction() as db:
                body = m.memory._meta(db, "localNativeEffect:" + m.service.child(effect.step))
            assert FrozenNativeCommand.model_validate(body) == effect
            saved.append(body)
            return await execute(effect)

        monkeypatch.setattr(m.service, "execute", inspect)
        result = await m.service.execute_local(m.identity, request.call, run_id="sdk-run")
        assert result.result["receipts"][0]["status"] == "confirmed"
        assert len(saved) == len(m.host.executed) == 1
        assert m.owner.load(m.identity).pending is None
        with m.memory.transaction() as db:
            assert m.memory._meta(db, "localNativeEffect:" + m.service.child(request)) is None


@run_async
async def test_retired_local_command_cannot_probe_or_send_from_an_old_callback(tmp_path):
    from civ_agent.orchestration.gateway_registry import job_key

    async with native(tmp_path) as m:
        frozen = (await m.service.prepare(step(m.identity))).effect
        assert frozen is not None
        with m.memory.transaction() as db:
            record = m.owner._load(db, m.identity)
            record.retired = True
            m.memory._set(db, job_key(m.identity), record.model_dump(mode="json"))
        with pytest.raises(JournalConflict, match="GATEWAY_JOB_RETIRED"):
            await m.service.execute(frozen)
        assert not m.host.executed and not m.probes
