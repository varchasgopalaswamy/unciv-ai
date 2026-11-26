"""Single-owner trusted gateway with durable job heads and pending-call fences.

This component belongs in the player coordinator container, never in model
workers. Its lifetime lock and serialized dispatch are required: a task queue or
an activity timeout cannot establish exclusive ownership of live domain state.
"""

import asyncio
import fcntl
import gzip
import json
import os
from collections.abc import Awaitable, Callable
from typing import Literal

from civ_agent import memory_schema as tables
from civ_agent.decision_contracts import compile_decision_contract
from civ_agent.decision_journal import journal
from civ_agent.execution_windows import ExecutionWindowScheduler, operation_cost
from civ_agent.orchestration.contracts import Digest, GatewayCall, GatewayJob, GatewayReceipt, canonical, digest
from civ_agent.orchestration.gateway_execution import GatewayExecution, NativeGatewayExecutor, operation_identity
from civ_agent.orchestration.gateway_state import GatewayState
from civ_agent.orchestration.store import JournalConflict
from civ_agent.proposal_gateway import JobAccess, ProposalGateway
from civ_agent.protocol import JsonObject
from civ_agent.schemas import BoundaryModel
from civ_agent.tls import private_directory, regular_file, write_private


class PendingCall(BoundaryModel):
    call: GatewayCall
    fingerprint: Digest
    execution: Literal["gateway-v1", "native-command-v1"] = "gateway-v1"


class RegisteredJob(BoundaryModel):
    version: Literal[1] = 1
    identity: GatewayJob
    catalogue: list[JsonObject]
    initial_digest: Digest
    state: GatewayState
    pending: PendingCall | None = None
    retired: bool = False


class RetiredHead(BoundaryModel):
    version: Literal[1] = 1
    identity: GatewayJob
    archive: Digest


def job_key(identity: GatewayJob):
    # Job IDs must never be reused within an epoch across activations/scopes.
    # Native operation identities and the domain notebook use the same invariant.
    return "nativeGatewayJob:" + digest([identity.job, identity.scope.epoch])


