"""Read-only acceptance audit of the scripted native Memory/Planning exercise."""

import json
import re
import sqlite3
from collections import Counter
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory

from pydantic import TypeAdapter
from pydantic_ai.messages import ModelMessage, ModelRequest, ModelResponse, ToolCallPart, ToolReturnPart, UserPromptPart
from pydantic_ai_harness.memory import MemorySearchResponse, MemoryWriteResult, SqliteMemoryStore
from pydantic_ai_harness.planning import SqlitePlanStore, TaskStatus
from pydantic_ai_harness.step_persistence import ToolEffectRecord

from civ_agent.memory_records import MEMORY_TYPES
from civ_agent.orchestration.authored_memory import (
    MAX_DOCUMENT_CHARS,
    MAX_DOCUMENTS,
    AuthoredMetadata,
    authored_prefix,
    contribution_prefix,
    decode_document,
)
from civ_agent.orchestration.contracts import GameRunRequest, GameRunResult, RoleResult, canonical
from civ_agent.orchestration.gateway_execution import operation_identity
from civ_agent.orchestration.player_plans import PLAN_TOOLS, plan_session
from civ_agent.orchestration.scripted_model import STORE_CITY_REASON, STORE_NOTE_PREFIX, STORE_TASK_PREFIX
from civ_agent.orchestration.store import JournalConflict, WorkflowStore
from civ_agent.orchestration.transcripts import backend, read
from civ_agent.protocol import JsonObject

_MEMORY_TOOLS = {"read_memory", "write_memory", "search_memory", "delete_memory"}
_STORE_TOOLS = _MEMORY_TOOLS | set(PLAN_TOOLS)
_WRITE_RESULT = TypeAdapter(MemoryWriteResult)
_SEARCH_RESULT = TypeAdapter(MemorySearchResponse)


def _require(condition: object, reason: str):
    if not condition:
        raise JournalConflict("NATIVE_STORES_" + reason)


def _copy_database(source: Path, destination: Path):
    """Snapshot completed artifacts without creating empty WAL sidecars."""
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    # This verifier accepts completed artifacts. Copy their frozen main/WAL
    # bytes before allowing SQLite to create coordination files in scratch.
    with TemporaryDirectory(prefix="native-verification-source-") as temporary:
        scratch = Path(temporary) / source.name
        before = source.read_bytes()
        scratch.write_bytes(before)
        wal = source.with_name(source.name + "-wal")
        saved_wal = wal.read_bytes() if wal.exists() else None
        if saved_wal is not None:
            scratch.with_name(scratch.name + "-wal").write_bytes(saved_wal)
        _require(
            source.read_bytes() == before and (wal.read_bytes() if wal.exists() else None) == saved_wal,
            "SOURCE_CHANGED",
        )
        with closing(sqlite3.connect(scratch.resolve().as_uri() + "?mode=ro", uri=True)) as original:
            with closing(sqlite3.connect(destination)) as copied:
                original.backup(copied)
                _require(copied.execute("PRAGMA integrity_check").fetchone() == ("ok",), "DATABASE_INTEGRITY")
    destination.chmod(0o600)


@dataclass
class _Tool:
    call: ToolCallPart
    result: ToolReturnPart
    issued: int
    returned: int
    effect: ToolEffectRecord


@dataclass
class _Job:
    role: RoleResult
    startup: JsonObject
    tools: list[_Tool]
    task_id: str
    task: str
    plan_write: _Tool
    memory_file: str | None
    memory_write: _Tool | None
    delivered: str | None


