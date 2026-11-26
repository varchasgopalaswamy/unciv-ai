"""Trusted player coordinator; model workers receive only scoped RPC."""

import asyncio
import copy
import json
import uuid
from pathlib import Path
from typing import TYPE_CHECKING

from civ_agent import memory_schema as tables
from civ_agent.civilization_state import CivilizationState, receipt_is_uncertain
from civ_agent.decision_contracts import compile_decision_contract
from civ_agent.economy_roles import TurnRoles
from civ_agent.execution_policy import coordinator_execution_policy
from civ_agent.gameplay_policy import OPPORTUNITY_GUIDANCE, coordinator_gameplay_policy
from civ_agent.host_connection import HostConnection
from civ_agent.operator_connection import Operator, OperatorError
from civ_agent.orchestration.contracts import (
    GatewayJob,
    ModelSelection,
    PrepareRole,
    PrepareTurn,
    RoleCheck,
    RoleRequest,
    Scope,
    TurnTicket,
    digest,
)
from civ_agent.orchestration.effort import select_effort
from civ_agent.orchestration.gateway_execution import NativeGatewayExecutor
from civ_agent.orchestration.gateway_registry import GatewayRegistry, RegisteredJob, job_key
from civ_agent.orchestration.gateway_state import GatewayState, capture_job, restore_job
from civ_agent.orchestration.managed_player import (
    ConfigureManagedPlayer,
    NativeModelRole,
    NativePlayerSession,
    PrepareManagedTurn,
)
from civ_agent.orchestration.preparation import prepare_job
from civ_agent.orchestration.role_state import capture_roles, restore_roles
from civ_agent.orchestration.store import JournalConflict, WorkflowStore
from civ_agent.orchestration.task_identity import TaskOwner, TurnRoleTask
from civ_agent.prepared_role_context import PreparedRoleContext, delivered_prepared_context
from civ_agent.proposal_gateway import JobAccess, PlayerConnection, ProposalGateway
from civ_agent.protocol import JsonObject
from civ_agent.service_io import ServiceIO
from civ_agent.startup_context import (
    civilization_reference,
    player_strategy_reference,
    prepare_startup,
)

if TYPE_CHECKING:
    from civ_agent.orchestration.authored_documents import AuthoredDocuments
    from civ_agent.orchestration.communication import NativeCommunication
    from civ_agent.orchestration.review_publication import NativeReviews


class TurnRecordCache(dict):
    """Eight mutable turn records; durable history remains in the notebook."""

    def __init__(self, memory):
        super().__init__()
        self.memory = memory

    def __setitem__(self, key, value):
        self.pop(key, None)
        super().__setitem__(key, value)
        while len(self) > 8:
            self.pop(next(iter(self)))

    def __missing__(self, key):
        with self.memory.transaction() as db:
            value = self.memory._meta(db, "nativeTurn:" + key)
        if value is None:
            raise KeyError(key)
        self[key] = value
        return value