class GatewayRegistry:
    async def startup_delivery(self, identity: GatewayJob, prepared, *, delivered: bool):
        """Local SDK hook: preparation is private until its exact input was sent.

        A new run resets presentation credit, while observations, raw receipts
        and all native effect identities remain in their existing journals.
        """
        from civ_agent.prepared_role_context import PreparedDelivery, empty_prepared_access

        credit = PreparedDelivery.model_validate(prepared)

        def apply():
            with self.gateway.memory.transaction() as db:
                record = self._load(db, identity)
                if record.pending is not None or record.retired:
                    raise JournalConflict("GATEWAY_STARTUP_DELIVERY_CONFLICT")
                record.state = record.state.model_copy(
                    update={
                        "delivered_access": credit.access if delivered else empty_prepared_access(),
                        "military_review": credit.militaryReview if delivered else None,
                        "military_targets": credit.militaryTargets if delivered else [],
                        "pending_access": None,
                        "pending_military_review": None,
                        "pending_military_targets": [],
                    }
                )
                self.gateway.memory._set(db, job_key(identity), record.model_dump(mode="json"))

        async with self.dispatch:
            await self.gateway.io.call(apply)

    def __init__(
        self,
        gateway: ProposalGateway,
        *,
        game: str,
        player: str,
        resolve: Callable[[RegisteredJob], Awaitable[JobAccess]],
        refresh: Callable[[JobAccess], Awaitable[None]] | None = None,
        resolve_receipts: Callable[[RegisteredJob], Awaitable[JobAccess]] | None = None,
    ):
        self.gateway, self.game, self.player, self.resolve = gateway, game, player, resolve
        self.refresh = refresh
        self.resolve_receipts = resolve_receipts
        self.dispatch = asyncio.Lock()
        self.windows = ExecutionWindowScheduler(game=game, player=player)
        self.gateway.execution_windows = self.windows
        self.jobs: dict[str, JobAccess] = {}
        self.closed = False
        self.lock = os.open(gateway.memory.root / ".native-gateway.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        try:
            fcntl.flock(self.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            # Bind even an empty notebook to one trusted game/player identity.
            with gateway.memory.transaction() as db:
                expected = {"version": 1, "game": game, "player": player}
                owner = gateway.memory._meta(db, "nativeGatewayOwner")
                if owner is not None and owner != expected:
                    raise JournalConflict("GATEWAY_OWNER_CONFLICT")
                gateway.memory._set(db, "nativeGatewayOwner", expected)
        except BaseException as error:
            os.close(self.lock)
            if isinstance(error, BlockingIOError):
                raise JournalConflict("GATEWAY_ALREADY_RUNNING") from None
            raise

    async def close(self):
        await self.windows.close()
        async with self.dispatch:
            if not self.closed:
                self.closed = True
                os.close(self.lock)

    def _check(self, db, identity: GatewayJob):
        if self.closed:
            raise JournalConflict("GATEWAY_REGISTRY_CLOSED")
        if (identity.scope.game, identity.scope.player) != (self.game, self.player):
            raise JournalConflict("GATEWAY_OWNER_CONFLICT")
        if self.gateway.memory._meta(db, "epoch") != identity.scope.epoch:
            raise JournalConflict("GATEWAY_EPOCH_CONFLICT")

    def _load(self, db, identity: GatewayJob) -> RegisteredJob:
        self._check(db, identity)
        data = self.gateway.memory._meta(db, job_key(identity))
        if data is None:
            raise JournalConflict("GATEWAY_JOB_UNREGISTERED")
        if "archive" in data:
            head = RetiredHead.model_validate(data)
            if head.identity != identity:
                raise JournalConflict("GATEWAY_JOB_BINDING_CONFLICT")
            path = self.gateway.memory.root / "native-archives" / (head.archive + ".json.gz")
            regular_file(path, private=True)
            data = json.loads(gzip.decompress(path.read_bytes()))
            if digest(data) != head.archive or data.get("retired") is not True:
                raise JournalConflict("GATEWAY_ARCHIVE_CORRUPT")
        record = RegisteredJob.model_validate(data)
        if record.identity != identity:
            raise JournalConflict("GATEWAY_JOB_BINDING_CONFLICT")
        return record

    def _resume(self, db, record: RegisteredJob):
        row = db.execute(tables.jobs.select().where(tables.jobs.c.id == record.identity.job)).mappings().first()
        if (
            row is None
            or (row["epoch"], row["purpose"]) != (record.identity.scope.epoch, record.identity.purpose)
            or row["status"] not in {"RUNNING", "INTERRUPTED"}
            or record.state.data["closed"]
        ):
            raise JournalConflict("GATEWAY_DOMAIN_JOB_EXPIRED")
        # The legacy notebook marks RUNNING jobs interrupted on process startup.
        # Only a registered native job may resume its existing snapshot, versions
        # and receipts. start_job would allocate a fresh snapshot and lose credit.
        if row["status"] == "INTERRUPTED":
            db.execute(tables.jobs.update().where(tables.jobs.c.id == record.identity.job).values(status="RUNNING"))

    def load(self, identity: GatewayJob) -> RegisteredJob:
        """Private coordinator lookup. Never expose this record as a model tool."""
        with self.gateway.memory.transaction() as db:
            return self._load(db, identity)

    async def register(self, identity: GatewayJob, state: GatewayState, catalogue: list[JsonObject]):
        if (state.job, state.purpose, state.epoch) != (identity.job, identity.purpose, identity.scope.epoch):
            raise JournalConflict("GATEWAY_STATE_IDENTITY_CONFLICT")
        record = RegisteredJob(
            identity=identity,
            catalogue=catalogue,
            initial_digest=digest([state.model_dump(mode="json"), catalogue]),
            state=state,
        )

        def save():
            with self.gateway.memory.transaction() as db:
                self._check(db, identity)
                prior = self.gateway.memory._meta(db, job_key(identity))
                if prior is not None:
                    existing = self._load(db, identity)
                    if existing.initial_digest != record.initial_digest:
                        raise JournalConflict("GATEWAY_REGISTRATION_CHANGED")
                    return
                # Losing the registration while retaining prior native calls
                # indicates mismatched state restoration, not a fresh job.
                existing_call = db.execute(
                    tables.decision_journal.select()
                    .where(
                        tables.decision_journal.c.job == identity.job,
                        tables.decision_journal.c.epoch == identity.scope.epoch,
                        tables.decision_journal.c.id.like("native:%"),
                    )
                    .limit(1)
                ).first()
                if existing_call is not None:
                    raise JournalConflict("GATEWAY_REGISTRATION_MISSING")
                self.gateway.memory._set(db, job_key(identity), record.model_dump(mode="json"))

        async with self.dispatch:
            await self.gateway.io.call(save)

    def _claim(self, identity: GatewayJob, call: GatewayCall) -> tuple[RegisteredJob, bool, GatewayReceipt | None]:
        with self.gateway.memory.transaction() as db:
            record = self._load(db, identity)
            contract = compile_decision_contract(identity.purpose, record.catalogue)
            fingerprint = digest(contract.parse(contract.encode(call.decision)))
            identifier = operation_identity(identity.job, identity.scope.epoch, call.model_step, call.call_id)
            # Look up without a nested transaction. Completed duplicates can
            # return their own immutable result while another call is pending.
            saved = (
                db.execute(tables.decision_journal.select().where(tables.decision_journal.c.id == identifier))
                .mappings()
                .first()
            )
            if saved is not None:
                body = json.loads(saved["body"])
                if digest(body["decision"]) != fingerprint:
                    raise JournalConflict("GATEWAY_OPERATION_CHANGED")
                if record.retired and body.get("result") is not None:
                    return record, False, GatewayReceipt(result=body["result"])
                if record.retired:
                    raise JournalConflict("GATEWAY_JOB_RETIRED")
                if body.get("gatewayState") is not None:
                    pending = record.pending
                    advance = pending is not None and (pending.call.call_id, pending.call.model_step) == (
                        call.call_id,
                        call.model_step,
                    )
                    if advance:
                        # Bootstrap a fresh transport from the committed command
                        # context, even if the job head acknowledgement was lost.
                        record.state = GatewayState.model_validate(body["gatewayState"])
                    return record, advance, None
            if record.retired:
                raise JournalConflict("GATEWAY_JOB_RETIRED")
            if call.model_step < record.state.delivery_step:
                raise JournalConflict("GATEWAY_MODEL_STEP_REWOUND")
            pending = PendingCall(call=call, fingerprint=fingerprint)
            if record.pending is not None:
                if record.pending.execution != "gateway-v1":
                    raise JournalConflict("GATEWAY_NATIVE_RECONCILIATION_REQUIRED")
                if record.pending.call.call_id != call.call_id or record.pending.call.model_step != call.model_step:
                    raise JournalConflict("GATEWAY_RECONCILIATION_REQUIRED")
                if record.pending.fingerprint != fingerprint:
                    raise JournalConflict("GATEWAY_OPERATION_CHANGED")
            else:
                record.pending = pending
                self.gateway.memory._set(db, job_key(identity), record.model_dump(mode="json"))
            self._resume(db, record)
            return record, True, None

    async def retire(self, identity: GatewayJob):
        self.windows.interrupt("job_retired", identity=identity)
        async with self.dispatch:
            await self.gateway.io.call(self._retire, identity)

    def _retire(self, identity: GatewayJob):
        """Called with dispatch held after lifecycle completion, including recovery.

        Archive one compressed final context; retain operation identities/results,
        and remove repeated delivery snapshots from completed operation journals.
        The file is durable before the atomic head/journal replacement commits.
        """
        with self.gateway.memory.transaction() as db:
            record = self._load(db, identity)
            if record.retired:
                return
            if record.pending is not None:
                raise JournalConflict("GATEWAY_RECONCILIATION_REQUIRED")
            unfinished_call = db.execute(
                tables.calls.select()
                .where(tables.calls.c.job == identity.job, tables.calls.c.result.is_(None))
                .limit(1)
            ).first()
            unfinished_request = db.execute(
                tables.outbox.select()
                .where(tables.outbox.c.job == identity.job, tables.outbox.c.result.is_(None))
                .limit(1)
            ).first()
            journals = db.scalars(
                tables.decision_journal.select()
                .with_only_columns(tables.decision_journal.c.body)
                .where(tables.decision_journal.c.job == identity.job)
            )
            if (
                unfinished_call is not None
                or unfinished_request is not None
                or any(json.loads(body).get("result") is None for body in journals)
            ):
                raise JournalConflict("GATEWAY_UNSETTLED_JOB")
            row = db.execute(tables.jobs.select().where(tables.jobs.c.id == identity.job)).mappings().one()
            if row["status"] != "COMPLETED":
                self._resume(db, record)
        self.gateway.memory.finish_job(identity.job, True)
        with self.gateway.memory.transaction() as db:
            record = self._load(db, identity)
            # Each delivery snapshot can be several megabytes. Retain only IDs
            # while updating this table, then parse and discard one body at a
            # time. Loading every snapshot together can exhaust the controller
            # after END_TURN has already committed.
            identifiers = db.scalars(
                tables.decision_journal.select()
                .with_only_columns(tables.decision_journal.c.id)
                .where(tables.decision_journal.c.job == identity.job)
            ).all()
            record.retired = True
            archive = record.model_dump(mode="json")
            head = RetiredHead(identity=identity, archive=digest(archive))
            root = private_directory(self.gateway.memory.root / "native-archives")
            write_private(root / (head.archive + ".json.gz"), gzip.compress(canonical(archive).encode(), mtime=0))
            for identifier in identifiers:
                body = json.loads(
                    db.scalar(
                        tables.decision_journal.select()
                        .with_only_columns(tables.decision_journal.c.body)
                        .where(tables.decision_journal.c.id == identifier)
                    )
                )
                for key in ("gatewayState", "deliveredAccess", "receiptAccess", "receipts", "dispatch"):
                    body.pop(key, None)
                db.execute(
                    tables.decision_journal.update()
                    .where(tables.decision_journal.c.id == identifier)
                    .values(body=canonical(body))
                )
            self.gateway.memory._set(db, job_key(identity), head.model_dump(mode="json"))
        self.jobs.pop(job_key(identity), None)
        for token, cached in list(self.gateway.jobs.items()):
            if (cached.id, cached.epoch) == (identity.job, identity.scope.epoch):
                cached.closed = True
                self.gateway.jobs.pop(token, None)

    def _finish(self, identity: GatewayJob, call: GatewayCall, execution: GatewayExecution):
        with self.gateway.memory.transaction() as db:
            record = self._load(db, identity)
            if record.pending is None or (
                record.pending.call.call_id,
                record.pending.call.model_step,
            ) != (call.call_id, call.model_step):
                raise JournalConflict("GATEWAY_PENDING_CALL_CHANGED")
            record.state, record.pending = execution.state, None
            self.gateway.memory._set(db, job_key(identity), record.model_dump(mode="json"))

    async def recover(self, identity: GatewayJob) -> GatewayReceipt | None:
        """Reconcile a persisted call even when the original activity cannot retry."""
        record = await self.gateway.io.call(self.load, identity)
        if record.pending is None:
            return None
        if record.pending.execution == "native-command-v1":
            raise JournalConflict("GATEWAY_NATIVE_RECONCILIATION_REQUIRED")
        call = record.pending.call
        return await self(identity, call.call_id, call.model_step, call.decision)

    async def recover_receipts(self, identity: GatewayJob, *, wait=True) -> GatewayReceipt | None:
        try:
            async with asyncio.timeout(None if wait else 0):
                await self.dispatch.acquire()
        except TimeoutError:
            return None
        try:
            return await self._recover_receipts(identity)
        finally:
            self.dispatch.release()

    async def _recover_receipts(self, identity: GatewayJob) -> GatewayReceipt | None:
        if not self.gateway.retiring_epoch(identity.scope.epoch):
            raise JournalConflict("SOURCE_RETIREMENT_REQUIRED")
        record = await self.gateway.io.call(self.load, identity)
        if record.pending is None:
            return None
        if record.pending.execution != "gateway-v1":
            raise JournalConflict("GATEWAY_NATIVE_RECONCILIATION_REQUIRED")
        if self.resolve_receipts is None:
            raise JournalConflict("GATEWAY_RECEIPT_RESTORATION_REQUIRED")
        job = await self.resolve_receipts(record)
        executor = NativeGatewayExecutor(
            self.gateway, job, compile_decision_contract(identity.purpose, record.catalogue), record.state
        )
        call = record.pending.call
        execution = await executor.receipts_only(call.call_id, call.decision, model_step=call.model_step)
        await self.gateway.io.call(self._finish, identity, call, execution)
        self.windows.settled(operation_identity(identity.job, identity.scope.epoch, call.model_step, call.call_id))
        return execution

    async def acknowledge_delivery(self, identity: GatewayJob, call_id: str, model_step: int):
        """A trusted sandbox received this child's result before its next call.

        Direct sibling tools still wait for the next model response. This is
        invoked only after the child has settled, never from model arguments.
        """

        def acknowledge():
            with self.gateway.memory.transaction() as db:
                record = self._load(db, identity)
                if record.pending is not None or record.retired or record.state.delivery_step != model_step:
                    raise JournalConflict("GATEWAY_DELIVERY_STATE_CONFLICT")
                identifier = operation_identity(identity.job, identity.scope.epoch, model_step, call_id)
                row = (
                    db.execute(tables.decision_journal.select().where(tables.decision_journal.c.id == identifier))
                    .mappings()
                    .first()
                )
                if row is None or json.loads(row["body"]).get("result") is None:
                    raise JournalConflict("GATEWAY_DELIVERY_RECEIPT_REQUIRED")
                state = record.state
                if state.pending_access is not None:
                    record.state = state.model_copy(
                        update={
                            "delivered_access": state.pending_access,
                            "military_review": state.pending_military_review,
                            "military_targets": state.pending_military_targets,
                            "pending_access": None,
                            "pending_military_review": None,
                            "pending_military_targets": [],
                        }
                    )
                    self.gateway.memory._set(db, job_key(identity), record.model_dump(mode="json"))

        async with self.dispatch:
            await self.gateway.io.call(acknowledge)

    async def __call__(self, deps, call_id: str, model_step: int, decision: JsonObject) -> GatewayReceipt:
        identity = GatewayJob(scope=deps.scope, job=deps.job, purpose=deps.purpose)
        return await self.windows.run(
            identity,
            operation_identity(identity.job, identity.scope.epoch, model_step, call_id),
            lambda: self._window_call(deps, call_id, model_step, decision),
            write=decision.get("kind") == "act",
            cost=operation_cost(decision),
            release=decision.get("kind") in {"finish", "wait"},
        )

    async def _window_call(self, deps, call_id: str, model_step: int, decision: JsonObject) -> GatewayReceipt:
        identity = GatewayJob(scope=deps.scope, job=deps.job, purpose=deps.purpose)
        call = GatewayCall(call_id=call_id, model_step=model_step, decision=decision)
        async with self.dispatch:
            if self.gateway.retiring_epoch(identity.scope.epoch):
                record = await self.gateway.io.call(self.load, identity)
                if record.pending is not None and record.pending.call == call:
                    recovered = await self._recover_receipts(identity)
                    assert recovered is not None
                    return recovered
                saved = await self.gateway.io.call(
                    journal,
                    self.gateway.memory,
                    operation_identity(identity.job, identity.scope.epoch, call.model_step, call.call_id),
                )
                if saved is not None:
                    canonical_decision = compile_decision_contract(identity.purpose, record.catalogue).parse(
                        compile_decision_contract(identity.purpose, record.catalogue).encode(call.decision)
                    )
                    if saved["decision"] != canonical_decision:
                        raise JournalConflict("GATEWAY_OPERATION_CHANGED")
                    if saved.get("result") is not None:
                        return GatewayReceipt(result=saved["result"])
                return GatewayReceipt(result={"error": "SOURCE_EPOCH_RETIRED", "applied": False})
            record, advance, receipt = await self.gateway.io.call(self._claim, identity, call)
            if receipt is not None:
                return receipt
            key = job_key(identity)
            job = self.jobs.get(key)
            if job is None:
                job = await self.resolve(record)
            try:
                if self.refresh is not None:
                    await self.refresh(job)
                executor = NativeGatewayExecutor(
                    self.gateway, job, compile_decision_contract(identity.purpose, record.catalogue), record.state
                )
                self.jobs[key] = job
                execution = await executor.execute(call_id, decision, model_step=model_step)
                if advance:
                    # A crash here leaves a pending call and its completed domain
                    # receipt. A retry replays it before moving the head forward.
                    await self.gateway.io.call(self._finish, identity, call, execution)
                self.windows.settled(operation_identity(identity.job, identity.scope.epoch, model_step, call_id))
                return execution
            except BaseException:
                # A failed transport can close/fence the in-memory job. Rebuild
                # from its durable head before reconciling the original call.
                self.jobs.pop(key, None)
                self.gateway.jobs.pop(job.token, None)
                raise
