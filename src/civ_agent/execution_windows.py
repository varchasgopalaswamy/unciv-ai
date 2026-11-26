"""Fair, ephemeral admission for short delivered-read/command sequences.

The gate coordinates scheduling only. It never supplies observations, grants
inspection credit, rewrites revisions, or restores a window after a restart.
All domain checks and durable native effect identities remain authoritative.
"""

import asyncio
import copy
import hashlib
import logging
import time
from collections import deque
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager, contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any

from pydantic_ai import ModelRetry

from civ_agent.orchestration.contracts import GatewayJob
from civ_agent.orchestration.store import JournalConflict

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class WindowBinding:
    identity: GatewayJob
    run_id: str
    parent_call_id: str
    program_sha256: str

    @classmethod
    def create(cls, identity, *, run_id: str, parent_call_id: str, code: str):
        if not run_id or not parent_call_id or not isinstance(code, str):
            raise JournalConflict("FOCUSED_WINDOW_BINDING_REQUIRED")
        return cls(
            GatewayJob.model_validate(identity.model_dump(include={"scope", "job", "purpose"})),
            run_id,
            parent_call_id,
            hashlib.sha256(code.encode()).hexdigest(),
        )


@dataclass(eq=False)
class _Ticket:
    ready: asyncio.Future
    window: "FocusedWindow | None" = None


@dataclass(eq=False)
class FocusedWindow:
    scheduler: "ExecutionWindowScheduler"
    binding: WindowBinding
    seconds: float
    max_calls: int
    queue_timeout: float
    ticket: _Ticket | None = None
    timer: asyncio.TimerHandle | None = None
    deadline: float | None = None
    calls: int = 0
    in_flight: int = 0
    ended: str | None = None

    def end(self, reason: str):
        if self.ended is None:
            self.ended = reason
            if self.timer is not None:
                self.timer.cancel()
            self.scheduler._event("ended", self, reason=reason)
        if self.in_flight == 0 and self.ticket is not None:
            self.scheduler._abandon(self.ticket)

    def check(self):
        if self.deadline is not None and time.monotonic() >= self.deadline:
            self.end("expired")
        if self.ended is not None:
            if self.ended in {"source_epoch_retired", "job_retired", "scheduler_closed"}:
                raise JournalConflict("FOCUSED_WINDOW_AUTHORITY_RETIRED")
            raise ModelRetry(
                f"FOCUSED_WINDOW_ENDED ({self.ended}). Preserve settled receipts; "
                "choose any further work in a new program with current observations."
            )


@dataclass
class _Admission:
    scheduler: "ExecutionWindowScheduler"
    identity: GatewayJob
    call_key: str
    effect_started: bool = False


_focused: ContextVar[FocusedWindow | None] = ContextVar("native_focused_window", default=None)
_admitted: ContextVar[_Admission | None] = ContextVar("native_window_admission", default=None)


def release_focus(reason: str):
    """Trusted runtime/lifecycle hook; it supplies no model-visible authority."""
    window = _focused.get()
    if window is not None:
        window.end(reason)


@contextmanager
def detached_focus(reason: str):
    """Run deferred children without inheriting their ended parent's binding."""
    release_focus(reason)
    token = _focused.set(None)
    try:
        yield
    finally:
        # The parent remains ended. Returning from a wait grants no new focus.
        _focused.reset(token)


def focused_stop_reason():
    """Stop fresh batch children after expiry; frozen effects still settle normally."""
    window = _focused.get()
    if window is None:
        return None
    if window.deadline is not None and time.monotonic() >= window.deadline:
        window.end("expired")
    if window.ended is not None:
        return "FOCUSED_WINDOW_EXPIRED" if window.ended == "expired" else "FOCUSED_WINDOW_ENDED"
    return None


def native_effect_started():
    """Trusted transport hook, immediately before an exact-ID native request."""
    admission = _admitted.get()
    if admission is not None:
        admission.effect_started = True


def record_invalidation(job, provenance=None):
    """Remember only an exactly linked chain of trusted own-role receipts."""
    prior = job.observation or job.prior_role_observation or {}
    previous = job.memory.get("observationInvalidation")
    expected = prior.get("revision")
    chain = []
    if isinstance(previous, dict) and previous.get("epoch") == job.epoch and previous.get("fromRevision") == expected:
        chain = list(previous.get("ownActions", []))
    frontier = chain[-1].get("toRevision") if chain else expected
    known = (
        isinstance(provenance, dict)
        and provenance.get("epoch") == job.epoch
        and isinstance(expected, str)
        and provenance.get("fromRevision") == frontier
        and isinstance(provenance.get("toRevision"), str)
        and provenance["toRevision"] != frontier
        and all(isinstance(provenance.get(key), str) and provenance[key] for key in ("role", "tool", "operationId"))
        and len(chain) < 4
        and not (
            isinstance(previous, dict)
            and previous.get("epoch") == job.epoch
            and previous.get("fromRevision") == expected
            and previous.get("cause") == "unknown"
        )
    )
    job.memory["observationInvalidation"] = {
        "epoch": job.epoch,
        "fromRevision": expected,
        "cause": "known_own_action" if known else "unknown",
        "ownActions": copy.deepcopy([*chain, provenance]) if known else [],
    }


