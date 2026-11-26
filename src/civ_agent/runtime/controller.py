"""One supervised async game controller; process failures require explicit resume."""

import asyncio
import json
import sys
import uuid
from contextlib import AsyncExitStack
from datetime import UTC, datetime, timedelta
from pathlib import Path

from filelock import Timeout
from pydantic_ai import ConcurrencyLimiter
from pydantic_ai.exceptions import UnexpectedModelBehavior, UsageLimitExceeded

from civ_agent.diagnostics import exception_details
from civ_agent.game_config import ResolvedGameConfig
from civ_agent.gameplay_policy import legacy_gameplay_policy
from civ_agent.operator_connection import Operator, operator_token
from civ_agent.orchestration.authored_documents import AuthoredDocuments
from civ_agent.orchestration.communication import NativeCommunication
from civ_agent.orchestration.communication_store import communication_scope
from civ_agent.orchestration.contracts import (
    CommunicationBatch,
    CommunicationRun,
    EventNotice,
    FinishCommunication,
    GatewayJob,
    RoleResult,
    Scope,
    TurnTicket,
    digest,
)
from civ_agent.orchestration.coordinator import NativeCoordinator
from civ_agent.orchestration.incoming_routing import IncomingBatch, NativeIncoming
from civ_agent.orchestration.job_contracts import NativeJob, NativeJobReport, host_job_id
from civ_agent.orchestration.managed_host import validate_limit_increase
from civ_agent.orchestration.managed_player import NativePlayerSession, NativePlayerSettings
from civ_agent.orchestration.peace_authorization import NativePeace
from civ_agent.orchestration.provider_preparation import prepare_managed_sessions
from civ_agent.orchestration.review_publication import NativeReviews
from civ_agent.orchestration.store import JournalConflict, WorkflowStore
from civ_agent.orchestration.worker_configuration import WorkerProviderConfig, provider_clients
from civ_agent.runtime.agents import configure_cache_warning_logging
from civ_agent.runtime.model_policy import FreshConversationRequired
from civ_agent.runtime.player import LocalPlayer
from civ_agent.runtime.review_cycles import ReviewScheduler
from civ_agent.runtime.state import AttemptRecord, CommunicationDeferral, ControllerState, ControllerStore
from civ_agent.tls import private_directory, regular_file, write_private_json

# These reasons come from controller-owned state checks. Exception messages
# outside this set may contain provider or tool data, even in all caps.
_CONTROLLER_FAILURE_CODES = (
    "LOCAL_GAME_EPOCH_CHANGED",
    "LOCAL_GAME_POSITION_REWOUND",
    "LOCAL_RECOVERY_PLAYER_UNAVAILABLE",
    "LOCAL_RECOVERY_ACCOUNTING_REJECTED",
    "LOCAL_TERMINAL_STATE_CHANGED",
    "LOCAL_CONTROLLER_CONFIGURATION_CHANGED",
    "LOCAL_RESUME_AUTHORIZATION_REQUIRED",
    "NATIVE_GAME_HALTED",
)


