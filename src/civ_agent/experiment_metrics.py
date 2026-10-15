"""Reachable experiment outcomes. Action matches are correlations, not attribution."""

import hashlib
from collections import Counter

from civ_agent.diplomacy_store import event_states
from civ_agent.memory_store import RecordStore


def key(job, call_id):
    return "guidance:" + hashlib.sha256((job + ":" + call_id).encode()).hexdigest()


def exposure(memory, job, call_id, advice, startup):
    with memory.transaction() as db:
        memory._validate(db, job.id)
        RecordStore(db).write(
            key(job.id, call_id),
            "guidance",
            {
                "type": "exposure",
                "epoch": job.epoch,
                "job": job.id,
                "startup": startup,
                "revision": advice.get("revision"),
                "available": advice.get("available", False),
                "source": advice.get("source", "native"),
                "recommendations": advice.get("recommendations", []),
                "limitations": advice.get("limitations", []),
            },
            job.id,
        )


def action_match(memory, job, call_id, arguments, receipt, revision):
    if "error" in receipt or receipt.get("kind") != arguments.get("kind"):
        return
    with memory.transaction() as db:
        store = RecordStore(db)
        seen = [
            record["body"]
            for record in store.all(categories={"guidance"}).values()
            if record["body"].get("type") == "exposure"
            and record["body"].get("job") == job.id
            and record["body"].get("epoch") == job.epoch
            and record["body"].get("revision") == revision
            and record["body"].get("available")
        ]
        if not seen:
            return
        matches = set()
        for item in seen:
            for advice in item["recommendations"]:
                command = advice.get("command") or {}
                expected = command.get("arguments", {})
                if (
                    command.get("tool") == "player_command"
                    and expected
                    and all(arguments.get(k) == v for k, v in expected.items())
                ):
                    matches.add(advice["id"])
        store.write(
            key(job.id, call_id),
            "guidance",
            {
                "type": "action_match",
                "epoch": job.epoch,
                "job": job.id,
                "revision": revision,
                "kind": arguments.get("kind"),
                "matchedRecommendations": sorted(matches),
                "interpretation": "Observed action overlap only; not evidence that advice caused the action.",
            },
            job.id,
        )


def summarize(memory):
    """Only current record heads count; discarded future outcomes are excluded."""
    with memory.transaction() as db:
        records = RecordStore(db).all(categories={"guidance", "routing", "delivery"}).values()
        return summarize_records(records, event_states(memory, db))


def summarize_records(records, states):
    """Pure aggregation shared by the service and read-only operator status."""
    counts = Counter()
    for state in states.values():
        if state.get("choice"):
            counts["incoming_" + state["choice"]] += 1
    delays = []
    for record in records:
        body = record["body"]
        if record["category"] == "guidance":
            counts[body["type"]] += 1
            if body["type"] == "exposure":
                counts["recommendations_exposed"] += len(body["recommendations"])
                counts["advice_unavailable"] += not body["available"]
            else:
                counts["actions_matching_advice"] += bool(body["matchedRecommendations"])
        elif record["category"] == "delivery":
            counts["delivery_" + body["status"]] += 1
            if body.get("override"):
                counts["override_" + body["override"]] += 1
            if "queueDelaySeconds" in body:
                delays.append(body["queueDelaySeconds"])
            if body.get("decision", {}).get("choice") == "omit":
                counts["omissions"] += 1
        else:
            # Coalesced classifications can cover the same event repeatedly.
            # Count its effective disposition above, not once per decision.
            counts["incoming_routing_decisions"] += 1
        reason = body.get("decision", {}).get("fallback")
        if reason:
            counts["fallback_" + reason] += 1
    return {
        "reachableOutcomes": dict(counts),
        "queueDelaySeconds": delays,
        "interpretation": "Action matches do not establish advice causality. Classifier billing is accounted separately.",
    }
