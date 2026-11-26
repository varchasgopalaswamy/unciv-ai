"""Portable, versioned gateway state captured only at settled activity boundaries.

Connections, bearer tokens, locks and tasks are recreated. Delivered evidence is
saved separately from cached observations so a hidden refresh never grants credit.
"""

import asyncio
import copy
import time
from collections.abc import Callable
from dataclasses import fields
from typing import Annotated, Literal, get_type_hints

from pydantic import Field, TypeAdapter

from civ_agent.observations import DetailQuery
from civ_agent.orchestration.contracts import canonical
from civ_agent.orchestration.store import JournalConflict
from civ_agent.proposal_gateway import JobAccess, PlayerConnection
from civ_agent.protocol import JsonObject
from civ_agent.review_grounding import ReviewEvidence
from civ_agent.schemas import BoundaryModel, Identifier

JSON_FIELDS = frozenset(
    {
        "memory",
        "observation",
        "prior_role_observation",
        "delivered_overview",
        "compact_overviews",
        "resume_delta_available",
        "overview_view",
        "detail_seen",
        "detail_pending",
        "memory_seen",
        "memory_pending",
        "memory_read_seen",
        "memory_read_pending",
        "memory_versions",
        "payload_sequence",
        "social",
        "social_read",
        "detail_revision",
        "role_refresh_required",
        "city_scope",
        "economy_seen",
        "coordination_seen",
        "coordination_scope",
        "coordination_sequence",
        "coordination_preparing",
        "coordination_pending",
        "trades",
        "closed",
        "completed_wait",
        "catalogue",
        "gameplay_fenced",
        "war_versions",
        "review_inputs",
        "outgoing_start",
        "suspend_for_negotiation",
    }
)
QUERY_FIELDS = frozenset({"details_read", "pending_invalidated"})
SET_FIELDS = frozenset({"seen_messages", "delivered_messages"})
EVENT_FIELDS = frozenset(
    {"role_wait_pending", "gameplay_ended", "model_stopped", "negotiation_pending", "role_finished"}
)
TASK_FIELDS = frozenset({"role_wait_calls", "pending_calls", "negotiation_calls", "completed_actions"})
LOCK_FIELDS = frozenset({"lock", "bundle_lock"})
CLOCK_FIELDS = frozenset({"wait_started", "handoff_at"})
IDENTITY_FIELDS = frozenset({"id", "purpose", "epoch"})
EXCLUDED_FIELDS = frozenset({"connection", "token", "verified", "owns_connection", "review_evidence"})


class GatewayState(BoundaryModel):
    version: Literal[1] = 1
    job: Identifier
    purpose: str
    epoch: Identifier
    data: JsonObject
    queries: dict[str, list[DetailQuery]]
    sets: dict[str, list[str] | None]
    events: dict[str, bool]
    elapsed: dict[str, Annotated[float, Field(ge=0, allow_inf_nan=False)] | None]
    captured_at: Annotated[float, Field(ge=0, allow_inf_nan=False)]
    delivered_access: JsonObject
    military_review: JsonObject | None = None
    military_targets: list[str] = Field(default_factory=list)
    delivery_step: Annotated[int, Field(ge=0)] = 0
    pending_access: JsonObject | None = None
    pending_military_review: JsonObject | None = None
    pending_military_targets: list[str] = Field(default_factory=list)
    review_evidence: JsonObject | None = None


def check_fields():
    classified = (
        JSON_FIELDS
        | QUERY_FIELDS
        | SET_FIELDS
        | EVENT_FIELDS
        | TASK_FIELDS
        | LOCK_FIELDS
        | CLOCK_FIELDS
        | IDENTITY_FIELDS
        | EXCLUDED_FIELDS
    )
    if classified != {field.name for field in fields(JobAccess)}:
        raise JournalConflict("GATEWAY_STATE_FIELDS_CHANGED")


