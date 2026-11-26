"""Private operator reads of native task metadata and SDK-owned histories."""

import asyncio
import json
import tempfile
from collections import Counter
from contextlib import contextmanager
from dataclasses import dataclass, replace
from pathlib import Path

from pydantic_ai.messages import (
    RetryFeedbackPart,
    RetryPromptPart,
    SystemPromptPart,
    TextPart,
    ThinkingPart,
    ToolCallPart,
    ToolReturnPart,
    UserPromptPart,
)
from pydantic_ai_harness.step_persistence import SqliteStepStore
from sqlalchemy import func, select

from civ_agent import memory_schema as tables
from civ_agent.monitoring import read_database
from civ_agent.orchestration.contracts import PeacePhase, PeaceRequest, ReviewRequest, RoleRequest, Scope, TurnTicket
from civ_agent.orchestration.history_exports import (
    copy_native_database,
    native_run_scope,
    verify_native_store_owner,
)
from civ_agent.orchestration.job_contracts import NativeJob, NativeReviewBinding, host_job_id
from civ_agent.player_paths import PlayerPaths as ManagedPlayerPaths
from civ_agent.protocol import JsonObject
from civ_agent.runtime.review_cycles import ReviewCycle
from civ_agent.terminal_output import visible_text
from civ_agent.tls import private_directory

MAX_LINES = 600
MAX_TEXT = 16000


@dataclass(frozen=True)
class WatchTask:
    scope: Scope
    purpose: str
    turn: int
    host_job: str
    settings: JsonObject
    agent: str | None = None
    job: str | None = None
    source_turn: int | None = None
    target_turn: int | None = None
    review_cycle_id: str | None = None

    @property
    def gateway_job(self):
        return self.job or self.scope.activation

    @property
    def agent_name(self):
        return self.agent or ("strategic_review_v1" if self.purpose == "review" else "player_role_v1")


@dataclass(frozen=True)
class SDKLine:
    key: str
    activation: str
    purpose: str
    turn: int
    time: str
    label: str
    text: str
    verbose: bool
    published_at: float = 0


def verify_native_notebook(db, scope: Scope) -> None:
    values = {
        key: json.loads(value)
        for key, value in db.execute(
            select(tables.meta.c.key, tables.meta.c.value).where(
                tables.meta.c.key.in_(["format", "nativeGatewayOwner", "epoch"])
            )
        )
    }
    if values != {
        "format": tables.FORMAT_VERSION,
        "nativeGatewayOwner": {"version": 1, "game": scope.game, "player": scope.player},
        "epoch": scope.epoch,
    }:
        raise ValueError("NATIVE_WATCH_NOTEBOOK_SCOPE")


