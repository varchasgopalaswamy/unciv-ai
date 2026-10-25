"""Isolated subordinate conversations sharing the civilization gateway and storage."""

import asyncio
import contextlib
import copy
from typing import Any

from civ_agent.effort_decisions import base_settings
from civ_agent.provider_usage import reconcile_provider_usage
from civ_agent.startup_context import prepare_startup, startup_prompt
from civ_agent.tls import private_directory


class RoleRuntime:
    def __init__(self, service, coordinator, payload, attempt=1, *, role="economy"):
        self.role = role
        self.service, self.coordinator, self.payload = service, coordinator, payload
        self.active_id = payload["activationId"] + ":" + role + (f":{attempt}" if attempt > 1 else "")
        self.stop_event = asyncio.Event()
        self.failure: Any = None
        self.turn_id: Any = None
        self.completion: Any = None
        self.process: Any = None
        self.early_usage, self.early_completions = {}, {}
        self.accept_usage = self.stopping = False
        self.started = asyncio.Event()
        self.job: Any = None
        # Reuse the tested model transport, with all conversation state owned by this instance.
        for name in (
            "_notification",
            "_record_usage",
            "_model_turn",
            "_start_model_turn",
            "_interrupt_model_turn",
            "_settle_negotiation",
        ):
            setattr(self, name, getattr(type(service), name).__get__(self))

    def __getattr__(self, name) -> Any:
        return getattr(self.service, name)

    async def run(self, parent_record):
        service, payload = self.service, self.payload
        selected = base_settings(parent_record)
        selected.update(
            {
                ("reasoningEffort" if k == "reasoning_effort" else k): v
                for k, v in getattr(service, self.role + "_model").items()
            }
        )
        self.fast = service.fast and selected["provider"] == "openai"
        record = {
            "activationId": self.active_id,
            "purpose": self.role,
            "epoch": payload["epoch"],
            "gameTurn": payload["gameTurn"],
            "civilization": service.civilization,
            "phase": "STARTING",
            "modelPhase": self.role,
            "settings": selected,
            "usage": {},
            "modelRequests": 0,
            "requestedServiceTier": "fast" if self.fast else None,
            "handoff": payload["handoff"],
            "parentActivation": parent_record["activationId"],
        }
        service.records[self.active_id] = record
        granted = False
        monitor = None
        try:
            if service.chat.running:
                from civ_agent.agent_service import ServiceError

                while True:
                    admission = await service.chat.social(
                        "job_claim", job=self.active_id, purpose=self.role, parentJob=parent_record["activationId"]
                    )
                    if admission["granted"]:
                        break
                    if admission.get("reason") in {"MODEL_INVOCATION_LIMIT", "ROLE_CAPACITY_TOO_SMALL"}:
                        raise ServiceError(admission["reason"], 503)
                    if service.failure or service.stopping:
                        raise RuntimeError(self.role.upper() + "_ADMISSION_FAILED")
                    await asyncio.sleep(0.25)
                granted = True
            await service.io.call(service.accounting.start, self.active_id, payload["epoch"], granted)
            async with service.connection_factory(
                service.mcp_url,
                payload["playerToken"],
                service.ca_file,
                diagnostics=service.diagnostics,
                context={"purpose": self.role},
            ) as connection:
                self.job = await service.gateway.open(
                    self.active_id, self.role, payload["epoch"], connection, selected, payload["gameTurn"]
                )
                self.coordinator.register(self.job)
                await service.io.call(service.history.begin, copy.deepcopy(record), None, payload["playerToken"])

                async def heartbeat():
                    try:
                        while True:
                            await asyncio.sleep(10)
                            await service.io.call(service.accounting.sample, record, self.job.waiting_seconds())
                            await service.flush_telemetry()
                            if granted:
                                result = await service.chat.social("job_heartbeat", job=self.active_id)
                                if not result["granted"]:
                                    self.failure = "JOB_PERMIT_EXPIRED"
                                    self.stop_event.set()
                                    return
                    except Exception as error:
                        self.failure = self.role.upper() + "_HEARTBEAT_FAILED"
                        service.diagnostics.emit(
                            self.role + "_heartbeat_failed", error=error, activationId=self.active_id
                        )
                        self.stop_event.set()

                monitor = asyncio.create_task(heartbeat())
                self.started.set()
                prepared = await prepare_startup(service.gateway, self.job)
                prompt = startup_prompt(
                    prepared,
                    self.role,
                    payload["prompt"],
                    strategy_guide=service.strategy_guide,
                    civilization_guide=service.civilization_guide,
                    civilization=service.civilization,
                )
                record["startup"] = {"characters": len(prompt.user), **prompt.metadata()}
                await service._save(record)
                self.started.set()
                workspace = private_directory(service.workspace / "jobs" / (self.active_id.replace(":", "_")[-64:]))
                await self._model_turn(record, self.job, prompt, workspace, selected, prepared)
                if self.failure or not (self.coordinator.committed or self.job.gameplay_ended.is_set()):
                    raise RuntimeError(self.failure or self.role.upper() + "_ROLE_INCOMPLETE")
                record["phase"] = "COMPLETED"
        except BaseException as error:
            record["phase"] = "INTERRUPTED" if isinstance(error, asyncio.CancelledError) else "FAILED"
            record["error"] = str(error) or self.role.upper() + "_ROLE_FAILED"
            raise
        finally:
            self.started.set()
            self.stopping = True
            if monitor:
                monitor.cancel()
                await asyncio.gather(monitor, return_exceptions=True)
            if self.process:
                try:
                    await self.process.close()
                except BaseException:
                    record["phase"] = "RUNNING"
                    record["error"] = "STOP_UNCONFIRMED"
                    if self.job:
                        self.job.gameplay_fenced = True
                    await service._save(record)
                    raise
                self.process = None
            if self.job:
                self.job.model_stopped.set()
            if self.job:
                await service.gateway.close(self.job, record["phase"] == "COMPLETED")
            reconciled = await service.io.call(reconcile_provider_usage, record, service.provider_usage)
            record["usageComplete"] = reconciled or (
                record["phase"] == "COMPLETED" and record.get("usageReported", False)
            )
            await service.io.call(service.accounting.sample, record, self.job.waiting_seconds() if self.job else 0)
            await service._save(record)
            if granted:
                with contextlib.suppress(OSError, ValueError, TimeoutError):
                    await service.flush_telemetry()
                    await service.chat.social("job_release", job=self.active_id, usage=record["usage"])


