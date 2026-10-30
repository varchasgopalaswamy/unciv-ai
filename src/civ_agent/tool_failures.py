"""Operator triage of durable tool receipts; never opens a game service or model."""

import hashlib
import json
from collections import Counter, defaultdict
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import inspect, select
from sqlalchemy.exc import SQLAlchemyError

from civ_agent import memory_schema as tables
from civ_agent.monitoring import read_database
from civ_agent.protocol import JsonObject
from civ_agent.response_compaction import expand_response
from civ_agent.runtime_profile import game_directories

STATE_ERRORS = {
    "OBSERVATION_REQUIRED",
    "STALE_REVISION",
    "MILITARY_OBSERVATION_CHANGED",
    "ECONOMY_CONTEXT_CHANGED",
    "CONVERSATION_UPDATED",
    "STALE_EPOCH",
}
OWNERSHIP_ERRORS = {
    "ROLE_ACTION_NOT_PERMITTED",
    "UNIT_OWNED_BY_OTHER_ROLE",
    "CITY_OWNED_BY_OTHER_ROLE",
    "MILITARY_REQUIRED",
    "MAIN_REQUIRED",
    "TOOL_NOT_PERMITTED",
    "MESSAGE_RECIPIENT_REQUIRED",
}
RESPONSE_FIELDS = (
    "error",
    "applied",
    "recoverable",
    "recovery",
    "instruction",
    "invalidFields",
    "requiredFields",
    "missingFields",
    "allowedFields",
    "queryIndex",
    "actionSummary",
)


def call_source(identifier: str) -> str:
    if identifier.isdecimal():
        return "model"
    if identifier.startswith("inspection:"):
        return "bundle"
    if identifier.startswith("startup:"):
        return "startup"
    if identifier.startswith(("host:", "resume:", "role-refresh:", "role-wait-resume:")):
        return "internal"
    return "unknown"


def error_code(error) -> str:
    if isinstance(error, dict):
        return str(error.get("code") or "UNCLASSIFIED_ERROR")
    return error if isinstance(error, str) and error else "UNCLASSIFIED_ERROR"


def category(code: str) -> str:
    if code in STATE_ERRORS:
        return "state_changed"
    if code.startswith("INVALID_"):
        return "arguments"
    if code in OWNERSHIP_ERRORS:
        return "ownership"
    if code in {"MILITARY_COMPLETION_REVIEW_REQUIRED", "URGENT_REQUEST_DISPOSITION_REQUIRED"}:
        return "completion"
    return "other"


def issue_parts(name: str, result: JsonObject):
    if "error" in result:
        code = error_code(result["error"])
        yield "error", code, category(code), None
    elif name == "memory_update":
        for index, change in enumerate(result.get("changes", [])):
            if change.get("accepted") is False:
                code = error_code(change.get("error", change.get("reason")))
                yield "rejected_edit", code, "memory", index
    if (result.get("stateUpdate") or {}).get("kind") == "unavailable":
        yield "refresh", "STATE_UPDATE_UNAVAILABLE", "refresh", None


def _identity(database, job, call, kind, index):
    value = json.dumps([str(database), job, call, kind, index], separators=(",", ":"))
    return hashlib.sha256(value.encode()).hexdigest()[:20]


def _diagnostic(result: JsonObject, arguments: JsonObject, index: int | None) -> JsonObject:
    diagnostic = {key: result[key] for key in RESPONSE_FIELDS if key in result}
    if index is not None:
        diagnostic["rejectedEdit"] = result["changes"][index]
    if result.get("stateUpdate"):
        diagnostic["stateUpdate"] = {k: v for k, v in result["stateUpdate"].items() if k in {"kind", "instruction"}}
    if result.get("militaryCompletion"):
        review = result["militaryCompletion"]
        supplied = arguments.get("militaryReview") or {}
        diagnostic["militaryCompletion"] = {
            "remainingTargets": review.get("total", review.get("remainingTargets")),
            "suppliedBasis": supplied.get("basis"),
            "expectedBasis": review.get("basis"),
            "review": review.get("read"),
        }
    return diagnostic


