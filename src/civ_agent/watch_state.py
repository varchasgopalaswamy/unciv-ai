"""Read-only operator dashboard over published native game and SDK stores."""

import datetime as dt
import json
import sqlite3
import time
from dataclasses import dataclass, replace
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError

from civ_agent.container_support import ContainerError
from civ_agent.diplomacy_view import DiplomaticEvent
from civ_agent.game_config import ResolvedGameConfig
from civ_agent.monitoring import path_for, read_database, records
from civ_agent.operator_state import is_local
from civ_agent.operator_state import projection as native_projection
from civ_agent.operator_state import publication_basis as epoch_publication_basis
from civ_agent.operator_state import published_game as published_managed_epoch
from civ_agent.orchestration.contracts import Scope
from civ_agent.orchestration.job_contracts import HostUsage, JobTelemetry
from civ_agent.orchestration.native_experiments import native_experiment_players
from civ_agent.orchestration.native_watch import operator_tasks, sdk_lines, task_metadata
from civ_agent.player_paths import PlayerPaths as ManagedPlayerPaths
from civ_agent.protocol import JsonObject
from civ_agent.runtime.state import ControllerState
from civ_agent.strategy_view import StrategyReader, StrategyView
from civ_agent.tls import regular_file

MAX_ACTIVITY = 600
SPECIALIST_ROLES = ("economy", "military")


@dataclass(frozen=True)
class Activity:
    key: str
    civilization: str
    turn: int
    time: str
    label: str
    text: str
    verbose: bool = False
    participants: tuple[str, ...] = ()
    published_at: float = 0

    @property
    def sort_key(self):
        try:
            timestamp = dt.datetime.fromisoformat(self.time).timestamp()
        except ValueError:
            timestamp = self.published_at
        return self.turn, timestamp, self.key


@dataclass(frozen=True)
class SpecialistView:
    civilization: str
    role: str
    job: JsonObject | None = None
    activity: tuple[Activity, ...] = ()
    error: str | None = None
    schedule: JsonObject | None = None

    @property
    def status(self):
        if self.schedule:
            phase = self.schedule.get("phase")
            turn, due = self.schedule["gameTurn"], self.schedule.get("nextReviewTurn")
            if phase == "sleeping":
                return (
                    f"Sleeping · review in {max(0, due - turn)} turns"
                    if due is not None
                    else "Sleeping · waiting for first city"
                )
            if phase == "running" and (self.job or {}).get("gameTurn") != turn:
                return f"Starting T{turn}"
        if self.job is None:
            return "No job yet"
        phase = (self.job.get("telemetry") or {}).get("phase") or "Waiting for telemetry"
        return f"{phase.replace('_', ' ').capitalize()} T{self.job['gameTurn']}"


@dataclass(frozen=True)
class WatchSnapshot:
    run: JsonObject
    result: JsonObject
    turns: tuple[JsonObject, ...]
    activity: tuple[Activity, ...]
    errors: tuple[str, ...]
    updated_at: float | None
    diplomacy: tuple[DiplomaticEvent, ...] = ()
    negotiation: JsonObject | None = None
    diplomacy_available: bool = False
    strategies: tuple[StrategyView, ...] = ()
    specialists: tuple[SpecialistView, ...] = ()
    container_status: JsonObject | None = None
    diagnostics: tuple[JsonObject, ...] = ()

    @property
    def epoch(self):
        return self.run.get("epoch") or self.result.get("epoch")

    @property
    def players(self):
        return list(
            dict.fromkeys(
                [
                    *self.run.get("sessions", {}),
                    *(row["civilization"] for row in self.result.get("scores", [])),
                    *(record["turn"]["civilization"] for record in self.turns),
                ]
            )
        )

    def latest_turn(self, civilization=None):
        return next(
            (r for r in reversed(self.turns) if civilization is None or r["turn"]["civilization"] == civilization), None
        )

    def player_model(self, civilization):
        return (self.run.get("sessions", {}).get(civilization, {}).get("configuredSettings") or {}).get("model")


def _settings(selection):
    return {"provider": selection.provider, "model": selection.model, "reasoningEffort": selection.reasoning_effort}


def _sessions(request):
    return {
        seat.civilization: {"configuredSettings": _settings(seat.session.get_selection("gameplay"))}
        for seat in request.seats
    }


