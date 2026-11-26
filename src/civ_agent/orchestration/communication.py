"""Player-scoped event collection and communication jobs for the async runner."""

import asyncio
import uuid

from civ_agent.execution_policy import coordinator_execution_policy
from civ_agent.gameplay_policy import coordinator_gameplay_policy
from civ_agent.host_connection import api_origin
from civ_agent.orchestration.communication_store import CommunicationStore, communication_scope
from civ_agent.orchestration.contracts import (
    CommunicationBatch,
    CommunicationRun,
    FinishCommunication,
    GatewayJob,
    RoleRequest,
    Scope,
    digest,
)
from civ_agent.orchestration.gateway_state import GatewayState, capture_job, restore_job
from civ_agent.orchestration.preparation import prepare_job
from civ_agent.orchestration.store import JournalConflict
from civ_agent.player_prompts import COMMUNICATION_INSTRUCTIONS
from civ_agent.prepared_role_context import PreparedRoleContext, delivered_prepared_context
from civ_agent.proposal_gateway import PlayerConnection
from civ_agent.protocol import JsonObject
from civ_agent.schemas import BoundaryModel
from civ_agent.startup_context import prepare_startup


class NativeCommunication:
    def __init__(self, coordinator, client=None, *, signal_checkpoint=None, notify=None):
        self.root = coordinator
        self.signal_checkpoint, self.notify = signal_checkpoint, notify
        self.transport: PlayerConnection | None = None
        self.transport_key: tuple[str, str, str | None] | None = None
        self.collection, self.transport_lock = asyncio.Lock(), asyncio.Lock()
        self.closed, self.held = False, True
        self.generation = uuid.uuid4().hex

    async def collect(self, request: CommunicationRun) -> int:
        with api_origin("background_poll"):
            return await self._collect(request)

    async def pending(self, scope: Scope):
        return await self.root.io.call(self.store(scope).pending)

    async def set_held(self, held: bool):
        self.held = held
        routing = self.root.gateway.routing
        if routing is not None:
            scope = communication_scope(self.root.game, self.root.player, (await self.root.lease())["epoch"])
            await routing.set_hold(scope, held)
        async with self.collection:
            pass

    async def restart(self, scope: Scope):
        self.held = True
        self.generation = uuid.uuid4().hex
        await self.root.io.call(self.store(scope).reset_unfinished)

    def check(self, scope):
        self.root.check_owner(scope)
        if scope != communication_scope(self.root.game, self.root.player, scope.epoch):
            raise JournalConflict("COMMUNICATION_SCOPE_CONFLICT")

    def store(self, scope):
        self.check(scope)
        return CommunicationStore(self.root.memory, self.root.store, scope)

    async def connection(self, epoch):
        # Collection and native job heartbeats share this transport. Publish a
        # single owner after connect; an overwritten owner would leak MCP tasks.
        async with self.transport_lock:
            if self.closed:
                raise JournalConflict("COMMUNICATION_CLOSED")
            root = self.root
            status = (await root.op("status"))["status"] if root.external_lease_id is not None else await root.lease()
            if status["epoch"] != epoch:
                raise JournalConflict("COMMUNICATION_EPOCH_CONFLICT")
            if root.external_lease_id is not None:
                lease = status["leaseId"]
                if lease is None:
                    # Only the managed owner may reacquire its original lease.
                    # A passive poll can arrive first after a host restart.
                    raise ConnectionError("NATIVE_HOST_LEASE_UNAVAILABLE")
                if lease != root.external_lease_id:
                    raise JournalConflict("NATIVE_LEASE_CONFLICT")
            if self.transport_key != (status["serverInstance"], epoch, status.get("leaseGeneration")):
                if self.transport is not None:
                    await self.transport.close()
                connected = await root.op(
                    "connect", leaseId=root.game_state["lease"], epoch=epoch, civilization=root.civilization
                )
                self.transport = root.connection_factory(
                    root.operator.host_url + "/mcp", connected["playerToken"], root.host_ca
                )
                self.transport_key = (status["serverInstance"], epoch, status.get("leaseGeneration"))
            assert self.transport is not None
            return self.transport

    async def _collect(self, request: CommunicationRun) -> int:
        async with self.collection:
            root, store = self.root, self.store(request.scope)
            if await root.get("nativeCommunicationSealed:" + request.scope.epoch, False):
                return 0
            if root.gateway.routing is not None:
                await root.gateway.routing.recover(request.scope)
            connection = await self.connection(request.scope.epoch)
            identity = await connection.tool("player_communication", {"op": "state", "epoch": request.scope.epoch})
            if identity.get("civilization") != root.civilization:
                raise JournalConflict("PLAYER_CREDENTIAL_MISMATCH")
            cursor = await root.io.call(root.memory.cursor)
            page = await connection.tool(
                "player_communication",
                {
                    "op": "read",
                    "epoch": request.scope.epoch,
                    "cursor": cursor,
                    "limit": 100,
                    "waitSeconds": 0,
                },
            )
            if (
                page.get("epoch") != request.scope.epoch
                or type(page.get("nextCursor")) is not int
                or page["nextCursor"] < cursor
            ):
                raise JournalConflict("COMMUNICATION_PAGE_INVALID")
            events = page["events"]
            ids = [event["id"] for event in events]
            if ids != sorted(set(ids)) or (ids and not cursor < ids[0] <= ids[-1] <= page["nextCursor"]):
                raise JournalConflict("COMMUNICATION_PAGE_INVALID")
            await root.io.call(store.ingest, events, page["nextCursor"])
            if self.notify is not None and not self.held:
                for notice in await root.io.call(store.pending):
                    await self.notify(notice)
                    await root.io.call(store.signaled, notice)
            return page["nextCursor"]

    async def resolve(self, registered):
        binding = registered.identity
        connection = await self.connection(binding.scope.epoch)
        job = restore_job(
            registered.state, connection, job_id=binding.job, purpose="communication", epoch=binding.scope.epoch
        )
        self.root.gateway.jobs[job.token] = job
        return job

    async def prepare(self, batch: CommunicationBatch) -> RoleRequest | None:
        root, store = self.root, self.store(batch.run.scope)
        identifier = digest(
            [
                batch.run.scope.model_dump(),
                self.generation,
                [notice.model_dump(mode="json") for notice in batch.notices],
            ]
        )
        binding = GatewayJob(
            scope=batch.run.scope.model_copy(update={"activation": identifier}), job=identifier, purpose="communication"
        )
        key = "nativeCommunicationJob:" + identifier
        async with root.registry.dispatch:
            record = await root.get(key)
            if record is not None and record.get("result"):
                return None
            connection = await self.connection(batch.run.scope.epoch)
            if record is not None:
                if record["batch"] != batch.model_dump(mode="json"):
                    raise JournalConflict("COMMUNICATION_BATCH_CHANGED")
                request = RoleRequest.model_validate(record["request"])
                state = GatewayState.model_validate(record["state"])
                catalogue = root.store.get(binding.scope, request.catalogue)
            else:
                pending = await root.io.call(store.assign, batch.notices, identifier)
                if not pending:
                    return None
                snapshot = await root.io.call(store.snapshot, pending)
                job, prefix = await prepare_job(root, binding, connection, snapshot=snapshot)
                prepared = await prepare_startup(root.gateway, job, call_prefix=prefix)
                root.gateway.commit_startup_coordination(job)
                catalogue = await root.gateway.tools(job)
                catalogue.append(
                    {
                        "name": "finish_role",
                        "description": "Finish this bounded communication job after reading and handling its messages.",
                        "inputSchema": BoundaryModel.model_json_schema(),
                    }
                )
                job.catalogue = catalogue
                startup: JsonObject = {
                    "instructions": COMMUNICATION_INSTRUCTIONS
                    + "\nUse native function tools. End with finish_role including a concise summary.",
                    "user": PreparedRoleContext.from_prepared(
                        prepared,
                        events=[notice.native_id for notice in pending],
                    ).serialize(fixture=root.fixture_startup(binding)),
                }
                delivery = delivered_prepared_context(root.gateway, job, catalogue, startup["user"])
                from civ_agent.prepared_role_context import PreparedDelivery

                startup["preparedDelivery"] = PreparedDelivery.freeze(delivery)
                state = capture_job(job, delivered_access=delivery.delivered_access)
                request = RoleRequest(
                    gameplay_policy=coordinator_gameplay_policy(root),
                    execution_policy=coordinator_execution_policy(root),
                    **binding.model_dump(),
                    typed_authored_plans=True,
                    selection=batch.run.selection,
                    provider_contract=root.get_provider_contract("communication", selection=batch.run.selection),
                    request_limit=batch.run.request_limit,
                    managed=batch.run.managed and batch.run.model_recovery,
                    peace_approvals=batch.run.managed and batch.run.model_recovery,
                    startup=root.store.put(binding.scope, startup),
                    catalogue=root.store.put(binding.scope, catalogue),
                )
                record = {
                    "batch": batch.model_dump(mode="json"),
                    "request": request.model_dump(mode="json"),
                    "state": state.model_dump(mode="json"),
                }
                await root.put(key, record)
        await root.registry.register(binding, state, catalogue)
        return request

    async def finish(self, finished: FinishCommunication):
        root, batch = self.root, finished.batch
        store = self.store(batch.run.scope)
        identifier = digest(
            [
                batch.run.scope.model_dump(),
                self.generation,
                [notice.model_dump(mode="json") for notice in batch.notices],
            ]
        )
        expected = GatewayJob(
            scope=batch.run.scope.model_copy(update={"activation": identifier}), job=identifier, purpose="communication"
        )
        if (finished.result.scope, finished.result.job, finished.result.purpose, finished.result.status) != (
            expected.scope,
            identifier,
            "communication",
            "COMPLETED",
        ):
            raise JournalConflict("COMMUNICATION_RESULT_CONFLICT")
        if root.store.receipt(finished.result.scope, "role-result") != finished.result.model_dump(mode="json"):
            raise JournalConflict("COMMUNICATION_RECEIPT_REQUIRED")
        async with root.registry.dispatch:
            registered = await root.io.call(root.registry.load, expected)
            if not registered.state.events["role_finished"]:
                raise JournalConflict("COMMUNICATION_JOB_UNFINISHED")
            await root.io.call(root.registry._retire, expected)
            await root.io.call(store.finish, batch.notices, identifier)
            record = await root.get("nativeCommunicationJob:" + identifier)
            record.pop("state", None)
            record["result"] = finished.result.model_dump(mode="json")
            await root.put("nativeCommunicationJob:" + identifier, record)

    async def close(self):
        self.closed = True
        async with self.transport_lock:
            if self.transport is not None:
                await self.transport.close()
                self.transport, self.transport_key = None, None
