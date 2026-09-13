"""Receive chat continuously while bounded model computations run independently."""

import asyncio
import contextlib
import hashlib
import shutil
import time

import aiohttp

from civ_agent.agent_history import HISTORY_METHODS
from civ_agent.agent_settings import valid_setting
from civ_agent.civilization_state import StateConflict, job_id
from civ_agent.proposal_gateway import RECOVERY_INSTRUCTIONS, HostConnection
from civ_agent.protocol import JsonObject
from civ_agent.tls import private_directory, write_private_json


class ChatRuntime:
    def __init__(self, service):
        self.service = service
        self.connection = None
        self.epoch = None
        self.listener = None
        self.worker = None
        self.running = False
        self.settings = {}
        self.timeout = 300
        self.pending = asyncio.Event()
        self.jobs = {}
        self.error = None

    async def connect(self, payload):
        if set(payload) != {"epoch", "playerToken", "model", "reasoningEffort", "timeoutSeconds"}:
            raise ValueError("INVALID_ARGUMENT")
        if (
            any(payload[key] is not None and not valid_setting(payload[key]) for key in ("model", "reasoningEffort"))
            or type(payload["timeoutSeconds"]) is not int
            or not 1 <= payload["timeoutSeconds"] <= 86400
        ):
            raise ValueError("INVALID_ARGUMENT")
        await self.close()
        service = self.service
        await service.io.call(service.memory.bind, payload["epoch"])
        self.epoch = payload["epoch"]
        self.connection = HostConnection(service.mcp_url, payload["playerToken"], service.ca_file)
        defaults = (
            service.match.get("effectiveSettings", service.default_settings)
            if service.match
            else service.default_settings
        )
        self.settings = {
            key: payload[key] if payload[key] is not None else defaults[key] for key in ("model", "reasoningEffort")
        }
        self.timeout = payload["timeoutSeconds"]
        self.running = True
        identity = await self.social("state")
        if identity.get("civilization") != service.civilization:
            self.running = False
            raise StateConflict("PLAYER_CREDENTIAL_MISMATCH")
        await self.reconcile()
        await service.io.call(service.memory.allow_communication_retry)
        if await service.io.call(service.memory.has_wake):
            self.pending.set()
        self.listener = asyncio.create_task(self.receive())
        self.worker = asyncio.create_task(self.dispatch())
        return {"connected": True, "epoch": self.epoch}

    async def social(self, op, **fields):
        if self.connection is None:
            raise StateConflict("COMMUNICATION_NOT_CONNECTED")
        value = await self.connection.tool("player_communication", {"op": op, "epoch": self.epoch, **fields})
        if "error" in value:
            raise StateConflict("COMMUNICATION_UNAVAILABLE")
        return value

    async def reconcile(self):
        if self.connection is None:
            if await self.service.io.call(self.service.memory.pending):
                raise StateConflict("COMMUNICATION_NOT_CONNECTED")
            return
        # Only the host can settle an uncertain command. Never re-run a model to
        # guess what happened, and never send a new identity for an old request.
        for request in await self.service.io.call(self.service.memory.pending):
            import json

            payload = json.loads(request["payload"])
            if request["transport"] == "player_command":
                result = await self.connection.tool("player_receipt", payload)
            else:
                result = await self.connection.tool(
                    "player_communication", {"op": "receipt", "epoch": self.epoch, "requestId": request["id"]}
                )
            await self.service.io.call(self.service.memory.receipt, request["id"], result)

    async def receive(self):
        while self.running:
            try:
                cursor = await self.service.io.call(self.service.memory.cursor)
                page = await self.social("read", cursor=cursor, limit=100, waitSeconds=25)
                if self.service.gateway.paused:
                    # Preserve the host cursor; uncommitted arrivals are fetched
                    # again after the checkpoint barrier, without waking a model.
                    await asyncio.sleep(0.1)
                    continue
                await self.service.io.call(self.service.memory.ingest, self.epoch, page["events"], page["nextCursor"])
                if page["events"]:
                    await self.service.io.call(self.service.memory.export, self.service.workspace)
                if await self.service.io.call(self.service.memory.has_wake):
                    self.pending.set()
                self.error = None
            except (OSError, ValueError, TimeoutError, aiohttp.ClientError):
                self.error = "EVENT_SUBSCRIPTION_RECONNECTING"
                await asyncio.sleep(1)

    async def dispatch(self):
        while self.running:
            await self.pending.wait()
            await asyncio.sleep(1)  # Batch in application code, never in model context.
            self.pending.clear()
            if self.service.gateway.paused or not await self.service.io.call(self.service.memory.has_wake):
                continue
            job = job_id()
            try:
                grant = await self.social("job_claim", job=job, purpose="communication")
                if not grant["granted"]:
                    self.pending.set()
                    await asyncio.sleep(1)
                    continue
                try:
                    async with asyncio.timeout(self.timeout):
                        await self.compute(job)
                finally:
                    with contextlib.suppress(OSError, ValueError, TimeoutError, aiohttp.ClientError):
                        await self.social("job_release", job=job)
                if await self.service.io.call(self.service.memory.has_wake):
                    self.pending.set()
            except (OSError, ValueError, TimeoutError, aiohttp.ClientError):
                self.error = "COMMUNICATION_JOB_FAILED"
                # A failure does not cause an unbounded stream of paid retries.
                # The next new event or operator resume can trigger reconsideration.

    async def compute(self, id):
        service = self.service
        job = await service.gateway.open(id, "communication", self.epoch, self.connection, self.settings)
        record = {
            "activationId": id,
            "civilization": service.civilization,
            "epoch": self.epoch,
            "threadId": None,
            "phase": "STARTING",
            "usage": {},
            "settings": self.settings,
            "purpose": "communication",
        }
        done = asyncio.Event()
        completed = False
        final = ""
        expected_turn = None
        early = {}

        async def notification(method, params):
            nonlocal final, completed
            if method in HISTORY_METHODS:
                await service.io.call(service.history.notify, id, record["threadId"], method, params)
            if params.get("threadId") == record["threadId"]:
                if method == "turn/completed":
                    turn = params.get("turn", {})
                    if turn.get("id") == expected_turn:
                        completed = turn.get("status") == "completed"
                        done.set()
                    else:
                        early[turn.get("id")] = turn.get("status")
                elif method == "thread/tokenUsage/updated":
                    from civ_agent.agent_service import USAGE_KEYS

                    total = params.get("tokenUsage", {}).get("total", {})
                    record["usage"] = {key: total.get(field, 0) for key, field in USAGE_KEYS.items()}
                elif method == "item/completed":
                    item = params.get("item", {})
                    if item.get("type") == "agentMessage":
                        final = item.get("text", "")
            if method in {"service/disconnected", "service/approvalRequired"}:
                done.set()

        workspace = private_directory(service.workspace / "jobs" / id)
        process = service.process_factory(
            service.executable, workspace, service.gateway_url, service.gateway_ca_file, notification
        )
        self.jobs[id] = (job, process)
        prompt = (
            f"Handle {service.civilization}'s newly received communication. Ordinary gameplay is handled separately. "
            "Read chat_read and relevant memory once. Its context includes the turn, economy, diplomatic status and "
            "your scenario.instructions. Use player_turn only if a specific question needs additional gameplay detail. "
            "Follow your scenario.instructions within your "
            "communication permissions; decide whether a useful response, social commitment or binding trade/peace deal "
            "is warranted. Read exact binding terms with trade_read before trade_decide; trade_options and trade_offer "
            "support negotiation outside your gameplay turn. Social consent alone never transfers resources or enacts a treaty. "
            "You may send private/public messages and propose memory changes. Never poll, wait for a "
            "reply, or simulate another player. Statements are attributed claims, not authoritative game facts. "
            "Do not answer greetings with endless acknowledgements. Make concise memory updates for decisions or "
            "beliefs worth retaining. "
            + RECOVERY_INSTRUCTIONS
            + "Finish with a short summary.\n\n"
            + service.memory.context(job.memory)
        )

        def export_history():
            log = service.history.logs.get(id)
            if log is not None:
                log.render()
                destination = private_directory(service.workspace / "communication" / id)
                for name in ("events.jsonl", "history.md"):
                    source = service.history.path(id) / name
                    if source.exists():
                        shutil.copyfile(source, destination / name)
                        (destination / name).chmod(0o600)

        try:
            await service.io.call(service.history.begin, record, prompt, job.token)
            await process.start(job.token, self.settings.get("model"))
            options: JsonObject = {"cwd": str(workspace), "approvalPolicy": "never", "sandbox": "workspace-write"}
            if self.settings.get("model"):
                options["model"] = self.settings["model"]
            if self.settings.get("reasoningEffort"):
                options["config"] = {"model_reasoning_effort": self.settings["reasoningEffort"]}
            thread = await process.request("thread/start", options)
            record["threadId"] = thread["thread"]["id"]
            record["effectiveSettings"] = {
                "model": thread.get("model"),
                "reasoningEffort": thread.get("reasoningEffort"),
            }
            await service.io.call(service.history.lifecycle, record)
            turn = await process.request(
                "turn/start",
                {
                    "threadId": record["threadId"],
                    "input": [{"type": "text", "text": prompt}],
                    "approvalPolicy": "never",
                    "summary": "auto",
                    **({"effort": self.settings["reasoningEffort"]} if self.settings.get("reasoningEffort") else {}),
                },
            )
            expected_turn = turn["turn"]["id"]
            await service.io.call(service.history.bind_turn, id, expected_turn)
            if expected_turn in early:
                completed = early[expected_turn] == "completed"
                done.set()
            deadline = time.monotonic() + self.timeout
            while not done.is_set():
                try:
                    await asyncio.wait_for(done.wait(), min(10, max(0.01, deadline - time.monotonic())))
                except TimeoutError:
                    if time.monotonic() >= deadline:
                        break
                    grant = await self.social("job_heartbeat", job=id, usage=record["usage"])
                    if not grant["granted"]:
                        break
        finally:
            job.closed = True
            await process.close()
            await service.gateway.close(job, completed)
            record["phase"] = "COMPLETED" if completed else "INTERRUPTED"
            record["summary"] = final[:2400]
            await service.io.call(service.history.lifecycle, record)
            await service.io.call(export_history)
            await service.io.call(
                write_private_json,
                service.state_dir / "chat-jobs" / (hashlib.sha256(id.encode()).hexdigest() + ".json"),
                record,
            )
            with contextlib.suppress(OSError, ValueError, TimeoutError, aiohttp.ClientError):
                await self.social("job_release", job=id, usage=record["usage"])
            self.jobs.pop(id, None)

    async def close(self):
        self.running = False
        for task in (self.listener, self.worker):
            if task is not None:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
        self.listener = self.worker = None
