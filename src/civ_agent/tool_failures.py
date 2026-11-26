"""Scoped native tool feedback and unresolved effects for the private operator."""

import asyncio
import json
import re
import sqlite3
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError

from civ_agent import memory_schema as tables
from civ_agent.monitoring import read_database
from civ_agent.protocol import JsonObject
from civ_agent.response_compaction import expand_response

STATE_ERRORS = {
    "AGREEMENT_CHANGED",
    "OBSERVATION_REQUIRED",
    "STALE_REVISION",
    "MILITARY_OBSERVATION_CHANGED",
    "ECONOMY_CONTEXT_CHANGED",
    "CONVERSATION_UPDATED",
    "STALE_EPOCH",
    "FRESH_OBSERVATION_REQUIRED",
    "FRESH_INSPECTION_REQUIRED",
    "TARGET_CHANGED",
    "DECISION_CONTEXT_EXPIRED",
    "ECONOMY_OBSERVATION_CHANGED",
    "MESSAGES_NOT_DELIVERED_TO_JOB",
}
OWNERSHIP_ERRORS = {
    "ROLE_ACTION_NOT_PERMITTED",
    "UNIT_OWNED_BY_OTHER_ROLE",
    "CITY_OWNED_BY_OTHER_ROLE",
    "MILITARY_REQUIRED",
    "MAIN_REQUIRED",
    "TOOL_NOT_PERMITTED",
    "MESSAGE_RECIPIENT_REQUIRED",
    "MESSAGE_ACKNOWLEDGEMENT_GAMEPLAY_ONLY",
}


def error_code(error) -> str:
    code = error.get("code") if isinstance(error, dict) else error
    return code if isinstance(code, str) and re.fullmatch(r"[A-Z][A-Z0-9_]{0,127}", code) else "UNCLASSIFIED_ERROR"


def category(code: str) -> str:
    if code in {"PROVIDER_DECISION_REQUEST_LIMIT", "MODEL_REQUEST_BUDGET_EXHAUSTED"}:
        return "request_budget"
    if code in STATE_ERRORS or code.startswith("STALE_"):
        return "state_changed"
    if code.startswith("INVALID_") or code == "SDK_TOOL_VALIDATION_RETRY":
        return "arguments"
    if code in OWNERSHIP_ERRORS:
        return "ownership"
    if code in {
        "MILITARY_COMPLETION_REVIEW_REQUIRED",
        "URGENT_REQUEST_DISPOSITION_REQUIRED",
        "ECONOMY_WORKER_SKIPS_REQUIRED",
    }:
        return "completion"
    if code in {"STATE_UPDATE_FAILED", "STATE_UPDATE_UNAVAILABLE"}:
        return "refresh"
    if code in {
        "DECISION_EXECUTION_UNCERTAIN",
        "NATIVE_RECEIPT_UNCERTAIN",
        "COMMUNICATION_UNAVAILABLE",
        "INTERNAL_ERROR",
    }:
        return "transport"
    if code in {"OPTION_UNAVAILABLE", "UNIT_UNAVAILABLE", "INSUFFICIENT_GOLD", "ILLEGAL_ACTION", "NO_MOVEMENT"}:
        return "legality"
    if code in {"SOURCE_EPOCH_RETIRED", "REQUEST_CANCELLED"}:
        return "retirement"
    return "other"


@dataclass(frozen=True)
class NativeCall:
    owner: JsonObject
    run: str | None
    call: str
    operation: str
    tool: str
    arguments: object
    result: object
    effect: str | None
    status: str
    source: str
    operation_recorded: bool = True


def _owner(scope, task, civilization):
    return {
        "scope": scope.model_dump(mode="json"),
        "civilization": civilization,
        "role": task.purpose,
        "turn": task.turn,
        "job": task.gateway_job,
        **task.settings,
    }


def _arguments(name, args, index):
    if not isinstance(args, dict):
        return args
    if name == "execute_commands":
        commands = args.get("commands", [])
        return commands[index] if isinstance(commands, list) and index < len(commands) else None
    return args