class LocalController:
    def __init__(
        self,
        root,
        config,
        manifest,
        operator,
        *,
        players=None,
        host_ca=None,
        provider_factory=None,
        advisory_socket: Path | None = None,
    ):
        self.root, self.config, self.manifest, self.operator = root, config, manifest, operator
        self.store = ControllerStore(root)
        self.players = players or {}
        self.capacity = ConcurrencyLimiter(config.limits.max_concurrent_jobs, name="game-model-requests")
        self.reviews = ReviewScheduler(config.limits.max_concurrent_jobs)
        self.communication_tasks: list[asyncio.Task] = []
        self.closing = False
        self.host_ca = host_ca
        self.provider_factory = provider_factory
        self.advisory_socket = advisory_socket
        self._observation_lock = asyncio.Lock()
        self._native_finished = asyncio.Event()
        self._heartbeat_task = None
        self._terminal_reconciliation_started = False
        self.publisher = None

    async def op(self, name, **fields):
        return await asyncio.to_thread(self.operator.call, name, **fields)

    async def _observe(self, status):
        def update(state):
            if status["epoch"] != state.epoch:
                raise RuntimeError("LOCAL_GAME_EPOCH_CHANGED")
            if status["handoff"] < state.handoff or status["turn"] < state.turn:
                raise RuntimeError("LOCAL_GAME_POSITION_REWOUND")
            state.turn, state.handoff = status["turn"], status["handoff"]
            state.model_invocations = max(state.model_invocations, status.get("modelInvocations", 0))
            state.active_civilization = status.get("activeCivilization")
            state.active_controller = status.get("activeController")

        await self.store.save(update)
        if status["phase"] == "FINISHED":
            self._native_finished.set()

    async def observed_status(self, operation="status", **fields):
        # The request is part of the critical section: applying under a lock
        # alone would still accept a delayed snapshot after a newer response.
        async with self._observation_lock:
            status = (await self.op(operation, **fields))["status"]
            await self._observe(status)
            return status

    def _limit(self):
        state, limits = self.store.current, self.config.limits
        if datetime.now(UTC) >= state.deadline:
            return "MAX_SECONDS"
        if limits.max_turns is not None and state.turn - state.initial_turn >= limits.max_turns:
            return "MAX_TURNS"
        return None

    async def communications(self, held):
        operation = "hold_communication" if held else "release_communication"
        results = await asyncio.gather(
            *[getattr(p, operation)() for p in self.players.values()], return_exceptions=True
        )
        for result in results:
            if isinstance(result, BaseException):
                raise result

    async def _background(self, player):
        scope = communication_scope(player.session.game, player.session.player, self.store.current.epoch)
        run = CommunicationRun(
            scope=scope,
            selection=player.session.get_selection("communication"),
            request_limit=player.session.get_request_limit("communication"),
            managed=True,
        )
        service = player.root.communication
        incoming = NativeIncoming(service)
        while not self.closing:
            await service.collect(run)
            deferred = self.store.current.communication_deferrals.get(player.session.player)
            eligible = deferred is None or self.store.current.handoff >= deferred.next_handoff
            if player.communication_gate.is_set() and eligible:
                if deferred is not None:
                    notices = [EventNotice.model_validate(value) for value in deferred.notices]
                    await player.root.io.call(service.store(scope).reopen, notices, deferred.job)
                    service.generation = uuid.uuid4().hex
                    self.store.current.communication_deferrals.pop(player.session.player, None)
                    await self.store.save()
                notices = await service.pending(scope)
                if notices:
                    plan = await incoming.plan(IncomingBatch(run=run, notices=notices[:50]))
                    if plan.classifications:
                        await asyncio.gather(*[incoming.classify(work) for work in plan.classifications])
                        continue
                    for notice in plan.consumed:
                        await player.root.io.call(service.store(scope).signaled, notice)
                    if not plan.ready:
                        await asyncio.sleep(1)
                        continue
                    batch = CommunicationBatch(run=run, notices=plan.ready)
                    request = await service.prepare(batch)
                    for notice in batch.notices:
                        await player.root.io.call(service.store(scope).signaled, notice)
                    if request is not None:
                        try:
                            result = await player.run_request(request)
                        except (FreshConversationRequired, UnexpectedModelBehavior, UsageLimitExceeded) as error:
                            if any(
                                p.job.purpose == "communication" and not p.released for p in player.permits.values()
                            ):
                                raise JournalConflict("COMMUNICATION_SETTLEMENT_UNCERTAIN") from error
                            await player.root.registry.retire(
                                GatewayJob(scope=request.scope, job=request.job, purpose=request.purpose)
                            )
                            self.store.current.communication_deferrals[player.session.player] = CommunicationDeferral(
                                job=request.job,
                                next_handoff=self.store.current.handoff + 1,
                                notices=[notice.model_dump(mode="json") for notice in batch.notices],
                                reason=type(error).__name__,
                            )
                            await self.store.save()
                            continue
                        self.store.current.communication_deferrals.pop(player.session.player, None)
                        history = player.root.store.put(request.scope, {"run_id": result.run_id})
                        completed = RoleResult(
                            scope=request.scope,
                            job=request.job,
                            purpose="communication",
                            status="COMPLETED",
                            history=history,
                            usage=result.usage.opentelemetry_attributes(),
                        )
                        body = {"request": request.model_dump(mode="json"), "run_id": result.run_id}
                        player.root.store.reserve(request.scope, "role-result", body)
                        player.root.store.complete(
                            request.scope, "role-result", body, completed.model_dump(mode="json")
                        )
                        await service.finish(FinishCommunication(batch=batch, result=completed))
            await asyncio.sleep(1)

    def _background_failure(self):
        self.reviews.raise_if_failed()
        for task in self.communication_tasks:
            if task.done() and not task.cancelled():
                error = task.exception()
                if error is not None:
                    raise error
        for player in self.players.values():
            routing = getattr(getattr(player.root, "gateway", None), "routing", None)
            if routing is not None:
                routing.raise_if_failed()
            for task in player.tasks:
                if task.done() and not task.cancelled() and task.exception() is not None:
                    raise task.exception()

    async def _heartbeat(self):
        while not self.closing:
            await self.observed_status("heartbeat", leaseId=self.store.current.lease_id)
            await asyncio.sleep(5)

    async def _recover(self, *, terminal=False):
        state = self.store.current
        await self.store.phase("RECOVERING")
        await self.op("barrier", leaseId=state.lease_id)
        await self.communications(True)
        old = state.active_attempt
        if old is not None:
            player = self.players.get(old.civilization)
            if player is None:
                raise RuntimeError("LOCAL_RECOVERY_PLAYER_UNAVAILABLE")
            old.status = "RECOVERING"
            await self.store.save()
            await player.recover(old)
        for player in self.players.values():
            if old is None or player.root.civilization != old.civilization:
                scope = Scope(game=state.game, player=player.session.player, epoch=state.epoch, activation="recovery")
                await player.root.retire_local_attempt(
                    TurnTicket(scope=scope, turn=state.turn, handoff=state.handoff, index=0)
                )
            for role in list(state.native_jobs.values()):
                if role.released or role.native_job is None:
                    continue
                job = NativeJob.model_validate(role.native_job)
                if job.scope.player != player.session.player:
                    continue
                if role.pending_report is not None:
                    pending = NativeJobReport.model_validate(role.pending_report)
                    result = await (
                        player.admission.release(pending) if role.pending_release else player.admission.report(pending)
                    )
                    if not result.granted:
                        raise RuntimeError("LOCAL_RECOVERY_ACCOUNTING_REJECTED")
                await self.op(
                    "retire_job",
                    leaseId=state.lease_id,
                    epoch=state.epoch,
                    civilization=player.root.civilization,
                    job=host_job_id(job),
                )
                role.released, role.phase = True, "INTERRUPTED"
                await self.store.role(role)
            if not terminal:
                scope = communication_scope(player.session.game, player.session.player, state.epoch)
                await player.root.communication.restart(scope)
        status = await self.observed_status()
        if terminal and status["phase"] != "FINISHED":
            raise RuntimeError("LOCAL_TERMINAL_STATE_CHANGED")
        if old is not None:
            old.status = "COMPLETED" if terminal or status["handoff"] > old.handoff else "INTERRUPTED"
            await self.store.archive_attempt(old)
            state.active_attempt = None
        state.communication_deferrals.clear()
        state.mode, state.reason = "RUNNING", None
        if terminal:
            state.mode, state.phase = "FINISHED", "COMPLETED"
        await self.store.save()

    async def _complete(self):
        """Stop new work and settle admitted receipts against the finished native game."""
        self._terminal_reconciliation_started = True
        for player in self.players.values():
            player.dispatch_fenced = True
        # Keep the native lease renewed while potentially large journals settle.
        tasks = [task for task in self.communication_tasks if task is not self._heartbeat_task]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await self.reviews.close()
        await self._recover(terminal=True)

    async def _prepare_reviews(self):
        state = self.store.current
        active = self.players.get(state.active_civilization)
        if active is not None:
            await self.reviews.prioritize(active.session.player, state.turn)
        for player in self.players.values():
            service = getattr(player.root, "reviews", None)
            if service is None:
                continue
            # A copied raw save can begin at a later native turn with fresh
            # stores. Never invent a turn-zero review from that later position.
            if state.initial_turn == 0:
                await service.capture(0, 0, opening=True)
            else:
                await service.initialize_import(state.initial_turn)
            for cycle in await service.pending(state.handoff):
                await self.reviews.submit(player, cycle)

    async def _attempt(self, status):
        state = self.store.current
        player = self.players[status["activeCivilization"]]
        attempt = AttemptRecord(
            gameplay_policy=getattr(player.session.settings, "gameplay_policy", legacy_gameplay_policy()),
            execution_policy=player.session.settings.execution_policy,
            id=uuid.uuid4().hex,
            player=player.session.player,
            civilization=player.root.civilization,
            epoch=state.epoch,
            turn=status["turn"],
            handoff=status["handoff"],
        )
        state.active_attempt, state.attempts = attempt, state.attempts + 1
        await self.store.phase("PREPARING")
        await self.communications(True)
        await self.store.phase("WAITING_REVIEW")
        await self.reviews.wait(player, status["turn"])
        # A previous recovery barrier stays closed until every old attempt is fenced.
        await self.op("unbarrier", leaseId=state.lease_id)
        context = await player.begin(attempt)
        attempt.status = "RUNNING"
        await self.store.save()
        await self.communications(False)
        while True:
            self._background_failure()
            await self.store.phase("ROLE_WORK")
            await player.run_roles(context)
            await self.communications(True)
            await self.store.phase("HANDOFF")
            if getattr(player.root, "reviews", None) is not None:
                committed = await player.handoff(context, on_committed=lambda cycle: self.reviews.submit(player, cycle))
            else:
                committed = await player.handoff(context)
            if committed:
                attempt.status = "COMPLETED"
                await self.store.archive_attempt(attempt)
                state.active_attempt = None
                await self.store.save()
                return
            await self.communications(False)
            await self.store.phase("MAIN_FOLLOWUP")

    async def _supervise(self, work):
        async def watch():
            while True:
                if self._native_finished.is_set():
                    return
                self._background_failure()
                await asyncio.sleep(0.02)

        active = asyncio.create_task(work)
        watcher = asyncio.create_task(watch())
        try:
            done, _ = await asyncio.wait((active, watcher), return_when=asyncio.FIRST_COMPLETED)
            if watcher in done:
                for player in self.players.values():
                    player.dispatch_fenced = True
                watcher.result()
                if self._native_finished.is_set():
                    return
            return await active
        finally:
            active.cancel()
            watcher.cancel()
            await asyncio.gather(active, watcher, return_exceptions=True)

    async def run(self, *, resume=False):
        runtime_ready = False
        try:
            self.store.lock.acquire()
        except Timeout:
            raise RuntimeError("LOCAL_CONTROLLER_ALREADY_RUNNING") from None
        try:
            state = self.store.load()
            status = (await self.op("status"))["status"]
            config_hash = configuration_digest(self.config)
            if state is None:
                began = datetime.now(UTC)
                self.store.state = state = ControllerState(
                    game=self.manifest["id"],
                    epoch=status["epoch"],
                    lease_id=self.manifest.get("native", {}).get("leaseId", "local-" + uuid.uuid4().hex),
                    config_sha256=config_hash,
                    initial_started_at=began,
                    sdk_requests=0,
                    limits=self.config.limits.model_dump(mode="json"),
                    deadline=began + timedelta(seconds=self.config.limits.max_seconds),
                    initial_turn=status["turn"],
                    turn=status["turn"],
                    handoff=status["handoff"],
                )
                await self.store.save()
            elif (state.game, state.config_sha256) != (self.manifest["id"], config_hash):
                raise RuntimeError("LOCAL_CONTROLLER_CONFIGURATION_CHANGED")
            elif not resume or state.resume_authorization == self.manifest.get("launch"):
                if state.mode == "FINISHED":
                    return state
                state.mode, state.reason, state.phase = "PAUSED", "EXPLICIT_RESUME_REQUIRED", "PAUSED"
                await self.store.save()
                return state
            if resume:
                authorization = self.manifest.get("launch")
                if not isinstance(authorization, str) or not authorization:
                    raise RuntimeError("LOCAL_RESUME_AUTHORIZATION_REQUIRED")
                state.resume_authorization = authorization
                if state.limits:
                    from civ_agent.game_config import Limits

                    previous = Limits.model_validate(state.limits)
                    validate_limit_increase(previous, self.config.limits)
                    if self.config.limits.max_seconds > previous.max_seconds:
                        state.deadline = state.initial_started_at + timedelta(seconds=self.config.limits.max_seconds)
                state.limits = self.config.limits.model_dump(mode="json")
                await self.store.save()
            await self.op("acquire", leaseId=state.lease_id)
            # Player/provider preparation can await remote metadata for longer
            # than the native lease lifetime. Renew from acquisition onward,
            # including before opening-review capture, not only during turns.
            self._heartbeat_task = asyncio.create_task(self._heartbeat())
            self.communication_tasks.append(self._heartbeat_task)
            await self.op(
                "job_limits",
                leaseId=state.lease_id,
                maxInvocations=0,
                maxConcurrent=self.config.limits.max_concurrent_jobs,
                maxPerCivilization=min(4, self.config.limits.max_concurrent_jobs),
            )
            await self.op("barrier", leaseId=state.lease_id)
            if not self.players:
                self.players = await build_players(
                    self.root,
                    self.config,
                    self.manifest,
                    self.operator,
                    self.store,
                    self.capacity,
                    self.host_ca,
                    provider_factory=self.provider_factory,
                    advisory_socket=self.advisory_socket,
                )
            runtime_ready = True
            status = await self.observed_status()
            if status["phase"] == "FINISHED":
                await self._complete()
            elif resume:
                await self._recover()
            from civ_agent.operator_views import OperatorPublisher

            self.publisher = OperatorPublisher(self.root, self.store, players=self.players)
            await self.publisher.start()
            if state.mode != "FINISHED":
                await self._prepare_reviews()
                self.communication_tasks.extend(asyncio.create_task(self._background(p)) for p in self.players.values())
                self.communication_tasks.append(asyncio.create_task(self.publisher.run()))
            while state.mode == "RUNNING":
                self._background_failure()
                status = await self.observed_status("heartbeat", leaseId=state.lease_id)
                if status["phase"] == "FINISHED":
                    await self._complete()
                    break
                if status["phase"] == "HALTED":
                    raise RuntimeError("NATIVE_GAME_HALTED")
                reason = self._limit()
                if reason:
                    state.mode, state.phase, state.reason = "STOPPED", "STOPPED", reason
                    break
                controller = status.get("activeController")
                if controller == "AGENT":
                    remaining = max(0, (state.deadline - datetime.now(UTC)).total_seconds())
                    async with asyncio.timeout(min(remaining, self.config.limits.turn_timeout)):
                        await self._supervise(self._attempt(status))
                else:
                    await self.store.phase("WAITING_HUMAN" if controller == "HUMAN" else "WAITING_ENGINE")
                    await self.op("unbarrier", leaseId=state.lease_id)
                    if controller == "HUMAN" and status["phase"] == "PAUSED":
                        await self.op(
                            "activate",
                            leaseId=state.lease_id,
                            epoch=state.epoch,
                            serverInstance=status["serverInstance"],
                            handoff=status["handoff"],
                            civilization=status["activeCivilization"],
                            activationId=uuid.uuid4().hex,
                        )
                    await self.communications(False)
                    await asyncio.sleep(1)
            await self.store.save()
        except (asyncio.CancelledError, KeyboardInterrupt):
            for player in self.players.values():
                player.dispatch_fenced = True
            state = self.store.state
            if state is not None:
                state.mode, state.phase, state.reason = "PAUSED", "PAUSED", "OPERATOR_STOP"
                await self.store.save()
            raise
        except Exception as error:
            for player in self.players.values():
                player.dispatch_fenced = True
            state = self.store.state
            if state is None:
                raise
            completed = False
            if runtime_ready and not self._terminal_reconciliation_started:
                try:
                    status = await self.observed_status()
                except Exception:
                    status = None
                if status is not None and status["phase"] == "FINISHED":
                    write_private_json(
                        self.store.root / "terminal-transition.json",
                        {"epoch": state.epoch, "turn": state.turn, "exception": exception_details(error)},
                    )
                    try:
                        await self._complete()
                    except Exception as settlement_error:
                        error = ExceptionGroup("Terminal receipt reconciliation failed", [error, settlement_error])
                    else:
                        completed = True
            if not completed:
                await self._record_failure(error)
        finally:
            primary = sys.exception()
            cleanup = asyncio.create_task(self._shutdown())
            cancelled = False
            while not cleanup.done():
                try:
                    await asyncio.shield(cleanup)
                except asyncio.CancelledError:
                    cancelled = True
                except BaseException:
                    break
            try:
                cleanup.result()
            except BaseException as error:
                if primary is None and self.store.state is None:
                    raise
                write_private_json(self.store.root / "cleanup-failure.json", {"exception": exception_details(error)})
            finally:
                self.store.lock.release()
            if cancelled and primary is None:
                raise asyncio.CancelledError

        return self.store.current

    async def _record_failure(self, error):
        state = self.store.current
        limit = limit_reason(error, state.deadline)
        reason_error = error
        while isinstance(reason_error, BaseExceptionGroup) and len(reason_error.exceptions) == 1:
            reason_error = reason_error.exceptions[0]
        reason = type(reason_error).__name__
        if type(reason_error) is RuntimeError and len(reason_error.args) == 1:
            code = reason_error.args[0]
            if type(code) is str and code in _CONTROLLER_FAILURE_CODES:
                reason = code
        state.mode = "STOPPED" if limit else "FAILED"
        state.phase = state.mode
        state.reason = limit or reason
        await self.store.save()
        if not limit:
            native_failure = None
            try:
                native_failure = (await self.op("status"))["status"].get("failure")
            except Exception:
                pass
            write_private_json(
                self.store.root / "failure.json",
                {
                    "type": type(error).__name__,
                    "reason": state.reason,
                    "exception": exception_details(error),
                    "native_failure": native_failure,
                },
            )

    async def _shutdown(self):
        self.closing = True
        cleanup = []

        async def close_resource(operation):
            try:
                await operation()
            except BaseException as error:
                cleanup.append(exception_details(error))

        state = self.store.state
        if state is not None:
            try:
                await self.op("barrier", leaseId=state.lease_id)
            except Exception as error:
                cleanup.append(exception_details(error))
        for task in self.communication_tasks:
            task.cancel()
        await asyncio.gather(*self.communication_tasks, return_exceptions=True)
        if self.publisher is not None:
            # A stopped controller has no next publication tick. Flush the
            # terminal state while the stores and player histories remain open.
            await close_resource(self.publisher.publish)
        async with AsyncExitStack() as resources:
            resources.push_async_callback(close_resource, self.store.close)
            if self.publisher is not None:
                resources.push_async_callback(close_resource, self.publisher.close)
            for player in self.players.values():
                resources.push_async_callback(close_resource, player.close)
            resources.push_async_callback(close_resource, self.reviews.close)
        cleanup.extend(detail for player in self.players.values() for detail in getattr(player, "cleanup_errors", []))
        if cleanup:
            write_private_json(self.store.root / "cleanup-failure.json", {"exceptions": cleanup[:24]})