class NativeCoordinator:
    def __init__(
        self,
        *,
        game: str,
        player: str,
        civilization: str,
        state_dir: Path,
        store: WorkflowStore,
        operator: Operator,
        host_ca: Path,
        connection_factory=HostConnection,
        handoff_checkpoint=None,
        external_lease_id: str | None = None,
    ):
        if external_lease_id is not None and (
            not isinstance(external_lease_id, str) or not external_lease_id or len(external_lease_id) > 128
        ):
            raise JournalConflict("NATIVE_EXTERNAL_LEASE_INVALID")
        self.game, self.player, self.civilization = game, player, civilization
        self.state_dir, self.store, self.operator, self.host_ca = state_dir, store, operator, host_ca
        self.connection_factory = connection_factory
        self.handoff_checkpoint = handoff_checkpoint
        self.external_lease_id = external_lease_id
        self.communication: NativeCommunication | None = None
        self.authored_documents: AuthoredDocuments | None = None
        self.reviews: NativeReviews | None = None
        self.memory = CivilizationState(state_dir / "notebook", civilization)
        self.records = TurnRecordCache(self.memory)
        self.record: JsonObject | None = None
        try:
            with self.memory.transaction() as db:
                saved = self.memory._meta(db, "nativeGame")
                if saved is not None and (
                    bool(saved.get("externalLease", False)) != (external_lease_id is not None)
                    or external_lease_id is not None
                    and saved["lease"] != external_lease_id
                ):
                    raise JournalConflict("NATIVE_LEASE_BINDING_CONFLICT")
                self.game_state = saved or {
                    "version": 1,
                    "lease": external_lease_id or uuid.uuid4().hex,
                    "externalLease": external_lease_id is not None,
                    "tickets": {},
                }
                if saved is None:
                    self.memory._set(db, "nativeGame", self.game_state)
                session = self.managed_session
                if session is not None and (session.game, session.player) != (self.game, self.player):
                    raise JournalConflict("COORDINATOR_OWNER_CONFLICT")
        except BaseException:
            self.memory.engine.dispose()
            raise
        self.io, self.operator_io = ServiceIO(), ServiceIO()
        self.gateway = ProposalGateway(self.memory, self.io, state_dir / "workspace")
        self.registry = GatewayRegistry(
            self.gateway,
            game=game,
            player=player,
            resolve=self.resolve,
            refresh=self.refresh,
            resolve_receipts=self.resolve_receipts,
        )
        self.turn_roles: TurnRoles | None = None
        self.failure, self.stopping = None, False
        self.economy_interval, self.military_interval = 5, 10
        self.connection: PlayerConnection | None = None
        self.connection_key = None
        self._apply_session_settings()

    @property
    def managed_session(self) -> NativePlayerSession | None:
        saved = self.game_state.get("managedSession")
        return NativePlayerSession.model_validate_json(json.dumps(saved)) if saved is not None else None

    @property
    def settings(self) -> JsonObject:
        session = self.managed_session
        return session.settings.model_dump(mode="json") if session is not None else {}

    @property
    def review_interval(self) -> int:
        session = self.managed_session
        return (
            session.review_interval
            if session is not None
            else self.game_state.get("request", {}).get("review_interval", 5)
        )

    @property
    def review_attempts(self) -> int:
        session = self.managed_session
        return session.settings.review_attempts if session is not None else 2

    def get_selection(self, role: NativeModelRole) -> ModelSelection:
        session = self.managed_session
        return (
            session.get_selection(role)
            if session is not None
            else ModelSelection.model_validate(self.game_state["request"]["selection"])
        )

    def get_request_limit(self, role: NativeModelRole) -> int:
        session = self.managed_session
        if session is not None:
            return session.get_request_limit(role)
        limit = self.game_state.get("request", {}).get("request_limit", 24)
        return min(limit, 2 if role in {"peace", "peace_main", "peace_military"} else 12 if role == "review" else 100)

    def get_provider_contract(self, role: NativeModelRole, *, selection: ModelSelection | None = None):
        session = self.managed_session
        return session.get_provider_contract(role, selection=selection) if session is not None else None

    def job_settings(self, role: NativeModelRole) -> JsonObject:
        session = self.managed_session
        return session.job_settings(role) if session is not None else {}

    def _apply_session_settings(self):
        session = self.managed_session
        if session is not None:
            self.economy_interval = session.settings.economy_interval
            self.military_interval = session.settings.military_interval
            self.gateway.native_advice_enabled = session.settings.native_advice_enabled

    async def configure_managed_session(
        self, session: NativePlayerSession, *, expected_epoch: str | None = None, allow_fast_change: bool = False
    ):
        async with self.registry.dispatch:
            await self._configure_managed_session(
                session, expected_epoch=expected_epoch, allow_fast_change=allow_fast_change
            )

    async def _configure_managed_session(
        self, session: NativePlayerSession, *, expected_epoch: str | None = None, allow_fast_change: bool = False
    ):
        session = NativePlayerSession.model_validate_json(session.model_dump_json())
        if self.external_lease_id is None:
            raise JournalConflict("MANAGED_EXTERNAL_LEASE_REQUIRED")
        if (session.game, session.player) != (self.game, self.player):
            raise JournalConflict("COORDINATOR_OWNER_CONFLICT")
        spec = session.model_dump(mode="json")
        if self.game_state.get("managedSession", spec) != spec:
            previous, proposed = copy.deepcopy(self.game_state["managedSession"]), copy.deepcopy(spec)
            for value in (previous, proposed):
                value["settings"].pop("fast", None)
                value.pop("provider_contracts", None)
            if not allow_fast_change or previous != proposed:
                raise JournalConflict("MANAGED_SESSION_CHANGED")
            session.require_provider_contracts()
        if expected_epoch is not None:
            status = await self.lease()
            if status["epoch"] != expected_epoch:
                raise JournalConflict("MANAGED_EPOCH_CONFLICT")
            await self.io.call(self.memory.bind, expected_epoch)
            await self.io.call(self.store.bind_epoch, self.game, expected_epoch)
        self.game_state["managedSession"] = spec
        await self.put("nativeGame", self.game_state)
        self._apply_session_settings()

    async def configure_managed_player(self, request: ConfigureManagedPlayer):
        await self.configure_managed_session(request.session, expected_epoch=request.expected_epoch)

    def configure_authored_memory(self, reader: "AuthoredDocuments"):
        """Opt in before admitting jobs; native files remain their only author store."""
        if self.authored_documents is not None and self.authored_documents is not reader:
            raise JournalConflict("AUTHORED_READER_ALREADY_CONFIGURED")

        def read(epoch, turn):
            return reader.read(Scope(game=self.game, player=self.player, epoch=epoch, activation="authored"), turn)

        self.memory.enable_authored(read)
        self.authored_documents = reader

    async def op(self, name, **fields):
        if self.external_lease_id is not None and name in {"acquire", "heartbeat", "pause"}:
            raise JournalConflict("NATIVE_EXTERNAL_LEASE_OWNER_REQUIRED")
        return await self.operator_io.call(self.operator.call, name, **fields)

    async def get(self, key, default=None):
        def load():
            with self.memory.transaction() as db:
                return self.memory._meta(db, key, default)

        return await self.io.call(load)

    async def put(self, key, value):
        frozen = copy.deepcopy(value)

        def save():
            with self.memory.transaction() as db:
                self.memory._set(db, key, frozen)

        await self.io.call(save)

    async def _save(self, record):
        if self.turn_roles is not None:
            record["roleContext"] = capture_roles(self.turn_roles)
        self.records[record["activationId"]] = record
        await self.put("nativeTurn:" + record["activationId"], record)

    def context(self) -> tuple[JsonObject, TurnRoles]:
        assert self.record is not None and self.turn_roles is not None
        return self.record, self.turn_roles

    def check_owner(self, scope):
        if (scope.game, scope.player) != (self.game, self.player):
            raise JournalConflict("COORDINATOR_OWNER_CONFLICT")

    def fixture_startup(self, identity: GatewayJob):
        session = self.managed_session
        exercise = (
            session.settings.scripted
            if session is not None
            else self.game_state.get("request", {}).get("exercise_native_stores", False)
        )
        if not exercise:
            return {}
        from civ_agent.orchestration.authored_memory import contribution_prefix

        return {
            "nativeStoreExercise": True,
            "nativeStoreJob": identity.job,
            "nativeStoreContributionFile": contribution_prefix(identity) + "native-stores.md",
        }

    async def lease(self):
        status = (await self.op("status"))["status"]
        if status.get("leaseId") != self.game_state["lease"]:
            if self.external_lease_id is not None:
                raise JournalConflict("NATIVE_LEASE_CONFLICT")
            status = (await self.op("acquire", leaseId=self.game_state["lease"]))["status"]
        return status

    async def heartbeat(self):
        failures = 0
        while True:
            try:
                if self.external_lease_id is not None:
                    status = (await self.op("status"))["status"]
                    if status.get("leaseId") not in {None, self.external_lease_id}:
                        raise JournalConflict("NATIVE_LEASE_CONFLICT")
                else:
                    status = await self.lease()
                    await self.op("heartbeat", leaseId=self.game_state["lease"])
            except OperatorError as error:
                task = asyncio.current_task()
                if task is not None and task.cancelling():
                    # ServiceIO joins an accepted read before returning. A host
                    # failure from that read must not turn shutdown into retry.
                    raise asyncio.CancelledError from error
                # A host can restart independently of this coordinator. Retry
                # only connection failures; auth, fencing and host rejections
                # must still stop the service. No player command runs here.
                failures += 1
                # The managed game owner controls downtime budgets and pause.
                # Its passive observers must also survive a longer host restart
                # or ordered shutdown without abandoning retained SDK work.
                if (
                    self.external_lease_id is None
                    and failures >= 15
                    or error.status is not None
                    or error.code not in {"TRANSPORT_UNAVAILABLE", "TRANSPORT_TIMEOUT"}
                ):
                    raise
                if failures == 1:
                    print("HOST-RECONNECTING", flush=True)
                await asyncio.sleep(2)
                continue
            if failures:
                print("HOST-RECONNECTED", flush=True)
            failures = 0
            if status["phase"] == "HALTED":
                raise JournalConflict("NATIVE_HOST_HALTED")
            await asyncio.sleep(5)

    async def activate(self, ticket):
        self.check_owner(ticket.scope)
        status = await self.lease()
        if (status["epoch"], status["handoff"], status["activeCivilization"]) != (
            ticket.scope.epoch,
            ticket.handoff,
            self.civilization,
        ):
            raise JournalConflict("NATIVE_TURN_EXPIRED")
        activated = await self.op(
            "activate",
            leaseId=self.game_state["lease"],
            epoch=ticket.scope.epoch,
            serverInstance=status["serverInstance"],
            handoff=ticket.handoff,
            civilization=self.civilization,
            activationId=ticket.scope.activation,
        )
        key = (status["serverInstance"], activated["playerToken"])
        if self.connection_key != key:
            if self.connection is not None:
                await self.connection.close()
            self.connection = self.connection_factory(self.operator.host_url + "/mcp", key[1], self.host_ca)
            self.connection_key = key
            for job in self.gateway.jobs.values():
                job.connection, job.verified = self.connection, False
        assert self.connection is not None
        return self.connection

    async def load_turn(self, ticket):
        connection = await self.activate(ticket)
        if self.record is not None and self.record["activationId"] == ticket.scope.activation:
            return connection
        record = await self.get("nativeTurn:" + ticket.scope.activation)
        if record is None:
            raise JournalConflict("NATIVE_TURN_UNREGISTERED")
        self.record = record
        self.records[record["activationId"]] = record
        self.turn_roles = TurnRoles(self, record)
        if record.get("roleContext"):
            restore_roles(self.turn_roles, record["roleContext"])
        self.gateway.role_coordinator = self.turn_roles
        for raw in record.get("roleJobs", {}).values():
            binding = GatewayJob.model_validate(raw)
            registered = await self.io.call(self.registry.load, binding)
            job = self.registry.jobs.get(job_key(binding))
            if job is None:
                job = restore_job(
                    registered.state,
                    connection,
                    job_id=binding.job,
                    purpose=binding.purpose,
                    epoch=binding.scope.epoch,
                )
            self.gateway.jobs[job.token] = job
            self.turn_roles.jobs[job.purpose] = job
        return connection

    async def resolve(self, registered: RegisteredJob) -> JobAccess:
        if registered.identity.purpose == "communication":
            if self.communication is None:
                raise JournalConflict("COMMUNICATION_NOT_CONFIGURED")
            return await self.communication.resolve(registered)
        activation = await self.get("nativeRoleTurn:" + registered.identity.job)
        record = await self.get("nativeTurn:" + activation)
        ticket = TurnTicket.model_validate(record["ticket"])
        connection = await self.load_turn(ticket)
        identity = registered.identity
        job = restore_job(
            registered.state, connection, job_id=identity.job, purpose=identity.purpose, epoch=identity.scope.epoch
        )
        self.gateway.jobs[job.token] = job
        _, roles = self.context()
        roles.jobs[job.purpose] = job
        return job

    async def resolve_receipts(self, registered: RegisteredJob) -> JobAccess:
        """Rebuild only an admitted call's bookkeeping, without activation or job resume."""
        binding = registered.identity
        self.check_owner(binding.scope)
        if not self.gateway.retiring_epoch(binding.scope.epoch) and not registered.retired:
            raise JournalConflict("SOURCE_RETIREMENT_REQUIRED")
        status = await self.lease()
        if status["epoch"] != binding.scope.epoch or status["phase"] not in {"PAUSED", "FINISHED"}:
            raise JournalConflict("RECOVERY_RECEIPT_BARRIER_REQUIRED")
        connected = await self.op(
            "connect", leaseId=self.game_state["lease"], epoch=binding.scope.epoch, civilization=self.civilization
        )
        key = (status["serverInstance"], connected["playerToken"])
        if self.connection_key != key:
            if self.connection is not None:
                await self.connection.close()
            self.connection = self.connection_factory(self.operator.host_url + "/mcp", key[1], self.host_ca)
            self.connection_key = key
        assert self.connection is not None
        job = restore_job(
            registered.state, self.connection, job_id=binding.job, purpose=binding.purpose, epoch=binding.scope.epoch
        )
        self.gateway.jobs[job.token] = job
        if binding.purpose != "communication":
            activation = await self.get("nativeRoleTurn:" + binding.job)
            record = await self.get("nativeTurn:" + activation)
            ticket = TurnTicket.model_validate(record["ticket"])
            if ticket.scope.epoch != binding.scope.epoch:
                raise JournalConflict("RECOVERY_RECEIPT_SCOPE_CONFLICT")
            self.record = record
            self.records[activation] = record
            self.turn_roles = TurnRoles(self, record)
            if record.get("roleContext"):
                restore_roles(self.turn_roles, record["roleContext"])
            self.turn_roles.jobs[binding.purpose] = job
            self.gateway.role_coordinator = self.turn_roles
        return job

    async def refresh(self, job: JobAccess):
        if job.purpose == "communication":
            if self.communication is None:
                raise JournalConflict("COMMUNICATION_NOT_CONFIGURED")
            job.connection = await self.communication.connection(job.epoch)
            return
        activation = await self.get("nativeRoleTurn:" + job.id)
        saved = await self.get("nativeTurn:" + activation)
        job.connection = await self.load_turn(TurnTicket.model_validate(saved["ticket"]))

    async def lookup_receipt(self, epoch, payload):
        """Epoch-scoped receipt lookup; never activate an expired turn."""
        status = await self.lease()
        if status["epoch"] != epoch:
            raise JournalConflict("NATIVE_TURN_EXPIRED")
        connected = await self.op(
            "connect", leaseId=self.game_state["lease"], epoch=epoch, civilization=self.civilization
        )
        connection = self.connection_factory(self.operator.host_url + "/mcp", connected["playerToken"], self.host_ca)
        try:
            receipt = await connection.tool("player_receipt", payload)
        finally:
            await connection.close()
        return receipt

    async def finalize_turn(self, record):
        """Retryable cleanup after a proven handoff, without reactivating its turn."""
        if record.get("lifecycleFinalized"):
            return
        identities = {
            raw["job"]: GatewayJob(scope=Scope.model_validate(raw["scope"]), job=raw["job"], purpose=raw["purpose"])
            for raw in record["requests"].values()
        }
        for identity in identities.values():
            await self.io.call(self.registry._retire, identity)
        if self.record is not None and self.record["activationId"] == record["activationId"]:
            self.turn_roles = None
            self.gateway.role_coordinator = None
        record.pop("roleContext", None)
        record["bootstraps"] = {}
        record["lifecycleFinalized"] = True
        self.records[record["activationId"]] = record
        await self.put("nativeTurn:" + record["activationId"], record)

    async def prepare_turn(self, request: PrepareTurn) -> TurnTicket:
        if self.external_lease_id is not None:
            raise JournalConflict("MANAGED_TURN_REQUIRED")
        if (request.run.game, request.run.player) != (self.game, self.player):
            raise JournalConflict("COORDINATOR_OWNER_CONFLICT")
        # This first deployment accepts the credential-free scripted pilot only.
        if request.run.selection.model != "scripted-v1":
            raise JournalConflict("ISOLATED_GAME_REQUIRES_SCRIPTED_MODEL")
        spec = request.run.model_dump(mode="json")
        if self.game_state.get("request", spec) != spec:
            raise JournalConflict("GAME_REQUEST_CHANGED")
        self.game_state["request"] = spec
        status = await self.lease()
        return await self._prepare_turn(request.index, status, {})

    async def prepare_managed_turn(self, request: PrepareManagedTurn) -> TurnTicket:
        request = PrepareManagedTurn.model_validate_json(request.model_dump_json())
        async with self.registry.dispatch:
            await self._configure_managed_session(request.session, expected_epoch=request.expected_epoch)
            status = await self.lease()
            if (status["epoch"], status["handoff"]) != (request.expected_epoch, request.expected_handoff):
                raise JournalConflict("MANAGED_TURN_CONFLICT")
            return await self._prepare_turn(request.index, status, self.settings)

    async def resume_managed_turn(self, ticket: TurnTicket):
        async with self.registry.dispatch:
            self.check_owner(ticket.scope)
            saved = await self.get("nativeTurn:" + ticket.scope.activation)
            if saved is None or saved["ticket"] != ticket.model_dump(mode="json"):
                raise JournalConflict("NATIVE_TURN_UNREGISTERED")
            await self.load_turn(ticket)

    async def prepare_local_attempt(
        self, session: NativePlayerSession, epoch: str, handoff: int, attempt_id: str
    ) -> TurnTicket:
        """Admit fresh reasoning at the current position, without rewinding it."""
        if (session.game, session.player) != (self.game, self.player):
            raise JournalConflict("LOCAL_ATTEMPT_OWNER_CONFLICT")
        async with self.registry.dispatch:
            await self._configure_managed_session(session, expected_epoch=epoch)
            status = await self.lease()
            if (status["epoch"], status["handoff"], status["activeCivilization"]) != (
                epoch,
                handoff,
                self.civilization,
            ):
                raise JournalConflict("LOCAL_ATTEMPT_POSITION_CHANGED")
            activation = digest([self.game, self.player, epoch, handoff, attempt_id])
            previous = next(
                (value for value in self.game_state["tickets"].values() if value["scope"]["activation"] == activation),
                None,
            )
            index = previous["index"] if previous is not None else len(self.game_state["tickets"])
            return await self._prepare_turn(
                index, status, session.settings.model_dump(mode="json"), attempt_id=attempt_id
            )

    async def retire_local_attempt(self, ticket: TurnTicket) -> JsonObject:
        """Close old work at the current world position; never execute a saved action."""
        from sqlalchemy import select

        from civ_agent.orchestration.gateway_registry import RegisteredJob
        from civ_agent.orchestration.handoff_contracts import FrozenNativeHandoff
        from civ_agent.orchestration.host_effect_contracts import (
            FrozenNativeCommand,
            NativeCommandApplication,
            NativeCommandFinish,
        )
        from civ_agent.orchestration.host_effect_service import NativeCommandService

        self.check_owner(ticket.scope)
        status = await self.lease()
        if status["epoch"] != ticket.scope.epoch or status["phase"] not in {"PAUSED", "FINISHED"}:
            raise JournalConflict("LOCAL_RECOVERY_BARRIER_REQUIRED")
        self.registry.windows.interrupt("job_retired")

        def fence():
            found = []
            with self.memory.transaction() as db:
                self.memory._set(
                    db, "localRecoveryBarrier", {"epoch": ticket.scope.epoch, "attempt": ticket.scope.activation}
                )
                # _set uses INSERT OR REPLACE, which can move a row to the end
                # of a live SQLite scan. Freeze the input before retiring jobs
                # so an unfinished replacement is never visited repeatedly.
                rows = db.scalars(select(tables.meta.c.value).where(tables.meta.c.key.like("nativeGatewayJob:%"))).all()
                for raw in rows:
                    body = json.loads(raw)
                    if "archive" in body:
                        continue
                    item = RegisteredJob.model_validate(body)
                    if item.identity.scope.epoch != ticket.scope.epoch or (
                        item.retired and item.state.data["closed"] and item.pending is None
                    ):
                        continue
                    item.retired = True
                    self.memory._set(db, job_key(item.identity), item.model_dump(mode="json"))
                    found.append(item)
                self.memory._set(db, "localRetiredAttempt:" + ticket.scope.activation, ticket.model_dump(mode="json"))
            return found

        async with self.registry.dispatch:
            registered = await self.io.call(fence)
        await self.io.call(self.memory.resume_run)
        if self.gateway.routing is not None:
            await self.gateway.routing.retire_pending(ticket.scope)

        def frozen_commands():
            with self.memory.transaction() as db:
                return [
                    (key, FrozenNativeCommand.model_validate(json.loads(raw)))
                    for key, raw in db.execute(
                        select(tables.meta.c.key, tables.meta.c.value).where(
                            tables.meta.c.key.like("localNativeEffect:%")
                        )
                    )
                    if json.loads(raw) is not None
                ]

        commands = NativeCommandService(self.registry, workflow_store=self.store)
        for key, effect in await self.io.call(frozen_commands):
            if effect.step.identity.scope.epoch != ticket.scope.epoch:
                raise JournalConflict("LOCAL_PENDING_REQUEST_EPOCH_CONFLICT")
            commands._verify(effect)
            payload = effect.prepared.payload
            result = await self.op(
                "resolve_request",
                leaseId=self.game_state["lease"],
                epoch=ticket.scope.epoch,
                civilization=self.civilization,
                transport="player_command",
                request=payload,
            )
            receipt = result.get("receipt")
            if not isinstance(receipt, dict) or receipt_is_uncertain(receipt):
                raise JournalConflict("LOCAL_PENDING_REQUEST_UNRESOLVED")
            await commands.apply(NativeCommandApplication(effect=effect, receipt=receipt))

            def clear_pending(key=key):
                with self.memory.transaction() as db:
                    self.memory._set(db, key, None)

            await self.io.call(clear_pending)
        for pending in await self.io.call(self.memory.pending):
            payload = json.loads(pending["payload"])
            if payload.get("epoch") != ticket.scope.epoch:
                raise JournalConflict("LOCAL_PENDING_REQUEST_EPOCH_CONFLICT")
            result = await self.op(
                "resolve_request",
                leaseId=self.game_state["lease"],
                epoch=ticket.scope.epoch,
                civilization=self.civilization,
                transport=pending["transport"],
                request=payload,
            )
            receipt = result.get("receipt")
            if not isinstance(receipt, dict) or receipt_is_uncertain(receipt):
                raise JournalConflict("LOCAL_PENDING_REQUEST_UNRESOLVED")
            await self.io.call(self.memory.receipt, pending["id"], receipt)

        pending_handoff = await self.get("localPendingHandoff:" + ticket.scope.activation)
        if pending_handoff is not None:
            effect = FrozenNativeHandoff.model_validate(pending_handoff)
            payload = effect.prepared.payload
            if payload is None:
                raise JournalConflict("LOCAL_HANDOFF_REQUEST_MISSING")
            result = await self.op(
                "resolve_request",
                leaseId=self.game_state["lease"],
                epoch=ticket.scope.epoch,
                civilization=self.civilization,
                transport="player_command",
                request=payload,
            )
            receipt = result.get("receipt")
            if not isinstance(receipt, dict) or receipt_is_uncertain(receipt):
                raise JournalConflict("LOCAL_HANDOFF_UNRESOLVED")
            await self.put("localHandoffReceipt:" + ticket.scope.activation, receipt)
            await self.put("localPendingHandoff:" + ticket.scope.activation, None)

        for item in registered:
            if item.pending is not None:
                if item.pending.execution == "native-command-v1":
                    await commands.finish(NativeCommandFinish(identity=item.identity, call=item.pending.call))
                elif item.pending.execution != "gateway-v1":
                    raise JournalConflict("LOCAL_LEGACY_COMMAND_UNSUPPORTED")
                else:
                    job = await self.resolve_receipts(item)
                    executor = NativeGatewayExecutor(
                        self.gateway, job, compile_decision_contract(item.identity.purpose, item.catalogue), item.state
                    )
                    call = item.pending.call
                    execution = await executor.receipts_only(call.call_id, call.decision, model_step=call.model_step)
                    await self.io.call(self.registry._finish, item.identity, call, execution)

            def close(identity=item.identity):
                with self.memory.transaction() as db:
                    current = self.registry._load(db, identity)
                    current.retired = True
                    current.state.data["closed"] = True
                    self.memory._set(db, job_key(identity), current.model_dump(mode="json"))
                self.memory.finish_job(identity.job, False)

            await self.io.call(close)
            self.registry.jobs.pop(job_key(item.identity), None)
        self.gateway.jobs.clear()
        await self.put("localRecoveryBarrier", None)
        return await self.lease()

    async def _prepare_turn(
        self, index: int, status: JsonObject, settings: JsonObject, *, attempt_id: str | None = None
    ) -> TurnTicket:
        if status["activeCivilization"] != self.civilization:
            raise JournalConflict("NATIVE_PLAYER_UNAVAILABLE")
        await self.io.call(self.memory.bind, status["epoch"])
        await self.io.call(self.store.bind_epoch, self.game, status["epoch"])
        key = str(index)
        if key in self.game_state["tickets"]:
            ticket = TurnTicket.model_validate(self.game_state["tickets"][key])
            if (ticket.scope.epoch, ticket.handoff, ticket.index) != (status["epoch"], status["handoff"], index):
                raise JournalConflict("NATIVE_TURN_REQUEST_CHANGED")
        else:
            activation = digest(
                [self.game, self.player, status["epoch"], status["handoff"]]
                + ([attempt_id] if attempt_id is not None else [])
            )
            ticket = TurnTicket(
                scope=Scope(game=self.game, player=self.player, epoch=status["epoch"], activation=activation),
                turn=status["turn"],
                handoff=status["handoff"],
                index=index,
            )
            self.game_state["tickets"][key] = ticket.model_dump(mode="json")
            record = {
                "activationId": activation,
                "epoch": status["epoch"],
                "gameTurn": status["turn"],
                "ticket": ticket.model_dump(mode="json"),
                "settings": copy.deepcopy(settings),
                "roleJobs": {},
                "requests": {},
                "bootstraps": {},
            }
            saved_state = copy.deepcopy(self.game_state)

            def save():
                with self.memory.transaction() as db:
                    self.memory._set(db, "nativeTurn:" + activation, record)
                    self.memory._set(db, "nativeGame", saved_state)

            await self.io.call(save)
        await self.load_turn(ticket)
        return ticket

    async def prepare_role(self, request: PrepareRole) -> RoleRequest | None:
        async with self.registry.dispatch:
            if self.reviews is not None:
                await self.io.call(self.reviews.require, request.ticket)
            connection = await self.load_turn(request.ticket)
            record, roles = self.context()
            if request.purpose not in {"gameplay", "economy", "military"}:
                raise JournalConflict("NATIVE_ROLE_UNAVAILABLE")
            key = request.purpose + ":" + str(request.pass_number)
            if key in record["requests"]:
                return RoleRequest.model_validate(record["requests"][key])
            if request.purpose != "gameplay" and roles.ready[request.purpose]:
                return None
            if request.purpose == "gameplay" and roles.ready["gameplay"]:
                return None
            identity = GatewayJob(
                scope=request.ticket.scope.model_copy(
                    update={"activation": digest([request.ticket.scope.activation, key])}
                ),
                job=digest([request.ticket.scope.activation, key]),
                purpose=request.purpose,
            )
            if key in record["bootstraps"]:
                bootstrap = record["bootstraps"][key]
                state = GatewayState.model_validate(bootstrap["state"])
                job = restore_job(
                    state, connection, job_id=identity.job, purpose=identity.purpose, epoch=identity.scope.epoch
                )
                self.gateway.jobs[job.token] = job
                roles.jobs[job.purpose] = job
                role_request = RoleRequest.model_validate(bootstrap["request"])
                catalogue = self.store.get(identity.scope, role_request.catalogue)
            else:
                prior = roles.jobs.get(request.purpose)
                if prior is not None and prior.id != identity.job:
                    await self.gateway.close(prior, True)

                job, prefix = await prepare_job(self, identity, connection, turn=request.ticket.turn)
                # New preparations freeze this policy in the captured gateway
                # state. Retained requests and bootstraps keep their old policy.
                job.memory["nativeRoleContract"] = 1
                policy = coordinator_gameplay_policy(self)
                if policy.version:
                    record["gameplayPolicy"] = policy.model_dump(mode="json")
                roles.register(job)
                if request.purpose == "gameplay" and not record.get("initialized"):
                    view = await connection.tool("player_turn", {})
                    await roles.initialize_economy(view)
                    await roles.initialize_military(view)
                    record["initialized"] = True
                if request.purpose == "economy":
                    roles.economy_requested = True
                if request.purpose == "military":
                    roles.military_requested = True
                prepared = await prepare_startup(self.gateway, job, call_prefix=prefix)
                self.gateway.commit_startup_coordination(job)
                catalogue = await self.gateway.tools(job)
                job.catalogue = catalogue
                selection = await select_effort(
                    self,
                    identity,
                    job,
                    prepared,
                    self.get_selection(request.purpose),
                    task=TurnRoleTask(
                        owner=TaskOwner(game=self.game, player=self.player, civilization=self.civilization),
                        turn=request.ticket.turn,
                        purpose=request.purpose,
                        pass_number=request.pass_number,
                    ),
                )
                from civ_agent.orchestration.role_instructions import native_role_instructions
                from civ_agent.orchestration.role_tasks import build_role_task
                from civ_agent.runtime.preload import build_preload

                task = build_role_task(
                    request.ticket,
                    identity.purpose,
                    request.pass_number,
                    prepared["reads"]["player_turn"],
                    message=roles.messages.get(job.purpose),
                    handoff_review=record.get("handoffReview"),
                )
                session = self.managed_session
                preload = build_preload(
                    identity.purpose,
                    instructions=native_role_instructions(identity.purpose),
                    strategy_guide=player_strategy_reference(prepared["reads"], job.purpose)
                    if session is not None and session.settings.strategy_guide
                    else "",
                    civilization_guide=civilization_reference(self.civilization)
                    if session is not None and session.settings.civilization_guide
                    else "",
                )
                startup = {
                    "instructions": preload.instructions + ("\n\n" + OPPORTUNITY_GUIDANCE if policy.version else ""),
                    "preload": preload.metadata(),
                    "user": PreparedRoleContext.from_prepared(
                        prepared,
                        roleTask=task.model_dump(mode="json"),
                        waitForEconomy=bool(self.game_state.get("request", {}).get("exercise_role_wait"))
                        and job.purpose == "gameplay"
                        and request.ticket.index == 0,
                    ).serialize(fixture=self.fixture_startup(identity)),
                }
                delivery = delivered_prepared_context(self.gateway, job, catalogue, startup["user"])
                from civ_agent.prepared_role_context import PreparedDelivery

                startup["preparedDelivery"] = PreparedDelivery.freeze(delivery)
                state = capture_job(
                    job,
                    delivered_access=delivery.delivered_access,
                    military_review=delivery.review,
                    military_targets=delivery.review_targets,
                )
                role_request = RoleRequest(
                    gameplay_policy=policy,
                    execution_policy=coordinator_execution_policy(self),
                    **identity.model_dump(),
                    typed_authored_plans=True,
                    selection=selection,
                    provider_contract=self.get_provider_contract(request.purpose, selection=selection),
                    request_limit=self.get_request_limit(request.purpose),
                    startup=self.store.put(identity.scope, startup),
                    catalogue=self.store.put(identity.scope, catalogue),
                    managed=self.external_lease_id is not None,
                    peace_approvals=self.external_lease_id is not None,
                )
                record["bootstraps"][key] = {
                    "request": role_request.model_dump(mode="json"),
                    "state": state.model_dump(mode="json"),
                }
                await self._save(record)
            # register takes the same lock; persist the already validated record
            # after releasing preparation's admission lock below.
            record["roleJobs"][job.purpose] = identity.model_dump(mode="json")
            await self.put("nativeRoleTurn:" + identity.job, record["activationId"])
        await self.registry.register(identity, state, catalogue)
        record["requests"][key] = role_request.model_dump(mode="json")
        await self._save(record)
        return role_request

    async def check_role(self, request: RoleRequest) -> RoleCheck:
        # Confirm this exact job's accepted completion. A sibling can reopen
        # the role while its model response is settling; current turn readiness
        # belongs to the scheduler and the native handoff check, not this pass.
        self.check_owner(request.scope)
        identity = GatewayJob(scope=request.scope, job=request.job, purpose=request.purpose)
        registered = await self.io.call(self.registry.load, identity)
        return RoleCheck(
            finished=registered.state.events["role_finished"],
            feedback="The coordinator has not confirmed this role is finished. Inspect current player_turn and required completion reviews, resolve remaining decisions, then call finish_role.",
        )

    async def close(self):
        self.stopping = True
        self.registry.windows.suspend("scheduler_closed")
        if self.gateway.routing is not None:
            await self.gateway.routing.close()
        if self.gateway.advisories is not None:
            self.gateway.advisories.suspend()
            await self.gateway.advisories.drain()
        await self.registry.close()
        if self.reviews is not None:
            await self.reviews.close()
        if self.communication is not None:
            await self.communication.close()
        if self.connection is not None:
            await self.connection.close()
        await self.io.close()
        await self.operator_io.close()
        if self.authored_documents is not None:
            await asyncio.to_thread(self.authored_documents.close)
        self.memory.engine.dispose()
