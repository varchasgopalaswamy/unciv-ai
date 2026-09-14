"""Deterministic history from committed memory and receipts, independent of model prose."""

import json

from civ_agent.memory_records import MEMORY_TYPES, TurnSummary, record_text
from civ_agent.memory_store import REACHABLE, RecordStore


def action_text(command, receipt, settlement=None):
    kind = command.get("kind") or command.get("op", "action")
    target = command.get("target") or command.get("partner") or command.get("to") or ""
    selection = command.get("selection") or ""
    if kind == "QUEUE_CONSTRUCTION":
        return f"Queued {selection} in {target}".strip()
    if kind in {"trade_offer", "trade_decide", "friendship_offer"}:
        result = settlement or receipt or {}
        trade = result.get("trade", {})
        if trade.get("status") == "ENACTED" and (trade.get("outcome") or result.get("outcome")):
            return "Engine agreement enacted"
        return f"Diplomatic proposal {trade.get('status', 'recorded').lower()}"
    return " ".join(part for part in (kind.replace("_", " ").capitalize(), str(target), str(selection)) if part)


def action_record(key, decision):
    intent = decision.get("intent") or {}
    reason = intent.get("reason") or decision.get("_pinned", {}).get("plan", {}).get("objective", "")
    return {
        "decision": key,
        "action": action_text(decision["command"], decision["receipt"], decision.get("settlement")),
        "reason": reason,
        "outcome": decision["outcome"],
        "command": decision["command"],
        "receipt": decision["receipt"],
        "settlement": decision.get("settlement"),
    }


def build_summary(db, *, turn=None, job=None):
    where, args = ("j.id=?", (job,)) if job else ("j.game_turn=? AND j.purpose='gameplay'", (turn,))
    records = RecordStore(db).all()
    rows = db.execute(
        "SELECT o.* FROM outbox o JOIN outgoing a ON o.id=a.id JOIN jobs j ON j.id=o.job WHERE "
        + where
        + " ORDER BY o.rowid",
        args,
    ).fetchall()
    actions, unresolved = [], []
    for row in rows:
        decision = records[row["decision"]]["body"]
        entry = action_record(row["decision"], decision)
        actions.append(entry)
        if decision["outcome"] in {"pending", "uncertain", "rejected"}:
            unresolved.append(entry["action"] + ": " + decision["outcome"])
    revisions = db.execute(
        REACHABLE
        + "SELECT r.* FROM records r JOIN reachable a USING(key,version) JOIN jobs j ON j.id=r.job WHERE "
        + where
        + " ORDER BY r.rowid",
        args,
    ).fetchall()
    changes, observations = {}, {}
    for row in revisions:
        body = json.loads(row["body"])
        if row["category"] in MEMORY_TYPES:
            changes[row["key"]] = {
                "id": row["key"],
                "type": row["category"],
                "text": record_text(body),
                "state": body["state"],
                "retirementReason": body.get("retirementReason"),
                "assessment": body.get("assessment", ""),
            }
        elif (
            row["category"] == "observation"
            and row["key"].startswith("observation:")
            and row["key"] not in {"observation:game", "observation:turn", "observation:communication"}
        ):
            observations[row["key"]] = {"id": row["key"], "type": "observation", "body": body}
    jobs = db.execute(
        "SELECT j.snapshot FROM jobs j JOIN live_jobs a USING(id) WHERE " + where + " ORDER BY j.rowid", args
    ).fetchall()
    if jobs:
        snapshot = json.loads(jobs[-1][0])
        for record in snapshot.get("closingRecords", snapshot["records"]).values():
            body = record["body"]
            if record["category"] == "plan" and body.get("state") == "active":
                unresolved.append(record_text(body) + (" (needs review)" if body.get("needsReview") else ""))
        if turn is None:
            turn = next(
                (
                    r["body"].get("context", {}).get("turn")
                    for r in snapshot["records"].values()
                    if r["category"] == "observation" and "context" in r["body"]
                ),
                None,
            )
    return TurnSummary.model_validate(
        {
            "turn": turn,
            "actions": actions,
            "observations": list(observations.values()),
            "memoryChanges": list(changes.values()),
            "unresolved": list(dict.fromkeys(unresolved)),
        }
    ).model_dump(exclude_none=True) | {"turn": turn}


def summary_markdown(summary):
    lines = [f"# Turn {summary['turn']}" if summary["turn"] is not None else "# Communication"]
    candidates = []
    # Confirmed losses must remain visible even in a busy turn's short summary.
    for observation in summary["observations"]:
        if observation["id"] == "observation:tactics":
            candidates.extend(
                f"Loss (turn {loss['turn']}): {loss['text']}" for loss in observation["body"]["recentLosses"]
            )
        elif observation["body"].get("absent"):
            candidates.append(observation["id"].split("/", 1)[-1] + " is no longer owned.")
    for action in summary["actions"]:
        if action["outcome"] == "confirmed":
            candidates.append(action["action"] + (": " + action["reason"] if action["reason"] else "") + ".")
    for change in summary["memoryChanges"]:
        if change["state"] == "retired":
            candidates.append(f"Memory assessed {change['retirementReason']}: {change['text']}.")
        elif change["type"] == "belief":
            candidates.append("Belief: " + change["text"] + ".")
    for observation in summary["observations"]:
        body = observation["body"]
        if body.get("absent"):
            candidates.append(observation["id"].split("/", 1)[-1] + " is no longer owned.")
        relation = body.get("relation", {})
        if relation.get("status"):
            candidates.append(f"Observed {relation.get('civilizationId', observation['id'])}: {relation['status']}.")
    candidates.extend("Open: " + text + "." for text in summary["unresolved"])
    remaining = 200 - len(lines[0].split())
    for text in dict.fromkeys(candidates):
        text = text.rstrip(" .") + "."
        words = text.split()
        if remaining <= 1:
            break
        available = remaining - 1  # Include the Markdown bullet in the word allowance.
        lines.append("- " + " ".join(words[:available]) + ("…" if len(words) > available else ""))
        remaining -= min(available, len(words)) + 1
    return lines[0] + "\n" + ("\n" + "\n".join(lines[1:]) + "\n" if len(lines) > 1 else "")
