"""Prove native durability can recover host effects without a Python receipt journal.

The HTTP fixture models the Kotlin host's serialized exact-request ledger. It is
not a production adapter or a proof of the Kotlin checkpoint implementation.
"""

import asyncio
import copy
from dataclasses import dataclass

import aiohttp
from aiohttp import web
from pydantic_ai import RunContext
from pydantic_ai.capabilities import AbstractCapability, durable_operation
from pydantic_ai.messages import ModelRequest, ModelResponse, TextPart, ToolCallPart, ToolReturnPart
from pydantic_ai_harness import StepPersistence
from pydantic_ai_harness.step_persistence import InMemoryStepStore

from civ_agent.orchestration.contracts import digest
from civ_agent.schemas import BoundaryModel


class ProbeDeps(BoundaryModel):
    host: str


class FrozenCommand(BoundaryModel):
    epoch: str
    revision: str
    requestId: str
    kind: str = "SET_CITY_FOCUS"
    target: str = "city1"
    selection: str = "Production"


class HostLedger:
    """Remote authority: serialize lookup/mutation and bind IDs to exact inputs."""

    def __init__(self, *, unknown=False, lose_reply=False):
        self.unknown, self.lose_reply = unknown, lose_reply
        self.lock = asyncio.Lock()
        self.epoch, self.revision = "epoch1", "revision0"
        self.requests = {}
        self.effects = 0
        self.prepares = 0
        self.lookups = []
        self.sends = []
        self.committed = asyncio.Event()
        self.release_reply = asyncio.Event()

    async def state(self, request):
        self.prepares += 1
        return web.json_response({"epoch": self.epoch, "revision": self.revision})

    async def lookup(self, request):
        command = await request.json()
        async with self.lock:
            self.lookups.append(copy.deepcopy(command))
            if self.unknown:
                return web.json_response({"error": "HOST_UNAVAILABLE"})
            stored = self.requests.get(command["requestId"])
            # Kotlin completedReceipt also returns no receipt for non-matching
            # fields. A missing lookup is not proof that no request is in flight.
            result = stored[1] if stored is not None and stored[0] == command else {"error": "NOT_ENACTED"}
            return web.json_response(result)

    async def execute(self, request):
        command = await request.json()
        async with self.lock:
            self.sends.append(copy.deepcopy(command))
            if command["epoch"] != self.epoch:
                return web.json_response({"error": "STALE_EPOCH"})
            stored = self.requests.get(command["requestId"])
            if stored is not None:
                return web.json_response(stored[1] if stored[0] == command else {"error": "REQUEST_ID_CONFLICT"})
            if command["revision"] != self.revision:
                return web.json_response({"error": "STALE_REVISION"})
            self.effects += 1
            self.revision = f"revision{self.effects}"
            receipt = {"requestId": command["requestId"], "revision": self.revision, "actionSummary": "Focus set."}
            self.requests[command["requestId"]] = (copy.deepcopy(command), receipt)
            self.committed.set()
            lose_reply, self.lose_reply = self.lose_reply, False
        if lose_reply:
            # Commit and release the host's mutation lock before losing the ACK.
            # A retry can therefore recover through the read-only endpoint.
            await self.release_reply.wait()
            raise web.HTTPServiceUnavailable()
        return web.json_response(receipt)


async def serve(ledger):
    app = web.Application()
    app.router.add_get("/state", ledger.state)
    app.router.add_post("/receipt", ledger.lookup)
    app.router.add_post("/execute", ledger.execute)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    assert runner.addresses
    return runner, f"http://127.0.0.1:{runner.addresses[0][1]}"


@dataclass
class NativeBoundary(AbstractCapability[ProbeDeps]):
    id: str | None = "native-receipt-boundary"

    @durable_operation("freeze_command")
    async def prepare(self, ctx: RunContext[ProbeDeps]) -> FrozenCommand:
        async with aiohttp.ClientSession() as client:
            async with client.get(ctx.deps.host + "/state") as response:
                response.raise_for_status()
                state = await response.json()
        return FrozenCommand(**state, requestId="r" + digest([ctx.run_id, ctx.tool_call_id]))

    @durable_operation("execute_command")
    async def execute(self, ctx: RunContext[ProbeDeps], command: FrozenCommand) -> dict:
        # The native durable-operation activity input owns this frozen envelope.
        # Never rebuild it from a newly refreshed revision on activity retry.
        payload = command.model_dump()
        async with aiohttp.ClientSession() as client:
            async with client.post(ctx.deps.host + "/receipt", json=payload) as response:
                response.raise_for_status()
                found = await response.json()
            if found.get("error") != "NOT_ENACTED":
                return found if "error" not in found else {"uncertain": True, "requestId": command.requestId}
            # A lookup miss does not fence a pending send. Retrying only this same
            # ID and exact envelope is safe because the host serializes mutations
            # and rejects ID/argument conflicts. It never authorizes a fresh ID.
            async with client.post(ctx.deps.host + "/execute", json=payload) as response:
                response.raise_for_status()
                return await response.json()


def respond(messages, info):
    if any(
        isinstance(part, ToolReturnPart)
        for message in messages
        if isinstance(message, ModelRequest)
        for part in message.parts
    ):
        return ModelResponse(parts=[TextPart("Observed the native outcome.")])
    return ModelResponse(parts=[ToolCallPart("native_action", {}, "action1")])


boundary = NativeBoundary()
persistence = StepPersistence(store=InMemoryStepStore(), capture_frontier=True)


def test_remote_exact_payload_ledger_handles_pending_duplicates_and_rejects_conflicts():
    async def run():
        ledger = HostLedger()
        runner, host = await serve(ledger)
        original = FrozenCommand(
            epoch=ledger.epoch, revision=ledger.revision, requestId="stable-operation"
        ).model_dump()
        try:
            async with aiohttp.ClientSession() as client:

                async def post(path, payload):
                    async with client.post(host + path, json=payload) as response:
                        response.raise_for_status()
                        return await response.json()

                first, pending_duplicate = await asyncio.gather(post("/execute", original), post("/execute", original))
                assert first == pending_duplicate and ledger.effects == 1
                assert await post("/receipt", original) == first
                assert await post("/execute", {**original, "selection": "Gold"}) == {"error": "REQUEST_ID_CONFLICT"}
                assert await post("/receipt", {**original, "selection": "Gold"}) == {"error": "NOT_ENACTED"}
                assert await post("/execute", {**original, "requestId": "changed-id"}) == {"error": "STALE_REVISION"}
                assert await post("/execute", {**original, "epoch": "old-epoch"}) == {"error": "STALE_EPOCH"}
                assert ledger.effects == 1
                # The host cannot recognize a semantically duplicate action under
                # a fresh ID and fresh revision. Stable identity is indispensable.
                assert "error" not in await post(
                    "/execute", {**original, "requestId": "fresh-decision", "revision": ledger.revision}
                )
                assert ledger.effects == 2
        finally:
            await runner.cleanup()

    asyncio.run(run())
