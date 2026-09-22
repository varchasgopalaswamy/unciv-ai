"""Bounded recovery and durable diagnostics for pre-dispatch conflicts."""

import hashlib
import json

from civ_agent.memory_store import encode

REPEATED_STALE = "REPEATED_STALE_ASSUMPTIONS"
RECOVERY_INSTRUCTIONS = (
    "This operation was not applied. Follow recovery.reads, reconsider the changed evidence, and revise any "
    "flagged plan before making one fresh attempt. Skip it only if it is no longer wanted or already handled. "
    "A second stale rejection of the same operation stops the run for inspection; do not defer it to keep playing."
)


def conflict_details(db, job, keys, expected=None, cause="version_changed"):
    snapshot = json.loads(db.execute("SELECT snapshot FROM jobs WHERE id=?", (job,)).fetchone()[0])
    versions = snapshot.get("delivered", {}) if expected is None else expected
    details = []
    for key in sorted(set(keys)):
        row = db.execute("SELECT r.* FROM records r JOIN heads h USING(key,version) WHERE key=?", (key,)).fetchone()
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


def recovery_reads(db, keys, name, arguments):
    keys = set(keys)
    intent = arguments.get("intent") or {}
    references = [intent.get("plan")] + [assumption.get("source") for assumption in intent.get("assumptions", [])]
    for key in references:
        if key and db.execute("SELECT 1 FROM heads WHERE key=?", (key,)).fetchone():
            keys.add(key)
    private = sorted({key for key in keys if not key.startswith("observation:")})
    evidence = set()
    for key in private:
        row = db.execute("SELECT body FROM records JOIN heads USING(key,version) WHERE key=?", (key,)).fetchone()
        if row:
            evidence.update(ref["key"] for ref in json.loads(row[0]).get("_review", []))
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
        if not key.startswith("observation:detail/"):
            continue
        row = db.execute("SELECT body FROM records JOIN heads USING(key,version) WHERE key=?", (key,)).fetchone()
        query = json.loads(row[0]).get("query") if row else None
        if query:
            reads.append(query)
            delivered.add(key)
    # The compact overview doesn't deliver raw entity/economy evidence. Read
    # those versions explicitly, and read plans last because refreshes can flag
    # them for review. Never pretend that cached evidence was seen by the model.
    records = sorted((set(private) | evidence | keys) - delivered)
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
    """Called once in the transaction that saves a tool response, including partial memory batches."""
    row = db.execute("SELECT * FROM jobs WHERE id=?", (job,)).fetchone()
    snapshot = json.loads(row["snapshot"])
    pending = snapshot.setdefault("staleRecovery", {})
    operations = (
        [
            ({"op": change["op"], "id": change.get("id")}, outcome)
            for change, outcome in zip(arguments["changes"], result["changes"], strict=True)
        ]
        if name == "memory_update" and "changes" in result
        else [({key: value for key, value in arguments.items() if key != "intent"}, result)]
    )
    failure = None
    for operation, outcome in operations:
        key = hashlib.sha256(encode([name, operation]).encode()).hexdigest()
        stale = outcome.get("error", outcome.get("reason")) == "STALE_ASSUMPTIONS"
        if not stale:
            if outcome.get("accepted") is True or name != "memory_update" and "error" not in outcome:
                pending.pop(key, None)
            continue
        attempt = {
            "callId": call_id,
            "arguments": arguments,
            "changedRecords": outcome.get("changedRecords", []),
            "conflicts": outcome.get("conflicts", []),
            "recovery": outcome.get("recovery"),
        }
        first = pending.get(key)
        # Two edits of the same record in one batch aren't two recovery attempts.
        if first is None or first["callId"] == call_id:
            pending.setdefault(key, attempt)
            continue
        reads = [
            {"callId": item["id"], "tool": item["name"], "arguments": json.loads(item["arguments"])}
            for item in db.execute(
                "SELECT * FROM calls WHERE job=? AND result IS NOT NULL AND rowid > "
                "(SELECT rowid FROM calls WHERE job=? AND id=?) ORDER BY rowid",
                (job, job, first["callId"]),
            )
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
        db.execute("INSERT OR REPLACE INTO meta VALUES ('runFailure',?)", (encode(failure),))
        break
    db.execute("UPDATE jobs SET snapshot=? WHERE id=?", (encode(snapshot), job))
    if failure:
        return {
            **result,
            "error": REPEATED_STALE,
            "recoverable": False,
            "diagnosticId": failure["diagnosticId"],
            "instruction": "Stop. Repeated stale assumptions halted the run for operator inspection. Successful memory edits in this response remain applied.",
        }
    return result