async def _sdk_calls(paths, scope, tasks):
    from dataclasses import asdict

    from pydantic_ai.messages import RetryFeedbackPart, RetryPromptPart, ToolCallPart, ToolReturnPart

    from civ_agent.orchestration.native_watch import native_step_store, recorded_sdk_runs

    result, covered = [], set()
    with native_step_store(paths, scope) as store:
        if store is None:
            return result, covered
        for run, task in await recorded_sdk_runs(store, scope, tasks):
            snapshot = await store.latest_snapshot(run_id=run.run_id, include_interrupted=True)
            calls, returns = {}, {}
            if snapshot is not None:
                if (snapshot.run_id, snapshot.agent_name, snapshot.conversation_id) != (
                    run.run_id,
                    run.agent_name,
                    run.conversation_id,
                ):
                    raise ValueError("NATIVE_FAILURE_SNAPSHOT_SCOPE")
                for message_index, message in enumerate(snapshot.messages):
                    if message.run_id is not None and message.run_id != run.run_id:
                        continue
                    for part_index, part in enumerate(message.parts):
                        if isinstance(part, ToolCallPart):
                            calls[part.tool_call_id] = part
                        elif isinstance(part, (ToolReturnPart, RetryPromptPart)) and part.tool_call_id is not None:
                            returns[part.tool_call_id] = part
                        elif isinstance(part, RetryFeedbackPart):
                            position = f"message:{message_index}:part:{part_index}"
                            result.append(
                                NativeCall(
                                    _owner(task.scope, task, paths.civilization),
                                    run.run_id,
                                    position,
                                    f"{run.run_id}:{position}",
                                    "model_output",
                                    None,
                                    {"feedback": part.model_response(), "retry": asdict(part.retry)},
                                    None,
                                    "model_feedback",
                                    "native_sdk_feedback",
                                    False,
                                )
                            )
            effects = {
                effect.tool_call_id: effect for effect in await store.list_unresolved_tool_effects(run_id=run.run_id)
            }
            for identifier in calls:
                effect = await store.get_tool_effect(run_id=run.run_id, tool_call_id=identifier)
                if effect is not None:
                    effects[identifier] = effect
            for identifier in dict.fromkeys([*calls, *effects]):
                call, effect, response = calls.get(identifier), effects.get(identifier), returns.get(identifier)
                if effect is not None and (effect.run_id != run.run_id or effect.tool_call_id != identifier):
                    raise ValueError("NATIVE_FAILURE_EFFECT_SCOPE")
                if call is None:
                    assert effect is not None
                    name = effect.tool_name
                else:
                    name = call.tool_name
                if call is not None and effect is not None and effect.tool_name != name:
                    raise ValueError("NATIVE_FAILURE_EFFECT_TOOL")
                if response is not None and response.tool_name != name:
                    raise ValueError("NATIVE_FAILURE_RETURN_TOOL")
                arguments = call.args if call is not None else None
                if isinstance(arguments, str):
                    try:
                        arguments = json.loads(arguments)
                    except ValueError:
                        pass  # Expected invalid tool arguments remain triageable.
                recorded_operation = effect is not None and effect.idempotency_key is not None
                operation = (
                    effect.idempotency_key
                    if effect is not None and effect.idempotency_key is not None
                    else f"{run.run_id}:{identifier}"
                )
                if isinstance(response, ToolReturnPart):
                    body = (
                        {
                            "feedback": response.model_response_str(),
                            "retry": asdict(response.retry) if response.retry is not None else None,
                        }
                        if response.outcome == "retried"
                        else response.content
                    )
                else:
                    body = {"feedback": response.model_response()} if isinstance(response, RetryPromptPart) else None
                owner = _owner(task.scope, task, paths.civilization)
                state = (
                    "retry"
                    if isinstance(response, RetryPromptPart)
                    or isinstance(response, ToolReturnPart)
                    and response.outcome == "retried"
                    else response.outcome
                    if isinstance(response, ToolReturnPart) and response.outcome != "success"
                    else "returned"
                    if response is not None
                    else "failed"
                    if effect is not None and effect.status == "failed"
                    else "pending"
                )
                if recorded_operation and state not in {"pending", "interrupted"}:
                    covered.add(operation)
                if state == "returned" and isinstance(body, dict) and isinstance(body.get("receipts"), list):
                    for index, receipt in enumerate(body["receipts"]):
                        if (
                            not isinstance(receipt, dict)
                            or not isinstance(receipt.get("operationId"), str)
                            or receipt.get("status") not in {"confirmed", "rejected", "unexecuted"}
                        ):
                            raise ValueError("NATIVE_FAILURE_RECEIPT")
                        covered.add(receipt["operationId"])
                        result.append(
                            NativeCall(
                                owner,
                                run.run_id,
                                identifier,
                                receipt["operationId"],
                                receipt.get("tool", name),
                                _arguments(name, arguments, index),
                                receipt.get("result"),
                                effect.status if effect else None,
                                receipt["status"],
                                "native_sdk",
                            )
                        )
                else:
                    result.append(
                        NativeCall(
                            owner,
                            run.run_id,
                            identifier,
                            operation,
                            name,
                            arguments,
                            body,
                            effect.status if effect else None,
                            state,
                            "native_sdk",
                            recorded_operation,
                        )
                    )
    return result, covered