def describe_stale(job, expected_revision=None):
    """Descriptive failure feedback; never a freshness or command authority token."""
    saved = job.memory.get("observationInvalidation")
    if (
        isinstance(saved, dict)
        and saved.get("epoch") == job.epoch
        and saved.get("fromRevision") == expected_revision
        and saved.get("cause") == "known_own_action"
        and saved.get("ownActions")
        and (job.observation is None or saved["ownActions"][-1].get("toRevision") == job.observation.get("revision"))
    ):
        return {
            **copy.deepcopy(saved),
            "instruction": "Reconsider current observations; the unexecuted action was not applied.",
        }
    return {
        "cause": "unknown",
        "instruction": "The delivered observation is no longer current. Reconsider current observations; no invalidating action is attributed.",
    }


def operation_cost(decision) -> int:
    """Count underlying requested operations; nested bundle queries are not free."""
    cost = 0
    operations = decision.get("operations", [])
    if not isinstance(operations, list):
        return 1
    for operation in operations:
        if not isinstance(operation, dict):
            cost += 1
            continue
        arguments = operation.get("arguments", {})
        queries = arguments.get("queries") if isinstance(arguments, dict) else None
        cost += max(1, len(queries)) if operation.get("tool") == "inspect_bundle" and isinstance(queries, list) else 1
    return max(1, cost)


