"""Receive chat continuously while bounded model computations run independently."""

import asyncio
import contextlib
import hashlib
import time

import aiohttp

from civ_agent.agent_history import HISTORY_METHODS
from civ_agent.agent_settings import codex_thread_settings, communication_effort
from civ_agent.civilization_state import StateConflict, job_id, receipt_is_uncertain
from civ_agent.host_connection import HostConnection
from civ_agent.protocol import JsonObject
from civ_agent.provider_usage import reconcile_provider_usage
from civ_agent.schemas import ConnectRequest, validate
from civ_agent.startup_context import prepare_startup, startup_prompt, startup_text
from civ_agent.telemetry import merge_model_usage
from civ_agent.tls import private_directory, write_private_json

COMMUNICATION_BATCH_SECONDS = 3


class ChatRuntime:
    def __init__(self, service):
        self.service = service
        self.connection = None
        self.epoch = None
        self.listener = None
        self.worker = None
        self.routing_worker = None
        self.routing_task = None
        self.running = False
        self.held = True
        self.dispatching = False
        self.settings = {}
        self.gameplay_settings = {}
        self.timeout = 300
        self.pending = asyncio.Event()
        self.jobs = {}
        self.job_records = {}
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
        self.player_token = payload["playerToken"]
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
        gameplay_effort = (
            payload["reasoningEffort"] if payload["reasoningEffort"] is not None else defaults["reasoningEffort"]
        )
        self.gameplay_settings = {
            "provider": defaults["provider"],
            "model": payload["model"] if payload["model"] is not None else defaults["model"],
            "reasoningEffort": gameplay_effort,
        }
        self.settings = {
            "provider": self.gameplay_settings["provider"],
            "model": self.gameplay_settings["model"],
            "reasoningEffort": communication_effort(gameplay_effort, service.communication_reasoning_effort),
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
        if service.gateway.routing is not None:
            self.routing_worker = asyncio.create_task(self.schedule_routing())
        self.subscription.update(status="CONNECTING", consecutiveFailures=0)
        return {
            "connected": True,
            "epoch": self.epoch,
            "reviewInterval": service.reviews.interval,
            "decisionsEnabled": service.decisions_enabled,
        }

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
        gateway = self.service.gateway
        routing = gateway.routing
        if routing is not None:
            await routing.reconcile(self.epoch, self.connection)
        # Only the host can settle an uncertain command. Never re-run a model to
        # guess what happened, and never send a new identity for an old request.
        async with gateway.dispatch:
            gateway.in_flight += 1
            try:
                for request in await self.service.io.call(self.service.memory.pending, exclude_purpose="delivery"):
                    import json

                    payload = json.loads(request["payload"])
                    if request["transport"] == "player_command":
                        result = await self.connection.tool("player_receipt", payload)
                    else:
                        result = await self.connection.tool(
                            "player_communication", {"op": "receipt", "epoch": self.epoch, "requestId": request["id"]}
                        )
                    if receipt_is_uncertain(result):
                        await self.service.io.call(self.service.memory.uncertain, request["id"])
                        continue
                    await self.service.io.call(self.service.memory.receipt, request["id"], result)
            finally:
                gateway.in_flight -= 1

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

    async def schedule_routing(self):
        """Delivery deadlines run independently of player-model computation."""

        async def classify_arrivals():
            try:
                routing = self.service.gateway.routing
                rows = await self.service.io.call(routing.store.incoming)
                if any(not row["routing"].get("choice") for row in rows):
                    await routing.route_incoming(self.epoch, await self.social("state"), lambda: self.social("state"))
            except (OSError, ValueError, TimeoutError, aiohttp.ClientError):
                pass  # The next tick can reconsider unclassified events.
            self.pending.set()

        while self.running:
            await asyncio.sleep(0.25)
            gateway = self.service.gateway
            if self.held or gateway.paused or gateway.checkpoint_requested or gateway.run_failure:
                continue
            try:
                await gateway.routing.expire_incoming(self.epoch)
                await gateway.routing.flush_due(self.epoch, self.connection)
            except (OSError, ValueError, TimeoutError, aiohttp.ClientError):
                pass  # Uncertain sends retain their durable identity for receipt lookup.
            if self.routing_task is None or self.routing_task.done():
                self.routing_task = asyncio.create_task(classify_arrivals())
            if await self.service.io.call(self.service.memory.has_wake):
                self.pending.set()

    async def dispatch(self):
        while self.running:
            routing = self.service.gateway.routing
            if routing is None:
                await self.pending.wait()
                await asyncio.sleep(COMMUNICATION_BATCH_SECONDS)
            else:
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(self.pending.wait(), 0.5)
            self.pending.clear()
            if (
                self.held
                or self.service.gateway.paused
                or self.service.gateway.checkpoint_requested
                or self.service.gateway.run_failure
            ):
                continue
            if not await self.service.io.call(self.service.memory.has_wake):
                continue
            job = job_id()
            self.dispatching = True
            try:
                grant = await self.social("job_claim", job=job, purpose="communication")
                if not grant["granted"]:
                    self.pending.set()
                    await asyncio.sleep(1)
                    continue
                try:
                    if not self.held:
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
            finally:
                self.dispatching = False

    async def compute(self, id):
        from civ_agent.agent_service import ProcessDrainError

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
            "modelRequests": 0,
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
            merge_model_usage(record, total, USAGE_KEYS)

        async def notification(method, params):
            nonlocal final, completed
            if method == "service/transportStatus":
                service.diagnostics.emit("codex_transport", activationId=id, purpose="communication", **params)
                return
            if method == "service/transportFailure":
                if params.get("reason") in {
                    "CODEX_NOTIFICATION_OVERFLOW",
                    "CODEX_NOTIFICATION_FAILED",
                    "CODEX_DISCONNECTED",
                }:
                    record["transportError"] = params["reason"]
                    record["error"] = record.get("error") or params["reason"]
                    completed = False
                    done.set()
                return
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
        record_file = service.state_dir / "chat-jobs" / (hashlib.sha256(id.encode()).hexdigest() + ".json")
        process = service.process_factory(
            service.executable, workspace, service.gateway_url, service.gateway_ca_file, notification
        )
        self.jobs[id] = (job, process)
        self.job_records[id] = (record, record_file)
        prompt = f"Pursue {service.civilization}'s diplomatic objectives in light of newly received communication."

        owner = asyncio.current_task()
        assert owner is not None

        async def stop_on_failure():
            await service.gateway.failed.wait()
            owner.cancel()

        failure_monitor = asyncio.create_task(stop_on_failure())

        try:
            prepared = await prepare_startup(service.gateway, job)
            startup = startup_prompt(
                prepared,
                "communication",
                prompt,
                civilization=service.civilization,
            )
            record["startup"] = {
                "characters": len(
                    startup_text(
                        prepared,
                        civilization=service.civilization,
                    )
                ),
                "coreTools": [tool["name"] for tool in prepared["coreTools"]],
                "reads": list(prepared["reads"]),
                "decisionDetails": len(prepared.get("decisionDetails", [])),
                **startup.metadata(),
            }
            await service.io.call(service.history.begin, record, None, job.token)
            await service.io.call(write_private_json, record_file, record)
            await service.io.call(service.history.startup_context, id, startup.instructions, role="developer")
            await service.io.call(service.history.startup_context, id, startup.user)
            await process.start(job.token, self.settings)
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
                "provider": thread.get("modelProvider"),
                "model": thread.get("model"),
                "reasoningEffort": thread.get("reasoningEffort"),
            }
            if any(
                value is not None and record["effectiveSettings"][key] != value for key, value in self.settings.items()
            ):
                raise ValueError("CODEX_SETTINGS_NOT_APPLIED")
            await service.io.call(service.history.lifecycle, record)
            await service.io.call(write_private_json, record_file, record)
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
            record["summary"] = final[:2400]
            try:
                await process.close()
            except ProcessDrainError:
                record["error"] = "HISTORY_DRAIN_FAILED"
                self.error = "HISTORY_DRAIN_FAILED"
                raise
            except BaseException:
                # Retain ownership for close() to retry. In particular, do not
                # finish the gateway job or shut down I/O under live callbacks.
                record["error"] = "STOP_UNCONFIRMED"
                self.error = "STOP_UNCONFIRMED"
                raise
            if record.get("transportError"):
                # close() drains accepted notifications; a completion followed
                # by transport failure during that drain is still a failed job.
                completed = False
            record["summary"] = final[:2400]
            await self.finalize(id, completed)

    async def finalize(self, id, completed):
        service = self.service
        job, _ = self.jobs[id]
        record, record_file = self.job_records[id]
        trace_complete = await service.io.call(reconcile_provider_usage, record, service.provider_usage)
        await service.gateway.close(job, completed)
        record["phase"] = "FAILED" if record.get("error") else "COMPLETED" if completed else "INTERRUPTED"
        record["usageComplete"] = trace_complete or (completed and record.get("usageReported", False))
        await service.io.call(service.accounting.sample, record, job.waiting_seconds())
        await service.io.call(service.history.lifecycle, record)
        await service.io.call(write_private_json, record_file, record)
        with contextlib.suppress(OSError, ValueError, TimeoutError, aiohttp.ClientError):
            await service.flush_telemetry()
            await self.social("job_release", job=id, usage=record["usage"])
        self.jobs.pop(id, None)
        self.job_records.pop(id, None)

    async def close(self):
        await self.service.reviews.close()
        self.held = True
        self.service.gateway.reviewing = False
        self.running = False
        self.subscription["status"] = "DISCONNECTED"
        for task in (self.listener, self.worker, self.routing_worker, self.routing_task):
            if task is not None:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
                except Exception:
                    if not self.jobs:
                        raise
                    self.error = self.error or "COMMUNICATION_JOB_FAILED"
        self.listener = self.worker = None
        self.routing_worker = self.routing_task = None
        for id, (_, process) in list(self.jobs.items()):
            await process.close()
            record, _ = self.job_records[id]
            record["error"] = record.get("error") or "COMMUNICATION_JOB_FAILED"
            await self.finalize(id, False)
        if self.connection is not None:
            with contextlib.suppress(OSError, ValueError, TimeoutError, aiohttp.ClientError):
                await self.service.flush_telemetry()
            await self.connection.close()
            self.connection = None
