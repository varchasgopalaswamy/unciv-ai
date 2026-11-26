"""Local player runs over the existing native rules and receipt boundary."""

import asyncio
import sys
import time
from contextlib import AsyncExitStack
from contextvars import ContextVar
from dataclasses import dataclass, field, replace
from enum import StrEnum
from pathlib import Path
from uuid import uuid4

from pydantic_ai import ModelRetry
from pydantic_ai.exceptions import UnexpectedModelBehavior, UsageLimitExceeded
from pydantic_ai.tools import DeferredToolResults
from pydantic_ai.usage import RunUsage
from pydantic_ai_harness.compaction import ContextUsageEvent
from pydantic_ai_harness.step_persistence import SqliteStepStore

from civ_agent.civilization_state import StateConflict
from civ_agent.diagnostics import exception_details
from civ_agent.host_connection import api_origin
from civ_agent.orchestration.authored_memory import AuthoredMemoryRequest, AuthoredMemoryService
from civ_agent.orchestration.contracts import (
    FinishPeace,
    FinishPeacePhase,
    GatewayCall,
    GatewayCatalogue,
    GatewayJob,
    GatewayReceipt,
    PeacePhase,
    PrepareRole,
    RoleRequest,
    TurnTicket,
)
from civ_agent.orchestration.handoff import NativeHandoffs
from civ_agent.orchestration.handoff_contracts import (
    FrozenNativeHandoff,
    NativeHandoffApplication,
    NativeHandoffRequest,
)
from civ_agent.orchestration.host_effect_service import NativeCommandService, native_effect_key
from civ_agent.orchestration.job_admission import JobAdmission
from civ_agent.orchestration.job_contracts import (
    HostUsage,
    JobTelemetry,
    NativeJob,
    NativeJobReport,
    NativeReviewBinding,
    host_job_id,
    host_usage,
)
from civ_agent.orchestration.peace_service import PeaceServices, finish_consultation, finish_phase, prepare_phase
from civ_agent.orchestration.player_plans import PlayerPlanRequest, PlayerPlanService
from civ_agent.orchestration.review import ReviewServices, delivered_reads, read_tool
from civ_agent.orchestration.review_service import check_review, finish_review, prepare_review
from civ_agent.orchestration.store import JournalConflict
from civ_agent.orchestration.transcripts import pointer
from civ_agent.orchestration.workflows_types import CheckReview, FinishReview
from civ_agent.peace_contracts import PeaceApproval, TacticalOverview
from civ_agent.runtime.agents import LocalAgentRunner, LocalAgentServices
from civ_agent.runtime.model_policy import FreshConversationRequired
from civ_agent.runtime.review_cycles import ReviewCycle
from civ_agent.runtime.state import RoleStatus
from civ_agent.step_store_notifications import CommittedStepStore
from civ_agent.strategic_review import REVIEW_READ_TOOLS, StrategicAdvice
from civ_agent.tls import regular_file

JOB_HEARTBEAT_INTERVAL = 20
JOB_HEARTBEAT_RETRY_INTERVAL = 5
JOB_LEASE_RENEWAL_WINDOW = 80  # Leave a margin within the host's 90-second job lease.


class LocalGateway:
    def __init__(self, root, database: Path):
        self.root = root
        self.memory = AuthoredMemoryService(root.registry, database)
        self.plans = PlayerPlanService(root.registry, database)

    async def catalogue(self, request):
        binding = GatewayJob(scope=request.scope, job=request.job, purpose=request.purpose)
        registered = await self.root.io.call(self.root.registry.load, binding)
        return GatewayCatalogue(catalogue=registered.catalogue)

    async def post(self, route, request):
        if hasattr(request, "model_dump"):
            request = request.model_dump(mode="json")
        if route == "memory":
            result = await self.memory.invoke(AuthoredMemoryRequest.model_validate(request))
        elif route == "plans":
            result = await self.plans.invoke(PlayerPlanRequest.model_validate(request))
        else:
            raise JournalConflict("LOCAL_GATEWAY_ROUTE_UNAVAILABLE")
        return result.model_dump(mode="json")


class PermitState(StrEnum):
    NEW = "NEW"
    CLAIM_UNCERTAIN = "CLAIM_UNCERTAIN"
    WAITING = "WAITING"
    COMPUTING = "COMPUTING"
    REPORT_UNCERTAIN = "REPORT_UNCERTAIN"
    RELEASE_UNCERTAIN = "RELEASE_UNCERTAIN"
    RELEASED = "RELEASED"
    DENIED = "DENIED"


@dataclass
class Allowance:
    usage: RunUsage = field(default_factory=RunUsage)
    provider_attempts: int = 0