class ExecutionWindowScheduler:
    def __init__(self, *, game: str, player: str):
        self.game, self.player = game, player
        self.owner: _Ticket | None = None
        self.queue: deque[_Ticket] = deque()
        self.tasks: set[asyncio.Task] = set()
        self.fenced: set[str] = set()
        self.closed = False
        self.suspended = False
        self.events: deque[dict[str, Any]] = deque(maxlen=256)

    def _event(self, kind, window, **extra):
        binding = window.binding
        event = {
            "kind": kind,
            "job": binding.identity.job,
            "scope": binding.identity.scope.model_dump(),
            "run_id": binding.run_id,
            "parent_call_id": binding.parent_call_id,
            "program_sha256": binding.program_sha256,
            "calls": window.calls,
            **extra,
        }
        self.events.append(event)
        logger.info("Focused execution window %s", event)

    def _grant(self):
        if self.closed or self.owner is not None:
            return
        while self.queue:
            ticket = self.queue.popleft()
            if ticket.ready.done():
                continue
            self.owner = ticket
            ticket.ready.set_result(None)
            return

    async def _acquire(self, window=None):
        if self.closed:
            raise JournalConflict("FOCUSED_WINDOW_SCHEDULER_CLOSED")
        ticket = _Ticket(asyncio.get_running_loop().create_future(), window)
        if window is not None:
            window.ticket = ticket
        self.queue.append(ticket)
        self._grant()
        try:
            async with asyncio.timeout(window.queue_timeout if window is not None else None):
                await asyncio.shield(ticket.ready)
            return ticket
        except BaseException:
            ticket.ready.cancel()
            self._release(ticket)
            try:
                self.queue.remove(ticket)
            except ValueError:
                pass
            raise

    def _release(self, ticket):
        if self.owner is ticket:
            self.owner = None
            self._grant()

    def _abandon(self, ticket):
        if not ticket.ready.done():
            ticket.ready.set_exception(ModelRetry("FOCUSED_WINDOW_INTERRUPTED. No native action was admitted."))
            try:
                self.queue.remove(ticket)
            except ValueError:
                pass
        self._release(ticket)

    def interrupt(self, reason: str, *, identity: GatewayJob | None = None):
        """End focus without releasing an admitted, still-settling operation."""
        for ticket in [self.owner, *self.queue]:
            window = ticket.window if ticket is not None else None
            if window is not None and (identity is None or window.binding.identity == identity):
                window.end(reason)

    def suspend(self, reason: str):
        self.suspended = True
        self.interrupt(reason)

    def resume(self):
        # Old window objects stay ended; this only allows a new explicit one.
        self.suspended = False

    def settled(self, call_key: str):
        """Trusted receipt/context commit hook; preparation and reads cannot clear a fence."""
        self.fenced.discard(call_key)

    async def close(self):
        self.closed = True
        self.interrupt("scheduler_closed")
        for ticket in self.queue:
            if not ticket.ready.done():
                ticket.ready.set_exception(JournalConflict("FOCUSED_WINDOW_SCHEDULER_CLOSED"))
        self.queue.clear()
        if self.tasks:
            await asyncio.gather(*list(self.tasks), return_exceptions=True)

    @asynccontextmanager
    async def focused(self, binding, *, seconds=10.0, max_calls=6, queue_timeout=30.0):
        if _focused.get() is not None:
            raise JournalConflict("FOCUSED_WINDOW_NESTING_FORBIDDEN")
        if (binding.identity.scope.game, binding.identity.scope.player) != (self.game, self.player):
            raise JournalConflict("FOCUSED_WINDOW_OWNER_CONFLICT")
        if not (0 < seconds <= 30 and 1 <= max_calls <= 12 and 0 < queue_timeout <= 300):
            raise ValueError("FOCUSED_WINDOW_LIMITS_INVALID")
        window = FocusedWindow(self, binding, seconds, max_calls, queue_timeout)
        token = _focused.set(window)
        try:
            yield window
        finally:
            window.end("program_finished")
            _focused.reset(token)

    async def run[T](
        self,
        identity: GatewayJob,
        call_key: str,
        handler: Callable[[], Awaitable[T]],
        *,
        write=False,
        cost=1,
        release=False,
        complete=True,
        run_id: str | None = None,
        settlement=False,
    ) -> T:
        """Wait before any domain lock; accepted work outlives caller cancellation."""
        previous = _admitted.get()
        if previous is not None and previous.scheduler is self:
            if previous.identity != identity or previous.call_key != call_key:
                raise JournalConflict("FOCUSED_WINDOW_ADMISSION_CONFLICT")
            return await handler()
        if (identity.scope.game, identity.scope.player) != (self.game, self.player):
            raise JournalConflict("FOCUSED_WINDOW_OWNER_CONFLICT")
        window = _focused.get()
        if window is not None:
            if window.scheduler is not self or window.binding.identity != identity:
                raise JournalConflict("FOCUSED_WINDOW_BINDING_CONFLICT")
            if run_id is not None and window.binding.run_id != run_id:
                raise JournalConflict("FOCUSED_WINDOW_RUN_CONFLICT")
            if settlement:
                # Frozen receipt work can finish an existing turn, without
                # restoring an expired window or waiting behind its own gate.
                if window.ticket is None or self.owner is not window.ticket:
                    window = None
            elif release:
                window.end("lifecycle_or_wait")
                window = None
            elif self.suspended:
                window.end("admission_suspended")
                window.check()
            else:
                window.check()
        if self.fenced and call_key not in self.fenced:
            raise JournalConflict("FOCUSED_WINDOW_RECONCILIATION_REQUIRED")
        if window is not None:
            if window.ticket is None:
                try:
                    window.ticket = await self._acquire(window)
                except TimeoutError:
                    window.end("queue_timeout")
                    raise ModelRetry("FOCUSED_WINDOW_QUEUE_TIMEOUT. No native action was admitted.") from None
                window.deadline = time.monotonic() + window.seconds
                window.timer = asyncio.get_running_loop().call_later(window.seconds, window.end, "expired")
                self._event("started", window)
            if not settlement:
                window.check()
            if not settlement and window.calls + cost > window.max_calls:
                window.end("call_limit")
                window.check()
            window.calls += cost
            window.in_flight += 1
            ticket = window.ticket
        else:
            ticket = await self._acquire()
        if self.closed:
            if window is not None:
                window.in_flight -= 1
                window.end("scheduler_closed")
            else:
                self._release(ticket)
            raise JournalConflict("FOCUSED_WINDOW_SCHEDULER_CLOSED")
        # A preceding admitted writer can become uncertain while this caller
        # waits. Recheck after grant, before any new domain preparation.
        if self.fenced and call_key not in self.fenced:
            if window is not None:
                window.in_flight -= 1
                window.end("reconciliation_required")
            else:
                self._release(ticket)
            raise JournalConflict("FOCUSED_WINDOW_RECONCILIATION_REQUIRED")

        async def accepted():
            admission = _Admission(self, identity, call_key)
            token = _admitted.set(admission)
            try:
                result = await handler()
                return result
            except BaseException as error:
                # Do not let loss of the caller or an unknown write response
                # open the next writer. Exact-identity recovery remains allowed.
                if not isinstance(error, ModelRetry) and (
                    admission.effect_started
                    or str(error) in {"DECISION_EXECUTION_UNCERTAIN", "NATIVE_RECEIPT_UNCERTAIN"}
                ):
                    self.fenced.add(call_key)
                raise
            finally:
                _admitted.reset(token)
                if window is not None:
                    window.in_flight -= 1
                    if write and complete:
                        window.end("mutation_settled")
                    elif window.ended is not None:
                        window.end(window.ended)
                else:
                    self._release(ticket)

        task = asyncio.create_task(accepted())
        self.tasks.add(task)

        def finished(done):
            self.tasks.discard(done)
            if not done.cancelled():
                done.exception()

        task.add_done_callback(finished)
        return await asyncio.shield(task)