def task_metadata(path: Path, scope: Scope) -> tuple[dict[str, WatchTask], JsonObject | None]:
    """Select only request references and schedule summaries, never bootstrap state."""
    if not path.exists():
        return {}, None
    tasks, schedule, position = {}, None, (-1, -1)

    def add(request, purpose, turn, native):
        if (request.scope.game, request.scope.player, request.scope.epoch) != (scope.game, scope.player, scope.epoch):
            return
        task = WatchTask(
            request.scope,
            purpose,
            turn,
            host_job_id(native),
            {
                "provider": request.selection.provider,
                "model": request.selection.model,
                "reasoningEffort": request.selection.reasoning_effort,
            },
            job=request.job if isinstance(request, RoleRequest) else request.scope.activation,
            source_turn=getattr(request, "source_turn", None),
            target_turn=getattr(request, "target_turn", None),
            review_cycle_id=getattr(request, "review_cycle_id", None),
        )
        prior = tasks.setdefault(request.scope.activation, task)
        if prior != task:
            raise ValueError("NATIVE_WATCH_TASK_CONFLICT")

    with read_database(path) as db:
        verify_native_notebook(db, scope)
        gates = {
            key: raw
            for key, raw in db.execute(
                select(tables.meta.c.key, func.json_extract(tables.meta.c.value, "$.request")).where(
                    tables.meta.c.key.like("nativeReviewGate:%")
                )
            )
        }
        rows = db.execute(
            select(
                func.json_extract(tables.meta.c.value, "$.ticket"),
                func.json_extract(tables.meta.c.value, "$.requests"),
                func.json_extract(tables.meta.c.value, "$.roles.economySchedule"),
            ).where(tables.meta.c.key.like("nativeTurn:%"))
        )
        for ticket_json, requests_json, summary in rows:
            ticket = TurnTicket.model_validate_json(ticket_json)
            if (ticket.scope.game, ticket.scope.player, ticket.scope.epoch) != (scope.game, scope.player, scope.epoch):
                continue
            main = NativeJob(scope=ticket.scope, job=ticket.scope.activation, purpose="gameplay")
            requests = json.loads(requests_json or "{}")
            if not isinstance(requests, dict):
                raise ValueError("NATIVE_WATCH_REQUESTS")
            for raw in requests.values():
                request = RoleRequest.model_validate(raw)
                native = (
                    main
                    if request.purpose == "gameplay"
                    else NativeJob(scope=request.scope, job=request.job, purpose=request.purpose, parent=main)
                )
                add(request, request.purpose, ticket.turn, native)
            if (ticket.turn, ticket.handoff) >= position:
                position = ticket.turn, ticket.handoff
                schedule = json.loads(summary) if summary is not None else None
                if schedule is not None:
                    schedule = {key: schedule[key] for key in ("phase", "nextReviewTurn", "reasons") if key in schedule}
                    schedule.update(epoch=scope.epoch, gameTurn=ticket.turn)
            gate = gates.get("nativeReviewGate:" + ticket.scope.activation)
            if gate:
                review = ReviewRequest.model_validate_json(gate)
                add(
                    review,
                    "review",
                    max(0, ticket.turn - 1),
                    NativeJob(scope=review.scope, job=review.scope.activation, purpose="review", parent=main),
                )
        for raw in db.scalars(select(tables.meta.c.value).where(tables.meta.c.key.like("nativeReviewCycle:%"))):
            if raw == "null":
                continue
            cycle = ReviewCycle.model_validate_json(raw)
            request = cycle.request
            if request is None:
                continue
            if (request.scope.game, request.scope.player, request.scope.epoch) != (
                cycle.scope.game,
                cycle.scope.player,
                cycle.scope.epoch,
            ) or (request.review_cycle_id, request.source_turn, request.target_turn) != (
                cycle.id,
                cycle.source_turn,
                cycle.target_turn,
            ):
                raise ValueError("NATIVE_WATCH_REVIEW_CYCLE_BINDING")
            add(
                request,
                "review",
                cycle.source_turn,
                NativeJob(
                    scope=request.scope,
                    job=request.scope.activation,
                    purpose="review",
                    review=NativeReviewBinding(
                        cycle_id=cycle.id, source_turn=cycle.source_turn, target_turn=cycle.target_turn
                    ),
                ),
            )
        jobs = {key: turn for key, turn in db.execute(select(tables.jobs.c.id, tables.jobs.c.game_turn))}
        for raw in db.execute(
            select(func.json_extract(tables.meta.c.value, "$.request")).where(
                tables.meta.c.key.like("nativeCommunicationJob:%")
            )
        ).scalars():
            request = RoleRequest.model_validate_json(raw)
            turn = jobs.get(request.job)
            if type(turn) is not int:
                raise ValueError("NATIVE_WATCH_COMMUNICATION_TURN")
            add(
                request,
                "communication",
                turn,
                NativeJob(scope=request.scope, job=request.job, purpose="communication"),
            )
    return tasks, schedule


def operator_tasks(path: Path, scope: Scope, jobs) -> dict[str, WatchTask]:
    """Include native consultation phases without opening their frozen inputs."""
    tasks, _ = task_metadata(path, scope)
    if not path.exists():
        return tasks
    host = {job["id"]: job for job in jobs}
    with read_database(path) as db:
        verify_native_notebook(db, scope)
        rows = db.scalars(
            select(func.json_extract(tables.meta.c.value, "$.request")).where(tables.meta.c.key.like("nativePeace:%"))
        )
        for raw in rows:
            request = PeaceRequest.model_validate_json(raw)
            if (request.scope.game, request.scope.player, request.scope.epoch) != (
                scope.game,
                scope.player,
                scope.epoch,
            ):
                continue
            for name, agent in (("military", "peace_military_v1"), ("gameplay", "peace_main_v1")):
                phase = PeacePhase(request=request, phase=name)
                identifier = host_job_id(
                    NativeJob(scope=phase.scope, job=phase.scope.activation, purpose="consultation")
                )
                job = host.get(identifier)
                if job is None:
                    continue  # No invented native turn or per-phase admission.
                if type(job.get("gameTurn")) is not int or job.get("purpose") != "consultation":
                    raise ValueError("NATIVE_OPERATOR_PHASE_JOB")
                tasks[phase.scope.activation] = WatchTask(
                    request.scope,
                    "consultation",
                    job["gameTurn"],
                    identifier,
                    {
                        "provider": phase.selection.provider,
                        "model": phase.selection.model,
                        "reasoningEffort": phase.selection.reasoning_effort,
                    },
                    agent=agent,
                    job=phase.scope.activation,
                )
    return tasks