async def _tools(store: WorkflowStore, role: RoleResult, messages: list[ModelMessage]) -> list[_Tool]:
    steps = backend(store)
    runs = await steps.list_runs(conversation_id=role.scope.identity("conversation", "role"))
    _require(runs, "RUN_REQUIRED")
    effects: dict[str, ToolEffectRecord] = {}
    for run in runs:
        _require(
            run.agent_name == "player_role_v1"
            and run.metadata.get("scope") == canonical(role.scope.model_dump(mode="json")),
            "RUN_SCOPE",
        )
        offset = int(run.metadata["modelStepOffset"])
        events = await steps.list_events(run_id=run.run_id)
        for call_id in {event.tool_call_id for event in events if event.kind == "tool_call_started"}:
            _require(call_id is not None, "TOOL_ID")
            assert call_id is not None
            effect = await steps.get_tool_effect(run_id=run.run_id, tool_call_id=call_id)
            _require(effect is not None, "TOOL_EFFECT")
            assert effect is not None
            if effect.tool_name not in _STORE_TOOLS | {"finish_role"}:
                continue
            expected = {
                operation_identity(role.job, role.scope.epoch, offset + event.step_index, call_id)
                for event in events
                if event.kind == "tool_call_started" and event.tool_call_id == call_id
            }
            _require(
                effect.status == "completed" and expected == {effect.idempotency_key} and call_id not in effects,
                "TOOL_EFFECT_BINDING",
            )
            effects[call_id] = effect
    pending: dict[str, tuple[ToolCallPart, int]] = {}
    delivered = []
    counts = Counter(
        part.tool_call_id
        for message in messages
        if isinstance(message, ModelResponse)
        for part in message.parts
        if isinstance(part, ToolCallPart)
    )
    returns = Counter(
        part.tool_call_id
        for message in messages
        if isinstance(message, ModelRequest)
        for part in message.parts
        if isinstance(part, ToolReturnPart)
    )
    for index, message in enumerate(messages):
        if isinstance(message, ModelResponse):
            for part in message.parts:
                if isinstance(part, ToolCallPart) and part.tool_name in _STORE_TOOLS | {"finish_role"}:
                    _require(counts[part.tool_call_id] == returns[part.tool_call_id] == 1, "DUPLICATE_TOOL_ID")
                    pending[part.tool_call_id] = part, index
        elif isinstance(message, ModelRequest):
            for part in message.parts:
                if not isinstance(part, ToolReturnPart):
                    continue
                if part.tool_call_id not in pending:
                    _require(part.tool_name not in _STORE_TOOLS, "UNPAIRED_TOOL_RETURN")
                    continue
                call, issued = pending.pop(part.tool_call_id)
                effect = effects.get(part.tool_call_id)
                _require(
                    effect is not None
                    and effect.tool_name == call.tool_name == part.tool_name
                    and part.outcome == "success",
                    "TOOL_DELIVERY",
                )
                assert effect is not None
                if call.tool_name == "read_memory":
                    _require(isinstance(part.content, str), "MEMORY_READ_TYPE")
                elif call.tool_name == "search_memory":
                    _SEARCH_RESULT.validate_python(part.content, strict=True)
                delivered.append(_Tool(call, part, issued, index, effect))
    _require(not pending and {tool.call.tool_call_id for tool in delivered} == set(effects), "TOOL_HISTORY")
    return delivered


def _one(tools: list[_Tool], name: str) -> _Tool:
    selected = [tool for tool in tools if tool.call.tool_name == name]
    _require(len(selected) == 1, name.upper() + "_REQUIRED")
    return selected[0]