def request_counts(jobs):
    """Count recorded model requests by purpose; a held Main job is counted once."""
    totals = {"gameplay": 0, "review": 0, "communication": 0}
    players = {}
    seen = set()
    missing = 0
    for job in jobs:
        if job["id"] in seen:
            continue
        seen.add(job["id"])
        count = (job.get("telemetry") or {}).get("modelRequests")
        if type(count) is not int:
            missing += 1
            continue
        group = (
            "review"
            if job["purpose"] == "review"
            else "communication"
            if job["purpose"] in {"communication", "consultation"}
            else "gameplay"
        )
        totals[group] += count
        row = players.setdefault(job["civilization"], {"gameplay": 0, "review": 0, "communication": 0})
        row[group] += count
    return {**totals, "players": players, "missingJobs": missing}


def native_host_rows(root, epoch, match_id, names, *, cache=None):
    turns, jobs, diplomacy, negotiation, updated = [], [], [], None, None
    if match_id is None or not path_for(root).exists():
        return (), (), (), None, None
    with read_database(path_for(root)) as db:
        query = select(records).where(
            records.c.epoch == epoch, records.c.collection.in_(["turns", "jobs", "diplomacy", "state"])
        )
        if cache is None:
            rows = db.execute(query.order_by(records.c.revision)).mappings()
        else:
            if cache.get("epoch") != epoch:
                cache.clear()
                cache.update(epoch=epoch, revision=0, rows={})
            changed = db.execute(
                query.where(records.c.revision > cache["revision"]).order_by(records.c.revision.desc()).limit(512)
            ).mappings()
            for row in changed:
                cache["rows"][(row["collection"], row["key"])] = dict(row)
                cache["revision"] = max(cache["revision"], row["revision"])
            kept = sorted(cache["rows"].values(), key=lambda row: row["revision"])[-512:]
            cache["rows"] = {(row["collection"], row["key"]): row for row in kept}
            rows = kept
        for row in rows:
            if row["collection"] == "state" and row["key"] != "diplomacy":
                continue
            value = json.loads(row["payload"])
            if not isinstance(value, dict):
                raise ValueError("NATIVE_WATCH_HOST_ROW")
            updated = max(updated or 0, row["updated_ms"] / 1000)
            if row["collection"] in {"turns", "jobs"}:
                if value.get("schemaVersion") != 1 or value.get("matchId") != match_id:
                    raise ValueError("NATIVE_WATCH_HOST_SCOPE")
                body = value["turn"] if row["collection"] == "turns" else value["job"]
                if body["epoch"] != epoch or body["civilization"] not in names:
                    raise ValueError("NATIVE_WATCH_HOST_OWNER")
                if row["collection"] == "turns":
                    if type(body["handoff"]) is not int or type(body["gameTurn"]) is not int:
                        raise ValueError("NATIVE_WATCH_TURN")
                    usage = value.get("usage")
                    if usage is not None:
                        HostUsage.model_validate({key: usage[key] for key in HostUsage.model_fields})
                    turns.append(value)
                else:
                    job = {"id": value["jobId"], **body}
                    if job.get("usage") is not None:
                        if (
                            not isinstance(job["usage"], dict)
                            or not HostUsage.model_fields.keys() <= job["usage"].keys()
                        ):
                            raise ValueError("NATIVE_WATCH_USAGE_INCOMPLETE")
                        job["usage"] = HostUsage.model_validate(job["usage"]).model_dump(mode="json")
                    if job.get("telemetry") is not None:
                        job["telemetry"] = JobTelemetry.model_validate(job["telemetry"]).model_dump(mode="json")
                    jobs.append(job)
            elif row["collection"] == "diplomacy":
                event = DiplomaticEvent.from_record(value, row["revision"], row["updated_ms"] / 1000)
                if event.epoch != epoch:
                    raise ValueError("NATIVE_WATCH_DIPLOMACY_SCOPE")
                diplomacy.append(event)
            elif value.get("epoch") == epoch:
                negotiation = value.get("activeNegotiation")
    return (
        tuple(sorted(turns, key=lambda value: value["turn"]["handoff"])),
        tuple(jobs),
        tuple(diplomacy),
        negotiation,
        updated,
    )