def _part(part) -> tuple[str, str, bool] | None:
    if isinstance(part, TextPart):
        return "Assistant", part.content, False
    if isinstance(part, ThinkingPart):
        return "Reasoning", part.content, True
    if isinstance(part, ToolCallPart):
        return "Tool call", part.tool_name + "\n" + part.args_as_json_str(), True
    if isinstance(part, ToolReturnPart):
        if part.outcome == "retried":
            return "Correction", part.tool_name + "\n" + part.model_response_str(), False
        return "Tool result", part.tool_name + "\n" + part.model_response_str(), True
    if isinstance(part, (RetryPromptPart, RetryFeedbackPart)):
        return "Correction", part.model_response(), False
    if isinstance(part, SystemPromptPart):
        return "Developer instructions", part.content, True
    if isinstance(part, UserPromptPart):
        # Images/files remain SDK-owned typed parts; never stringify their data.
        content = (
            part.content
            if isinstance(part.content, str)
            else "\n".join(item for item in part.content if isinstance(item, str))
        )
        return "User input", content, True
    return None


def _command_summary(part) -> tuple[str, str] | None:
    if isinstance(part, ToolCallPart) and part.tool_name == "execute_commands":
        try:
            commands = part.args_as_dict().get("commands", [])
        except (ValueError, AssertionError):
            return "Command request", "Arguments await native SDK validation."
        if (
            not isinstance(commands, list)
            or not commands
            or not all(isinstance(command, dict) and isinstance(command.get("kind"), str) for command in commands)
        ):
            return "Command request", "Arguments await native SDK validation."
        return "Command request", ", ".join(command["kind"] for command in commands)
    if isinstance(part, ToolReturnPart) and part.tool_name == "execute_commands":
        if part.outcome != "success":
            return None  # Public outcome feedback is rendered by _part.
        result = part.content
        if isinstance(result, dict) and isinstance(result.get("receipts"), list):
            counts = Counter(receipt.get("status") for receipt in result["receipts"] if isinstance(receipt, dict))
            if counts.keys() <= {"confirmed", "rejected", "unexecuted"}:
                return "Command receipts", " · ".join(
                    f"{counts[key]} {key}" for key in ("confirmed", "rejected", "unexecuted") if counts[key]
                )
        return "Command receipts", "Native command response recorded; inspect verbose receipt."
    return None


@contextmanager
def native_step_store(paths: ManagedPlayerPaths, scope: Scope):
    """Public SDK reader on coherent private backups; originals stay read-only."""
    source = paths.store / "agent-steps.sqlite3"
    if not source.exists() and not source.is_symlink():
        yield None
        return
    with tempfile.TemporaryDirectory(prefix="native-operator-") as directory:
        scratch = private_directory(Path(directory))
        copy_native_database(paths.store / "workflow.sqlite3", scratch / "workflow.sqlite3")
        verify_native_store_owner(scratch / "workflow.sqlite3", scope.game, scope.player, scope.epoch)
        copy_native_database(source, scratch / "steps.sqlite3")
        yield SqliteStepStore(database=scratch / "steps.sqlite3")


def _metadata_settings(run, task):
    settings = dict(task.settings)
    for key in ("model", "provider", "reasoningEffort"):
        if key in run.metadata:
            value = run.metadata[key]
            if not isinstance(value, str) or not 1 <= len(value) <= 256:
                raise ValueError("NATIVE_SDK_MODEL_METADATA")
            if key == "provider" and value not in {"openai", "openrouter", "google-cloud"}:
                raise ValueError("NATIVE_SDK_MODEL_METADATA")
            if key == "reasoningEffort" and value not in {"none", "minimal", "low", "medium", "high", "xhigh", "max"}:
                raise ValueError("NATIVE_SDK_MODEL_METADATA")
            if settings.get(key) not in {None, "unavailable", value}:
                raise ValueError("NATIVE_SDK_MODEL_BINDING")
            settings[key] = value
    return settings


