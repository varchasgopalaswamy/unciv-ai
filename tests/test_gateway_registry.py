import asyncio
import json
import weakref

import pytest

from civ_agent import memory_schema as tables
from civ_agent.civilization_state import CivilizationState, StateConflict
from civ_agent.orchestration.contracts import GatewayDependencies, GatewayJob, Scope
from civ_agent.orchestration.gateway_execution import GatewayExecution
from civ_agent.orchestration.gateway_registry import GatewayRegistry, job_key
from civ_agent.orchestration.gateway_state import restore_job
from civ_agent.orchestration.store import JournalConflict
from tests.domain_fixtures import command, managed, run_async
from tests.test_gateway_execution import executor


def identity(m):
    return GatewayJob(
        scope=Scope(game="game", player="p0", epoch="g1", activation="turn1"),
        job=m.economy.id,
        purpose="economy",
    )


def action(selection="FoodFocus"):
    return {"kind": "act", "operations": [command("SET_CITY_FOCUS", target="city2", selection=selection)]}


async def registry(m, *, restore=False):
    binding = identity(m)

    async def resolve(record):
        if not restore:
            return m.economy
        job = restore_job(record.state, m.host, job_id=binding.job, purpose=binding.purpose, epoch=binding.scope.epoch)
        m.gateway.jobs[job.token] = job
        m.roles.jobs[job.purpose] = job
        return job

    return GatewayRegistry(m.gateway, game="game", player="p0", resolve=resolve)


@run_async
async def test_single_owner_registration_scopes_and_duplicate_initialization(tmp_path):
    async with managed(tmp_path) as m:
        _, state = await executor(m)
        binding = identity(m)
        owner = await registry(m)
        try:
            with pytest.raises(JournalConflict, match="ALREADY_RUNNING"):
                await registry(m)
            catalogue = await m.gateway.tools(m.economy)
            await owner.register(binding, state, catalogue)
            await owner.register(binding, state, catalogue)
            with pytest.raises(JournalConflict, match="REGISTRATION_CHANGED"):
                await owner.register(binding, state.model_copy(update={"delivery_step": 5}), catalogue)
            for field, value in (("player", "p1"), ("game", "other"), ("epoch", "g2"), ("activation", "another")):
                changed = binding.model_copy(update={"scope": binding.scope.model_copy(update={field: value})})
                with pytest.raises(JournalConflict):
                    owner.load(changed)
            deps = GatewayDependencies(**binding.model_dump())
            first = await owner(deps, "action1", 1, action())
            assert isinstance(first, GatewayExecution)
            assert await owner(deps, "action1", 1, action()) == first
            with pytest.raises(JournalConflict, match="OPERATION_CHANGED"):
                await owner(deps, "action1", 1, action("GoldFocus"))
            assert len(m.host.executed) == 1
        finally:
            await owner.close()


@run_async
async def test_context_and_domain_job_resume_after_notebook_reopen(tmp_path):
    async with managed(tmp_path) as m:
        _, state = await executor(m)
        binding = identity(m)
        deps = GatewayDependencies(**binding.model_dump())
        owner = await registry(m)
        try:
            await owner.register(binding, state, await m.gateway.tools(m.economy))
            first = await owner(deps, "action1", 1, action())
            assert isinstance(first, GatewayExecution)
        finally:
            await owner.close()
        reopened = CivilizationState(m.memory.root, "Rome")
        try:
            owner = await registry(m, restore=True)
            try:
                assert owner.load(binding).state == first.state
                assert await owner(deps, "action1", 1, action()) == first
                second = await owner(deps, "action2", 2, action("GoldFocus"))
                assert isinstance(first, GatewayExecution) and isinstance(second, GatewayExecution)
                assert second.result["receipts"][0]["status"] == "confirmed"
                assert owner.load(binding).state == second.state
                assert await owner(deps, "action1", 1, action()) == first
                assert owner.load(binding).state == second.state
                assert len(m.host.executed) == 2
            finally:
                await owner.close()
        finally:
            reopened.engine.dispose()