@dataclass
class Permit:
    job: NativeJob
    model: str
    started: int = field(default_factory=lambda: int(time.time() * 1000))
    lease_started: float = field(default_factory=time.monotonic)
    sequence: int = 0
    requests: int = 0
    sdk_requests: int = 0
    usage: HostUsage = field(default_factory=HostUsage)
    lifecycle: PermitState = PermitState.NEW
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    pending_report: NativeJobReport | None = None
    pending_release: bool = False
    sdk_usage: RunUsage = field(default_factory=RunUsage)
    last_run_id: str | None = None
    context_usage: dict[str, int | float | bool] = field(default_factory=dict)

    @property
    def released(self):
        return self.lifecycle in {PermitState.RELEASED, PermitState.DENIED}

    @property
    def releasing(self):
        return self.lifecycle == PermitState.RELEASE_UNCERTAIN

    @property
    def computing(self):
        return self.lifecycle == PermitState.COMPUTING

    @property
    def admitted(self):
        return self.lifecycle not in {PermitState.NEW, PermitState.CLAIM_UNCERTAIN, PermitState.DENIED}


@dataclass
class AttemptContext:
    ticket: TurnTicket
    parent: Permit
    role_passes: dict[str, int] = field(default_factory=lambda: dict.fromkeys(("gameplay", "economy", "military"), 0))
    allowances: dict[str, Allowance] = field(default_factory=dict)
    handoff_pass: int = 0


