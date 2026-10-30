"""Deterministic history from committed memory and receipts, independent of model prose."""

from sqlalchemy import and_, or_, select

from civ_agent import memory_schema as tables
from civ_agent.memory_records import MEMORY_TYPES, TurnSummary, record_text
from civ_agent.memory_snapshots import load_snapshot, record_bodies
from civ_agent.memory_store import RecordStore
from civ_agent.protocol import JsonObject


def action_text(command, receipt, settlement=None, *, transport):
    native = transport == "player_command"
    confirmed = receipt and "error" not in receipt
    if native and confirmed:
        return receipt["actionSummary"]
    # Social offers also have a `kind` (peace or other). The journal's
    # transport identifies the response contract, not the payload's field names.
    kind = command["kind"] if native else command["op"]
    target = command.get("target") or command.get("partner") or command.get("to") or ""
    selection = command.get("selection") or ""
    if kind in {"trade_offer", "trade_decide", "friendship_offer"}:
        result = settlement or receipt or {}
        trade = result.get("trade") or {}
        if trade.get("status") == "ENACTED" and (trade.get("outcome") or result.get("outcome")):
            return "Engine agreement enacted"
        return f"Diplomatic proposal {trade.get('status', 'recorded').lower()}"
    if kind in {"offer", "revise"} and confirmed:
        offer = receipt["offer"]
        verb = "Offered" if kind == "offer" else "Revised"
        participants = ", ".join(offer["participants"])
        return f"{verb} {offer['kind']} commitment {offer['proposal']} v{offer['version']} between {participants}"
    label = " ".join(part for part in (kind.replace("_", " ").capitalize(), str(target), str(selection)) if part)
    return f"Requested {label}" if native or not confirmed else label


def action_record(key, decision, *, transport) -> JsonObject:
    intent = decision.get("intent") or {}
    reason = intent.get("reason", "")
    plan = (decision.get("_pinned") or {}).get("cityPlan")
    return {
        "decision": key,
        "action": action_text(
            decision["command"], decision["receipt"], decision.get("settlement"), transport=transport
        ),
        "reason": reason,
        **({"cityPlan": {"id": plan["key"], "version": plan["version"]}} if plan else {}),
        "outcome": decision["outcome"],
        "command": decision["command"],
        "receipt": decision["receipt"],
        "settlement": decision.get("settlement"),
        **({"initiativeDecisions": decision["initiativeDecisions"]} if decision.get("initiativeDecisions") else {}),
    }


def build_summary(db, *, turn=None, job=None):
    return SummaryReader(db, [(turn, job)]).build(turn, job)


class SummaryReader:
    """Read shared evidence once for a batch; fetch revision bodies only on demand."""

    def __init__(self, db, targets):
        self.db = db
        turns = [turn for turn, job in targets if job is None]
        jobs = [job for turn, job in targets if job is not None]
        self.jobs = list(
            db.execute(
                select(tables.jobs.c.id, tables.jobs.c.game_turn, tables.jobs.c.purpose)
                .select_from(tables.active_jobs)
                .where(
                    or_(
                        tables.jobs.c.id.in_(jobs),
                        and_(tables.jobs.c.game_turn.in_(turns), tables.jobs.c.purpose.in_(["gameplay", "economy"])),
                    )
                )
                .order_by(tables.job_order)
            ).mappings()
        )
        ids = [row["id"] for row in self.jobs]
        self.requests = list(
            db.execute(
                select(tables.outbox)
                .select_from(tables.live_requests)
                .where(tables.outbox.c.job.in_(ids))
                .order_by(tables.request_order)
            ).mappings()
        )
        self.decisions = RecordStore(db).all(keys=[row["decision"] for row in self.requests], project=False)
        # Follow only keys written by the selected jobs. A single recursive walk
        # serves the whole batch, and discarded branches remain unreachable.
        keys = select(tables.records.c.key).where(tables.records.c.job.in_(ids))
        reachable = select(tables.heads).where(tables.heads.c.key.in_(keys)).cte("summary_reachable", recursive=True)
        reachable = reachable.union(
            select(tables.records.c.key, tables.records.c.parent)
            .select_from(tables.record_versions(reachable))
            .where(tables.records.c.parent.is_not(None))
        )
        self.revisions = list(
            db.execute(
                select(tables.records.c.key, tables.records.c.version, tables.records.c.category, tables.records.c.job)
                .select_from(tables.record_versions(reachable))
                .where(tables.records.c.job.in_(ids))
                .order_by(tables.record_order)
            ).mappings()
        )
        self.bodies = {}
        self.snapshots = {}

    def selected(self, turn, job):
        jobs = (
            [row for row in self.jobs if row["id"] == job]
            if job is not None
            else [row for row in self.jobs if row["purpose"] in {"gameplay", "economy"} and row["game_turn"] == turn]
        )
        ids = {row["id"] for row in jobs}
        return (
            jobs,
            [row for row in self.requests if row["job"] in ids],
            [row for row in self.revisions if row["job"] in ids],
        )

    def snapshot(self, job):
        if job not in self.snapshots:
            self.snapshots[job] = self.db.execute(
                select(tables.jobs.c.snapshot).where(tables.jobs.c.id == job)
            ).scalar_one()
        return self.snapshots[job]

    def build(self, turn, job):
        jobs, rows, revisions = self.selected(turn, job)
        actions, unresolved = [], []
        for row in rows:
            decision = self.decisions[row["decision"]]["body"]
            entry = action_record(row["decision"], decision, transport=row["transport"])
            actions.append(entry)
            if decision["outcome"] in {"pending", "uncertain", "rejected"}:
                unresolved.append(entry["action"] + ": " + decision["outcome"])
        revisions = [
            row
            for row in revisions
            if row["category"] in MEMORY_TYPES
            or (
                row["category"] == "observation"
                and row["key"].startswith("observation:")
                and row["key"] not in {"observation:game", "observation:turn", "observation:communication"}
            )
        ]
        missing = [(row["key"], row["version"]) for row in revisions if (row["key"], row["version"]) not in self.bodies]
        self.bodies.update(record_bodies(self.db, missing))
        changes, observations = {}, {}
        for row in revisions:
            body = self.bodies[row["key"], row["version"]]["body"]
            if row["category"] in MEMORY_TYPES:
                changes[row["key"]] = {
                    "id": row["key"],
                    "type": row["category"],
                    "text": record_text(body),
                    "state": body["state"],
                    "retirementReason": body.get("retirementReason"),
                    "assessment": body.get("retirementAssessment", ""),
                }
            else:
                observations[row["key"]] = {"id": row["key"], "type": "observation", "body": body}
        # Gameplay already supplies its turn. Decoding its entire saved model
        # snapshot used to serve no purpose here.
        if turn is None and jobs:
            snapshot = load_snapshot(self.db, self.snapshot(jobs[-1]["id"]))
            turn = max(
                (
                    observed_turn
                    for r in snapshot["records"].values()
                    if r["category"] == "observation"
                    and (observed_turn := r["body"].get("turn", r["body"].get("context", {}).get("turn"))) is not None
                ),
                default=None,
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