def failure_report(
    roots: list[Path],
    *,
    role: str | None = None,
    tool: str | None = None,
    code: str | None = None,
    since_turn: int | None = None,
    include_internal: bool = False,
) -> JsonObject:
    failures, games, unavailable = [], [], []
    for game in game_directories(roots):
        counts = Counter(completedCalls=0, pendingCalls=0, errorCalls=0, rejectedEdits=0, refreshFailures=0)
        latest_turn = None
        databases = sorted((game / "agent-services").glob("*/civilization/memory.sqlite3"))
        if not databases:
            unavailable.append({"game": str(game), "reason": "No durable call journals yet"})
        for database in databases:
            try:
                with read_database(database) as db:
                    if not {"calls", "jobs"} <= set(inspect(db).get_table_names()):
                        raise ValueError("Call journal schema unavailable")
                    query = select(
                        tables.call_order.label("rowid"),
                        tables.calls,
                        tables.jobs.c.purpose,
                        tables.jobs.c.game_turn,
                        tables.jobs.c.epoch,
                        tables.jobs.c.settings,
                        tables.jobs.c.status.label("job_status"),
                    ).select_from(tables.calls.outerjoin(tables.jobs, tables.calls.c.job == tables.jobs.c.id))
                    if role:
                        query = query.where(tables.jobs.c.purpose == role)
                    if tool:
                        query = query.where(tables.calls.c.name == tool)
                    if since_turn is not None:
                        query = query.where(tables.jobs.c.game_turn >= since_turn)
                    # One read transaction per civilization. No writer objects or recovery are instantiated.
                    for row in db.execute(query.order_by(tables.call_order)).mappings():
                        source = call_source(row["id"])
                        if not include_internal and source in {"startup", "internal"}:
                            counts["excludedInternalCalls"] += 1
                            continue
                        if row["game_turn"] is not None:
                            latest_turn = max(latest_turn or 0, row["game_turn"])
                        if row["result"] is None:
                            counts["pendingCalls"] += 1
                            continue
                        result = expand_response(json.loads(row["result"]))
                        # Children have independent receipts. Count them once, not again in the wrapper.
                        if row["name"] == "inspect_bundle" and result.get("results"):
                            counts["excludedBundleWrappers"] += 1
                            continue
                        counts["completedCalls"] += 1
                        counts["unknownSourceCalls"] += source == "unknown"
                        arguments = json.loads(row["arguments"])
                        settings = json.loads(row["settings"] or "{}")
                        for kind, found_code, group, index in issue_parts(row["name"], result):
                            counts[
                                {"error": "errorCalls", "rejected_edit": "rejectedEdits", "refresh": "refreshFailures"}[
                                    kind
                                ]
                            ] += 1
                            if code is not None and found_code != code:
                                continue
                            failures.append(
                                {
                                    "id": _identity(database, row["job"], row["id"], kind, index),
                                    "game": str(game),
                                    "civilization": database.parent.parent.name,
                                    "role": row["purpose"],
                                    "turn": row["game_turn"],
                                    "epoch": row["epoch"],
                                    "model": settings.get("model"),
                                    "provider": settings.get("provider"),
                                    "reasoningEffort": settings.get("reasoningEffort"),
                                    "jobStatus": row["job_status"],
                                    "job": row["job"],
                                    "call": row["id"],
                                    "source": source,
                                    "tool": row["name"],
                                    "command": arguments.get("kind", arguments.get("action")),
                                    "kind": kind,
                                    "code": found_code,
                                    "category": group,
                                    "arguments": arguments,
                                    "diagnostic": _diagnostic(result, arguments, index),
                                    "evidence": {"database": str(database), "rowid": row["rowid"], "editIndex": index},
                                }
                            )
            except (OSError, ValueError, KeyError, TypeError, SQLAlchemyError) as error:
                unavailable.append({"database": str(database), "reason": type(error).__name__})
        games.append({"game": str(game), "latestRecordedTurn": latest_turn, **counts})
    grouped = defaultdict(list)
    for failure in failures:
        key = tuple(failure[k] for k in ("category", "code", "tool", "command", "role", "kind"))
        grouped[key].append(failure)
    groups: list[JsonObject] = []
    for key, items in grouped.items():
        turns = [item["turn"] for item in items if item["turn"] is not None]
        groups.append(
            {
                **dict(zip(("category", "code", "tool", "command", "role", "kind"), key, strict=True)),
                "count": len(items),
                "games": len({item["game"] for item in items}),
                "models": dict(Counter(item["model"] or "unknown" for item in items)),
                "firstTurn": min(turns) if turns else None,
                "lastTurn": max(turns) if turns else None,
                "examples": [item["id"] for item in items[:3]],
            }
        )
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
        "groups": sorted(groups, key=lambda g: (-g["count"], g["code"], g["tool"], str(g["command"]), str(g["role"]))),
        "failures": failures,
        "unavailable": unavailable,
        "notes": [
            "Read-only snapshots of durable gateway receipts, deduplicated by database/job/call; replays do not add failures.",
            "Bundle children are counted instead of their enclosing result. Startup/host reads are excluded unless requested; source is inferred from saved call IDs, with unfamiliar IDs marked unknown.",
            "Pending or interrupted calls without a response are not assumed to have failed. Applied/unknown execution must be reconciled before retrying.",
            "Rejected memory edits and unavailable state refreshes are separate from error responses. No later success is assumed to resolve an earlier failure.",
            "Coverage excludes failures before receipt journaling, frozen-review reads, provider transport errors and JavaScript execution errors. Source journals may be unavailable; this is not a count of every model request or every runner failure.",
            "Role/tool/turn filters apply to call counts; the code filter selects issues only. Databases are read independently while games continue.",
        ],
    }


