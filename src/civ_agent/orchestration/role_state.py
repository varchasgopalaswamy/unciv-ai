"""Portable domain coordination; schedules/connections/tasks are recreated locally."""

import copy
import time
from typing import Annotated, Literal

from pydantic import Field, JsonValue

from civ_agent.economy_schedule import EconomySchedule
from civ_agent.military_schedule import MilitarySchedule
from civ_agent.orchestration.contracts import Digest, canonical
from civ_agent.orchestration.store import JournalConflict
from civ_agent.protocol import JsonObject
from civ_agent.schemas import BoundaryModel, Identifier, NonnegativeInt

type TurnRole = Literal["gameplay", "economy", "military"]


class RoleData(BoundaryModel):
    ready: dict[TurnRole, bool]
    requests: list[JsonObject]
    overrides: dict[str, str]
    deferral: JsonObject
    committed: bool
    committing: bool
    messages: dict[TurnRole, str]
    native_call_id: Identifier | None
    war_state: JsonObject
    military_active: bool
    military_requested: bool
    military_status: JsonObject
    military_observation: JsonObject | None
    military_observation_state: Digest | None = None
    economy_status: JsonObject
    economy_requested: bool
    economy_observation: JsonObject | None
    economy_completions: NonnegativeInt
    economy_wait_record: JsonObject | None
    main_attention: NonnegativeInt
    intent_handoffs: JsonObject = Field(default_factory=dict)


class RoleCheckpoint(BoundaryModel):
    version: Literal[1]
    key: tuple[Identifier, NonnegativeInt, Identifier]
    data: RoleData
    sets: dict[str, list[str] | None]
    tuples: dict[str, list[JsonValue] | None]
    events: dict[str, bool]
    resumes: dict[TurnRole, bool]
    economySchedule: bool
    militarySchedule: bool
    waitDeadline: Annotated[float, Field(ge=0, allow_inf_nan=False)] | None


JSON_FIELDS = frozenset(
    {
        "ready",
        "requests",
        "overrides",
        "deferral",
        "committed",
        "committing",
        "messages",
        "native_call_id",
        "war_state",
        "military_active",
        "military_requested",
        "military_status",
        "military_observation",
        "military_observation_state",
        "economy_status",
        "economy_requested",
        "economy_observation",
        "economy_completions",
        "economy_wait_record",
        "main_attention",
        "intent_handoffs",
    }
)
SET_FIELDS = frozenset({"known_cities", "known_units"})
TUPLE_FIELDS = frozenset({"military_revision", "economy_revision"})
EVENT_FIELDS = frozenset({"changed", "economy_start", "economy_changed"})
LOCAL_FIELDS = frozenset(
    {
        "service",
        "record",
        "jobs",
        "key",
        "lock",
        "wars",
        "start_military",
        "economy_schedule",
        "military_schedule",
        "advance_task",
        "military_tasks",
        "economy_wait_deadline",
        "resumes",
    }
)


def capture_roles(roles):
    if set(vars(roles)) != JSON_FIELDS | SET_FIELDS | TUPLE_FIELDS | EVENT_FIELDS | LOCAL_FIELDS:
        raise JournalConflict("ROLE_STATE_FIELDS_CHANGED")
    if roles.advance_task is not None or roles.military_tasks:
        raise JournalConflict("ROLE_LEGACY_TASKS_ACTIVE")
    data = {
        "version": 1,
        "key": list(roles.key),
        "data": {name: copy.deepcopy(getattr(roles, name)) for name in JSON_FIELDS},
        "sets": {
            name: sorted(getattr(roles, name)) if getattr(roles, name) is not None else None for name in SET_FIELDS
        },
        "tuples": {
            name: list(getattr(roles, name)) if getattr(roles, name) is not None else None for name in TUPLE_FIELDS
        },
        "events": {name: getattr(roles, name).is_set() for name in EVENT_FIELDS},
        "resumes": {role: event.is_set() for role, event in roles.resumes.items()},
        "economySchedule": roles.economy_schedule is not None,
        "militarySchedule": roles.military_schedule is not None,
        "waitDeadline": time.time() + max(0.0, roles.economy_wait_deadline - time.monotonic())
        if roles.economy_wait_record is not None
        else None,
    }
    # Canonical encoding rejects live objects before Pydantic can coerce them.
    return RoleCheckpoint.model_validate_json(canonical(data)).model_dump(mode="json")


def restore_roles(roles, data):
    if data["version"] != 1 or data["key"] != list(roles.key):
        raise JournalConflict("ROLE_STATE_IDENTITY_CONFLICT")
    # Older v1 snapshots lack these additive fields. An absent comparison
    # digest conservatively requires a fresh view on the next attention event.
    defaults = {"intent_handoffs": {}, "military_observation_state": None}
    missing = JSON_FIELDS - set(data["data"])
    if missing and missing <= defaults.keys():
        data = copy.deepcopy(data)
        for name in missing:
            data["data"][name] = defaults[name]
    for group, expected in (
        ("data", JSON_FIELDS),
        ("sets", SET_FIELDS),
        ("tuples", TUPLE_FIELDS),
        ("events", EVENT_FIELDS),
    ):
        if set(data[group]) != expected:
            raise JournalConflict("ROLE_STATE_INCOMPLETE")
    if set(data["data"]["ready"]) != set(roles.ready) or set(data["resumes"]) != set(roles.resumes):
        raise JournalConflict("ROLE_STATE_INCOMPLETE")
    data = RoleCheckpoint.model_validate_json(canonical(data)).model_dump(mode="json")
    for name, value in data["data"].items():
        setattr(roles, name, copy.deepcopy(value))
    for name, value in data["sets"].items():
        setattr(roles, name, set(value) if value is not None else None)
    for name, value in data["tuples"].items():
        setattr(roles, name, tuple(value) if value is not None else None)
    for name, value in data["events"].items():
        if value:
            getattr(roles, name).set()
    for role, value in data["resumes"].items():
        if value:
            roles.resumes[role].set()
    service = roles.service
    if data["economySchedule"]:
        roles.economy_schedule = EconomySchedule(service.memory, service.economy_interval)
    if data["militarySchedule"]:
        roles.military_schedule = MilitarySchedule(service.memory, service.military_interval)
    if data["waitDeadline"] is not None:
        roles.economy_wait_deadline = time.monotonic() + max(0.0, data["waitDeadline"] - time.time())