def _gateway_calls(path, scope, tasks, covered, include_internal):
    from civ_agent.orchestration.native_watch import verify_native_notebook

    if not path.exists():
        return []
    by_job, result = {task.gateway_job: task for task in tasks.values()}, []
    with read_database(path) as db:
        verify_native_notebook(db, scope)
        query = (
            select(tables.calls, tables.jobs.c.purpose)
            .select_from(tables.calls.join(tables.jobs, tables.calls.c.job == tables.jobs.c.id))
            .where(tables.jobs.c.epoch == scope.epoch)
            .order_by(tables.call_order)
        )
        for row in db.execute(query).mappings():
            task = by_job.get(row["job"])
            internal = row["id"].startswith(("startup:", "host:", "resume:", "role-refresh:"))
            if task is None or row["id"] in covered or internal and not include_internal:
                continue
            if row["purpose"] != task.purpose:
                raise ValueError("NATIVE_FAILURE_GATEWAY_ROLE")
            response = expand_response(json.loads(row["result"])) if row["result"] is not None else None
            if row["name"] == "inspect_bundle" and isinstance(response, dict) and response.get("results"):
                continue
            result.append(
                NativeCall(
                    _owner(scope.model_copy(update={"activation": task.scope.activation}), task, ""),
                    None,
                    row["id"],
                    row["id"],
                    row["name"],
                    json.loads(row["arguments"]),
                    response,
                    None,
                    "returned" if row["result"] is not None else "pending",
                    "native_gateway",
                )
            )
    return result


def _issues(call):
    body = call.result
    if call.status == "retry":
        retry = body.get("retry") if isinstance(body, dict) else None
        cause = retry.get("cause") if isinstance(retry, dict) else None
        code = {
            "validation_error": "SDK_TOOL_VALIDATION_RETRY",
            "model_retry": "SDK_TOOL_MODEL_RETRY",
            "unknown_tool": "SDK_UNKNOWN_TOOL",
            "unavailable_tool": "SDK_UNAVAILABLE_TOOL",
        }.get(cause if isinstance(cause, str) else "", "SDK_TOOL_RETRY")
        yield "tool_feedback", code, "arguments" if cause == "validation_error" else "tool", None
    elif call.status == "model_feedback":
        yield "model_feedback", "SDK_OUTPUT_RETRY", "generation", None
    elif call.status == "failed":
        yield "tool_failure", "SDK_TOOL_FAILED", "tool", None
    elif call.status == "denied":
        yield "tool_denied", "SDK_TOOL_DENIED", "authorization", None
    if isinstance(body, dict):
        if "error" in body:
            code = error_code(body["error"])
            yield "error", code, category(code), None
        elif call.status == "rejected":
            yield "rejection", "OPERATION_REJECTED", "other", None
        for index, change in enumerate(body.get("changes", [])):
            if isinstance(change, dict) and change.get("accepted") is False:
                code = error_code(change.get("error", change.get("reason")))
                yield "rejected_edit", code, "memory", index
        if isinstance(body.get("stateUpdate"), dict) and body["stateUpdate"].get("kind") == "unavailable":
            yield "refresh", "STATE_UPDATE_UNAVAILABLE", "refresh", None