EconomyRuntime = RoleRuntime


async def run_role(service, coordinator, payload, parent_record, role="economy"):
    """Run an isolated role with the same reconciliation and retry contract."""
    for attempt in range(1, 4):
        runtime = (
            getattr(service, role + "_runtime")
            if attempt == 1
            else RoleRuntime(service, coordinator, payload, attempt, role=role)
        )
        setattr(service, role + "_runtime", runtime)
        try:
            await runtime.run(parent_record)
            return
        except asyncio.CancelledError:
            raise
        except Exception as error:
            from sqlalchemy.exc import SQLAlchemyError

            from civ_agent.agent_service import ServiceError

            if isinstance(error, ServiceError) and error.code in {"MODEL_INVOCATION_LIMIT", "ROLE_CAPACITY_TOO_SMALL"}:
                service.failure = error.code
                service.stop_event.set()
                raise
            if runtime.process is not None:
                service.failure = "STOP_UNCONFIRMED"
                service.stop_event.set()
                raise ServiceError("STOP_UNCONFIRMED", 503) from error
            if coordinator.committed or isinstance(error, (OSError, SQLAlchemyError)):
                service.failure = "DURABLE_RECORD_FAILED"
                service.stop_event.set()
                raise ServiceError("DURABLE_RECORD_FAILED", 503) from error
            if service.gateway.run_failure or await service.io.call(service.memory.pending) or attempt == 3:
                service.failure = role.upper() + "_ROLE_FAILED"
                service.stop_event.set()
                raise
            coordinator.ready[role] = False
            coordinator.resumes[role].clear()
            await coordinator.save()


async def run_economy(service, coordinator, payload, parent_record):
    await run_role(service, coordinator, payload, parent_record, "economy")


async def run_scheduled_economy(service, coordinator, payload, parent_record):
    """A dormant role consumes no model process, compute permit or startup reads."""
    wake = asyncio.create_task(coordinator.economy_start.wait())
    ended = asyncio.create_task(coordinator.jobs["gameplay"].gameplay_ended.wait())
    try:
        await asyncio.wait({wake, ended}, return_when=asyncio.FIRST_COMPLETED)
        if coordinator.committed or ended.done():
            return
        service.economy_runtime = EconomyRuntime(service, coordinator, payload)
        await run_economy(service, coordinator, payload, parent_record)
    finally:
        wake.cancel()
        ended.cancel()
        await asyncio.gather(wake, ended, return_exceptions=True)