@run_async
async def test_head_commit_gap_blocks_new_calls_until_original_receipt_is_replayed(tmp_path, monkeypatch):
    async with managed(tmp_path) as m:
        _, state = await executor(m)
        binding = identity(m)
        deps = GatewayDependencies(**binding.model_dump())
        owner = await registry(m)
        try:
            await owner.register(binding, state, await m.gateway.tools(m.economy))

            def interrupted(*args):
                raise OSError("coordinator exited before head commit")

            monkeypatch.setattr(owner, "_finish", interrupted)
            with pytest.raises(OSError):
                await owner(deps, "action1", 1, action())
            assert len(m.host.executed) == 1
            assert owner.load(binding).pending is not None
            with pytest.raises(JournalConflict, match="RECONCILIATION_REQUIRED"):
                await owner(deps, "other", 2, action("GoldFocus"))
            with pytest.raises(JournalConflict, match="RECONCILIATION_REQUIRED"):
                await owner.retire(binding)
        finally:
            await owner.close()
        owner = await registry(m, restore=True)
        try:
            recovered = await owner(deps, "action1", 1, action())
            assert isinstance(recovered, GatewayExecution)
            assert recovered.result["receipts"][0]["status"] == "confirmed"
            assert owner.load(binding).state == recovered.state and owner.load(binding).pending is None
            assert len(m.host.executed) == 1
            next_call = await owner(deps, "next", 2, action("GoldFocus"))
            assert next_call.result["receipts"][0]["status"] == "confirmed"
            assert len(m.host.executed) == 2
        finally:
            await owner.close()


@run_async
async def test_serialized_duplicates_share_one_domain_execution(tmp_path):
    async with managed(tmp_path) as m:
        _, state = await executor(m)
        binding = identity(m)
        deps = GatewayDependencies(**binding.model_dump())
        owner = await registry(m)
        try:
            await owner.register(binding, state, await m.gateway.tools(m.economy))
            results = await asyncio.gather(*(owner(deps, "action1", 1, action()) for _ in range(3)))
            assert results[0] == results[1] == results[2]
            assert len(m.host.executed) == 1
            assert owner.load(binding).pending is None
        finally:
            await owner.close()


@pytest.mark.parametrize("uncertain", [False, True])
@run_async
async def test_interrupted_call_fences_new_work_and_reconciles_original_identity(tmp_path, uncertain):
    async with managed(tmp_path) as m:
        _, state = await executor(m)
        binding = identity(m)
        deps = GatewayDependencies(**binding.model_dump())
        original = m.host.tool

        async def lost(name, arguments):
            if name == "player_receipt" and uncertain:
                return {"error": "NOT_ENACTED"}
            result = await original(name, arguments)
            if name == "player_command":
                raise asyncio.CancelledError()
            return result

        m.host.tool = lost
        owner = await registry(m)
        try:
            await owner.register(binding, state, await m.gateway.tools(m.economy))
            with pytest.raises(asyncio.CancelledError):
                await owner(deps, "lost", 1, action())
        finally:
            await owner.close()
        owner = await registry(m, restore=True)
        try:
            with pytest.raises(JournalConflict, match="RECONCILIATION_REQUIRED"):
                await owner(deps, "new", 2, action("GoldFocus"))
            if uncertain:
                with pytest.raises(StateConflict, match="DECISION_EXECUTION_UNCERTAIN"):
                    await owner.recover(binding)
                assert owner.load(binding).pending is not None
            else:
                recovered = await owner.recover(binding)
                assert recovered is not None and recovered.result["refreshRequired"]
                assert owner.load(binding).pending is None
                assert await owner.recover(binding) is None
            assert len(m.host.executed) == 1
        finally:
            await owner.close()


@run_async
async def test_missing_registration_cannot_reset_a_job_with_existing_native_effects(tmp_path):
    async with managed(tmp_path) as m:
        runner, state = await executor(m)
        await runner.execute("orphan", action(), model_step=1)
        owner = await registry(m)
        try:
            with pytest.raises(JournalConflict, match="REGISTRATION_MISSING"):
                await owner.register(identity(m), state, await m.gateway.tools(m.economy))
            assert len(m.host.executed) == 1
        finally:
            await owner.close()


@run_async
async def test_retired_jobs_keep_duplicate_receipts_and_reject_new_effects(tmp_path):
    async with managed(tmp_path) as m:
        _, state = await executor(m)
        binding = identity(m)
        owner = await registry(m)
        try:
            await owner.register(binding, state, await m.gateway.tools(m.economy))
            first = await owner(binding, "action1", 1, action())
            await owner.retire(binding)
            await owner.retire(binding)
            assert owner.load(binding).retired and job_key(binding) not in owner.jobs
            assert m.economy.closed and m.economy.token not in m.gateway.jobs
            assert (await owner(binding, "action1", 1, action())).result == first.result
            with pytest.raises(JournalConflict, match="JOB_RETIRED"):
                await owner(binding, "new", 2, action())
            with pytest.raises(JournalConflict, match="OPERATION_CHANGED"):
                await owner(binding, "action1", 1, action("GoldFocus"))
            assert len(m.host.executed) == 1
            with m.memory.transaction() as db:
                head = m.memory._meta(db, job_key(binding))
                assert "archive" in head and "state" not in head
                body = json.loads(db.execute(tables.decision_journal.select()).mappings().first()["body"])
                assert "gatewayState" not in body and "result" in body
            archive = m.memory.root / "native-archives" / (head["archive"] + ".json.gz")
            assert archive.stat().st_mode & 0o777 == 0o600
        finally:
            await owner.close()
        owner = await registry(m, restore=True)
        try:
            assert (await owner(binding, "action1", 1, action())).result == first.result
            archive.write_bytes(b"broken archive")
            with pytest.raises((OSError, JournalConflict)):
                owner.load(binding)
        finally:
            await owner.close()