async def build_players(
    root,
    config,
    manifest,
    operator,
    state,
    capacity,
    host_ca,
    *,
    provider_factory=None,
    advisory_socket: Path | None = None,
):
    sessions = [
        NativePlayerSession(
            game=manifest["id"],
            player="p" + str(index),
            request_limit=config.agents.request_limit,
            settings=NativePlayerSettings.model_validate(
                {**config.settings(player), "scripted": config.runtime.scripted_models}
            ),
        )
        for index, player in enumerate(config.agent_players)
    ]
    frozen = [NativePlayerSession.model_validate(seat["session"]) for seat in state.current.seats] or None
    same_selections = frozen is not None and all(
        old.settings.fast == new.settings.fast for old, new in zip(frozen, sessions, strict=True)
    )
    sessions = await prepare_managed_sessions(sessions, frozen=frozen if same_selections else None)
    state.current.seats = [
        {"civilization": player.civilization, "session": session.model_dump(mode="json")}
        for player, session in zip(config.agent_players, sessions, strict=True)
    ]
    await state.save()
    players = {}
    async with AsyncExitStack() as resources:
        for assigned, session in zip(config.agent_players, sessions, strict=True):
            from civ_agent.player_paths import PlayerPaths

            paths = PlayerPaths(root, int(session.player[1:]), assigned.civilization)
            directory = private_directory(paths.coordinator)
            artifacts = WorkflowStore(private_directory(paths.store), player=session.player)
            resources.callback(artifacts.close)
            coordinator = NativeCoordinator(
                game=session.game,
                player=session.player,
                civilization=assigned.civilization,
                state_dir=directory,
                store=artifacts,
                operator=operator,
                host_ca=host_ca,
                external_lease_id=state.current.lease_id,
            )
            resources.push_async_callback(coordinator.close)
            coordinator.communication = NativeCommunication(coordinator)
            authored = artifacts.root / "authored.sqlite3"
            coordinator.configure_authored_memory(AuthoredDocuments(authored))
            await coordinator.configure_managed_session(
                session, expected_epoch=state.current.epoch, allow_fast_change=True
            )
            scripted = config.runtime.scripted_models
            selections = [session.get_selection(role) for role in ("gameplay", "economy", "military", "communication")]
            subscription = not scripted and any(s.authentication == "subscription" for s in selections)
            google_cloud = None
            if not scripted and any(s.provider == "google-cloud" for s in selections):
                from civ_agent.google_cloud_auth import CONTAINER_CONFIG, CONTAINER_CREDENTIALS, load_config

                google_cloud = load_config(CONTAINER_CONFIG, credentials_file=CONTAINER_CREDENTIALS)
            providers = WorkerProviderConfig(
                scripted=scripted,
                broker_socket=Path("/auth-ipc/broker.sock") if subscription else None,
                broker_token_file=_provider_token(root, session, assigned.civilization) if subscription else None,
                openrouter_key_file=Path("/run/secrets/openrouter.key")
                if not scripted and any(s.provider == "openrouter" for s in selections)
                else None,
                google_cloud=google_cloud,
            )
            clients = provider_factory(providers) if provider_factory is not None else provider_clients(providers)
            resources.push_async_callback(clients.close)
            if scripted:
                from civ_agent.runtime.scripted_model import scripted_model

                role_model_factory = scripted_model
            else:
                role_model_factory = None
            player = LocalPlayer(
                coordinator,
                session,
                clients=clients,
                model_factory=role_model_factory,
                state=state,
                model_limiter=capacity,
            )
            players[assigned.civilization] = player
            coordinator.reviews = NativeReviews(coordinator, coordinator.communication)
            coordinator.gateway.peace_reviews = NativePeace(coordinator, consult=player.consult)
            if session.settings.decisions_enabled:
                from civ_agent.decisions_broker import RoutingClient
                from civ_agent.orchestration.advisories import NativeAdvisories
                from civ_agent.orchestration.routing import NativeRouting

                broker = RoutingClient(
                    advisory_socket if advisory_socket is not None else Path("/advisory-ipc/broker.sock"),
                    paths.coordinator_security / "decisions-client.json",
                    backend=session.settings.decisions_backend,
                )
                routing = NativeRouting(
                    coordinator,
                    broker,
                    session.settings,
                    human_players={config.human} if config.human is not None else set(),
                )
                coordinator.gateway.routing = routing
                coordinator.gateway.advisories = NativeAdvisories(coordinator, broker, session.settings)
        resources.pop_all()
        return players