async def recorded_sdk_runs(store, scope: Scope, tasks: dict[str, WatchTask]):
    """Bind recorded runs to trusted task scopes, independent of conversation naming.

    Old runs may have only the canonical owning scope. New metadata strengthens
    that binding and identifies retained review attempts after a fresh restart.
    """
    selected = []
    for run in await store.list_runs():
        owner = native_run_scope(run, scope.game, scope.player)
        if owner.epoch != scope.epoch:
            continue
        matches = [task for task in tasks.values() if task.scope == owner and task.agent_name == run.agent_name]
        if not matches and run.agent_name == "strategic_review_v1" and run.metadata.get("review_cycle_id"):
            candidates = [task for task in tasks.values() if task.review_cycle_id == run.metadata["review_cycle_id"]]
            for task in candidates:
                assert (
                    task.review_cycle_id is not None and task.source_turn is not None and task.target_turn is not None
                )
                if (run.metadata.get("source_turn"), run.metadata.get("target_turn")) != (
                    str(task.source_turn),
                    str(task.target_turn),
                ):
                    raise ValueError("NATIVE_SDK_REVIEW_TURN_BINDING")
                native = NativeJob(
                    scope=owner,
                    job=owner.activation,
                    purpose="review",
                    review=NativeReviewBinding(
                        cycle_id=task.review_cycle_id, source_turn=task.source_turn, target_turn=task.target_turn
                    ),
                )
                if run.metadata.get("native_job_id") != host_job_id(native):
                    raise ValueError("NATIVE_SDK_REVIEW_JOB_BINDING")
                matches.append(
                    replace(
                        task,
                        scope=owner,
                        host_job=host_job_id(native),
                        job=owner.activation,
                        settings={"model": "unavailable", "provider": "unavailable", "reasoningEffort": "unavailable"},
                    )
                )
        if not matches:
            if any(task.scope == owner for task in tasks.values()):
                raise ValueError("NATIVE_SDK_AGENT_BINDING")
            continue
        if len(matches) != 1:
            raise ValueError("NATIVE_SDK_TASK_BINDING")
        task = matches[0]
        purpose = {"peace_military_v1": "military", "peace_main_v1": "gameplay"}.get(run.agent_name, task.purpose)
        expected = {
            "agent_name": task.agent_name,
            "purpose": purpose,
            "job": task.gateway_job,
            "native_job_id": task.host_job,
        }
        for key, value in expected.items():
            if key in run.metadata and run.metadata[key] != value:
                raise ValueError("NATIVE_SDK_RUN_METADATA_BINDING")
        for key in ("source_turn", "target_turn", "review_cycle_id"):
            value = getattr(task, key)
            if key in run.metadata and (value is None or run.metadata[key] != str(value)):
                raise ValueError("NATIVE_SDK_REVIEW_METADATA_BINDING")
        selected.append((run, replace(task, settings=_metadata_settings(run, task))))
    return sorted(selected, key=lambda item: (item[0].started_at, item[0].run_id))


async def _sdk_lines(paths: ManagedPlayerPaths, scope: Scope, tasks: dict[str, WatchTask]) -> tuple[SDKLine, ...]:
    with native_step_store(paths, scope) as store:
        if store is None:
            return ()
        entries = []
        for run, task in await recorded_sdk_runs(store, scope, tasks):
            owner = native_run_scope(run, scope.game, scope.player)
            snapshot = await store.latest_snapshot(run_id=run.run_id, include_interrupted=True)
            if snapshot is not None:
                if (snapshot.run_id, snapshot.agent_name, snapshot.conversation_id) != (
                    run.run_id,
                    run.agent_name,
                    run.conversation_id,
                ):
                    raise ValueError("NATIVE_WATCH_SNAPSHOT_SCOPE")
                for index, message in enumerate(snapshot.messages):
                    if message.run_id is not None and message.run_id != run.run_id:
                        continue  # SDK history inherited from an earlier run.
                    stamp = message.timestamp.isoformat() if message.timestamp is not None else ""
                    for number, part in enumerate(message.parts):
                        summary = _command_summary(part)
                        if summary is not None:
                            entries.append(
                                SDKLine(
                                    f"{run.run_id}:summary:{index}:{number}",
                                    owner.activation,
                                    task.purpose,
                                    task.turn,
                                    stamp,
                                    summary[0],
                                    visible_text(summary[1]),
                                    False,
                                    snapshot.timestamp.timestamp(),
                                )
                            )
                        value = _part(part)
                        if value is not None:
                            label, text, verbose = value
                            entries.append(
                                SDKLine(
                                    f"{run.run_id}:message:{index}:{number}",
                                    owner.activation,
                                    task.purpose,
                                    task.turn,
                                    stamp,
                                    label,
                                    visible_text(text)[:MAX_TEXT],
                                    verbose,
                                    snapshot.timestamp.timestamp(),
                                )
                            )
            for index, event in enumerate(await store.list_events(run_id=run.run_id)):
                if (
                    event.run_id != run.run_id
                    or event.agent_name not in {None, run.agent_name}
                    or event.conversation_id not in {None, run.conversation_id}
                ):
                    raise ValueError("NATIVE_WATCH_EVENT_SCOPE")
                # Native events provide live progress before the next snapshot.
                # Raw exception strings and metadata are not operator notices.
                entries.append(
                    SDKLine(
                        f"{run.run_id}:event:{index}",
                        owner.activation,
                        task.purpose,
                        task.turn,
                        event.timestamp.isoformat(),
                        "SDK",
                        event.kind.replace("_", " "),
                        True,
                    )
                )
        return tuple(entries[-MAX_LINES:])


def sdk_lines(paths: ManagedPlayerPaths, scope: Scope, tasks: dict[str, WatchTask]) -> tuple[SDKLine, ...]:
    return asyncio.run(_sdk_lines(paths, scope, tasks))
