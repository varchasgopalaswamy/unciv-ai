"""Bounded recovery and durable diagnostics for pre-dispatch conflicts."""

import hashlib
import json

from sqlalchemy import insert, select

from civ_agent import memory_schema as tables
from civ_agent.memory_store import encode
from civ_agent.protocol import JsonObject

REPEATED_STALE = "REPEATED_STALE_ASSUMPTIONS"
RECOVERY_INSTRUCTIONS = (
    "This operation was not applied. Follow recovery.reads and reconsider the current game state before retrying. "
    "Skip it only if it is no longer wanted or already handled. Notebook revisions are not required for recovery. "
    "The host tracks retries per operation: a successful operation resets its count. A later conflict after success "
    "starts a new recovery, even for the same plan. Follow recoverable responses; do not count all stale errors "
    "in the turn yourself. Only the host declares repeated-stale failure: REPEATED_STALE_ASSUMPTIONS with "
    "recoverable=false halts the run and saves diagnostics for inspection."
)


def conflict_details(db, job, keys, expected=None, cause="version_changed"):
    snapshot = json.loads(db.execute(select(tables.jobs.c.snapshot).where(tables.jobs.c.id == job)).scalar_one())
    versions = snapshot.get("delivered", {}) if expected is None else expected
    details = []
    for key in sorted(set(keys)):
        row = (
            db.execute(select(tables.records).select_from(tables.current_records).where(tables.records.c.key == key))
            .mappings()
            .fetchone()
        )
        body = json.loads(row["body"]) if row else {}
        details.append(
            {
                "record": key,
                "cause": cause,
                "expectedVersion": versions.get(key),
                "currentVersion": row["version"] if row else None,
                "changedByJob": row["job"] if row else None,
                "state": body.get("state"),
                "needsReview": body.get("needsReview", []),
            }
        )
    return details


def recovery_reads(db, keys, name, arguments) -> JsonObject:
    keys = set(keys)
    reads = []
    agreements = sorted(key for key in keys if key.startswith("observation:agreement/"))
    reads.extend({"tool": "agreement_read", "arguments": {"agreement": key.split("/", 1)[1]}} for key in agreements)
    if "observation:agreements" in keys:
        reads.append({"tool": "agreement_list", "arguments": {}})
    if any(
        key.startswith("observation:")
        and key not in agreements
        and key not in {"observation:agreements", "observation:communication"}
        for key in keys
    ):
        reads.append({"tool": "player_turn", "arguments": {}})
    delivered = {
        "observation:overview",
        "observation:turn",
        "observation:tactics",
        "observation:policy",
        "observation:agreements",
        *agreements,
    }
    if "observation:communication" in keys:
        reads.append({"tool": "chat_read", "arguments": {}})
        delivered.add("observation:communication")
    for key in sorted(keys):
        for prefix, tool in (("observation:unit/", "unit_details"), ("observation:city/", "city_details")):
            if key.startswith(prefix):
                reads.append({"tool": tool, "arguments": {"target": key.removeprefix(prefix)}})
    for key in sorted(keys):
        if not key.startswith("observation:detail/"):
            continue
        row = db.execute(
            select(tables.records.c.body).select_from(tables.current_records).where(tables.records.c.key == key)
        ).fetchone()
        query = json.loads(row[0]).get("query") if row else None
        if query:
            reads.append(query)
            delivered.add(key)
    # The compact overview doesn't deliver raw entity/economy evidence. Read
    # those versions explicitly. Never pretend cached evidence was seen by the model.
    records = sorted(keys - delivered)
    for offset in range(0, len(records), 20):
        reads.append(
            {"tool": "memory_read", "arguments": {"ids": records[offset : offset + 20], "includeRetired": True}}
        )
    if name in {"trade_offer", "trade_decide", "friendship_offer"}:
        reads.append(
            {"tool": "trade_read", "arguments": {"proposal": arguments["proposal"]}}
            if "proposal" in arguments
            else {"tool": "trade_options", "arguments": {"partner": arguments["partner"]}}
        )
    return {"reads": reads, "instruction": RECOVERY_INSTRUCTIONS}


def record_conflicts(db, job, call_id, name, arguments, result):
    """Track command conflicts in the transaction that saves their tool response."""
    if name == "memory_update":
        # Notebook disagreements are visible local outcomes, never match failures.
        return result
    row = db.execute(select(tables.jobs).where(tables.jobs.c.id == job)).mappings().fetchone()
    snapshot = json.loads(row["snapshot"])
    pending = snapshot.setdefault("staleRecovery", {})
    operation = {key: value for key, value in arguments.items() if key != "intent"}
    key = hashlib.sha256(encode([name, operation]).encode()).hexdigest()
    failure = None
    stale = result.get("error") == "STALE_ASSUMPTIONS"
    first = pending.get(key)
    attempt = {
        "callId": call_id,
        "arguments": arguments,
        "changedRecords": result.get("changedRecords", []),
        "conflicts": result.get("conflicts", []),
        "recovery": result.get("recovery"),
    }
    if not stale:
        if "error" not in result:
            pending.pop(key, None)
    elif first is None or first["callId"] == call_id:
        pending.setdefault(key, attempt)
    else:
        reads = [
            {"callId": item["id"], "tool": item["name"], "arguments": json.loads(item["arguments"])}
            for item in db.execute(
                select(tables.calls)
                .where(
                    tables.calls.c.job == job,
                    tables.calls.c.result.is_not(None),
                    tables.call_order
                    > select(tables.call_order)
                    .where(tables.calls.c.job == job, tables.calls.c.id == first["callId"])
                    .correlate(None)
                    .scalar_subquery(),
                )
                .order_by(tables.call_order)
            ).mappings()
            if item["name"].endswith("_details")
            or item["name"]
            in {
                "player_turn",
                "player_capabilities",
                "player_result",
                "victory_progress",
                "map_region",
                "movement_preview",
                "mission_preview",
                "player_inspect",
                "memory_read",
                "memory_search",
                "history_read",
                "chat_read",
                "chat_history",
                "trade_read",
                "trade_options",
                "agreement_read",
                "agreement_list",
            }
        ]
        failure = {
            "error": REPEATED_STALE,
            "diagnosticId": hashlib.sha256(encode([job, call_id, key]).encode()).hexdigest()[:24],
            "job": job,
            "epoch": row["epoch"],
            "purpose": row["purpose"],
            "tool": name,
            "operation": operation,
            "firstAttempt": first,
            "secondAttempt": attempt,
            "interveningReads": reads,
        }
        db.execute(insert(tables.meta).values(key="runFailure", value=encode(failure)).prefix_with("OR REPLACE"))
    db.execute(tables.jobs.update().where(tables.jobs.c.id == job).values(snapshot=encode(snapshot)))
    if failure:
        return {
            **result,
            "error": REPEATED_STALE,
            "recoverable": False,
            "diagnosticId": failure["diagnosticId"],
            "instruction": "Stop. Repeated stale command state halted the run for operator inspection.",
        }
    return result