def failure_detail(report: JsonObject, identifier: str) -> JsonObject:
    found = next((item for item in report["failures"] if item["id"] == identifier), None)
    if found is None:
        raise ValueError("No recorded failure matches that ID and selection")
    evidence = found["evidence"]
    with read_database(Path(evidence["database"])) as db:
        condition = (tables.calls.c.job == found["job"]) & (tables.calls.c.id == found["call"])
        row = db.execute(select(tables.calls).where(condition)).mappings().one()
        query = select(tables.call_order.label("rowid"), tables.calls).where(tables.calls.c.job == found["job"])
        before = (
            db.execute(query.where(tables.call_order < evidence["rowid"]).order_by(tables.call_order.desc()).limit(3))
            .mappings()
            .all()
        )
        after = (
            db.execute(query.where(tables.call_order > evidence["rowid"]).order_by(tables.call_order).limit(3))
            .mappings()
            .all()
        )

        def context(item):
            response = expand_response(json.loads(item["result"])) if item["result"] is not None else None
            return {
                "call": item["id"],
                "tool": item["name"],
                "arguments": json.loads(item["arguments"]),
                "result": response,
            }

        return {
            **found,
            "result": expand_response(json.loads(row["result"])),
            "context": {
                "ordering": "Accepted call order within this job, not completion order or proof of recovery",
                "before": [context(item) for item in reversed(before)],
                "after": [context(item) for item in after],
            },
        }


def render_report(report: JsonObject) -> str:
    completed = sum(row["completedCalls"] for row in report["games"])
    errors = sum(row["errorCalls"] for row in report["games"])
    percent = f" ({errors / completed:.1%})" if completed else ""
    lines = [
        f"Tool failures — {report['generatedAt']}",
        f"{errors} error responses / {completed} completed journaled calls{percent}",
    ]
    for game in report["games"]:
        lines.append(
            f"  {game['game']}: turn {game['latestRecordedTurn']}, {game['errorCalls']} errors, {game['rejectedEdits']} rejected edits, {game['refreshFailures']} refresh failures, {game['pendingCalls']} responses unavailable"
        )
    lines.extend(["", "Count  Category       Role           Tool / command                 Error (example ID)"])
    for group in report["groups"]:
        action = group["tool"] + ("/" + str(group["command"]) if group["command"] else "")
        lines.append(
            f"{group['count']:5}  {group['category']:13}  {str(group['role']):13}  {action:30}  {group['code']} ({group['examples'][0]})"
        )
    if report["unavailable"]:
        lines.extend(["", "INCOMPLETE: some journals could not be read.", json.dumps(report["unavailable"], indent=2)])
    lines.extend(["", "Use --id ID for exact arguments, full response and nearby calls.", *report["notes"]])
    return "\n".join(lines) + "\n"
