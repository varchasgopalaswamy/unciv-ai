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
from civ_agent.telemetry import llm_metrics

STATE_ERRORS = {
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
    if identifier.startswith("decision:"):
        return "structured"
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
    if code in {"PROVIDER_DECISION_REQUEST_LIMIT", "MODEL_REQUEST_BUDGET_EXHAUSTED"}:
        return "request_budget"
    if code in STATE_ERRORS or code.startswith("STALE_"):
        return "state_changed"
    if code in {
        "INVALID_ARGUMENT",
        "INVALID_ARGUMENTS",
        "INVALID_COMMAND_ARGUMENTS",
        "INVALID_TOOL_ARGUMENTS",
        "INVALID_MEMORY_ARGUMENTS",
        "INVALID_STRUCTURED_DECISION",
    }:
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
    if code in {"DECISION_EXECUTION_UNCERTAIN", "CODEX_DISCONNECTED", "COMMUNICATION_UNAVAILABLE", "INTERNAL_ERROR"}:
        return "transport"
    if code in {"OPTION_UNAVAILABLE", "UNIT_UNAVAILABLE", "INSUFFICIENT_GOLD", "ILLEGAL_ACTION", "NO_MOVEMENT"}:
        return "legality"
    return "other"


def decision_records(db):
    if "decision_journal" not in inspect(db).get_table_names():
        return []
    return [json.loads(value) for value in db.scalars(select(tables.decision_journal.c.body))]


def decision_usage(decisions):
    """Actual reported usage, keeping unknown usage and wire sizes separate."""
    groups: dict[tuple, JsonObject] = {}
    for decision in decisions:
        model = decision.get("model") or {}
        key = (
            decision["purpose"],
            model.get("provider"),
            model.get("model"),
            decision["schema"]["fingerprint"],
            decision.get("modelPhase", decision["purpose"]),
        )
        row = groups.setdefault(
            key,
            {
                "role": key[0],
                "provider": key[1],
                "model": key[2],
                "modelPhase": key[4],
                "schema": decision["schema"],
                "decisionRequests": 0,
                "usageReportedRequests": 0,
                "usageUnknownRequests": 0,
                "repairRequests": 0,
                "providerAttempts": 0,
                "compactionAttempts": 0,
                "providerAttemptsUnknownDecisions": 0,
                "policyRejections": 0,
                "policyRetryAttempts": 0,
                "unreportedRejectedAttempts": 0,
                "inputTokens": 0,
                "cachedInputTokens": 0,
                "uncachedInputTokens": 0,
                "outputTokens": 0,
                "repairInputTokens": 0,
                "repairOutputTokens": 0,
                "inputCharacters": 0,
                "outputCharacters": 0,
                "instructionCharacterSizes": [],
                "initialObservationCharacterSizes": [],
            },
        )
        row["decisionRequests"] += 1
        row["repairRequests"] += bool(decision.get("repair"))
        rejections = decision.get("providerRejections", {})
        row["policyRejections"] += len(rejections)
        row["policyRetryAttempts"] += sum(bool(rejection.get("retryRequestId")) for rejection in rejections.values())
        # The successful response's token totals do not describe rejected HTTP
        # requests. Keep their unknown usage explicit rather than assume zero.
        row["unreportedRejectedAttempts"] += len(rejections)
        attempts = decision.get("providerRequests")
        if attempts is None:
            row["providerAttemptsUnknownDecisions"] += 1
        else:
            row["providerAttempts"] += len(attempts)
            row["compactionAttempts"] += sum(kind == "compaction" for kind in attempts.values())
        row["inputCharacters"] += decision.get("inputCharacters", 0)
        row["outputCharacters"] += decision.get("outputCharacters", 0)
        for source, target in (
            ("instructionCharacters", "instructionCharacterSizes"),
            ("initialObservationCharacters", "initialObservationCharacterSizes"),
        ):
            size = decision.get(source)
            if size is not None and size not in row[target]:
                row[target] = sorted([*row[target], size])
        usage = decision.get("usage", {})
        if not {"inputTokens", "cachedInputTokens", "outputTokens"} <= usage.keys():
            row["usageUnknownRequests"] += 1
            continue
        row["usageReportedRequests"] += 1
        for field in ("inputTokens", "cachedInputTokens", "outputTokens"):
            row[field] += usage[field]
        row["uncachedInputTokens"] += max(0, usage["inputTokens"] - usage["cachedInputTokens"])
        if decision.get("repair"):
            row["repairInputTokens"] += usage["inputTokens"]
            row["repairOutputTokens"] += usage["outputTokens"]
    return list(groups.values())


def provider_counts(database, selected_jobs):
    """Keep observed transport attempts distinct from provider response counts."""
    path = database.parent.parent / "telemetry" / "accounting.sqlite3"
    if not path.exists():
        return []
    groups = Counter()
    with read_database(path) as db:
        if "llm_metrics" not in inspect(db).get_table_names():
            return []
        for row in db.execute(select(llm_metrics).where(llm_metrics.c.job.in_(selected_jobs))).mappings():
            value = json.loads(row["payload"])
            if value["metric"] not in {
                "codex.api_request.duration_ms",
                "codex.websocket.request.duration_ms",
                "codex.responses_api_inference_time.duration_ms",
                "codex.tool.call.duration_ms",
            }:
                continue
            groups[(value.get("purpose"), value.get("model"), value["metric"])] += value["count"]
    return [
        {"role": role, "model": model, "metric": metric, "count": count}
        for (role, model, metric), count in groups.items()
    ]


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


def structured_failure(game, database, decision, state, issue, group, diagnostic, *, request=None):
    settings = decision.get("model") or {}
    return {
        "id": _identity(database, decision["job"], request or decision["id"], state, None),
        "game": str(game),
        "civilization": database.parent.parent.name,
        "role": decision["purpose"],
        "modelPhase": decision.get("modelPhase", decision["purpose"]),
        "turn": decision.get("gameTurn"),
        "epoch": decision["epoch"],
        "model": settings.get("model"),
        "provider": settings.get("provider"),
        "reasoningEffort": settings.get("reasoningEffort"),
        "jobStatus": None,
        "job": decision["job"],
        "call": None,
        "source": "structured",
        "tool": "structured_decision",
        "command": None,
        "kind": state,
        "code": issue,
        "category": group,
        "arguments": None,
        "diagnostic": diagnostic,
        "schemaFingerprint": decision["schema"]["fingerprint"],
        "evidence": {"database": str(database), "decisionId": decision["id"]},
    }


def failure_report(
    roots: list[Path],
    *,
    role: str | None = None,
    tool: str | None = None,
    code: str | None = None,
    since_turn: int | None = None,
    include_internal: bool = False,
) -> JsonObject:
    failures, games, unavailable, generations, metrics = [], [], [], [], []
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
                    decisions = decision_records(db)
                    generations.extend(
                        d
                        for d in decisions
                        if (role is None or d["purpose"] == role)
                        and (since_turn is None or d.get("gameTurn") is not None and d["gameTurn"] >= since_turn)
                    )
                    metrics.extend(
                        provider_counts(
                            database,
                            [
                                d["job"]
                                for d in decisions
                                if (role is None or d["purpose"] == role)
                                and (
                                    since_turn is None or d.get("gameTurn") is not None and d["gameTurn"] >= since_turn
                                )
                            ],
                        )
                    )
                    correlation = {
                        child: decision for decision in decisions for child in decision.get("operationIds", [])
                    }
                    for decision in decisions:
                        if role and decision["purpose"] != role or tool and tool != "structured_decision":
                            continue
                        if since_turn is not None and (
                            decision.get("gameTurn") is None or decision["gameTurn"] < since_turn
                        ):
                            continue
                        state = decision["status"]
                        if code in {None, "PROMPT_POLICY_REJECTED"}:
                            for request, rejection in decision.get("providerRejections", {}).items():
                                failures.append(
                                    structured_failure(
                                        game,
                                        database,
                                        decision,
                                        "provider_rejection",
                                        "PROMPT_POLICY_REJECTED",
                                        "provider_policy",
                                        {"executionStatus": state, "requestId": request, **rejection},
                                        request=request,
                                    )
                                )
                                counts["providerRejections"] += 1
                        if state not in {
                            "generation_failed",
                            "validation_failed",
                            "uncertain",
                            "partial",
                            "rejected",
                            "report_rejected",
                        }:
                            continue
                        issue = (
                            decision.get("error")
                            or (decision.get("result") or {}).get("error")
                            or {
                                "partial": "PARTIAL_BATCH",
                                "validation_failed": "INVALID_STRUCTURED_DECISION",
                            }.get(state, "EXECUTION_UNCERTAIN")
                        )
                        policy_rejected = (
                            state == "generation_failed"
                            and (decision.get("modelFailure") or {}).get("reason") == "promptPolicyRejected"
                        )
                        if policy_rejected:
                            issue = "PROMPT_POLICY_REJECTED"
                            if decision.get("providerRejections"):
                                # The terminal wrapper describes the last HTTP
                                # rejection already counted by its request ID.
                                continue
                        if code and code != issue:
                            continue
                        failures.append(
                            structured_failure(
                                game,
                                database,
                                decision,
                                state,
                                issue,
                                "partial_batch"
                                if state == "partial"
                                else "provider_policy"
                                if policy_rejected
                                else decision.get("category", "transport"),
                                {
                                    "executionStatus": state,
                                    "operationIds": decision.get("operationIds", []),
                                    "unexecuted": (decision.get("result") or {}).get("unexecuted", []),
                                    **{
                                        key: decision[key]
                                        for key in ("modelFailure", "requestFailure")
                                        if key in decision
                                    },
                                },
                            )
                        )
                        counts["decisionIssues"] += 1
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
                        decision = correlation.get(row["id"])
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
                                    "modelPhase": (decision or {}).get("modelPhase", row["purpose"]),
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
                                    "schemaFingerprint": (decision or {}).get("schema", {}).get("fingerprint"),
                                    "decisionId": (decision or {}).get("id"),
                                    "executionStatus": "rejected"
                                    if kind == "rejected_edit" or result.get("applied") is False
                                    else "confirmed"
                                    if result.get("applied") is True or "error" not in result
                                    else "unknown",
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
        "structuredUsage": decision_usage(generations),
        "providerCounts": metrics,
        "unavailable": unavailable,
        "notes": [
            "Read-only snapshots of durable gateway receipts, deduplicated by database/job/call; replays do not add failures.",
            "Bundle children are counted instead of their enclosing result. Startup/host reads are excluded unless requested; source is inferred from saved call IDs, with unfamiliar IDs marked unknown.",
            "Pending or interrupted calls without a response are not assumed to have failed. Applied/unknown execution must be reconciled before retrying.",
            "Rejected memory edits and unavailable state refreshes are separate from error responses. No later success is assumed to resolve an earlier failure.",
            "Structured decision journals correlate generation, validation, dispatch and native receipts by decision/operation IDs and schema fingerprints. Partial batches are separate incidents, not additional failed child calls. Legacy runtimes have only gateway receipts. Unreported provider usage remains unknown.",
            "Structured usage uses cumulative thread-token differences. decisionRequests counts host turns; providerAttempts counts durable gate reservations, including compactions and failed attempts. Reservations interrupted before transmission may conservatively count an unsent attempt. Historical decisions without gate records have unknown attempt counts. Character/byte counts are sizes, not token measurements. Usage is grouped independently of tool/code filters.",
            "providerCounts contains Codex transport/timing series, which can include local rejected requests or WebSocket probes. These are not forwarded-provider counts. Completed response counts are reconciled separately from provider traces; unreported usage remains unknown.",
            "Role/tool/turn filters apply to call counts; the code filter selects issues only. Databases are read independently while games continue.",
        ],
    }


def failure_detail(report: JsonObject, identifier: str) -> JsonObject:
    found = next((item for item in report["failures"] if item["id"] == identifier), None)
    if found is None:
        raise ValueError("No recorded failure matches that ID and selection")
    evidence = found["evidence"]
    with read_database(Path(evidence["database"])) as db:
        if "decisionId" in evidence:
            value = db.scalar(
                select(tables.decision_journal.c.body).where(tables.decision_journal.c.id == evidence["decisionId"])
            )
            return {**found, "decision": json.loads(value)}
        condition = (tables.calls.c.job == found["job"]) & (tables.calls.c.id == found["call"])
        row = db.execute(select(tables.calls).where(condition)).mappings().one()
        native_id = "r" + hashlib.sha256((found["job"] + ":" + found["call"]).encode()).hexdigest()[:24]
        native = db.execute(select(tables.outbox).where(tables.outbox.c.id == native_id)).mappings().one_or_none()
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
            "nativeReceipt": {
                "requestId": native["id"],
                "transport": native["transport"],
                "payload": json.loads(native["payload"]),
                "result": json.loads(native["result"]) if native["result"] is not None else None,
            }
            if native is not None
            else None,
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