class LocalPlayer:
    def __init__(self, coordinator, session, *, clients, model_factory, state, capacity=None, model_limiter=None):
        self.root, self.session, self.clients = coordinator, session, clients
        self.model_factory, self.state, self.capacity = model_factory, state, capacity
        self.model_limiter = model_limiter
        self.review_generation = uuid4().hex
        self.ticket: TurnTicket | None = None
        self.parent: Permit | None = None
        self.permits: dict[str, Permit] = {}
        self.cleanup_errors: list[dict] = []
        self.dispatch_fenced = False
        self.economy_lock = asyncio.Lock()
        self.communication_gate = asyncio.Event()
        self.communication_active = 0
        self.communication_idle = asyncio.Event()
        self.communication_idle.set()
        self.tasks: set[asyncio.Task] = set()
        self._heartbeat_task = None
        self._caller = ContextVar("local_player_caller", default=None)
        self._request_permits = {}
        self.admission = JobAdmission(
            game=coordinator.game,
            player=coordinator.player,
            civilization=coordinator.civilization,
            connection=lambda scope: coordinator.communication.connection(scope.epoch),
        )
        database = coordinator.store.root / "authored.sqlite3"
        self.gateway = LocalGateway(coordinator, database)
        steps = coordinator.store.root / "agent-steps.sqlite3"
        steps.touch(mode=0o600, exist_ok=True)
        regular_file(steps, private=True)
        self.steps = CommittedStepStore(SqliteStepStore(database=steps))
        self.commands = NativeCommandService(
            coordinator.registry, workflow_store=coordinator.store, authored_store=self.gateway.memory.store
        )
        coordinator.native_commands = self.commands
        self.handoffs = NativeHandoffs(coordinator, seal_key=native_effect_key(coordinator.memory))

    async def begin(self, attempt):
        # Initialize the maintained SQLite store before parallel role runs
        # establish WAL and its first schema on an empty database.
        await self.steps.get_run(run_id="native-runtime-preflight")
        self.ticket = await self.root.prepare_local_attempt(self.session, attempt.epoch, attempt.handoff, attempt.id)
        attempt.ticket = self.ticket.model_dump(mode="json")
        await self.state.save()
        self.parent = Permit(
            NativeJob(scope=self.ticket.scope, job=self.ticket.scope.activation, purpose="gameplay"),
            self.session.get_selection("gameplay").model,
        )
        self.permits[self.parent.job.job] = self.parent
        await self.state.role(
            RoleStatus(
                purpose="gameplay",
                job=self.parent.job.job,
                phase="STARTING",
                model=self.parent.model,
                native_job=self.parent.job.model_dump(mode="json"),
            )
        )
        self._ensure_heartbeats()
        await self._compute(self.parent, True)
        await self._compute(self.parent, False)
        return AttemptContext(self.ticket, self.parent)

    async def report_runner(self, deps, *, permit, startup, model, output_type, name, tools=None, validate=None):
        async def status(phase, _delay):
            await self._compute(permit, phase == "inference")

        async def model_attempt(_deps, _run_id, _step, _attempt):
            permit.last_run_id = _run_id
            permit.requests += 1

            def count(state):
                state.model_requests += 1

            await self.state.save(count)

        cache_account_scope, cache_endpoint = await self._cache_owner(deps, model=model)
        runner = LocalAgentRunner(
            LocalAgentServices(
                admission_factory=getattr(getattr(self, "clients", None), "admission_factory", None),
                invoke=self._invoke,
                step_store=self.steps,
                on_model_status=status,
                on_model_attempt=model_attempt,
                overflow_root=self.root.store.root / "tool-output",
                cache_account_scope=cache_account_scope,
                cache_endpoint=cache_endpoint,
                model_limiter=self.model_limiter,
                run_metadata={
                    "native_job_id": host_job_id(permit.job),
                    "game_turn": str(
                        getattr(deps, "source_turn", getattr(getattr(self.state, "current", None), "turn", 0))
                    ),
                },
                on_context_usage=self._context_callback(permit) if model.system != "function" else None,
            )
        )
        await self._compute(permit, True)
        before_requests = permit.sdk_usage.requests
        before = host_usage(permit.sdk_usage)
        try:
            while True:
                try:
                    return await runner.run_report(
                        deps,
                        startup=startup,
                        model=model,
                        output_type=output_type,
                        name=name,
                        tools=tools,
                        usage=permit.sdk_usage,
                        validate=validate,
                    )
                except (FreshConversationRequired, UnexpectedModelBehavior) as error:
                    delay = error.retry_after if isinstance(error, FreshConversationRequired) else 2
                    if delay:
                        await asyncio.sleep(delay)
        finally:
            self._observe_sdk_requests(permit, permit.sdk_usage.requests - before_requests)
            self._observe_usage(permit, permit.sdk_usage, before)
            await self._settle(permit)

    async def _cache_owner(self, request, *, model):
        if model.system == "function":
            return None, None
        identify = getattr(self.clients, "cache_account_identity", None)
        endpoint = getattr(self.clients, "cache_endpoint", None)
        if identify is None:
            return None, None
        return await identify(request.selection, model=model), endpoint(request.selection) if endpoint else None

    def _observe_sdk_requests(self, permit, count):
        permit.sdk_requests += count
        state = self.state.current
        if state.sdk_requests is not None:
            state.sdk_requests += count
        state.sdk_requests_observed += count

    @staticmethod
    def _observe_usage(permit, usage, before):
        observed = host_usage(usage)
        permit.usage = HostUsage.model_validate(
            {
                name: getattr(permit.usage, name) + getattr(observed, name) - getattr(before, name)
                for name in HostUsage.model_fields
            }
        )

    def _context_callback(self, permit):
        async def record(_deps, event: ContextUsageEvent):
            permit.context_usage = {
                "used_tokens": event.used_tokens,
                "window_tokens": event.window_tokens,
                "fraction": event.fraction,
                "resolved": event.resolved,
            }
            # The model-attempt/status write publishes this with the existing
            # checkpoint; a metric does not need another filesystem write.
            state = self.state.current
            roles = list(state.native_jobs.values())
            if state.active_attempt is not None:
                roles.extend(state.active_attempt.roles)
            for role in roles:
                if role.job == permit.job.job:
                    role.context_usage = dict(permit.context_usage)

        return record

    async def _report_frontier(self, permit):
        if permit.last_run_id is None:
            raise JournalConflict("LOCAL_REPORT_FRONTIER_REQUIRED")
        snapshot = await self.steps.latest_snapshot(run_id=permit.last_run_id, include_interrupted=True)
        if snapshot is None:
            raise JournalConflict("LOCAL_REPORT_FRONTIER_REQUIRED")
        return pointer(permit.last_run_id, snapshot.messages)

    async def review(self, cycle: ReviewCycle):
        request = await self.root.reviews.begin(cycle, self.review_generation)
        if request is None:
            return
        permit = Permit(
            NativeJob(
                scope=request.scope,
                job=request.scope.activation,
                purpose="review",
                review=NativeReviewBinding(
                    cycle_id=cycle.id,
                    source_turn=cycle.source_turn,
                    target_turn=cycle.target_turn,
                ),
            ),
            request.selection.model,
        )
        self.permits[permit.job.job] = permit
        self._ensure_heartbeats()
        await self.state.role(
            RoleStatus(
                purpose="review",
                job=permit.job.job,
                phase="STARTING",
                model=permit.model,
                native_job=permit.job.model_dump(mode="json"),
            )
        )
        worker = ReviewServices(self.root.store, self.clients)
        prepared = await prepare_review(request, worker=worker)
        if self.session.settings.scripted:
            from civ_agent.orchestration.scripted_model import scripted_review_model

            model = scripted_review_model(request)
        else:
            model = await self.clients.model(request.selection, contract=request.provider_contract)

        async def validate(ctx, report):
            check = await check_review(
                CheckReview(
                    request=request,
                    report=report,
                    reads=delivered_reads(ctx.messages),
                ),
                worker=worker,
            )
            if not check.accepted:
                raise ModelRetry(check.feedback)
            return report

        result = None
        complete = False
        usage_complete = True
        try:
            try:
                async with asyncio.timeout(request.timeout_seconds):
                    result = await self.report_runner(
                        request,
                        permit=permit,
                        startup=prepared.model_dump(),
                        model=model,
                        output_type=StrategicAdvice,
                        name="strategic_review_v1",
                        tools=[read_tool(name, worker=worker) for name in sorted(REVIEW_READ_TOOLS)],
                        validate=validate,
                    )
            except UsageLimitExceeded:
                pass
            except (UnexpectedModelBehavior, FreshConversationRequired, TimeoutError):
                usage_complete = False
            if result is None and permit.last_run_id is None:
                await self.root.reviews.unavailable_before_model(cycle, request)
                complete = True
                return
            publication = await finish_review(
                FinishReview(
                    request=request,
                    report=result.report if result is not None else None,
                    reads=delivered_reads(result.messages) if result is not None else [],
                    history=pointer(result.run_id, result.messages)
                    if result is not None
                    else await self._report_frontier(permit),
                    usage=permit.sdk_usage.opentelemetry_attributes(),
                ),
                worker=worker,
            )
            await self.root.reviews.publish(cycle, request, publication)
            complete = True
        finally:
            await self._settle(permit, release=True, completed=complete and usage_complete)

    def _ensure_heartbeats(self):
        if self._heartbeat_task is None:
            self._heartbeat_task = asyncio.create_task(self._heartbeats())
            self.tasks.add(self._heartbeat_task)

    async def _heartbeats(self):
        while True:
            await asyncio.sleep(JOB_HEARTBEAT_RETRY_INTERVAL)
            async with asyncio.TaskGroup() as renewals:
                for permit in list(self.permits.values()):
                    renewals.create_task(self._renew_permit(permit))

    async def _renew_permit(self, permit: Permit) -> None:
        async with permit.lock:
            if not permit.admitted or permit.released or permit.releasing:
                return
            started = time.monotonic()
            if started < permit.lease_started + JOB_HEARTBEAT_INTERVAL:
                return
            deadline = permit.lease_started + JOB_LEASE_RENEWAL_WINDOW
            if started >= deadline:
                raise RuntimeError("LOCAL_JOB_LEASE_LOST")
            try:
                async with asyncio.timeout(min(30, deadline - started)):
                    result = await self.admission.heartbeat(permit.job)
            except (StateConflict, TimeoutError) as error:
                if isinstance(error, StateConflict) and str(error) != "HOST_UNAVAILABLE":
                    raise
                # An uncertain reply can be retried with the same native job ID.
                if time.monotonic() < deadline:
                    return
                raise RuntimeError("LOCAL_JOB_LEASE_LOST") from error
            if not result.granted:
                raise RuntimeError("LOCAL_JOB_LEASE_LOST")
            permit.lease_started = started

    async def _publish_permit(self, permit, phase, release=False):
        await self.state.role(
            RoleStatus(
                purpose=permit.job.purpose,
                job=permit.job.job,
                phase=phase,
                model=permit.model,
                model_requests=permit.requests,
                sdk_requests=permit.sdk_requests,
                run_id=permit.last_run_id,
                usage=permit.usage.model_dump(),
                context_usage=permit.context_usage,
                native_job=permit.job.model_dump(mode="json"),
                permit_state=permit.lifecycle.value,
                pending_report=permit.pending_report.model_dump(mode="json")
                if permit.pending_report is not None
                else None,
                pending_release=permit.pending_release,
                released=release,
            )
        )
        if release:
            self.permits.pop(permit.job.job, None)

    async def _send_report(self, permit):
        report = permit.pending_report
        assert report is not None
        grant = await (self.admission.release(report) if permit.pending_release else self.admission.report(report))
        if not grant.granted:
            raise JournalConflict("LOCAL_JOB_REPORT_REJECTED")
        release = permit.pending_release
        permit.lifecycle = (
            PermitState.RELEASED
            if release
            else (PermitState.COMPUTING if report.telemetry.phase == "RUNNING" else PermitState.WAITING)
        )
        # Publication is part of settlement. Keep the exact envelope until its
        # acknowledgement and durable inventory update both succeed.
        await self._publish_permit(permit, report.telemetry.phase, release)
        permit.pending_report = None

    async def _report_locked(self, permit, phase, *, release=False, complete=False):
        if permit.pending_report is not None:
            pending = permit.pending_report
            pending_release = permit.pending_release
            await self._send_report(permit)
            if pending_release or (not release and pending.telemetry.phase == phase):
                return
        if permit.released or permit.lifecycle in {PermitState.NEW, PermitState.CLAIM_UNCERTAIN}:
            return
        permit.sequence += 1
        now = int(time.time() * 1000)
        permit.pending_report = NativeJobReport(
            job=permit.job,
            usage=permit.usage.model_copy(deep=True),
            telemetry=JobTelemetry(
                sequence=permit.sequence,
                phase=phase,
                startedAtMs=permit.started,
                endedAtMs=now if release else None,
                elapsedMs=max(0, now - permit.started),
                usageReported=True,
                usageComplete=release and complete,
                model=permit.model,
                modelRequests=permit.requests,
            ),
        )
        permit.pending_release = release
        permit.lifecycle = PermitState.RELEASE_UNCERTAIN if release else PermitState.REPORT_UNCERTAIN
        await self._publish_permit(permit, phase)
        await self._send_report(permit)

    async def _report(self, permit: Permit, phase, *, release=False, complete=False):
        async with permit.lock:
            await self._report_locked(permit, phase, release=release, complete=complete)

    async def _compute(self, permit, acquire, *, phase="WAITING_ROLE"):
        async with permit.lock:
            if permit.pending_report is not None:
                await self._send_report(permit)
            if acquire:
                if getattr(self, "dispatch_fenced", False):
                    raise RuntimeError("LOCAL_DISPATCH_FENCED")
                if permit.released:
                    raise RuntimeError("LOCAL_JOB_RELEASED")
                if permit.computing:
                    return
                admitted_before_claim = permit.admitted
                while True:
                    permit.lifecycle = PermitState.CLAIM_UNCERTAIN
                    started = time.monotonic()
                    grant = await self.admission.claim(permit.job)
                    if grant.granted:
                        permit.lease_started = started
                        permit.lifecycle = PermitState.COMPUTING
                        await self._report_locked(permit, "RUNNING")
                        return
                    permit.lifecycle = PermitState.WAITING if admitted_before_claim else PermitState.NEW
                    if grant.reason:
                        permit.lifecycle = PermitState.DENIED
                        await self._publish_permit(permit, "INTERRUPTED", True)
                        raise RuntimeError(grant.reason)
                    await asyncio.sleep(0.25)
            elif permit.computing:
                await self._report_locked(permit, phase)

    async def _settle(self, permit, *, release=False, completed=False, phase="WAITING_ROLE"):
        primary = sys.exception()
        try:
            await self._compute(permit, False, phase=phase)
            if release and permit.admitted and not permit.released:
                await self._report(
                    permit, "COMPLETED" if completed else "INTERRUPTED", release=True, complete=completed
                )
        except BaseException as error:
            if primary is None:
                raise
            if not hasattr(self, "cleanup_errors"):
                self.cleanup_errors = []
            self.cleanup_errors = [*self.cleanup_errors[-11:], exception_details(error)]

    async def _invoke(self, request, call_id, model_step, decision):
        if getattr(self, "dispatch_fenced", False):
            raise RuntimeError("LOCAL_DISPATCH_FENCED")
        if request.purpose == "communication":
            await self.communication_gate.wait()
            self.communication_active += 1
            self.communication_idle.clear()
        token = self._caller.set(self._request_permits.get(request.job))
        try:
            identity = GatewayJob(scope=request.scope, job=request.job, purpose=request.purpose)
            with api_origin("agent"):
                result = await self.root.registry(identity, call_id, model_step, decision)
            return GatewayReceipt(result=result.result)
        finally:
            self._caller.reset(token)
            if request.purpose == "communication":
                self.communication_active -= 1
                if self.communication_active == 0:
                    self.communication_idle.set()

    async def _commands(self, request, run_id, call_id, model_step, operations):
        if getattr(self, "dispatch_fenced", False):
            raise RuntimeError("LOCAL_DISPATCH_FENCED")
        identity = GatewayJob(scope=request.scope, job=request.job, purpose=request.purpose)
        call = GatewayCall(call_id=call_id, model_step=model_step, decision={"kind": "act", "operations": operations})
        token = self._caller.set(self._request_permits.get(request.job))
        try:
            with api_origin("agent"):
                execution = await self.commands.execute_local(identity, call, run_id=run_id)
            return execution.result
        finally:
            self._caller.reset(token)

    async def _delivered(self, request, call_id, model_step):
        identity = GatewayJob(scope=request.scope, job=request.job, purpose=request.purpose)
        await self.root.registry.acknowledge_delivery(identity, call_id, model_step)

    async def _startup_delivered(self, request, run_id, visible):
        startup = self.root.store.get(request.scope, request.startup)
        if "preparedDelivery" not in startup:
            return
        permit = self._request_permits.get(request.job)
        if permit is None or permit.last_run_id != run_id:
            raise JournalConflict("LOCAL_STARTUP_RUN_CONFLICT")
        identity = GatewayJob(scope=request.scope, job=request.job, purpose=request.purpose)
        await self.root.registry.startup_delivery(identity, startup["preparedDelivery"], delivered=visible)

    async def consult(self, request):
        caller = self._caller.get()
        if caller is not None:
            await self._compute(caller, False)
        worker = PeaceServices(self.root.store, self.clients)
        completed = []
        try:
            for purpose in ("military", "gameplay"):
                phase = PeacePhase(
                    request=request,
                    phase=purpose,
                    military=completed[0].report if purpose == "gameplay" else None,
                )
                startup = await prepare_phase(phase, worker=worker)
                permit = Permit(
                    NativeJob(scope=phase.scope, job=phase.scope.activation, purpose="consultation"),
                    phase.selection.model,
                )
                self.permits[permit.job.job] = permit
                if self.session.settings.scripted:
                    from civ_agent.orchestration.scripted_model import scripted_peace_model

                    model = scripted_peace_model(phase)
                else:
                    model = await self.clients.model(phase.selection, contract=phase.provider_contract)
                try:
                    try:
                        result = await self.report_runner(
                            phase,
                            permit=permit,
                            startup=startup,
                            model=model,
                            output_type=TacticalOverview if purpose == "military" else PeaceApproval,
                            name="peace_military_v1" if purpose == "military" else "peace_main_v1",
                        )
                    except (UsageLimitExceeded, UnexpectedModelBehavior, FreshConversationRequired):
                        result = None
                    completed.append(
                        await finish_phase(
                            FinishPeacePhase(
                                phase=phase,
                                report=result.report.model_dump(mode="json") if result is not None else None,
                                history=pointer(result.run_id, result.messages)
                                if result is not None
                                else await self._report_frontier(permit),
                                usage=permit.sdk_usage.opentelemetry_attributes(),
                            ),
                            worker=worker,
                        )
                    )
                    await self._report(permit, "COMPLETED", release=True, complete=True)
                except BaseException:
                    await self._settle(permit, release=True)
                    raise
                if completed[-1].status == "UNAVAILABLE":
                    break
            result = await finish_consultation(FinishPeace(request=request, phases=completed), worker=worker)
        except BaseException:
            raise
        else:
            if caller is not None:
                await self._compute(caller, True)
            return result

    async def _wait(self, request, requests, permit, context):
        await self._compute(permit, False)
        try:
            if requests.approvals:
                handler = getattr(self.root, "local_peace_wait", None)
                if handler is None:
                    raise RuntimeError("LOCAL_PEACE_WAIT_HANDLER_REQUIRED")
                result = await handler(request, requests)
                await self._compute(permit, True)
                return result
            if any(call.tool_name != "economy_wait" for call in requests.calls):
                raise RuntimeError("LOCAL_DEFERRED_TOOL_UNAVAILABLE")
            if context is None:
                raise RuntimeError("LOCAL_PLAYER_ATTEMPT_REQUIRED")
            await self.run_role("economy", context)
            _, roles = self.root.context()
            job = roles.jobs["gameplay"]
            outcome = await roles.wait_for_economy(job)
            result = await roles.finish_economy_wait(job, outcome)
            result = DeferredToolResults(calls={call.tool_call_id: result for call in requests.calls})
        except BaseException:
            raise
        else:
            await self._compute(permit, True)
            return result

    async def run_request(
        self, request: RoleRequest, *, permit: Permit | None = None, context: AttemptContext | None = None
    ):
        permit = permit or Permit(
            NativeJob(scope=request.scope, job=request.job, purpose=request.purpose), request.selection.model
        )
        self.permits[permit.job.job] = permit

        async def status(phase, delay):
            if request.purpose == "communication" and phase == "inference":
                await self.communication_gate.wait()
            waiting = phase != "inference"
            await self._compute(permit, not waiting)
            await self.state.role(
                RoleStatus(
                    purpose=request.purpose,
                    job=permit.job.job,
                    phase=phase.upper(),
                    model=request.selection.model,
                    model_requests=permit.requests,
                    sdk_requests=permit.sdk_requests,
                    run_id=permit.last_run_id,
                    context_usage=permit.context_usage,
                    native_job=permit.job.model_dump(mode="json"),
                )
            )

        allowance = (
            context.allowances.setdefault(request.purpose, Allowance())
            if context is not None
            else Allowance(permit.sdk_usage)
        )

        async def model_attempt(_request, _run_id, _step, _attempt):
            permit.last_run_id = _run_id
            allowance.provider_attempts += 1
            permit.requests += 1

            def count(state):
                state.model_requests += 1

            await self.state.save(count)

        runner = LocalAgentRunner(
            LocalAgentServices(
                admission_factory=getattr(getattr(self, "clients", None), "admission_factory", None),
                invoke=self._invoke,
                step_store=self.steps,
                gateway=self.gateway,
                execution_windows=getattr(getattr(self.root, "registry", None), "windows", None),
                execute_commands=self._commands,
                on_observation_delivered=self._delivered,
                on_startup_delivered=self._startup_delivered,
                overflow_root=self.root.store.root / "tool-output",
                on_wait=lambda req, waits: self._wait(req, waits, permit, context),
                on_model_status=status,
                on_model_attempt=model_attempt,
                model_limiter=self.model_limiter,
                run_metadata={
                    "native_job_id": host_job_id(permit.job),
                    "game_turn": str(context.ticket.turn if context is not None else self.state.current.turn),
                },
                on_context_usage=self._context_callback(permit)
                if self.model_factory is None and not self.session.settings.scripted
                else None,
            )
        )
        await self.state.role(
            RoleStatus(
                purpose=request.purpose,
                job=permit.job.job,
                phase="STARTING",
                model=request.selection.model,
                native_job=permit.job.model_dump(mode="json"),
            )
        )
        completed = False
        usage = allowance.usage
        before_requests = usage.requests
        before = host_usage(usage)
        try:
            await self._compute(permit, True)
            model = (
                self.model_factory(request)
                if self.model_factory is not None
                else await self.clients.model(request.selection, contract=request.provider_contract)
            )
            cache_account_scope, cache_endpoint = await self._cache_owner(request, model=model)
            runner.services = replace(
                runner.services, cache_account_scope=cache_account_scope, cache_endpoint=cache_endpoint
            )
            self._request_permits[request.job] = permit
            startup = self.root.store.get(request.scope, request.startup)
            if "preparedDelivery" in startup:
                identity = GatewayJob(scope=request.scope, job=request.job, purpose=request.purpose)
                await self.root.registry.startup_delivery(identity, startup["preparedDelivery"], delivered=False)
            result = await runner.run_role(
                request,
                startup=startup,
                catalogue=self.root.store.get(request.scope, request.catalogue),
                model=model,
                usage=usage,
            )
            checked = await self.root.check_role(request)
            if not checked.finished:
                raise RuntimeError("LOCAL_ROLE_COMPLETION_UNCONFIRMED")
            completed = True
            await self.state.role(
                RoleStatus(
                    purpose=request.purpose,
                    job=permit.job.job,
                    phase="COMPLETED",
                    model=request.selection.model,
                    model_requests=permit.requests,
                    sdk_requests=permit.sdk_requests,
                    run_id=permit.last_run_id,
                    usage=permit.usage.model_dump(),
                    native_job=permit.job.model_dump(mode="json"),
                )
            )
            return result
        finally:
            self._request_permits.pop(request.job, None)
            self._observe_sdk_requests(permit, usage.requests - before_requests)
            self._observe_usage(permit, usage, before)
            await self._settle(
                permit,
                release=context is None or permit is not context.parent,
                completed=completed,
                phase="ROLE_FINISHED"
                if completed and context is not None and permit is context.parent
                else "WAITING_ROLE",
            )

    async def prepare_roles(self, context: AttemptContext):
        # Main preparation initializes the native Economy/Military schedules.
        # Freeze its first request before inspecting readiness; run_role reuses
        # that request, so this makes no model call or extra role pass.
        await self.root.prepare_role(
            PrepareRole(ticket=context.ticket, purpose="gameplay", pass_number=context.role_passes["gameplay"])
        )
        _, roles = self.root.context()
        return roles

    async def run_roles(self, context: AttemptContext):
        """Run eligible roles together and wake finished roles when work reopens.

        Readiness and notifications stay in TurnRoles. Tasks live only in this
        joined scope; receipt dispatch and role ownership stay in the gateway.
        """
        roles = await self.prepare_roles(context)
        running = {}

        async def run(purpose):
            try:
                await self.run_role(purpose, context)
            finally:
                roles.changed.set()

        async with asyncio.TaskGroup() as group:
            while True:
                # Clear before inspecting state so a wake during the following
                # wait cannot be lost. No await separates inspection and wait.
                roles.changed.clear()
                for purpose, task in list(running.items()):
                    if task.done():
                        task.result()
                        del running[purpose]
                for purpose in ("gameplay", "economy", "military"):
                    if not roles.ready[purpose] and purpose not in running:
                        running[purpose] = group.create_task(run(purpose), name=f"turn-{purpose}")
                if not running:
                    return
                await roles.changed.wait()

    async def run_role(self, purpose, context: AttemptContext):
        if purpose == "economy":
            async with self.economy_lock:
                return await self._role(purpose, context)
        return await self._role(purpose, context)

    async def _role(self, purpose, context: AttemptContext):
        output_failures = 0
        while True:
            index = context.role_passes[purpose]
            request = await self.root.prepare_role(
                PrepareRole(ticket=context.ticket, purpose=purpose, pass_number=index)
            )
            if request is None:
                return None
            context.role_passes[purpose] += 1
            permit = (
                context.parent
                if purpose == "gameplay"
                else Permit(
                    NativeJob(scope=request.scope, job=request.job, purpose=purpose, parent=context.parent.job),
                    request.selection.model,
                )
            )
            try:
                return await self.run_request(request, permit=permit, context=context)
            except UnexpectedModelBehavior:
                output_failures += 1
                # Each run already exhausts its SDK correction attempts. Do not
                # turn repeated invalid completions into unlimited fresh jobs.
                if output_failures >= request.attempt_limit:
                    raise
                await asyncio.sleep(2)
            except FreshConversationRequired as error:
                if error.retry_after:
                    await asyncio.sleep(error.retry_after)
            # Native progress and usage accounting both carry.

    async def handoff(self, context: AttemptContext, on_committed=None):
        attempt = self.state.current.active_attempt
        assert attempt is not None
        prepared = await self.handoffs.prepare(
            NativeHandoffRequest(ticket=context.ticket, attempt=context.handoff_pass)
        )
        context.handoff_pass += 1
        if prepared.result is not None:
            return prepared.result.committed
        effect = prepared.effect
        assert effect is not None
        attempt.handoff_effect = effect.model_dump(mode="json")
        await self.state.save()
        cycle = (
            await self.root.reviews.capture(
                context.ticket.turn, context.ticket.turn + 1, handoff=context.ticket.handoff
            )
            if self.root.reviews is not None
            else None
        )
        receipt = await self.handoffs.execute(effect)
        attempt.handoff_receipt = receipt
        await self.state.save()
        await self.handoffs.apply(NativeHandoffApplication(effect=effect, receipt=receipt))
        result = await self.handoffs.finish(effect)
        if result.committed and context.parent is not None:
            await self._report(context.parent, "COMPLETED", release=True, complete=True)
        if cycle is not None:
            assert self.root.reviews is not None
            if result.committed:
                cycle = await self.root.reviews.commit(cycle)
                if on_committed is not None:
                    await on_committed(cycle)
            else:
                await self.root.reviews.discard_unstarted(cycle)
        return result.committed

    async def recover(self, attempt):
        if attempt.ticket is not None:
            self.ticket = TurnTicket.model_validate(attempt.ticket)
        if attempt.handoff_effect is not None:
            effect = FrozenNativeHandoff.model_validate(attempt.handoff_effect)
            response = await self.root.op(
                "resolve_request",
                leaseId=self.root.game_state["lease"],
                epoch=attempt.epoch,
                civilization=self.root.civilization,
                transport="player_command",
                request=effect.prepared.payload,
            )
            receipt = response["receipt"]
            await self.handoffs.apply(NativeHandoffApplication(effect=effect, receipt=receipt))
            await self.handoffs.finish(effect)
        await self.root.retire_local_attempt(self.ticket)

    async def hold_communication(self):
        self.communication_gate.clear()
        await self.communication_idle.wait()
        await self.root.communication.set_held(True)
        for permit in list(self.permits.values()):
            if permit.job.purpose == "communication" and not permit.released:
                await self._compute(permit, False)

    async def release_communication(self):
        await self.root.communication.set_held(False)
        self.communication_gate.set()

    async def close(self):
        for task in self.tasks:
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        async with AsyncExitStack() as resources:
            resources.callback(self.root.store.close)
            resources.push_async_callback(self.root.close)
            resources.push_async_callback(self.clients.close)