def configuration_digest(config):
    value = config.model_dump(mode="json")
    for key in ("limits", "resources", "runtime"):
        value.pop(key, None)
    value["agents"].pop("fast", None)
    return digest(value)


def limit_reason(error: BaseException, deadline: datetime) -> str | None:
    if isinstance(error, BaseExceptionGroup):
        reasons = [limit_reason(child, deadline) for child in error.exceptions]
        return reasons[0] if all(reasons) else None
    if isinstance(error, TimeoutError):
        return "MAX_SECONDS" if datetime.now(UTC) >= deadline else "TURN_TIMEOUT"
    if isinstance(error, UsageLimitExceeded):
        return "MODEL_REQUEST_LIMIT"
    if isinstance(error, RuntimeError) and error.args in (
        ("MODEL_INVOCATION_LIMIT",),
        ("ROLE_CAPACITY_TOO_SMALL",),
    ):
        return error.args[0]
    return None


def _provider_token(root, session, civilization):
    from civ_agent.player_paths import PlayerPaths

    return PlayerPaths(root, int(session.player[1:]), civilization).security / "auth-client.json"


async def run_controller(root: Path, *, resume: bool, host_url="https://game:8765", host_ca: Path | None = None):
    configure_cache_warning_logging()
    root = root.resolve()
    config = ResolvedGameConfig.model_validate_json(regular_file(root / "resolved.json", private=True).read_bytes())
    manifest = json.loads(regular_file(root / "container.json", private=True).read_bytes())
    if manifest.get("runtime") != "pydantic-local-v1":
        raise ValueError("LOCAL_RUNTIME_REQUIRES_FRESH_GAME")
    if host_ca is None:
        host_ca = Path(json.loads(regular_file(root / "endpoints.json", private=True).read_bytes())["caFile"])
    operator = Operator(host_url, operator_token(root / "operator.json"), host_ca)
    tracing = config.runtime.tracing
    provider = None
    if tracing is not None:
        from civ_agent.orchestration.tracing import configure_tracing
        from civ_agent.orchestration.worker_configuration import TRACING_TOKEN_PATH

        token = TRACING_TOKEN_PATH if TRACING_TOKEN_PATH.exists() else tracing.token_file
        provider = configure_tracing(token, str(tracing.endpoint), local=tracing.local)
    try:
        return await LocalController(root, config, manifest, operator, host_ca=host_ca).run(resume=resume)
    finally:
        if provider is not None:
            await asyncio.to_thread(provider.force_flush)
            await asyncio.to_thread(provider.shutdown)