class WatchReader:
    def __init__(self, root: Path, *, owner=False, include_history=True):
        self.root = root
        self.owner = owner
        self.include_history = include_history
        self.host_cache = {} if not include_history else None
        self.previous = WatchSnapshot({}, {}, (), (), (), None)
        self.strategy_reader = StrategyReader(root)

    def read(self, selected: tuple[str, int] | None = None) -> WatchSnapshot:
        if not self.owner:
            from pydantic import TypeAdapter

            from civ_agent.operator_views import ViewGeneration, passive_root

            try:
                pointer = self.root / "operator-views/current.json"
                if selected is not None and pointer.exists():
                    with passive_root(self.root) as captured:
                        self.previous = WatchReader(captured, owner=True).read(selected)
                    return self.previous
                if pointer.exists():
                    generation = ViewGeneration.model_validate_json(pointer.read_bytes())
                    current = published_managed_epoch(self.root)
                    if (generation.game, generation.epoch) != (current.authority.game, current.authority.epoch):
                        raise ValueError("OPERATOR_VIEW_EPOCH_CONFLICT")
                    path = self.root / "operator-views" / generation.generation / "view.json"
                    view = TypeAdapter(WatchSnapshot).validate_json(path.read_bytes())
                    stale = time.time() - generation.captured_at > 10
                    availability = self.root / "operator-views/availability.json"
                    if availability.exists():
                        error = json.loads(availability.read_bytes())
                        stale = stale or (
                            error.get("epoch") == generation.epoch and error.get("time", 0) > generation.captured_at
                        )
                    self.previous = replace(
                        view, errors=(*view.errors, "Operator view is stale") if stale else view.errors
                    )
                else:
                    with passive_root(self.root) as captured:
                        reader = WatchReader(captured, owner=True)
                        reader.previous = self.previous
                        self.previous = reader.read(selected)
                return self.previous
            except (OSError, ValueError, sqlite3.Error, SQLAlchemyError) as error:
                return replace(self.previous, errors=(type(error).__name__,))
        run: JsonObject
        try:
            local = is_local(self.root)
            expected = ("controller.json",) if local else ("game.json", "current-epoch.json")
            if not any((self.root / "native-runtime" / name).exists() for name in expected):
                self.previous = WatchSnapshot({}, {}, (), (), (), None)
                return self.previous
            request = published_managed_epoch(self.root)
            basis = epoch_publication_basis(self.root)
            if self.previous.epoch != request.authority.epoch:
                self.previous = WatchSnapshot(
                    {"epoch": request.authority.epoch, "sessions": _sessions(request)}, {}, (), (), (), None
                )
                self.strategy_reader = StrategyReader(self.root)
            projection = native_projection(self.root)
            status, host = projection.status, projection.result
            names = {seat.civilization for seat in request.seats}
            if host is not None:
                names.update(row.civilization for row in host.scores)
            turns, jobs, diplomacy, negotiation, updated = native_host_rows(
                self.root,
                request.authority.epoch,
                host.matchId if host else None,
                names,
                **({"cache": self.host_cache} if self.host_cache is not None else {}),
            )
            run = {
                "epoch": request.authority.epoch,
                "sessions": _sessions(request),
                "status": status.mode.lower() if status else "pending",
                "reason": status.reason if status else None,
            }
            if status is not None:
                run["runtimeStatus"] = status.model_dump(mode="json")
                run["readOnlyArchive"] = not local
                if projection.completion is not None:
                    run["status"] = "completed"
                run["inFlight"] = {
                    "civilization": status.active_civilization,
                    "gameTurn": status.turn,
                    "handoff": status.handoff,
                    "controller": status.active_controller,
                    "agentPhase": status.phase,
                }
                if isinstance(status, ControllerState):
                    run["inFlight"]["phaseStartedAt"] = status.phase_started_at.isoformat()
            entries, specialists, review_lines, errors = [], [], {}, []
            for index, seat in enumerate(request.seats):
                paths = ManagedPlayerPaths(self.root, index, seat.civilization)
                if paths.player != seat.session.player or seat.session.game != request.authority.game:
                    raise ValueError("NATIVE_WATCH_PLAYER_SCOPE")
                scope = Scope(
                    game=request.authority.game, player=paths.player, epoch=request.authority.epoch, activation="watch"
                )
                if self.include_history:
                    tasks, schedule = task_metadata(paths.coordinator / "notebook/memory.sqlite3", scope)
                    tasks = operator_tasks(paths.coordinator / "notebook/memory.sqlite3", scope, jobs)
                else:
                    tasks, schedule = {}, None
                for task in tasks.values():
                    if task.purpose == "review":
                        for job in jobs:
                            if job["id"] == task.host_job:
                                job["reviewTurn"] = task.turn
                wanted = {}
                for role in ("economy", "military", "review"):
                    latest_turn = max((task.turn for task in tasks.values() if task.purpose == role), default=-1)
                    wanted.update(
                        (key, task) for key, task in tasks.items() if task.purpose == role and task.turn == latest_turn
                    )
                wanted.update(
                    (key, task)
                    for key, task in tasks.items()
                    if task.purpose in {"gameplay", "communication", "consultation"}
                    and (
                        (seat.civilization, task.turn) == selected
                        if selected is not None
                        else task.turn >= (status.turn if status else 0) - 1
                    )
                )
                try:
                    lines = sdk_lines(paths, scope, wanted) if self.include_history else ()
                except (OSError, ValueError, TypeError, sqlite3.Error, SQLAlchemyError):
                    lines = ()
                    errors.append(f"Native SDK history unavailable for {seat.civilization}; retrying.")
                activity = tuple(
                    Activity(
                        line.key,
                        seat.civilization,
                        line.turn,
                        line.time,
                        line.label,
                        line.text,
                        line.verbose,
                        published_at=line.published_at,
                    )
                    for line in lines
                )
                purpose = {line.key: line.purpose for line in lines}
                review_lines[seat.civilization] = tuple(entry for entry in activity if purpose[entry.key] == "review")
                for role in SPECIALIST_ROLES:
                    matching = [
                        job for job in jobs if job["civilization"] == seat.civilization and job["purpose"] == role
                    ]
                    latest = max(
                        matching, key=lambda job: (job.get("gameTurn") or 0, job.get("claimedAtMs") or 0), default=None
                    )
                    if (
                        isinstance(status, ControllerState)
                        and status.active_attempt is not None
                        and status.active_civilization == seat.civilization
                    ):
                        current_role = next(
                            (r for r in reversed(status.active_attempt.roles) if r.purpose == role), None
                        )
                        if current_role is not None:
                            latest = {
                                **(latest or {}),
                                "gameTurn": status.turn,
                                "telemetry": {
                                    **((latest or {}).get("telemetry") or {}),
                                    "phase": current_role.phase,
                                    "model": current_role.model,
                                    "modelRequests": current_role.model_requests,
                                    "contextUsage": getattr(current_role, "context_usage", None),
                                    "phaseStartedAt": current_role.phase_started_at.isoformat(),
                                    "phaseElapsedMs": phase_elapsed_ms(current_role.phase_started_at.isoformat()),
                                },
                            }
                    if latest:
                        latest = {**latest, "configuredSettings": _settings(seat.session.get_selection(role))}
                    specialists.append(
                        SpecialistView(
                            seat.civilization,
                            role,
                            latest,
                            tuple(entry for entry in activity if purpose[entry.key] == role),
                            schedule=schedule if role == "economy" else None,
                        )
                    )
                for entry in activity:
                    if purpose[entry.key] in {"gameplay", "communication", "consultation"} and (
                        selected == (entry.civilization, entry.turn)
                        if selected is not None
                        else entry.turn >= (status.turn if status else 0) - 1
                    ):
                        entries.append(entry)
                flight = run.get("inFlight") or {}
                if flight.get("civilization") == seat.civilization:
                    current = [
                        task
                        for task in tasks.values()
                        if task.purpose == "gameplay" and task.turn == flight["gameTurn"]
                    ]
                    latest = current[-1] if current else None
                    if latest is not None:
                        flight.update(activationId=latest.scope.activation, effectiveSettings=latest.settings)
                        admitted = next((job for job in jobs if job["id"] == latest.host_job), None)
                        if not local and admitted and admitted.get("telemetry"):
                            flight["agentPhase"] = admitted["telemetry"]["phase"]
                    for failure in getattr(status, "model_failures", ()) if status is not None else ():
                        if (failure.scope.game, failure.scope.epoch) != (
                            scope.game,
                            scope.epoch,
                        ) or failure.scope.player not in {s.session.player for s in request.seats}:
                            raise ValueError("NATIVE_WATCH_MODEL_FAILURE_SCOPE")
                        if failure.scope.player == paths.player:
                            flight["modelStatus"] = {
                                "state": "retrying" if failure.retry_mode == "automatic" else "failed",
                                "message": f"{failure.category.replace('_', ' ')} · {failure.retry_mode} recovery",
                            }
            for event in diplomacy:
                if (
                    event.visible
                    and event.turn is not None
                    and (selected is None or selected[0] in event.participants and selected[1] == event.turn)
                ):
                    entries.append(
                        Activity(
                            f"diplomacy:{event.order:012d}",
                            event.author,
                            event.turn,
                            "",
                            "Diplomacy",
                            event.headline + (" Reason: " + event.body["failure"] if event.body.get("failure") else ""),
                            participants=event.participants,
                            published_at=event.published_at,
                        )
                    )
            run["requestCounts"] = request_counts(jobs)
            if not self.include_history:
                run["requestCountsScope"] = "bounded_recent_dashboard_jobs"
            if isinstance(status, ControllerState) and status.mode == "RUNNING" and status.active_attempt is not None:
                run["inFlight"]["activeRoles"] = [r.model_dump(mode="json") for r in status.active_attempt.roles]
            entries.sort(key=lambda entry: entry.sort_key)
            strategies = self.strategy_reader.read(
                [seat.civilization for seat in request.seats],
                request.authority.epoch,
                [job for job in jobs if job["purpose"] == "review"],
                histories=review_lines,
            )
            diagnostics = ()
            if (self.root / "resolved.json").exists():
                config = ResolvedGameConfig.model_validate_json(
                    regular_file(self.root / "resolved.json", private=True).read_bytes()
                )
                manifest = (
                    json.loads(regular_file(self.root / "container.json", private=True).read_bytes())
                    if (self.root / "container.json").exists()
                    else {"id": request.authority.game}
                )
                if not local:
                    diagnostics = tuple(native_experiment_players(self.root, config, manifest, now=time.time()))
            if epoch_publication_basis(self.root) != basis:
                raise ValueError("NATIVE_WATCH_PUBLICATION_CHANGED")
            container = (
                json.loads(regular_file(self.root / "container-status.json", private=True).read_bytes())
                if (self.root / "container-status.json").exists()
                else None
            )
            self.previous = WatchSnapshot(
                run,
                host.model_dump(mode="json") if host else {},
                turns,
                tuple(entries[-MAX_ACTIVITY:]),
                tuple(errors),
                max(
                    [
                        value
                        for value in [
                            updated,
                            projection.result_updated_at,
                            (self.root / "native-runtime/status.json").stat().st_mtime if status is not None else None,
                        ]
                        if value is not None
                    ],
                    default=None,
                ),
                diplomacy,
                negotiation,
                host is not None,
                strategies,
                tuple(specialists),
                container,
                diagnostics,
            )
            return self.previous
        except (OSError, ValueError, TypeError, KeyError, sqlite3.Error, SQLAlchemyError, ContainerError) as error:
            # A bad authority cannot authorize displaying a prior player's or
            # discarded epoch's history. Same-epoch storage locks retain metrics.
            try:
                current = published_managed_epoch(self.root).authority.epoch
            except (OSError, ValueError):
                current = None
            prior = self.previous if self.previous.epoch == current else WatchSnapshot({}, {}, (), (), (), None)
            return replace(prior, errors=(f"Native records unavailable ({type(error).__name__}); retrying.",))


def event_time(value: str) -> str:
    try:
        return dt.datetime.fromisoformat(value).astimezone().strftime("%H:%M:%S")
    except ValueError:
        return "--:--:--"


def phase_elapsed_ms(started: str | None, *, now: float | None = None) -> int | None:
    """Elapsed time from an explicit phase transition, independent of publication age."""
    if not started:
        return None
    try:
        stamp = dt.datetime.fromisoformat(started)
        if stamp.tzinfo is None:
            return None
        return max(0, int(((time.time() if now is None else now) - stamp.timestamp()) * 1000))
    except (ValueError, OverflowError):
        return None