def _identity(call, kind, index):
    from civ_agent.orchestration.contracts import digest

    return digest([call.owner["scope"], call.run, call.call, call.operation, kind, index])[:24]


def _summary(root, call, kind, code, group, index):
    arguments = call.arguments if isinstance(call.arguments, dict) else {}
    command = arguments.get("command", arguments)
    command = (
        command.get("kind")
        if isinstance(command, dict) and call.tool in {"player_command", "execute_commands"}
        else None
    )
    if not isinstance(command, str) or re.fullmatch(r"[A-Z][A-Z0-9_]{0,63}", command) is None:
        command = None
    return {
        "id": _identity(call, kind, index),
        "game": str(root),
        "civilization": call.owner["civilization"],
        "role": call.owner["role"],
        "turn": call.owner["turn"],
        "epoch": call.owner["scope"]["epoch"],
        "model": call.owner["model"],
        "provider": call.owner["provider"],
        "reasoningEffort": call.owner["reasoningEffort"],
        "job": call.owner["job"],
        "call": call.call,
        "tool": call.tool,
        "command": command,
        "kind": kind,
        "code": code,
        "category": group,
        "source": call.source,
        "executionStatus": call.status,
        "evidence": {
            "scope": call.owner["scope"],
            "runId": call.run,
            "operationId": call.operation if call.operation_recorded else None,
            "effectStatus": call.effect,
            "index": index,
        },
    }


def _selection(call, role, tool, since_turn):
    return (
        (role is None or role == call.owner["role"])
        and (tool is None or tool == call.tool)
        and (since_turn is None or call.owner["turn"] >= since_turn)
    )


def _collect(root, include_internal):
    from civ_agent.operator_state import projection as native_projection
    from civ_agent.operator_state import publication_basis as epoch_publication_basis
    from civ_agent.operator_state import published_game as published_managed_epoch
    from civ_agent.orchestration.contracts import Scope
    from civ_agent.orchestration.native_watch import operator_tasks
    from civ_agent.player_paths import PlayerPaths as ManagedPlayerPaths
    from civ_agent.watch_state import native_host_rows

    request, basis, projection = published_managed_epoch(root), epoch_publication_basis(root), native_projection(root)
    calls = []
    host = projection.result
    jobs = (
        native_host_rows(
            root,
            request.authority.epoch,
            host.matchId,
            {seat.civilization for seat in request.seats} | {score.civilization for score in host.scores},
        )[1]
        if host is not None
        else ()
    )
    for index, seat in enumerate(request.seats):
        paths = ManagedPlayerPaths(root, index, seat.civilization)
        if (paths.player, seat.session.game) != (seat.session.player, request.authority.game):
            raise ValueError("NATIVE_FAILURE_PLAYER_SCOPE")
        scope = Scope(
            game=request.authority.game, player=paths.player, epoch=request.authority.epoch, activation="failures"
        )
        notebook = paths.coordinator / "notebook/memory.sqlite3"
        tasks = operator_tasks(notebook, scope, jobs)
        sdk, covered = asyncio.run(_sdk_calls(paths, scope, tasks))
        gateway = _gateway_calls(notebook, scope, tasks, covered, include_internal)
        calls.extend(sdk)
        calls.extend(
            NativeCall(
                {**call.owner, "civilization": seat.civilization},
                call.run,
                call.call,
                call.operation,
                call.tool,
                call.arguments,
                call.result,
                call.effect,
                call.status,
                call.source,
            )
            for call in gateway
        )

    if epoch_publication_basis(root) != basis:
        raise ValueError("NATIVE_FAILURE_PUBLICATION_CHANGED")
    unique = {}
    for call in calls:
        key = json.dumps(call.owner["scope"], sort_keys=True), call.operation
        prior = unique.get(key)
        if prior is None or prior.status == "pending" and call.status != "pending":
            unique[key] = call
        elif call.status != "pending" and (call.result, call.status, call.arguments) != (
            prior.result,
            prior.status,
            prior.arguments,
        ):
            raise ValueError("NATIVE_FAILURE_OPERATION_CONFLICT")
    return list(unique.values()), projection