async def _job(store: WorkflowStore, role: RoleResult) -> _Job:
    messages = await read(store, role.scope, role.history, agent="player_role_v1")
    startups = []
    for message in messages:
        if not isinstance(message, ModelRequest):
            continue
        for part in message.parts:
            if not isinstance(part, UserPromptPart) or not isinstance(part.content, str):
                continue
            try:
                item = json.loads(part.content)
            except ValueError:
                continue
            if isinstance(item, dict) and item.get("nativeStoreExercise") is True:
                startups.append(item)
    _require(len(startups) == 1, "STARTUP_REQUIRED")
    startup = startups[0]
    _require(
        startup is not None and startup.get("role") == role.purpose and startup.get("nativeStoreJob") == role.job,
        "STARTUP_SCOPE",
    )
    assert startup is not None
    tools = await _tools(store, role, messages)
    task = f"{STORE_TASK_PREFIX} ({role.purpose}, {role.job})."
    written, planned, updated, finished = (
        _one(tools, name) for name in ("write_plan", "read_plan", "update_task_statuses", "finish_role")
    )
    _require(written.call.tool_name == "write_plan" and planned.call.tool_name == "read_plan", "PLAN_TOOL_ORDER")
    _require(written.call.args_as_dict() == {"items": [{"content": task, "status": "in_progress"}]}, "PLAN_WRITE")
    _require(isinstance(planned.result.content, str), "PLAN_READ")
    matches = [
        match.group(1)
        for line in str(planned.result.content).splitlines()
        if (match := re.fullmatch(r"\d+\. \[~\] \[([^]]+)\] " + re.escape(task), line))
    ]
    _require(len(matches) == 1, "PLAN_TASK_ID")
    task_id = matches[0]
    _require(
        updated.call.args_as_dict() == {"updates": [{"task_id": task_id, "status": "completed"}]}
        and isinstance(updated.result.content, str)
        and f"[{task_id}] {task} -> completed" in updated.result.content
        and written.returned < planned.issued < planned.returned < updated.issued < updated.returned < finished.issued,
        "PLAN_COMPLETION",
    )
    view = startup.get("reads", {}).get("player_turn", {})
    file = None
    if role.purpose == "gameplay":
        file = "MEMORY.md"
    elif role.purpose == "economy":
        city = next((city for city in view.get("cities", []) if city.get("owner") == "economy"), None)
        file = "city-" + city["handle"] + ".md" if city is not None else None
    elif role.purpose == "military":
        file = contribution_prefix(role) + "native-stores.md"
        _require(startup.get("nativeStoreContributionFile") == file, "CONTRIBUTION_SCOPE")
    memory_write, content = None, None
    if file is not None:
        memory_write = _one(tools, "write_memory")
        arguments = memory_write.call.args_as_dict()
        receipt = _WRITE_RESULT.validate_python(memory_write.result.content, strict=True)
        _require(arguments.get("file") == receipt["file"] == file, "MEMORY_WRITE_FILE")
        reads = [
            tool
            for tool in tools
            if tool.call.tool_name == "read_memory"
            and tool.call.args_as_dict() == {"file": file}
            and tool.issued > memory_write.returned
            and isinstance(tool.result.content, str)
        ]
        _require(reads, "MEMORY_WRITE_DELIVERED")
        content = reads[-1].result.content
        assert isinstance(content, str)
        marker = f"{STORE_NOTE_PREFIX}: {role.purpose}, {role.job}."
        _require((STORE_CITY_REASON if role.purpose == "economy" else marker) in content, "MEMORY_MARKER")
        _require(reads[-1].returned < updated.issued, "MEMORY_BEFORE_COMPLETION")
        if role.purpose != "economy":
            _require(arguments.get("content") == marker and "old_text" not in arguments, "NOTE_APPEND")
    else:
        _require(not any(tool.call.tool_name in _MEMORY_TOOLS for tool in tools), "UNEXPECTED_MEMORY_TOOL")
    return _Job(role, startup, tools, task_id, task, written, file, memory_write, content)