def capture_job(
    job: JobAccess,
    *,
    delivered_access: JsonObject,
    military_review: JsonObject | None = None,
    military_targets: set[str] | None = None,
    delivery_step: int = 0,
    pending_access: JsonObject | None = None,
    pending_military_review: JsonObject | None = None,
    pending_military_targets: set[str] | None = None,
    monotonic: Callable[[], float] = time.monotonic,
    wall: Callable[[], float] = time.time,
) -> GatewayState:
    check_fields()
    if set(vars(job)) != {field.name for field in fields(JobAccess)}:
        raise JournalConflict("GATEWAY_STATE_FIELDS_CHANGED")
    if any(getattr(job, name) for name in TASK_FIELDS) or any(getattr(job, name).locked() for name in LOCK_FIELDS):
        raise JournalConflict("GATEWAY_CAPTURE_IN_FLIGHT")
    now = monotonic()
    evidence = job.review_evidence
    snapshot = GatewayState(
        job=job.id,
        purpose=job.purpose,
        epoch=job.epoch,
        data={name: copy.deepcopy(getattr(job, name)) for name in JSON_FIELDS},
        queries={name: copy.deepcopy(getattr(job, name)) for name in QUERY_FIELDS},
        sets={name: sorted(getattr(job, name)) if getattr(job, name) is not None else None for name in SET_FIELDS},
        events={name: getattr(job, name).is_set() for name in EVENT_FIELDS},
        elapsed={
            name: max(0.0, now - getattr(job, name)) if getattr(job, name) is not None else None
            for name in CLOCK_FIELDS
        },
        captured_at=wall(),
        delivered_access=copy.deepcopy(delivered_access),
        military_review=copy.deepcopy(military_review),
        military_targets=sorted(military_targets or set()),
        delivery_step=delivery_step,
        pending_access=copy.deepcopy(pending_access),
        pending_military_review=copy.deepcopy(pending_military_review),
        pending_military_targets=sorted(pending_military_targets or set()),
        review_evidence={
            "turn": evidence.turn,
            "namespace": evidence.namespace,
            "sources": copy.deepcopy(evidence.sources),
            "delivered": copy.deepcopy(evidence.delivered),
        }
        if evidence is not None
        else None,
    )
    # Round-trip now, while the activity still owns the state. Unexpected live
    # objects must fail before publication, not become an unreadable checkpoint.
    return GatewayState.model_validate_json(canonical(snapshot.model_dump(mode="python")))


def restore_job(
    snapshot: GatewayState,
    connection: PlayerConnection,
    *,
    job_id: str,
    purpose: str,
    epoch: str,
    monotonic: Callable[[], float] = time.monotonic,
    wall: Callable[[], float] = time.time,
) -> JobAccess:
    check_fields()
    if (snapshot.job, snapshot.purpose, snapshot.epoch) != (job_id, purpose, epoch):
        raise JournalConflict("GATEWAY_STATE_IDENTITY_CONFLICT")
    groups = (
        (snapshot.data, JSON_FIELDS),
        (snapshot.queries, QUERY_FIELDS),
        (snapshot.sets, SET_FIELDS),
        (snapshot.events, EVENT_FIELDS),
        (snapshot.elapsed, CLOCK_FIELDS),
    )
    if any(set(data) != expected for data, expected in groups):
        raise JournalConflict("GATEWAY_STATE_INCOMPLETE")
    hints = get_type_hints(JobAccess)
    data = {name: TypeAdapter(hints[name]).validate_python(value, strict=True) for name, value in snapshot.data.items()}
    memory = data.pop("memory")
    # Verification belonged to the old authenticated transport. The new
    # connection must prove player/epoch identity again before a fresh call.
    # Its creator owns its lifetime; the old ownership flag grants no rights.
    job = JobAccess(job_id, purpose, epoch, connection, memory, **data)
    for name, queries in snapshot.queries.items():
        setattr(job, name, copy.deepcopy(queries))
    for name, values in snapshot.sets.items():
        if values is None and name == "seen_messages":
            raise JournalConflict("GATEWAY_STATE_INCOMPLETE")
        setattr(job, name, set(values) if values is not None else None)
    for name, value in snapshot.events.items():
        event = asyncio.Event()
        if value:
            event.set()
        setattr(job, name, event)
    now, downtime = monotonic(), max(0.0, wall() - snapshot.captured_at)
    for name, elapsed in snapshot.elapsed.items():
        setattr(job, name, now - elapsed - downtime if elapsed is not None else None)
    if snapshot.review_evidence is not None:
        if job.review_inputs is None:
            raise JournalConflict("GATEWAY_REVIEW_INPUT_MISSING")
        evidence = ReviewEvidence(job.review_inputs)
        if evidence.turn != snapshot.review_evidence["turn"]:
            raise JournalConflict("GATEWAY_REVIEW_POSITION_CHANGED")
        evidence.namespace = snapshot.review_evidence["namespace"]
        evidence.sources = copy.deepcopy(snapshot.review_evidence["sources"])
        evidence.delivered = copy.deepcopy(snapshot.review_evidence["delivered"])
        job.review_evidence = evidence
    return job