def _failure_report(
    roots: list[Path],
    *,
    role: str | None = None,
    tool: str | None = None,
    code: str | None = None,
    since_turn: int | None = None,
    include_internal: bool = False,
) -> JsonObject:
    from civ_agent.container_support import ContainerError
    from civ_agent.runtime_profile import game_directories

    failures, games, unavailable, usage, recoveries = [], [], [], [], []
    for root in game_directories(roots):
        counts = Counter(
            completedCalls=0,
            pendingCalls=0,
            errorCalls=0,
            rejectedEdits=0,
            refreshFailures=0,
            unexecutedCalls=0,
            modelFeedback=0,
        )
        try:
            if not any(
                (root / path).exists() for path in ("native-runtime/game.json", "native-runtime/controller.json")
            ):
                unavailable.append({"game": str(root), "reason": "native_configuration_pending"})
                continue
            calls, projection = _collect(root, include_internal)
            if projection.result is not None:
                usage.append(
                    {
                        "game": str(root),
                        "epoch": projection.result.epoch,
                        "usage": projection.result.usage.model_dump(mode="json"),
                        "modelJobs": projection.result.modelJobs,
                        "modelRequests": projection.result.modelRequests,
                    }
                )
            selected = [call for call in calls if _selection(call, role, tool, since_turn)]
            for call in selected:
                if call.status in {"pending", "interrupted"}:
                    counts["pendingCalls"] += 1
                    continue
                if call.status == "unexecuted":
                    counts["unexecutedCalls"] += 1
                    continue
                counts["modelFeedback" if call.status == "model_feedback" else "completedCalls"] += 1
                for kind, found, group, index in _issues(call):
                    counts[
                        {"rejected_edit": "rejectedEdits", "refresh": "refreshFailures"}.get(kind, "errorCalls")
                    ] += 1
                    if code is not None and found != code:
                        continue
                    failures.append(_summary(root, call, kind, found, group, index))
            games.append(
                {
                    "game": str(root),
                    "latestRecordedTurn": max((call.owner["turn"] for call in calls), default=None),
                    **counts,
                }
            )
        except (OSError, ValueError, KeyError, TypeError, sqlite3.Error, SQLAlchemyError, ContainerError) as error:
            unavailable.append(
                {
                    "game": str(root),
                    "reason": "native_failure_records_unreadable",
                    "exceptionType": type(error).__name__,
                }
            )
    grouped = defaultdict(list)
    for item in failures:
        grouped[tuple(item[key] for key in ("category", "code", "tool", "command", "role", "kind"))].append(item)
    groups: list[JsonObject] = [
        {
            **dict(zip(("category", "code", "tool", "command", "role", "kind"), key, strict=True)),
            "count": len(items),
            "games": len({item["game"] for item in items}),
            "models": dict(Counter(item["model"] for item in items)),
            "firstTurn": min(item["turn"] for item in items),
            "lastTurn": max(item["turn"] for item in items),
            "examples": [item["id"] for item in items[:3]],
        }
        for key, items in grouped.items()
    ]
    return {
        "generatedAt": datetime.now(UTC).isoformat(),
        "games": games,
        "filters": {
            "role": role,
            "tool": tool,
            "code": code,
            "sinceTurn": since_turn,
            "includeInternal": include_internal,
        },
        "groups": sorted(groups, key=lambda item: (-item["count"], item["code"])),
        "failures": failures,
        "nativeUsage": usage,
        "recoveries": recoveries,
        "unavailable": unavailable,
        "notes": [
            "Native SDK message receipts and scoped gateway children are deduplicated by their actual operation IDs; no conversation or receipt ledger is created.",
            "Pending effects have no presumed outcome. Never retry a native effect based on this observational report.",
            "SDK tool feedback is distinct from native error receipts. Raw event exceptions, model content, arguments and responses are omitted from summaries; --id rereads the exact current owning store.",
            "Usage is the native host publication, including all four counters and its actual completeness flag. SDK model starts are not claimed as forwarded or billed provider requests.",
            "Only the currently published epoch is selected. Retired attempts remain available for inspection.",
        ],
    }