async def _verify(root: Path, result: GameRunResult, request: GameRunRequest, copied: Path) -> JsonObject:
    _require(
        request.exercise_native_stores and (request.game, request.player) == (result.game, result.player),
        "REQUEST_SCOPE",
    )
    _require(result.roles and all(role.status == "COMPLETED" for role in result.roles), "ROLES_COMPLETED")
    epochs = {role.scope.epoch for role in result.roles}
    _require(len(epochs) == 1 and len({role.job for role in result.roles}) == len(result.roles), "JOB_SCOPE")
    _require(
        all((role.scope.game, role.scope.player) == (result.game, result.player) for role in result.roles), "ROLE_SCOPE"
    )
    for relative in (
        "player-store/workflow.sqlite3",
        "player-store/agent-steps.sqlite3",
        "coordinator/authored-memory.sqlite3",
        "coordinator/plans.sqlite3",
    ):
        _copy_database(root / relative, copied / relative)
    workflow = WorkflowStore(copied / "player-store", player=result.player)
    try:
        jobs = [await _job(workflow, role) for role in result.roles]
    finally:
        workflow.close()
    by_role: dict[str, list[_Job]] = {}
    for job in jobs:
        by_role.setdefault(job.role.purpose, []).append(job)
    for role_jobs in by_role.values():
        role_jobs.sort(key=lambda job: job.plan_write.effect.started_at)
        last = role_jobs[-1]
        items = await SqlitePlanStore(
            str(copied / "coordinator/plans.sqlite3"), session=plan_session(last.role)
        ).get_items()
        _require(
            len(items) == 1
            and items[0].id == last.task_id
            and items[0].content == last.task
            and items[0].status is TaskStatus.completed,
            "PLAN_SESSION",
        )
    main = by_role.get("gameplay", [])
    _require(len(main) >= 2, "MAIN_JOBS")
    previous_markers = []
    for job in main:
        assert job.memory_write is not None and job.delivered is not None
        prior = [
            tool
            for tool in job.tools
            if tool.call.tool_name == "read_memory"
            and tool.call.args_as_dict() == {"file": "MEMORY.md"}
            and tool.returned < job.memory_write.issued
        ]
        if previous_markers:
            _require(
                prior and all(marker in str(prior[-1].result.content) for marker in previous_markers),
                "MAIN_PRIOR_NOTES",
            )
        previous_markers.append(f"{STORE_NOTE_PREFIX}: gameplay, {job.role.job}.")
        _require(all(job.delivered.count(marker) == 1 for marker in previous_markers), "MAIN_NOTES_RETAINED")
    restart = request.restart_after
    _require(restart is not None and restart < request.turns, "RESTART_REQUIRED")
    assert restart is not None
    turns = {job.startup.get("reads", {}).get("player_turn", {}).get("turn") for job in main}
    _require(None not in turns and len(turns) > restart, "RESTART_TURNS")
    native = SqliteMemoryStore(database=copied / "coordinator/authored-memory.sqlite3")
    prefix = authored_prefix(main[0].role.scope)
    paths = await native.list_paths("", limit=MAX_DOCUMENTS + 1)
    _require(
        paths and len(paths) <= MAX_DOCUMENTS and all(path.startswith(prefix) for path in paths), "MEMORY_NAMESPACE"
    )
    writes: dict[str, _Job] = {}
    for job in sorted(jobs, key=lambda job: job.plan_write.effect.started_at):
        if job.memory_file is not None:
            writes[prefix + job.memory_file] = job
    _require(set(paths) == set(writes), "MEMORY_FILES")
    city_plans = contributions = 0
    for path in paths:
        document = await native.read(path, max_chars=MAX_DOCUMENT_CHARS)
        _require(document is not None and not document.truncated and document.operation_id, "MEMORY_INCOMPLETE")
        assert document is not None
        job = writes[path]
        assert job.memory_write is not None
        metadata = AuthoredMetadata.model_validate_strings(dict(document.metadata or {}))
        receipt = _WRITE_RESULT.validate_python(job.memory_write.result.content, strict=True)
        _require(
            (metadata.game, metadata.player, metadata.epoch, metadata.job, metadata.purpose)
            == (result.game, result.player, job.role.scope.epoch, job.role.job, job.role.purpose)
            and document.version == receipt["version"]
            and document.content == job.delivered,
            "MEMORY_PROVENANCE",
        )
        decoded = decode_document(path, document)
        if metadata.purpose == "economy":
            _require(
                decoded["category"] == "city_plan" and decoded["body"]["reasons"]["construction"] == STORE_CITY_REASON,
                "CITY_PLAN",
            )
            city_plans += 1
        elif metadata.purpose == "military":
            _require(decoded["category"] == "contribution", "CONTRIBUTION_KIND")
            contributions += 1
    with closing(
        sqlite3.connect((root / "coordinator/notebook/memory.sqlite3").resolve().as_uri() + "?mode=ro", uri=True)
    ) as db:
        authored = MEMORY_TYPES | {"contribution"}
        legacy = db.execute("SELECT category FROM records").fetchall()
        _require(not any(category in authored for (category,) in legacy), "LEGACY_AUTHORED_ROWS")
    counts = Counter(tool.call.tool_name for job in jobs for tool in job.tools)
    return {
        "verified": True,
        "roleRuns": len(jobs),
        "completedRoleTasks": len(jobs),
        "rolePlanSessions": len(by_role),
        "memoryFiles": len(paths),
        "mainNoteJobs": len(main),
        "notesAcrossRestart": True,
        "cityPlans": city_plans,
        "militaryContributions": contributions,
        "nativeMemoryToolEffects": sum(counts[name] for name in _MEMORY_TOOLS),
        "nativePlanningToolEffects": sum(counts[name] for name in PLAN_TOOLS),
        "legacyAuthoredRows": 0,
    }


async def verify_native_stores(root: Path, result: GameRunResult, request: GameRunRequest) -> JsonObject:
    """Raise on invalid exercise evidence, leaving all authoritative files untouched.

    The game verifier separately checks the worker restart logs. This audit binds
    notes delivered in jobs across that requested boundary to the final native
    files; it does not infer that a physical restart happened from notes alone.
    """
    try:
        with TemporaryDirectory(prefix="native-stores-audit-") as temporary:
            return await _verify(root, result, request, Path(temporary))
    except JournalConflict:
        raise
    except (KeyError, TypeError, ValueError, OSError, sqlite3.Error):
        raise JournalConflict("NATIVE_STORES_ARTIFACT_INVALID") from None
