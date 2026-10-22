"""An isolated economy conversation sharing only the civilization gateway and storage."""

import asyncio
import contextlib
import copy
from typing import Any

from civ_agent.provider_usage import reconcile_provider_usage
from civ_agent.startup_context import prepare_startup, startup_prompt
from civ_agent.tls import private_directory


class EconomyRuntime:
    def __init__(self, service, coordinator, payload, attempt=1):
        self.service, self.coordinator, self.payload = service, coordinator, payload
        self.active_id = payload["activationId"] + ":economy" + (f":{attempt}" if attempt > 1 else "")
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
        selected = dict(parent_record.get("effectiveSettings", parent_record["settings"]))
        selected.update(
            {("reasoningEffort" if k == "reasoning_effort" else k): v for k, v in service.economy_model.items()}
        )
        self.fast = service.fast and selected["provider"] == "openai"
        record = {
            "activationId": self.active_id,
            "purpose": "economy",
            "epoch": payload["epoch"],
            "gameTurn": payload["gameTurn"],
            "civilization": service.civilization,
            "phase": "STARTING",
            "modelPhase": "economy",
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
                        "job_claim", job=self.active_id, purpose="economy", parentJob=parent_record["activationId"]
                    )
                    if admission["granted"]:
                        break
                    if admission.get("reason") in {"MODEL_INVOCATION_LIMIT", "ROLE_CAPACITY_TOO_SMALL"}:
                        raise ServiceError(admission["reason"], 503)
                    if service.failure or service.stopping:
                        raise RuntimeError("ECONOMY_ADMISSION_FAILED")
                    await asyncio.sleep(0.25)
                granted = True
            await service.io.call(service.accounting.start, self.active_id, payload["epoch"], granted)
            async with service.connection_factory(
                service.mcp_url,
                payload["playerToken"],
                service.ca_file,
                diagnostics=service.diagnostics,
                context={"purpose": "economy"},
            ) as connection:
                self.job = await service.gateway.open(
                    self.active_id, "economy", payload["epoch"], connection, selected, payload["gameTurn"]
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
                        self.failure = "ECONOMY_HEARTBEAT_FAILED"
                        service.diagnostics.emit("economy_heartbeat_failed", error=error, activationId=self.active_id)
                        self.stop_event.set()

                monitor = asyncio.create_task(heartbeat())
                self.started.set()
                prepared = await prepare_startup(service.gateway, self.job)
                prompt = startup_prompt(
                    prepared,
                    "economy",
                    payload["prompt"],
                    strategy_guide=service.strategy_guide,
                    civilization_guide=service.civilization_guide,
                    civilization=service.civilization,
                )
                record["startup"] = {"characters": len(prompt.user), **prompt.metadata()}
                await service._save(record)
                self.started.set()
                workspace = private_directory(service.workspace / "jobs" / (self.active_id.replace(":", "_")[-64:]))
                await self._model_turn(record, self.job, prompt, workspace, selected)
                if self.failure or not self.coordinator.committed:
                    raise RuntimeError(self.failure or "ECONOMY_ROLE_INCOMPLETE")
                record["phase"] = "COMPLETED"
        except BaseException as error:
            record["phase"] = "INTERRUPTED" if isinstance(error, asyncio.CancelledError) else "FAILED"
            record["error"] = str(error) or "ECONOMY_ROLE_FAILED"
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


async def run_economy(service, coordinator, payload, parent_record):
    """Retry completed, reconciled attempts only; an uncertain native action pauses the run."""
    for attempt in range(1, 4):
        runtime = service.economy_runtime if attempt == 1 else EconomyRuntime(service, coordinator, payload, attempt)
        service.economy_runtime = runtime
        try:
            await runtime.run(parent_record)
            return
        except asyncio.CancelledError:
            raise
        except Exception as error:
            from sqlalchemy.exc import SQLAlchemyError

            from civ_agent.agent_service import ServiceError

            if runtime.process is not None:
                service.failure = "STOP_UNCONFIRMED"
                service.stop_event.set()
                raise ServiceError("STOP_UNCONFIRMED", 503) from error
            if isinstance(error, ServiceError) and error.code in {"MODEL_INVOCATION_LIMIT", "ROLE_CAPACITY_TOO_SMALL"}:
                service.failure = error.code
                service.stop_event.set()
                raise
            if coordinator.committed or isinstance(error, (OSError, SQLAlchemyError)):
                service.failure = "DURABLE_RECORD_FAILED"
                service.stop_event.set()
                raise ServiceError("DURABLE_RECORD_FAILED", 503) from error
            unresolved = await service.io.call(service.memory.pending)
            if service.gateway.run_failure or unresolved or attempt == 3:
                service.failure = "ECONOMY_ROLE_FAILED"
                service.stop_event.set()
                raise
            coordinator.ready["economy"] = False
            coordinator.resumes["economy"].clear()
            await coordinator.save()