@run_async
async def test_retirement_retries_after_archive_write_before_head_commit(tmp_path, monkeypatch):
    async with managed(tmp_path) as m:
        _, state = await executor(m)
        binding = identity(m)
        owner = await registry(m)
        try:
            await owner.register(binding, state, await m.gateway.tools(m.economy))
            first = await owner(binding, "action1", 1, action())
            save = m.memory._set

            def fail(db, key, value):
                if key == job_key(binding) and "archive" in value:
                    raise OSError("interrupted after archive write")
                return save(db, key, value)

            monkeypatch.setattr(m.memory, "_set", fail)
            with pytest.raises(OSError):
                await owner.retire(binding)
            assert not owner.load(binding).retired
            with m.memory.transaction() as db:
                body = json.loads(db.execute(tables.decision_journal.select()).mappings().first()["body"])
                assert "gatewayState" in body
            monkeypatch.setattr(m.memory, "_set", save)
            await owner.retire(binding)
            assert owner.load(binding).retired
            assert (await owner(binding, "action1", 1, action())).result == first.result
            assert len(m.host.executed) == 1
        finally:
            await owner.close()


@run_async
async def test_retirement_bounds_loaded_snapshots_and_preserves_each_receipt(tmp_path, monkeypatch):
    from civ_agent.orchestration import gateway_registry

    async with managed(tmp_path) as m:
        _, state = await executor(m)
        binding = identity(m)
        owner = await registry(m)
        try:
            await owner.register(binding, state, await m.gateway.tools(m.economy))
            first = await owner(binding, "action1", 1, action())
            with m.memory.transaction() as db:
                original = dict(db.execute(tables.decision_journal.select()).mappings().one())
                body = json.loads(original["body"])
                body["retirementProbe"] = True
                for index in range(16):
                    db.execute(
                        tables.decision_journal.insert().values(
                            **{**original, "id": f"probe-{index}", "body": json.dumps(body)}
                        )
                    )

            class TrackedBody(dict):
                pass

            loaded = []
            peak = 0
            loads = json.loads

            def tracked_loads(*args, **kwargs):
                nonlocal peak
                value = loads(*args, **kwargs)
                if isinstance(value, dict) and value.get("retirementProbe"):
                    value = TrackedBody(value)
                    loaded.append(weakref.ref(value))
                    peak = max(peak, sum(reference() is not None for reference in loaded))
                return value

            monkeypatch.setattr(gateway_registry.json, "loads", tracked_loads)
            await owner.retire(binding)
            assert peak <= 2, "Retirement must not retain the full job's parsed delivery snapshots."
            assert all(reference() is None for reference in loaded)
            with m.memory.transaction() as db:
                for raw in db.scalars(
                    tables.decision_journal.select().with_only_columns(tables.decision_journal.c.body)
                ):
                    saved = loads(raw)
                    assert saved["result"] == body["result"]
                    assert (
                        not {"gatewayState", "deliveredAccess", "receiptAccess", "receipts", "dispatch"} & saved.keys()
                    )
            assert (await owner(binding, "action1", 1, action())).result == first.result
            assert len(m.host.executed) == 1
        finally:
            await owner.close()


@run_async
@pytest.mark.parametrize("unfinished", ["call", "journal_null", "journal_missing"])
async def test_retirement_refuses_unsettled_work_before_completing_the_job(tmp_path, unfinished):
    async with managed(tmp_path) as m:
        _, state = await executor(m)
        binding = identity(m)
        owner = await registry(m)
        try:
            await owner.register(binding, state, await m.gateway.tools(m.economy))
            if unfinished == "call":
                m.memory.call(binding.job, "interrupted-read", "player_turn", {})
            else:
                await owner(binding, "action1", 1, action())
                with m.memory.transaction() as db:
                    row = db.execute(tables.decision_journal.select()).mappings().one()
                    body = json.loads(row["body"])
                    if unfinished == "journal_null":
                        body["result"] = None
                    else:
                        body.pop("result")
                    db.execute(
                        tables.decision_journal.update()
                        .where(tables.decision_journal.c.id == row["id"])
                        .values(body=json.dumps(body))
                    )
            with pytest.raises(JournalConflict, match="UNSETTLED_JOB"):
                await owner.retire(binding)
            assert not owner.load(binding).retired
            with m.memory.transaction() as db:
                row = db.execute(tables.jobs.select().where(tables.jobs.c.id == binding.job)).mappings().one()
                assert row["status"] == "RUNNING"
        finally:
            await owner.close()