def _failure_detail(report: JsonObject, identifier: str) -> JsonObject:

    found = next((item for item in report["failures"] if item["id"] == identifier), None)
    if found is None:
        raise ValueError("No recorded native failure matches this ID and selection")
    root = Path(found["game"])
    calls, _ = _collect(root, report["filters"]["includeInternal"])
    for call in calls:
        for kind, code, group, index in _issues(call):
            if _identity(call, kind, index) == identifier:
                current = _summary(root, call, kind, code, group, index)
                if current != found:
                    raise ValueError("NATIVE_FAILURE_DETAIL_SCOPE")
                return {
                    **current,
                    "arguments": call.arguments,
                    "result": call.result,
                    "toolEffect": {
                        "status": call.effect,
                        "runId": call.run,
                        "operationId": call.operation if call.operation_recorded else None,
                    },
                    "context": {
                        "ordering": "Native SDK/tool-effect identity; no inferred recovery or global completion order"
                    },
                }
    raise ValueError("The current published native stores no longer contain this exact failure")


def render_report(report: JsonObject) -> str:
    completed = sum(row["completedCalls"] for row in report["games"])
    errors = sum(row["errorCalls"] for row in report["games"])
    lines = [
        f"Native tool feedback — {report['generatedAt']}",
        f"{errors} error/feedback records / {completed} completed recorded operations",
        "",
        "Count  Category       Role           Tool / command                 Code (example ID)",
    ]
    for group in report["groups"]:
        action = group["tool"] + ("/" + str(group["command"]) if group["command"] else "")
        lines.append(
            f"{group['count']:5}  {group['category']:13}  {group['role']:13}  {action:30}  {group['code']} ({group['examples'][0]})"
        )
    for game in report["games"]:
        lines.append(
            f"{game['game']}: {game['pendingCalls']} unresolved operations; {game['unexecutedCalls']} recorded unexecuted children"
        )
    if report["unavailable"]:
        lines.extend(
            ["", "INCOMPLETE: some native records are unavailable.", json.dumps(report["unavailable"], indent=2)]
        )
    lines.extend(["", "Use --id ID to reread exact native tool arguments and response.", *report["notes"]])
    return "\n".join(lines) + "\n"


def failure_report(
    roots: list[Path],
    *,
    role: str | None = None,
    tool: str | None = None,
    code: str | None = None,
    since_turn: int | None = None,
    include_internal: bool = False,
) -> JsonObject:
    from contextlib import ExitStack

    from civ_agent.operator_views import passive_root, remap_capture_paths
    from civ_agent.runtime_profile import game_directories

    with ExitStack() as stack:
        mapping, unavailable = [], []
        for root in game_directories(roots):
            try:
                mapping.append((stack.enter_context(passive_root(root)), root))
            except (OSError, ValueError, sqlite3.Error) as error:
                unavailable.append({"game": str(root), "reason": type(error).__name__})
        report = _failure_report(
            [captured for captured, _ in mapping],
            role=role,
            tool=tool,
            code=code,
            since_turn=since_turn,
            include_internal=include_internal,
        )
        report["unavailable"].extend(unavailable)
        return remap_capture_paths(report, mapping)


def failure_detail(report: JsonObject, identifier: str) -> JsonObject:
    from civ_agent.operator_views import passive_root, remap_capture_paths

    found = next((item for item in report["failures"] if item["id"] == identifier), None)
    if found is None:
        raise ValueError("No recorded native failure matches this ID and selection")
    root = Path(found["game"])
    with passive_root(root) as captured:
        temporary = remap_capture_paths(report, [(root, captured)])
        return remap_capture_paths(_failure_detail(temporary, identifier), [(captured, root)])
