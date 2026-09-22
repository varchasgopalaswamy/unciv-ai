"""Receive chat continuously while bounded model computations run independently."""

import asyncio
import contextlib
import hashlib
import shutil
import time

import aiohttp

from civ_agent.agent_history import HISTORY_METHODS
from civ_agent.agent_settings import codex_thread_settings
from civ_agent.civilization_state import StateConflict, job_id
from civ_agent.host_connection import HostConnection
from civ_agent.protocol import JsonObject
from civ_agent.schemas import ConnectRequest, validate
from civ_agent.startup_context import prepare_startup, startup_prompt, startup_text
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
        self.subscription = {
            "status": "DISCONNECTED",
            "failures": 0,
            "consecutiveFailures": 0,
            "lastSuccessAtMs": None,
            "lastFailureAtMs": None,
        }

    def subscription_health(self):
        health = dict(self.subscription)
        if self.running and self.listener is not None and self.listener.done():
            health["status"] = "STOPPED"
        return health

    async def connect(self, payload):
        validate(ConnectRequest, payload)
        await self.close()
        service = self.service
        await service.io.call(service.memory.bind, payload["epoch"])
        self.epoch = payload["epoch"]
        self.connection = HostConnection(
            service.mcp_url,
            payload["playerToken"],
            service.ca_file,
            diagnostics=service.diagnostics,
            context={"purpose": "communication", "epoch": self.epoch},
        )
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
        try:
            identity = await self.social("state")
            if identity.get("civilization") != service.civilization:
                self.running = False
                raise StateConflict("PLAYER_CREDENTIAL_MISMATCH")
            await self.reconcile()
            if service.gateway.run_failure:
                # connect is an explicit operator run/resume boundary, not a
                # subscription reconnect or an automatic model retry.
                await service.gateway.resume_run()
            await service.flush_telemetry()
            await service.io.call(service.memory.allow_communication_retry)
            if await service.io.call(service.memory.has_wake):
                self.pending.set()
        except BaseException:
            await self.close()
            raise
        self.listener = asyncio.create_task(self.receive())
        self.worker = asyncio.create_task(self.dispatch())
        self.subscription.update(status="CONNECTING", consecutiveFailures=0)
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
                if self.service.gateway.paused or self.service.gateway.checkpoint_requested:
                    # Preserve the host cursor; uncommitted arrivals are fetched
                    # again after the checkpoint barrier, without waking a model.
                    await asyncio.sleep(0.1)
                else:
                    async with self.service.gateway.admit():
                        await self.service.io.call(
                            self.service.memory.ingest, self.epoch, page["events"], page["nextCursor"]
                        )
                    if page["events"]:
                        await self.service.io.call(self.service.memory.export, self.service.workspace)
                    if await self.service.io.call(self.service.memory.has_wake):
                        self.pending.set()
                recovered_failures = self.subscription["consecutiveFailures"]
                self.subscription.update(
                    status="HEALTHY", consecutiveFailures=0, lastSuccessAtMs=int(time.time() * 1000)
                )
                if recovered_failures:
                    self.service.diagnostics.emit(
                        "communication_subscription_recovered",
                        epoch=self.epoch,
                        recoveredFailures=recovered_failures,
                        **self.subscription,
                    )
            except (OSError, ValueError, TimeoutError, aiohttp.ClientError) as error:
                self.subscription.update(
                    status="RECONNECTING",
                    failures=self.subscription["failures"] + 1,
                    consecutiveFailures=self.subscription["consecutiveFailures"] + 1,
                    lastFailureAtMs=int(time.time() * 1000),
                )
                self.service.diagnostics.emit(
                    "communication_subscription_failed", error=error, epoch=self.epoch, **self.subscription
                )
                await asyncio.sleep(1)

    async def dispatch(self):
        while self.running:
            await self.pending.wait()
            await asyncio.sleep(1)  # Batch in application code, never in model context.
            self.pending.clear()
            if (
                self.service.gateway.paused
                or self.service.gateway.checkpoint_requested
                or self.service.gateway.run_failure
                or not await self.service.io.call(self.service.memory.has_wake)
            ):
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
                    self.error = None
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
        await service.io.call(service.accounting.start, id, self.epoch, True)
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
            "requestedServiceTier": "fast" if service.fast else None,
        }
        done = asyncio.Event()
        completed = False
        final = ""
        expected_turn = None
        early = {}
        early_usage = {}

        def record_usage(usage):
            from civ_agent.agent_service import USAGE_KEYS

            total = usage.get("total", {})
            if not isinstance(total, dict) or any(
                type(total.get(field, 0)) is not int or total.get(field, 0) < 0 for field in USAGE_KEYS.values()
            ):
                return
            record["usage"] = {
                key: max(record["usage"].get(key, 0), total.get(field, 0)) for key, field in USAGE_KEYS.items()
            }
            record["usageReported"] = all(field in total for field in USAGE_KEYS.values())

        async def notification(method, params):
            nonlocal final, completed
            if method in HISTORY_METHODS:
                await service.io.call(service.history.notify, id, record["threadId"], method, params)
            if method == "service/modelStatus" and params.get("threadId") == record["threadId"]:
                service.diagnostics.emit("model_status", activationId=id, purpose="communication", **params)
            if params.get("threadId") == record["threadId"]:
                if method == "turn/completed":
                    turn = params.get("turn", {})
                    if turn.get("id") == expected_turn:
                        completed = turn.get("status") == "completed"
                        done.set()
                    else:
                        early[turn.get("id")] = turn.get("status")
                elif method == "thread/tokenUsage/updated":
                    usage = params.get("tokenUsage", {})
                    if isinstance(usage, dict):
                        if expected_turn is None:
                            early_usage[params.get("turnId")] = usage
                        elif params.get("turnId") == expected_turn:
                            record_usage(usage)
                            await service.io.call(service.accounting.sample, record)
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
        prompt = f"Handle {service.civilization}'s newly received communication."

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

        owner = asyncio.current_task()
        assert owner is not None

        async def stop_on_failure():
            await service.gateway.failed.wait()
            owner.cancel()

        failure_monitor = asyncio.create_task(stop_on_failure())

        try:
            prepared = await prepare_startup(service.gateway, job)
            startup = startup_prompt(prepared, "communication", prompt, strategy_guide=service.strategy_guide)
            record["startup"] = {
                "characters": len(startup_text(prepared, strategy_guide=service.strategy_guide)),
                "coreTools": [tool["name"] for tool in prepared["coreTools"]],
                "reads": list(prepared["reads"]),
                **startup.metadata(),
            }
            await service.io.call(service.history.begin, record, None, job.token)
            await service.io.call(service.history.startup_context, id, startup.instructions, role="developer")
            await service.io.call(service.history.startup_context, id, startup.user)
            await process.start(job.token, self.settings.get("model"))
            options: JsonObject = {
                "cwd": str(workspace),
                "approvalPolicy": "never",
                "sandbox": "workspace-write",
                "developerInstructions": startup.instructions,
                **codex_thread_settings(self.settings, fast=service.fast),
            }
            thread = await process.request("thread/start", options)
            record["threadId"] = thread["thread"]["id"]
            record["serviceTier"] = thread.get("serviceTier")
            record["effectiveSettings"] = {
                "model": thread.get("model"),
                "reasoningEffort": thread.get("reasoningEffort"),
            }
            await service.io.call(service.history.lifecycle, record)
            turn = await process.request(
                "turn/start",
                {
                    "threadId": record["threadId"],
                    "input": [{"type": "text", "text": startup.user}],
                    "approvalPolicy": "never",
                    "summary": "auto",
                    **({"serviceTier": "fast"} if service.fast else {}),
                    **({"effort": self.settings["reasoningEffort"]} if self.settings.get("reasoningEffort") else {}),
                },
            )
            expected_turn = turn["turn"]["id"]
            await service.io.call(service.history.bind_turn, id, expected_turn)
            record["phase"] = "RUNNING"
            if expected_turn in early_usage:
                record_usage(early_usage[expected_turn])
            await service.io.call(service.accounting.sample, record)
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
                    await service.io.call(service.accounting.sample, record, job.waiting_seconds())
                    await service.flush_telemetry()
                    grant = await self.social("job_heartbeat", job=id, usage=record["usage"])
                    if not grant["granted"]:
                        break
        except asyncio.CancelledError:
            if not service.gateway.run_failure:
                raise
        finally:
            failure_monitor.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await failure_monitor
            if service.gateway.run_failure:
                completed = False
                record["error"] = service.gateway.run_failure["error"]
                record["runFailure"] = service.health()["runFailure"]
            job.closed = True
            await process.close()
            await service.gateway.close(job, completed)
            record["phase"] = "FAILED" if record.get("error") else "COMPLETED" if completed else "INTERRUPTED"
            record["summary"] = final[:2400]
            record["usageComplete"] = completed and record.get("usageReported", False)
            await service.io.call(service.accounting.sample, record, job.waiting_seconds())
            await service.io.call(service.history.lifecycle, record)
            await service.io.call(export_history)
            await service.io.call(
                write_private_json,
                service.state_dir / "chat-jobs" / (hashlib.sha256(id.encode()).hexdigest() + ".json"),
                record,
            )
            with contextlib.suppress(OSError, ValueError, TimeoutError, aiohttp.ClientError):
                await service.flush_telemetry()
                await self.social("job_release", job=id, usage=record["usage"])
            self.jobs.pop(id, None)

    async def close(self):
        self.running = False
        self.subscription["status"] = "DISCONNECTED"
        for task in (self.listener, self.worker):
            if task is not None:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
        self.listener = self.worker = None
        if self.connection is not None:
            with contextlib.suppress(OSError, ValueError, TimeoutError, aiohttp.ClientError):
                await self.service.flush_telemetry()
            await self.connection.close()
            self.connection = None
