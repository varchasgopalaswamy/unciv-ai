"""Transactional private memory. Markdown is an export, never an input authority.

All methods are short synchronous transactions, called on ServiceIO's worker.
Model computation and HTTPS requests happen outside these transactions.
"""

import hashlib
import json
import re
import secrets
import time
from collections.abc import Callable
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from sqlalchemy import bindparam, case, func, insert, inspect, or_, select

from civ_agent import memory_schema as tables
from civ_agent.agreement_memory import affected_subjects, lifecycle_terms
from civ_agent.conversation_context import conversation_context
from civ_agent.decision_context import choice_versions, decision_feedback, intent_feedback
from civ_agent.memory_exports import export_files, notebook_files
from civ_agent.memory_history import action_record, action_text, build_summary
from civ_agent.memory_records import (
    MEMORY_TYPES,
    HistoryReadResult,
    Intent,
    MemoryUpdate,
    public_record,
)
from civ_agent.memory_snapshots import dump_snapshot, expand, job_states, load_snapshot
from civ_agent.memory_store import EvidenceRequired, RecordStore, encode, expired_at, select_context, semantic
from civ_agent.notebook import Notebook
from civ_agent.observations import DetailQuery, detail_fingerprint, native_observation, project_detail
from civ_agent.progress_history import HISTORY_TURNS, advance_history, progress_context
from civ_agent.protocol import JsonObject
from civ_agent.review_store import latest_advice, latest_review_status
from civ_agent.schemas import validate
from civ_agent.sqlite_database import sqlite_engine
from civ_agent.tls import private_directory, write_private_json
from civ_agent.turn_changes import turn_basis, turn_changes

# Native offers belong to gameplay even when another message starts a conversation.
COMMUNICATION_EVENTS = or_(
    tables.events.c.kind != "trade",
    func.json_extract(tables.events.c.body, "$.body.mode").is_distinct_from("NATIVE_AI"),
)


class StateConflict(ValueError):
    pass


class MemoryFormatError(StateConflict):
    """An unsupported store needs a fresh match, not an implicit migration."""


def receipt_is_uncertain(result: JsonObject) -> bool:
    error = result.get("error")
    code = error.get("code") if isinstance(error, dict) else error
    return code in ("NOT_ENACTED", "COMMUNICATION_UNAVAILABLE", "INTERNAL_ERROR")


class CivilizationState:
    def __init__(self, root: Path, civilization: str, context_limit: int = 12000):
        self.root = private_directory(root)
        self.path = self.root / "memory.sqlite3"
        self.civilization = civilization
        self.routing_enabled = False
        if context_limit < 1000:
            raise ValueError("Private memory context limit must be at least 1000 characters")
        self.context_limit = context_limit
        if self.path.is_symlink():
            raise ValueError("Unsafe memory path")
        self.engine = sqlite_engine(self.path)
        with self.transaction() as db:
            exists = inspect(db).has_table("meta")
            if exists and self._meta(db, "format") != tables.FORMAT_VERSION:
                raise MemoryFormatError("Unsupported memory format; start a fresh match in a new state directory.")
            tables.metadata.create_all(db)
            existing = self._meta(db, "civilization")
            if existing is not None and existing != civilization:
                raise StateConflict("CIVILIZATION_CONFLICT")
            self._set(db, "civilization", civilization)
            self._set(db, "format", tables.FORMAT_VERSION)
            if self._meta(db, "storeId") is None:
                self._set(db, "storeId", secrets.token_hex(16))
            # A process restart never resumes a model's uncommitted computation.
            db.execute(tables.jobs.update().where(tables.jobs.c.status == "RUNNING").values(status="INTERRUPTED"))
        self.path.chmod(0o600)

    @contextmanager
    def transaction(self):
        with self.engine.begin() as db:
            yield db

    @staticmethod
    def _meta(db, key, default=None) -> Any:
        row = db.execute(select(tables.meta.c.value).where(tables.meta.c.key == key)).fetchone()
        return json.loads(row[0]) if row else default

    @staticmethod
    def _set(db, key, value):
        db.execute(insert(tables.meta).values(key=key, value=encode(value)).prefix_with("OR REPLACE"))

    def bind(self, epoch: str):
        with self.transaction() as db:
            if self._meta(db, "epoch", epoch) != epoch:
                raise StateConflict("STALE_EPOCH")
            self._set(db, "epoch", epoch)

    def cursor(self) -> int:
        with self.transaction() as db:
            return self._meta(db, "cursor", 0)

    def ingest(self, epoch: str, events: list[JsonObject], cursor: int):
        """Commit authenticated events and their reconnect cursor together."""
        with self.transaction() as db:
            if self._meta(db, "paused", False):
                raise StateConflict("STATE_PAUSED")
            if self._meta(db, "epoch") != epoch:
                raise StateConflict("STALE_EPOCH")
            for event in events:
                source = epoch + ":" + str(event["id"])
                db.execute(
                    insert(tables.events)
                    .values(source=source, kind=event["kind"], body=encode(event), wake=bool(event.get("wake")))
                    .prefix_with("OR IGNORE")
                )
                inserted = db.execute(
                    insert(tables.inbox)
                    .from_select(["id"], select(tables.events.c.id).where(tables.events.c.source == source))
                    .prefix_with("OR IGNORE")
                )
                if not inserted.rowcount:
                    # Reconnect replay must not reflag plans already reconsidered.
                    # The first durable event body remains the source of truth.
                    continue
                if self.routing_enabled:
                    event_id = db.execute(
                        select(tables.events.c.id).where(tables.events.c.source == source)
                    ).scalar_one()
                    routes = self._meta(db, "routing:events", {})
                    routes[str(event_id)] = {"receivedAt": time.time()}
                    self._set(db, "routing:events", routes)
                self._advance_turn(db, event.get("turn", event.get("body", {}).get("turn")))
                # Preserve an utterance as an authenticated statement, never a
                # world observation inferred from that utterance's content.
                self._record(
                    db,
                    "event:" + str(event["id"]),
                    "observation" if event["kind"] in {"engine", "agreement", "diplomacy"} else "statement",
                    event,
                )
                if event["kind"] in {"engine", "consent", "diplomacy"}:
                    body = event.get("body", {})
                    participants = body.get("participants", [])
                    subjects = set(participants)
                    if event["kind"] == "diplomacy":
                        subjects.update(
                            value for key in ("author", "target") if isinstance(value := body.get(key), str)
                        )
                    if event["kind"] == "engine":
                        for war in body.get("wars", []):
                            subjects.update((war["declaringCivilization"], war["targetCivilization"]))
                    label = next(
                        (
                            body[field]
                            for field in ("action", "kind", "event", "decision", "status")
                            if isinstance(body.get(field), str)
                        ),
                        event["kind"],
                    )
                    if body.get("status") and label != body["status"]:
                        label += " (" + body["status"] + ")"
                    RecordStore(db, self._meta(db, "gameTurn")).flag(
                        subjects=sorted(subjects),
                        reason=label + ": " + ", ".join(sorted(subjects)),
                        review_keys=["event:" + str(event["id"])],
                    )
                elif event["kind"] == "agreement":
                    body = event.get("body", {})
                    store = RecordStore(db, self._meta(db, "gameTurn"))
                    keys = []
                    if isinstance(body.get("agreement"), str):
                        listing = store.all().get("observation:agreements")
                        if listing is not None:
                            versions = {
                                row["agreement"]: row.get("version", 0) for row in listing["body"].get("agreements", [])
                            }
                            required = listing["body"].get("latestVersions", {})
                            latest = max(versions.get(body["agreement"], 0), required.get(body["agreement"], 0))
                            if body.get("version", 0) > latest:
                                keys.append("observation:agreements")
                                store.write(
                                    "observation:agreements",
                                    "observation",
                                    {
                                        **listing["body"],
                                        "requiresRefresh": True,
                                        "latestVersions": {**required, body["agreement"]: body["version"]},
                                    },
                                )
                        key = "observation:agreement/" + body["agreement"]
                        keys.append(key)
                        prior = store.all().get(key)
                        if prior is not None:
                            native_version = max(
                                prior["body"].get("agreement", {}).get("version", 0),
                                prior["body"].get("latestVersion", 0),
                            )
                            if body.get("version", 0) <= native_version:
                                # A read can race ahead of subscription delivery.
                                store.flag(
                                    keys=keys[:-1],
                                    reason=f"Agreement event {event['id']} needs review",
                                    review_keys=["event:" + str(event["id"])],
                                )
                                continue
                            store.write(
                                key,
                                "observation",
                                {
                                    **prior["body"],
                                    "requiresRefresh": True,
                                    "latestVersion": body["version"],
                                    "changedBy": "event:" + str(event["id"]),
                                },
                            )
                    if isinstance(body.get("partner"), str):
                        keys.append("observation:trade_options/" + body["partner"])
                    store.flag(
                        keys=keys,
                        subjects=sorted(affected_subjects(body)),
                        reason=f"Agreement event {event['id']} needs review",
                        review_keys=["event:" + str(event["id"])],
                    )
            self._set(db, "cursor", max(cursor, self._meta(db, "cursor", 0)))

    @staticmethod
    def _record(db, key, category, body, job=None):
        return RecordStore(db).write(key, category, body, job)

    def _advance_turn(self, db, turn):
        if type(turn) is int and turn >= 0:
            self._set(db, "gameTurn", max(turn, self._meta(db, "gameTurn", 0)))

    def observe(self, key: str, body: JsonObject, job: str | None = None):
        with self.transaction() as db:
            return self._observe(db, key, body, job)

    def observe_detail(self, key: str, body: JsonObject, job: str, fingerprint: str | None = None):
        """Return a short, immutable evidence handle for exactly this delivered revision."""
        with self.transaction() as db:
            version = self._observe(db, key, body, job)
            canonical = "observation:" + key
            cache = self._meta(db, "detailInputs", {})
            cache[canonical] = {"version": version, "fingerprint": fingerprint}
            self._set(db, "detailInputs", cache)
            record_id = db.execute(
                select(tables.record_order).where(
                    tables.records.c.key == canonical, tables.records.c.version == version
                )
            ).scalar_one()
            handle = "e" + str(record_id)
            snapshot = load_snapshot(
                db, db.execute(select(tables.jobs.c.snapshot).where(tables.jobs.c.id == job)).scalar_one()
            )
            snapshot.setdefault("historicalEvidence", {})[handle] = {
                "key": canonical,
                "version": version,
                "category": "observation",
            }
            db.execute(tables.jobs.update().where(tables.jobs.c.id == job).values(snapshot=dump_snapshot(db, snapshot)))
            return version, {**body, "evidence": handle}

    def _observe(self, db, key, body, job):
        body = native_observation(body)
        if self._meta(db, "paused", False):
            raise StateConflict("STATE_PAUSED")
        if job is not None:
            self._validate(db, job)
        self._advance_turn(db, body.get("turn", body.get("context", {}).get("turn")))
        key = "observation:" + key
        prior = RecordStore(db).all(keys=[key]).get(key)
        if (
            key.startswith("observation:agreement/")
            and prior
            and body.get("agreement", {}).get("version", 0)
            < max(prior["body"].get("agreement", {}).get("version", 0), prior["body"].get("latestVersion", 0))
        ):
            raise StateConflict("AGREEMENT_CHANGED: refresh with agreement_read")
        if key == "observation:agreements" and prior:
            versions = {row["agreement"]: row.get("version", 0) for row in body.get("agreements", [])}
            if any(versions.get(id, 0) < version for id, version in prior["body"].get("latestVersions", {}).items()):
                raise StateConflict("AGREEMENT_CHANGED: refresh with agreement_list")
        version = self._record(db, key, "observation", body, job)
        self._changed_observation(db, key, body, prior)
        if key.startswith("observation:trade/"):
            self._settle_trade(db, body)
        if job is not None:
            snapshot = load_snapshot(
                db, db.execute(select(tables.jobs.c.snapshot).where(tables.jobs.c.id == job)).scalar_one()
            )
            RecordStore.deliver(snapshot, {key: {"version": version, "category": "observation", "body": body}})
            db.execute(tables.jobs.update().where(tables.jobs.c.id == job).values(snapshot=dump_snapshot(db, snapshot)))
        return version

    def snapshot(self, purpose: str, game_turn: int | None = None) -> JsonObject:
        with self.transaction() as db:
            records = RecordStore(db).all(categories=MEMORY_TYPES | {"contribution"})
            cursor = self._meta(db, "handled:" + purpose, 0)
            query = select(tables.events.c.id, tables.events.c.body).select_from(tables.events.join(tables.inbox))
            if purpose == "communication" and self.routing_enabled:
                from civ_agent.diplomacy_store import attention_ids, ready_ids

                query = query.where(tables.events.c.id.in_(attention_ids(self, db))).order_by(
                    case((tables.events.c.id.in_(ready_ids(self, db)), 0), else_=1)
                )
            else:
                query = query.where(tables.events.c.id > cursor)
            if purpose == "communication":
                query = query.where(COMMUNICATION_EVENTS)
            rows = db.execute(query.order_by(tables.events.c.id).limit(50)).mappings().all()
            events = [json.loads(row["body"]) for row in rows]
            records.update(RecordStore(db).all(keys=[f"event:{event['id']}" for event in events]))
            high = max((row["id"] for row in rows), default=cursor)
            snapshot = {
                "epoch": self._meta(db, "epoch"),
                "purpose": purpose,
                "gameTurn": game_turn if game_turn is not None else self._meta(db, "gameTurn"),
                "strategicAdvice": latest_advice(
                    db, game_turn if game_turn is not None else self._meta(db, "gameTurn")
                ),
                "strategicReviewStatus": latest_review_status(
                    db, game_turn if game_turn is not None else self._meta(db, "gameTurn")
                ),
                "records": records,
                "contributionIds": [key for key, r in records.items() if r["category"] == "contribution"],
                "events": events,
                "cursor": max(cursor, high),
                **({"routingEventIds": [row["id"] for row in rows]} if self.routing_enabled else {}),
            }
            selected, omitted = select_context(snapshot, self.context_limit)
            snapshot["contextIds"] = list(selected)
            snapshot["omitted"] = omitted
            delivered = {**selected, **{f"event:{event['id']}": records[f"event:{event['id']}"] for event in events}}
            RecordStore.deliver(snapshot, delivered)
            return snapshot

    def _changed_observation(self, db, key, value, prior):
        change = self._observation_change(key, value, prior)
        if change:
            if key in {"observation:economy", "observation:victory_progress"}:
                change["strategies"] = list(RecordStore(db).all(categories={"strategy"}))
            RecordStore(db, self._meta(db, "gameTurn")).flag_many([change])

    @staticmethod
    def _observation_change(key, value, prior):
        if not prior or semantic(prior["body"]) == semantic(value):
            return
        if key in {"observation:game", "observation:overview", "observation:turn", "observation:tactics"}:
            return  # Material changes are identified below, not every changed counter.
        subjects = []
        reason = None
        if key.startswith("observation:agreement/"):
            before, after = prior["body"].get("agreement", {}), value.get("agreement", {})
            if lifecycle_terms(before) == lifecycle_terms(after):
                return
            subjects.extend(affected_subjects({**after, "before": before, "after": after}))
            reason = f"Agreement {after.get('agreement')}: terms or status changed ({after.get('status')})"
        if key == "observation:economy":
            before, after = prior["body"].get("economy") or {}, value.get("economy") or {}
            old = {r["name"]: r["available"] for r in before.get("resources", [])}
            new = {r["name"]: r["available"] for r in after.get("resources", [])}
            lost = sorted(name for name, amount in old.items() if amount > 0 and new.get(name, 0) <= 0)
            changes = ["Resources no longer available: " + ", ".join(lost)] if lost else []
            if before.get("happiness", 0) >= 0 > after.get("happiness", 0):
                changes.append(f"Happiness fell below zero ({after['happiness']})")
            if (before.get("goldenAge") or {}).get("turnsRemaining", 0) > 0 and (after.get("goldenAge") or {}).get(
                "turnsRemaining", 0
            ) == 0:
                changes.append("Golden Age ended")
            if not changes:
                return
            reason = "; ".join(changes)
            subjects.extend(["economy", *lost])
        if key == "observation:victory_progress":

            def milestones(body):
                result = {}
                for path in body.get("victories", []):
                    for milestone in path.get("milestones", []):
                        name = path["name"] + ": " + milestone["description"]
                        result[name] = milestone["status"]
                        for part in milestone.get("parts", []):
                            result[path["name"] + ": " + part["name"]] = f"{part['completed']}/{part['required']}"
                        for objective in milestone.get("objectives", []):
                            result[path["name"] + ": " + str(objective.get("name") or objective["kind"])] = objective[
                                "completed"
                            ]
                for rival in body.get("rivals", []):
                    result[rival["civilization"] + ": defeated"] = rival["defeated"]
                    for path, milestone in rival.get("nextMilestones", {}).items():
                        result[rival["civilization"] + ": " + path] = milestone
                return result

            before, after = milestones(prior["body"]), milestones(value)
            if before == after:
                return
            # Section-filtered queries are different views, not progress events.
            if prior["body"].get("sections") != value.get("sections") or prior["body"].get("query") != value.get(
                "query"
            ):
                return
            changed = [
                f"{name}: {before.get(name)} → {after.get(name)}"
                for name in sorted(before.keys() | after.keys())
                if before.get(name) != after.get(name)
            ]
            reason = "Victory progress: " + "; ".join(changed[:4])
        if key.startswith("observation:diplomacy/"):
            before, after = prior["body"].get("relation", {}), value.get("relation", {})
            fields = [
                field
                for field in ("atWar", "status", "relationship", "ally", "defeated")
                if before.get(field) != after.get(field)
            ]
            if not fields:
                return
            reason = (
                key.split("/", 1)[1]
                + ": "
                + "; ".join(f"{field} {before.get(field)} → {after.get(field)}" for field in fields)
            )
            subjects.append(key.split("/", 1)[1])
        if value.get("absent"):
            reason = key + " is no longer present in the owned view"
            subjects.append(key.split("/", 1)[-1])
        if key == "observation:communication":

            def relations(body):
                context = body.get("context", {})
                diplomacy = context.get("diplomacy", {})
                entries = diplomacy.get("knownCivilizations", []) if isinstance(diplomacy, dict) else diplomacy
                entries = context.get("knownCivilizations", entries)
                return {
                    r["civilizationId"]: {field: r.get(field) for field in ("atWar", "status", "defeated")}
                    for r in entries
                    if "civilizationId" in r
                }

            before, after = relations(prior["body"]), relations(value)
            subjects.extend(civ for civ in before.keys() | after.keys() if before.get(civ) != after.get(civ))
            # Ledger revisions identify parties, not unrelated chat traffic.
            old = {offer["proposal"]: offer for offer in prior["body"].get("offers", [])}
            for offer in value.get("offers", []):
                previous = old.get(offer["proposal"], {})
                if offer.get("effective") != previous.get("effective"):
                    subjects.extend(offer.get("participants", []))
            if not subjects:
                return
            reason = "Diplomatic relations or social commitment status changed: " + ", ".join(sorted(set(subjects)))
        if reason is None:
            fields = sorted(
                field
                for field in value.keys() | prior["body"].keys()
                if semantic(value.get(field)) != semantic(prior["body"].get(field))
                and field not in {"turn", "revision", "epoch", "observedTurn"}
            )
            reason = key + " changed fields: " + ", ".join(fields[:8])
        return dict(keys=[key], subjects=subjects, reason=reason)

    def observe_player(self, body: JsonObject, job: str, compact: JsonObject | None = None):
        with self.transaction() as db:
            return self._observe_player(db, body, job, compact)

    def decision_feedback(self, body: JsonObject):
        with self.transaction() as db:
            return self._decision_feedback(db, body)

    def progress_feedback(self, body: JsonObject):
        with self.transaction() as db:
            return self._progress_feedback(db, body)

    def _progress_feedback(self, db, body: JsonObject):
        history = advance_history(self._meta(db, "progressHistory", []), body)
        return progress_context(history, self._action_facts(db, history[0]["turn"], body["turn"]))

    def _action_facts(self, db, first: int, last: int):
        query = (
            select(tables.records.c.key, tables.records.c.body, tables.outbox.c.transport)
            .select_from(tables.current_records.join(tables.outbox, tables.outbox.c.decision == tables.records.c.key))
            .join(tables.outgoing, tables.outgoing.c.id == tables.outbox.c.id)
            .where(
                tables.records.c.category == "decision",
                func.json_extract(tables.records.c.body, "$.turn") >= first,
                func.json_extract(tables.records.c.body, "$.turn") <= last,
            )
            .order_by(tables.request_order)
        )
        actions = []
        for row in db.execute(query).mappings():
            saved = json.loads(row["body"])
            actions.append(
                {
                    "turn": saved["turn"],
                    "commandStatus": saved["outcome"],
                    "action": action_text(
                        saved["command"], saved.get("receipt"), saved.get("settlement"), transport=row["transport"]
                    ),
                    "command": saved["command"],
                    "receipt": row["key"],
                    "economy": (saved.get("receipt") or {}).get("economy"),
                    "_pinned": saved.get("_pinned", {}),
                }
            )
        return actions

    def _decision_feedback(self, db, body: JsonObject):
        history = (
            select(tables.records.c.key, tables.records.c.version)
            .select_from(tables.current_records)
            .where(tables.records.c.category == "strategy")
            .cte("strategy_history", recursive=True)
        )
        history = history.union(
            select(tables.records.c.key, tables.records.c.parent)
            .select_from(tables.record_versions(history))
            .where(tables.records.c.parent.is_not(None))
        )
        query = (
            select(tables.records.c.key, tables.records.c.version, tables.records.c.body)
            .select_from(tables.record_versions(history))
            .where(tables.records.c.category == "strategy")
            .order_by(tables.record_order.desc())
            .limit(257)
        )
        rows = db.execute(query).mappings().all()
        since = db.scalar(select(func.max(tables.reviews.c.turn)).where(tables.reviews.c.status == "COMPLETED"))
        choices = choice_versions([{**row, "body": json.loads(row["body"])} for row in rows[:256]], since or 0)
        first = min(
            (c["baseline"]["authoredTurn"] for c in choices if c["baseline"].get("authoredTurn") is not None),
            default=body["turn"],
        )
        actions = self._action_facts(db, min(first, body["turn"] - HISTORY_TURNS), body["turn"])
        history = advance_history(self._meta(db, "progressHistory", []), body)
        progress = progress_context(history, [a for a in actions if a["turn"] >= history[0]["turn"]])
        result = decision_feedback(choices, body, actions, progress)
        result["earlierStrategyRevisionsOmitted"] = len(rows) > 256
        return result

    def deliver_player(
        self,
        body: JsonObject,
        job: str,
        compact: JsonObject,
        call_id: str,
        name: str,
        arguments: JsonObject,
        respond: Callable[[JsonObject, JsonObject], JsonObject],
        update_memory: bool = True,
    ):
        """Commit delivered evidence and its replayable response in one transaction."""
        with self.transaction() as db:
            purpose = db.scalar(select(tables.jobs.c.purpose).where(tables.jobs.c.id == job))
            if purpose == "gameplay" and body.get("phase") == "ACTIVE":
                baseline = self._meta(db, "turnChangeBaseline")
                if baseline and baseline["turn"] != body["turn"]:
                    self._set(db, "openingChanges", turn_changes(baseline, body))
                changes = self._meta(db, "openingChanges")
                if changes:
                    compact["changesSincePreviousTurn"] = changes
                self._set(db, "turnChangeBaseline", turn_basis(body))
            observed = self._observe_player(db, body, job, compact)
            records = RecordStore(db).all(categories=MEMORY_TYPES)
            snapshot = load_snapshot(
                db, db.execute(select(tables.jobs.c.snapshot).where(tables.jobs.c.id == job)).scalar_one()
            )
            changed = {
                key: record
                for key, record in records.items()
                if update_memory
                and record["category"] in MEMORY_TYPES
                and key in snapshot.get("delivered", {})
                and snapshot["delivered"][key] != record["version"]
            }
            RecordStore.deliver(snapshot, changed)
            compact["decisionFeedback"] = self._decision_feedback(db, body)
            # strategyReview delivers advisory notices, without delivering a new
            # notebook revision or the contents of the referenced observations.
            for key, record in records.items():
                if (
                    record["category"] == "strategy"
                    and record["body"].get("state") == "active"
                    and record["body"].get("needsReview")
                    and key in snapshot.get("delivered", {})
                ):
                    cached = snapshot["records"][key]
                    cached["body"].update(
                        {field: record["body"][field] for field in ("needsReview", "_review", "_noticeVersion")}
                    )
                    RecordStore.deliver(snapshot, {key: cached})
            db.execute(tables.jobs.update().where(tables.jobs.c.id == job).values(snapshot=dump_snapshot(db, snapshot)))
            result = respond(records, changed)
            self._call_record(db, job, call_id, name, arguments, result)
            return {**observed, **changed}, result

    def _observe_player(self, db, body: JsonObject, job: str, compact: JsonObject | None):
        """Independent evidence versions; the host still validates the full view too."""
        self._validate(db, job)
        if compact is not None:
            history = advance_history(self._meta(db, "progressHistory", []), body)
            self._set(db, "progressHistory", history)
            opening = self._meta(db, "openingProgress")
            if opening is None or opening["toTurn"] != body["turn"]:
                opening = progress_context(history, self._action_facts(db, history[0]["turn"], body["turn"]))
                opening["asOf"] = (
                    "First delivered overview this turn. Current-turn changes appear in receipts and live state; strategic reviews recompute progress."
                )
                self._set(db, "openingProgress", opening)
            # Historical context stays stable within a turn. Otherwise every
            # receipt resends unrelated units, cities and prior-turn evidence.
            compact["recentProgress"] = opening
        body = native_observation(body)
        compact = native_observation(compact)
        scopes = {
            "game": body,
            "turn": {key: body.get(key) for key in ("epoch", "turn", "phase", "pending", "alerts", "victory")},
            "economy": {"turn": body.get("turn"), "economy": body.get("economy")},
            "research": {
                key: body.get(key)
                for key in ("turn", "research", "researchQueue", "researchOptions", "freeTechnologies")
            },
            "policy": {key: body.get(key) for key in ("turn", "policyOptions", "freePolicies")},
        }
        if "tacticalReview" in body:
            scopes["tactics"] = body["tacticalReview"]
        if compact is not None:
            scopes["overview"] = compact
        for kind, entities in (("unit", body.get("map", {}).get("units", [])), ("city", body.get("cities", []))):
            for entity in entities:
                scopes[f"{kind}/{entity['handle']}"] = {"turn": body.get("turn"), "entity": entity}
        for relation in body.get("diplomacy", {}).get("knownCivilizations", []) + body.get("diplomacy", {}).get(
            "cityStates", []
        ):
            scopes["diplomacy/" + relation["civilizationId"]] = {"turn": body.get("turn"), "relation": relation}
        self._validate(db, job)
        self._advance_turn(db, body.get("turn"))
        snapshot = load_snapshot(
            db, db.execute(select(tables.jobs.c.snapshot).where(tables.jobs.c.id == job)).scalar_one()
        )
        strategies = RecordStore(db).all(categories={"strategy"})
        # Old projections remain immutable evidence until inspected again. Only
        # running readers and active strategic assumptions need live refreshes.
        inspections = set()
        for saved in db.scalars(
            select(func.json_extract(tables.jobs.c.snapshot, "$.delivered"))
            .select_from(tables.active_jobs)
            .where(
                tables.jobs.c.status == "RUNNING",
            )
        ):
            inspections.update(json.loads(saved or "{}"))
        for record in strategies.values():
            if record["category"] == "strategy" and record["body"].get("state") == "active":
                inspections.update(ref["key"] for ref in record["body"].get("_assumptions", []))
        keys = {"observation:" + scope for scope in scopes} | inspections
        # Retain enough entity history to report removals. Unused historical
        # details, receipts and chat records need neither loading nor projection.
        keys.update(
            db.scalars(
                select(tables.heads.c.key).where(
                    or_(
                        tables.heads.c.key.startswith("observation:unit/"),
                        tables.heads.c.key.startswith("observation:city/"),
                    )
                )
            )
        )
        previous_records = {**strategies, **RecordStore(db).all(keys=keys)}
        detail_cache = {k: v for k, v in self._meta(db, "detailInputs", {}).items() if k in inspections}
        for key, previous in previous_records.items():
            scope = key.removeprefix("observation:")
            if scope.startswith("detail/"):
                if key not in inspections:
                    continue
                query = DetailQuery.model_validate(previous["body"]["query"])
                fingerprint = detail_fingerprint(body, query)
                cached = {"version": previous["version"], "fingerprint": fingerprint}
                if detail_cache.get(key) == cached:
                    continue
                detail_cache[key] = cached
                if query.tool in {"movement_preview", "mission_preview", "player_inspect"} and not previous["body"].get(
                    "available"
                ):
                    continue  # An expired native preview stays expired until explicitly inspected.
                if query.tool in {"movement_preview", "mission_preview", "player_inspect"} and previous["body"].get(
                    "revision"
                ) == body.get("revision"):
                    scopes[scope] = previous["body"]
                else:
                    projected = project_detail(body, query)
                    if semantic(previous["body"]) != semantic(projected):
                        scopes[scope] = projected
            if scope.startswith(("unit/", "city/")) and scope not in scopes and not previous["body"].get("absent"):
                scopes[scope] = {"turn": body.get("turn"), "absent": True}
        observed = {}
        changes = []
        # A cached host read can invalidate assumptions without claiming the
        # model has read all of its entity details or refreshed projections.
        delivered = set(scopes) if compact is None else {"overview", "turn", "tactics", "policy"}
        for key, value in scopes.items():
            delivered_job = job if key in delivered else None
            key = "observation:" + key
            version = self._record(db, key, "observation", value, delivered_job)
            change = self._observation_change(key, value, previous_records.get(key))
            if change:
                changes.append(change)
            observed[key] = {"version": version, "category": "observation", "body": value}
            if key in detail_cache:
                detail_cache[key]["version"] = version
        self._set(db, "detailInputs", detail_cache)
        tactics = body.get("tacticalReview")
        if tactics is not None:
            previous = previous_records.get("observation:tactics", {}).get("body", {})
            old_losses = {loss["report"] for loss in previous.get("recentLosses", [])}
            if any(loss["report"] not in old_losses for loss in tactics["recentLosses"]):
                changes.append(
                    dict(
                        subjects=["defense", "economy"],
                        keys=["observation:tactics"],
                        reason=f"Turn {body.get('turn')}: new confirmed losses",
                    )
                )
            if tactics["militaryCount"] == 0 and previous.get("militaryCount") != 0:
                changes.append(
                    dict(
                        subjects=["defense"],
                        keys=["observation:tactics"],
                        reason=f"Turn {body.get('turn')}: no military units remain",
                    )
                )
            if "cityCount" in previous and tactics["cityCount"] < previous["cityCount"]:
                changes.append(
                    dict(
                        subjects=["defense", "expansion", "economy"],
                        keys=["observation:tactics"],
                        reason=f"Turn {body.get('turn')}: owned city count fell",
                    )
                )
            current_force = tactics.get("nonReconMilitaryCount")
            old_force = previous.get("nonReconMilitaryCount")
            if current_force is not None and old_force is not None:
                reason = None
                if current_force < old_force:
                    reason = (
                        f"Turn {body.get('turn')}: non-recon military count fell from {old_force} to {current_force}"
                    )
                elif (
                    tactics["cityCount"] > previous.get("cityCount", tactics["cityCount"])
                    and current_force <= old_force
                ):
                    reason = f"Turn {body.get('turn')}: owned city count grew without an increase in non-recon military"
                if reason:
                    changes.append(dict(subjects=["defense", "expansion"], keys=["observation:tactics"], reason=reason))
        # Global strategy is relevant to material changes even when the model
        # omitted subject tags. These notices report facts, never prescribe a response.
        strategies = [
            key
            for key, r in previous_records.items()
            if r["category"] == "strategy" and r["body"].get("state") == "active"
        ]
        for change in changes:
            if set(change.get("keys", ())) & {"observation:tactics", "observation:economy"} and change.get("subjects"):
                change["strategies"] = strategies
                change["keys"] = [*change["keys"], "observation:overview", "observation:game"]
        for relation in body.get("diplomacy", {}).get("knownCivilizations", []):
            key = "observation:diplomacy/" + relation["civilizationId"]
            old = previous_records.get(key, {}).get("body", {}).get("relation", {})
            if old and old.get("atWar") != relation.get("atWar"):
                changes.append(
                    dict(
                        strategies=strategies,
                        keys=[key, "observation:overview", "observation:game"],
                        reason=(
                            f"Turn {body.get('turn')}: {relation['civilizationId']} war status "
                            f"{old.get('atWar')} → {relation.get('atWar')}"
                        ),
                    )
                )
        RecordStore(db, self._meta(db, "gameTurn")).flag_many(changes)
        RecordStore.deliver(
            snapshot,
            {key: value for key, value in observed.items() if key.removeprefix("observation:") in delivered},
        )
        # Operator reports follow the latest native view independently of the
        # versions actually delivered to this conversation.
        snapshot["gameViewVersion"] = observed["observation:game"]["version"]
        db.execute(tables.jobs.update().where(tables.jobs.c.id == job).values(snapshot=dump_snapshot(db, snapshot)))
        return observed

    def has_wake(self) -> bool:
        with self.transaction() as db:
            if self.routing_enabled:
                from civ_agent.diplomacy_store import ready_ids

                return bool(ready_ids(self, db))
            game_turn = self._meta(db, "gameTurn", 0)
            if self._meta(db, "lastCompletedCommunicationTurn", -1) >= game_turn:
                return False
            return (
                db.execute(
                    select(tables.events.c.id)
                    .select_from(tables.events.join(tables.inbox))
                    .where(
                        tables.events.c.wake == 1,
                        COMMUNICATION_EVENTS,
                        tables.events.c.id
                        > max(self._meta(db, "handled:communication", 0), self._meta(db, "attempted:communication", 0)),
                    )
                    .limit(1)
                ).fetchone()
                is not None
            )

    def allow_communication_retry(self):
        with self.transaction() as db:
            self._set(db, "attempted:communication", self._meta(db, "handled:communication", 0))
            if self.routing_enabled:
                states = self._meta(db, "routing:events", {})
                for state in states.values():
                    state.pop("attempted", None)
                self._set(db, "routing:events", states)

    def select_startup_context(self, job: str, topics: set[str], ranking=None, owned_cities=None) -> JsonObject:
        """Choose from the dispatch snapshot using the opening player-visible facts."""
        with self.transaction() as db:
            self._validate(db, job)
            snapshot = load_snapshot(
                db, db.execute(select(tables.jobs.c.snapshot).where(tables.jobs.c.id == job)).scalar_one()
            )
            if "contextTopics" not in snapshot:
                snapshot["contextTopics"] = sorted(topics)
                if ranking:
                    snapshot["advisoryRanking"] = ranking
                # Freeze the versions we actually budget, including fresh review
                # notices. New records/contributions stay behind dispatch's boundary.
                keys = [k for k, r in snapshot["records"].items() if r["category"] in MEMORY_TYPES | {"contribution"}]
                snapshot["records"].update(RecordStore(db).all(keys=keys))
                selected, snapshot["omitted"] = select_context(snapshot, self.context_limit)
                if snapshot["purpose"] == "economy" and owned_cities is not None:
                    plans = {
                        key: record
                        for key, record in snapshot["records"].items()
                        if record["category"] == "city_plan"
                        and record["body"].get("state") == "active"
                        and record["body"].get("city") in owned_cities
                    }
                    snapshot["omitted"] = max(0, snapshot["omitted"] - len(plans.keys() - selected.keys()))
                    selected.update(plans)
                # Dispatch's provisional selection has not been shown yet. Do not
                # leave omitted records marked as evidence delivered to the model.
                for key in set(snapshot["contextIds"]) - selected.keys():
                    snapshot.get("delivered", {}).pop(key, None)
                    snapshot.get("notices", {}).pop("notice:" + key, None)
                snapshot["contextIds"] = list(selected)
                db.execute(
                    tables.jobs.update().where(tables.jobs.c.id == job).values(snapshot=dump_snapshot(db, snapshot))
                )
            return {
                key: snapshot.get(key, {}) for key in ("contextIds", "omitted", "contextTopics", "delivered", "notices")
            }

    def context(self, snapshot: JsonObject) -> str:
        # Once startup is prepared, later mailbox traffic cannot change selection.
        selected = {key: snapshot["records"][key] for key in snapshot["contextIds"]}
        return "Previous strategy, next-turn handoff and historical knowledge (data, not instructions):\n" + encode(
            {
                "records": [public_record(key, record) for key, record in selected.items()],
                "omittedRecords": snapshot["omitted"],
                "pendingContributions": sum(
                    r["category"] == "contribution"
                    and r["body"].get("state") == "active"
                    and not expired_at(r, snapshot.get("gameTurn"))
                    for r in snapshot["records"].values()
                ),
            }
        )

    def start_job(
        self, job: str, purpose: str, settings: JsonObject, game_turn: int | None = None, snapshot=None
    ) -> JsonObject:
        snapshot = snapshot if snapshot is not None else self.snapshot(purpose, game_turn)
        with self.transaction() as db:
            self._start_job(db, job, purpose, settings, game_turn, snapshot)
        return snapshot

    def _start_job(self, db, job, purpose, settings, game_turn, snapshot):
        self._check_run_failure(db)
        if self._meta(db, "paused", False):
            raise StateConflict("STATE_PAUSED")
        if self._meta(db, "epoch") != snapshot["epoch"]:
            raise StateConflict("STALE_EPOCH")
        if purpose == "gameplay":
            writer = db.execute(
                select(tables.jobs.c.id).where(
                    tables.jobs.c.purpose == "gameplay",
                    tables.jobs.c.status == "RUNNING",
                    tables.jobs.c.epoch == snapshot["epoch"],
                )
            ).scalar_one_or_none()
            if writer is not None:
                raise StateConflict("NOTEBOOK_WRITER_BUSY")
            # Expire temporary entries at the next gameplay writer boundary.
            RecordStore(db, game_turn if game_turn is not None else self._meta(db, "gameTurn")).expire()
            current = RecordStore(db).all(keys=snapshot.get("contextIds", []))
            for key in snapshot.get("contextIds", []):
                if key in current:
                    RecordStore.deliver(snapshot, {key: current[key]})
        db.execute(
            insert(tables.jobs).values(
                id=job,
                epoch=snapshot["epoch"],
                purpose=purpose,
                snapshot=dump_snapshot(db, snapshot),
                settings=encode(settings),
                status="RUNNING",
                game_turn=game_turn,
            )
        )
        db.execute(insert(tables.live_jobs).values(id=job))
        if purpose == "communication":
            self._set(db, "attempted:communication", snapshot["cursor"])

    def finish_job(self, job: str, completed: bool):
        with self.transaction() as db:
            row = db.execute(select(tables.jobs).where(tables.jobs.c.id == job)).mappings().fetchone()
            if row is None or row["epoch"] != self._meta(db, "epoch") or row["status"] != "RUNNING":
                return
            db.execute(
                tables.jobs.update()
                .where(tables.jobs.c.id == job)
                .values(status="COMPLETED" if completed else "INTERRUPTED")
            )
            snapshot = json.loads(row["snapshot"])
            if self.routing_enabled and (row["purpose"] == "communication" or completed):
                from civ_agent.diplomacy_store import delivered

                delivered(
                    self,
                    db,
                    snapshot.get("routingEventIds", [])
                    if row["purpose"] == "communication"
                    else snapshot.get("handledRoutingEventIds", []),
                    completed=completed,
                )
            if completed:
                key = "handled:" + row["purpose"]
                self._set(db, key, max(snapshot["cursor"], self._meta(db, key, 0)))
                if row["purpose"] == "communication" and type(snapshot.get("gameTurn")) is int:
                    self._set(
                        db,
                        "lastCompletedCommunicationTurn",
                        max(snapshot["gameTurn"], self._meta(db, "lastCompletedCommunicationTurn", -1)),
                    )

    def _validate(self, db, job):
        if self._meta(db, "paused", False):
            raise StateConflict("STATE_PAUSED")
        row = db.execute(select(tables.jobs).where(tables.jobs.c.id == job)).mappings().fetchone()
        if row is None or row["status"] != "RUNNING" or row["epoch"] != self._meta(db, "epoch"):
            raise StateConflict("STALE_JOB")

    @staticmethod
    def _check_run_failure(db):
        failure = CivilizationState._meta(db, "runFailure")
        if failure:
            raise StateConflict(failure["error"])

    def run_failure(self):
        with self.transaction() as db:
            return self._meta(db, "runFailure")

    def resume_run(self):
        """Explicit operator reconnect starts a new attempt; keep diagnostics and old calls."""
        with self.transaction() as db:
            self._set(db, "runFailure", None)

    def export_failure(self):
        failure = self.run_failure()
        if failure:
            write_private_json(self.root / "failures" / (failure["diagnosticId"] + ".json"), failure)
        return failure

    def fail_run(self, job, call_id, error, details, result):
        """Fence mutations while retaining the call's outcome and failure diagnostics."""
        with self.transaction() as db:
            call = (
                db.execute(select(tables.calls).where(tables.calls.c.job == job, tables.calls.c.id == call_id))
                .mappings()
                .one()
            )
            identity = (
                db.execute(select(tables.jobs.c.epoch, tables.jobs.c.purpose).where(tables.jobs.c.id == job))
                .mappings()
                .one()
            )
            failure = {
                "error": error,
                "job": job,
                "epoch": identity["epoch"],
                "purpose": identity["purpose"],
                "callId": call_id,
                "tool": call["name"],
                "arguments": json.loads(call["arguments"]),
                "diagnosticId": hashlib.sha256(encode([job, call_id, error]).encode()).hexdigest()[:24],
                **details,
            }
            self._set(db, "runFailure", failure)
            result = {
                **result,
                "error": error,
                "recoverable": False,
                "diagnosticId": failure["diagnosticId"],
                "instruction": result.get(
                    "instruction",
                    "Stop. The action's confirmed receipt is preserved; never repeat it. The run requires operator inspection.",
                ),
            }
            result = self._call_record(db, job, call_id, call["name"], json.loads(call["arguments"]), result)
            assert result is not None
            return result

    def patch(self, job: str, changes: list[JsonObject], call_id: str | None = None, city_owners=None) -> JsonObject:
        """Commit independent notebook edits and their replayable result together."""
        changes = validate(MemoryUpdate, {"changes": changes}).model_dump()["changes"]
        with self.transaction() as db:
            self._check_run_failure(db)
            self._validate(db, job)
            if call_id is not None:
                prior = db.execute(
                    select(tables.calls.c.result).where(tables.calls.c.job == job, tables.calls.c.id == call_id)
                ).fetchone()
                if prior and prior[0]:
                    return json.loads(prior[0])
            row = db.execute(select(tables.jobs).where(tables.jobs.c.id == job)).mappings().one()
            snapshot = load_snapshot(db, row["snapshot"])
            notebook = Notebook(
                db, snapshot, job, row["purpose"], self._meta(db, "gameTurn", row["game_turn"]), row["game_turn"]
            )
            notebook.city_owners = city_owners
            result = {"changes": [notebook.update(change) for change in changes]}
            db.execute(tables.jobs.update().where(tables.jobs.c.id == job).values(snapshot=dump_snapshot(db, snapshot)))
            if call_id is not None:
                call = db.execute(
                    select(tables.calls.c.arguments).where(tables.calls.c.job == job, tables.calls.c.id == call_id)
                ).fetchone()
                arguments = json.loads(call[0]) if call else {"changes": changes}
                result = self._call_record(db, job, call_id, "memory_update", arguments, result)
                assert result is not None
            return result

    def search_memory(self, job, arguments):
        with self.transaction() as db:
            self._validate(db, job)
            snapshot = load_snapshot(
                db, db.execute(select(tables.jobs.c.snapshot).where(tables.jobs.c.id == job)).scalar_one()
            )
            result, keys = RecordStore(db).search(
                **arguments,
                contribution_ids=snapshot.get("contributionIds", []),
            )
            if not arguments.get("history"):
                current = RecordStore(db).all(keys=keys)
                RecordStore.deliver(snapshot, {key: current[key] for key in keys})
                result["records"] = [public_record(key, current[key]) for key in keys]
            else:
                for record in result["records"]:
                    alias = record["evidence"]
                    row = (
                        db.execute(
                            select(tables.records.c.key, tables.records.c.version, tables.records.c.category).where(
                                tables.record_order == int(alias[1:])
                            )
                        )
                        .mappings()
                        .one()
                    )
                    snapshot.setdefault("historicalEvidence", {})[alias] = dict(row)
            db.execute(tables.jobs.update().where(tables.jobs.c.id == job).values(snapshot=dump_snapshot(db, snapshot)))
            return result

    def queue(
        self,
        job: str,
        transport: str,
        payload: JsonObject,
        call_id: str,
        intent: JsonObject | None = None,
        initiative_decisions: list[JsonObject] | None = None,
    ) -> JsonObject:
        """Code owns identity. Retrying one tool call reuses the exact saved request."""
        request_id = "r" + hashlib.sha256((job + ":" + call_id).encode()).hexdigest()[:24]
        with self.transaction() as db:
            self._check_run_failure(db)
            self._validate(db, job)
            previous = db.execute(select(tables.outbox).where(tables.outbox.c.id == request_id)).mappings().fetchone()
            if previous:
                if previous["transport"] != transport or json.loads(previous["payload"]) != {
                    **payload,
                    "requestId": request_id,
                }:
                    raise StateConflict("CALL_CONFLICT")
                saved = RecordStore(db).all(keys=[previous["decision"]])[previous["decision"]]
                queued = {name: previous[name] for name in tables.outbox.c.keys()}
                queued.update(intent_feedback(saved["body"].get("_pinned", {})))
                return queued
            body = {**payload, "requestId": request_id}
            snapshot = load_snapshot(
                db, db.execute(select(tables.jobs.c.snapshot).where(tables.jobs.c.id == job)).scalar_one()
            )
            store = RecordStore(db, self._meta(db, "gameTurn"))
            pinned: JsonObject = {}
            initiatives = []
            if initiative_decisions:
                advice = snapshot.get("strategicAdvice")
                available = len(advice["review"]["diplomacy"]["initiatives"]) if advice else 0
                for entry in initiative_decisions:
                    if payload.get("kind") == "END_TURN" and advice and 1 <= entry["initiative"] <= available:
                        initiatives.append({**entry, "reviewTurn": advice["asOfTurn"]})
                    else:
                        pinned.setdefault("unverifiedInitiatives", []).append(entry["initiative"])
            if intent is not None:
                intent = validate(Intent, intent).model_dump()
                # Intent is an audit annotation; the host validates the action.
                city_plan = intent.get("cityPlan")
                city_record = snapshot["records"].get(city_plan)
                if (
                    city_record
                    and city_record["category"] == "city_plan"
                    and city_record["body"].get("city") == payload.get("target")
                    and snapshot.get("delivered", {}).get(city_plan) == city_record["version"]
                ):
                    pinned["cityPlan"] = {
                        "key": city_plan,
                        "version": city_record["version"],
                        "reason": intent["reason"],
                    }
                key = intent.get("strategy")
                chosen = intent.get("decision")
                if chosen:
                    pinned["unverifiedDecision"] = chosen
                prior = snapshot["records"].get(key)
                if (
                    key
                    and prior
                    and snapshot.get("delivered", {}).get(key) == prior["version"]
                    and prior["category"] == "strategy"
                ):
                    pinned["strategy"] = {
                        "key": key,
                        "version": prior["version"],
                        "assessment": prior["body"]["assessment"],
                    }
                    baseline = prior["body"].get("_decisionBaselines", {}).get(chosen)
                    if chosen and baseline:
                        pinned["decision"] = {"strategy": key, "id": chosen, "baseline": baseline["id"]}
                        pinned.pop("unverifiedDecision")
                elif key:
                    pinned["unverifiedStrategy"] = key
                pinned["assumptions"] = []
                for assumption in intent["assumptions"]:
                    if assumption.get("source"):
                        try:
                            pinned["assumptions"].extend(store.pin(snapshot, [assumption["source"]]))
                        except EvidenceRequired:
                            pinned.setdefault("unverifiedSources", []).append(assumption["source"])
            decision = store.handle("dec")
            store.write(
                decision,
                "decision",
                {
                    "turn": store.turn,
                    "intent": intent,
                    **({"initiativeDecisions": initiatives} if initiatives else {}),
                    "_pinned": pinned,
                    "command": body,
                    "outcome": "pending",
                    "receipt": None,
                    "subjects": self._command_subjects(payload),
                },
                job,
            )
            db.execute(
                insert(tables.outbox).values(
                    id=request_id,
                    job=job,
                    transport=transport,
                    payload=encode(body),
                    result=None,
                    decision=decision,
                )
            )
            db.execute(insert(tables.outgoing).values(id=request_id))
            return {
                "id": request_id,
                "job": job,
                "transport": transport,
                "payload": encode(body),
                "result": None,
                "decision": decision,
                **intent_feedback(pinned),
            }

    @staticmethod
    def _command_subjects(payload):
        return [value for key in ("target", "partner", "to") if isinstance(value := payload.get(key), str)]

    def uncertain(self, request_id):
        with self.transaction() as db:
            row = (
                db.execute(
                    select(tables.outbox).select_from(tables.live_requests).where(tables.outbox.c.id == request_id)
                )
                .mappings()
                .fetchone()
            )
            if row and row["result"] is None:
                record = RecordStore(db).all(keys=[row["decision"]])[row["decision"]]
                self._record(db, row["decision"], "decision", {**record["body"], "outcome": "uncertain"}, row["job"])

    def _settle_trade(self, db, value):
        trade = value.get("trade") or {}
        if trade.get("status") in {None, "PENDING"}:
            return
        records = RecordStore(db).all(categories={"decision"})
        for row in db.execute(
            select(tables.outbox).select_from(tables.live_requests).where(tables.outbox.c.result.is_not(None))
        ).mappings():
            receipt = json.loads(row["result"])
            if (receipt.get("trade") or {}).get("proposal") == trade.get("proposal"):
                record = records[row["decision"]]
                self._record(db, row["decision"], "decision", {**record["body"], "settlement": value}, row["job"])

    def pending(self, *, exclude_purpose=None):
        with self.transaction() as db:
            query = (
                select(tables.outbox)
                .select_from(tables.live_requests.join(tables.jobs, tables.jobs.c.id == tables.outbox.c.job))
                .where(tables.outbox.c.result.is_(None))
                .order_by(tables.request_order)
            )
            if exclude_purpose is not None:
                query = query.where(tables.jobs.c.purpose != exclude_purpose)
            return [dict(row) for row in db.execute(query).mappings()]

    def requests_for_job(self, job: str):
        with self.transaction() as db:
            return [
                dict(row)
                for row in db.execute(
                    select(tables.outbox)
                    .select_from(tables.live_requests)
                    .where(tables.outbox.c.job == job)
                    .order_by(tables.request_order)
                ).mappings()
            ]

    def outgoing_position(self) -> int:
        with self.transaction() as db:
            return db.execute(
                select(func.coalesce(func.max(tables.request_order), 0))
                .select_from(tables.live_requests)
                .where(tables.outbox.c.result.is_not(None))
            ).scalar_one()

    def outgoing_messages(
        self, after: int = 0, *, limit: int | None = None, echo_cursor: int | None = None
    ) -> list[JsonObject]:
        """Confirmed sends from the existing journal, including not-yet-ingested echoes."""
        with self.transaction() as db:
            query = (
                select(tables.outbox.c.id, tables.outbox.c.job, tables.outbox.c.result)
                .select_from(tables.live_requests)
                .where(
                    tables.request_order > after,
                    tables.outbox.c.transport == "player_communication",
                    func.json_extract(tables.outbox.c.payload, "$.op") == "send",
                    func.json_extract(tables.outbox.c.result, "$.message.message").is_not(None),
                )
                .order_by(tables.request_order.desc())
            )
            if limit is not None:
                query = query.limit(limit)
            origins = (
                {
                    r["body"].get("requestId"): r["body"]["sourceJob"]
                    for r in RecordStore(db).all(categories={"delivery"}).values()
                }
                if self.routing_enabled
                else {}
            )
            rows = [
                {"job": origins.get(row.id, row.job), "message": json.loads(row.result)["message"]}
                for row in reversed(db.execute(query).all())
            ]
            if echo_cursor is not None and rows:
                # Filter only this bounded page, without rescanning the whole
                # outgoing journal for older, already handled messages.
                message_id = func.json_extract(tables.events.c.body, "$.body.message")
                echoed = set(
                    db.scalars(
                        select(message_id)
                        .select_from(tables.events.join(tables.inbox))
                        .where(
                            tables.events.c.id <= echo_cursor,
                            tables.events.c.kind == "message",
                            message_id.in_([row["message"]["message"] for row in rows]),
                        )
                    )
                )
                rows = [row for row in rows if row["message"]["message"] not in echoed]
            return rows

    def call_receipt(self, job: str, call_id: str):
        request_id = "r" + hashlib.sha256((job + ":" + call_id).encode()).hexdigest()[:24]
        with self.transaction() as db:
            self._validate(db, job)
            row = db.execute(
                select(tables.outbox.c.result).where(tables.outbox.c.id == request_id, tables.outbox.c.job == job)
            ).fetchone()
            return json.loads(row[0]) if row and row[0] is not None else None

    def receipt(self, request_id: str, result: JsonObject):
        with self.transaction() as db:
            row = (
                db.execute(
                    select(tables.outbox).select_from(tables.live_requests).where(tables.outbox.c.id == request_id)
                )
                .mappings()
                .fetchone()
            )
            if row is None:
                raise StateConflict("UNKNOWN_REQUEST")
            if receipt_is_uncertain(result):
                raise StateConflict("RECEIPT_UNCERTAIN")
            if row["result"] is not None:
                if json.loads(row["result"]) != result:
                    raise StateConflict("RECEIPT_CONFLICT")
                return
            db.execute(
                tables.outbox.update()
                .where(tables.outbox.c.id == request_id, tables.outbox.c.result.is_(None))
                .values(result=encode(result))
            )
            self._record(db, "receipt:" + request_id, "observation", result, row["job"])
            record = RecordStore(db).all(keys=[row["decision"]])[row["decision"]]
            self._record(
                db,
                row["decision"],
                "decision",
                {**record["body"], "outcome": "rejected" if "error" in result else "confirmed", "receipt": result},
                row["job"],
            )
            city_ref = record["body"].get("_pinned", {}).get("cityPlan")
            if city_ref and "error" not in result:
                current_plan = RecordStore(db).all(keys=[city_ref["key"]]).get(city_ref["key"])
                if current_plan and current_plan["category"] == "city_plan":
                    confirmed = dict(current_plan["body"].get("_confirmedActions", {}))
                    command = record["body"]["command"]
                    confirmed[command.get("kind", "action")] = {
                        "receipt": "receipt:" + request_id,
                        "turn": record["body"]["turn"],
                        "planVersion": city_ref["version"],
                        "reason": city_ref["reason"],
                        "selection": command.get("selection"),
                        "actionSummary": result.get("actionSummary"),
                    }
                    self._record(
                        db,
                        city_ref["key"],
                        "city_plan",
                        {**current_plan["body"], "_confirmedActions": confirmed},
                        row["job"],
                    )
            self._settle_trade(db, result)
            trade = result.get("trade") or {}
            if trade.get("status") == "ENACTED" and (trade.get("outcome") or result.get("outcome")):
                RecordStore(db, self._meta(db, "gameTurn")).flag(
                    subjects=[trade.get("author"), trade.get("partner")],
                    reason="Engine agreement enacted; review diplomatic plans",
                    review_keys=["receipt:" + request_id],
                )

    def review_evidence(self, keys):
        with self.transaction() as db:
            records = RecordStore(db).all(keys=keys)
            return sorted(
                {ref["key"] for key in keys for ref in records.get(key, {}).get("body", {}).get("_review", [])}
            )

    def call(self, job, id, name, arguments, result=None):
        with self.transaction() as db:
            return self._call_record(db, job, id, name, arguments, result)

    def memory_edit_counts(self, job: str) -> JsonObject:
        """Count committed edit outcomes, including partial batches and lost responses.

        The call journal survives rollback and deduplicates transport retries.
        Return counters and reason codes only, never private record contents.
        """
        counts: JsonObject = {
            "batches": 0,
            "accepted": 0,
            "rejected": 0,
            "rejectedBatches": 0,
            "rejectionReasons": {},
            "complete": True,
        }
        with self.transaction() as db:
            for (saved,) in db.execute(
                select(tables.calls.c.result).where(
                    tables.calls.c.job == job,
                    tables.calls.c.name == "memory_update",
                    tables.calls.c.result.is_not(None),
                )
            ):
                result = json.loads(saved)
                if "changes" not in result:
                    continue  # Request validation errors remain API errors.
                counts["batches"] += 1
                rejected = False
                for change in result["changes"]:
                    counts["accepted" if change["accepted"] else "rejected"] += 1
                    if not change["accepted"]:
                        rejected = True
                        reason = change.get("reason", "UNKNOWN").split(":", 1)[0]
                        reason = reason if re.fullmatch(r"[A-Z][A-Z0-9_]{0,63}", reason) else "OTHER"
                        counts["rejectionReasons"][reason] = counts["rejectionReasons"].get(reason, 0) + 1
                counts["rejectedBatches"] += int(rejected)
        return counts

    def _call_record(self, db, job, id, name, arguments, result=None):
        self._validate(db, job)
        prior = (
            db.execute(select(tables.calls).where(tables.calls.c.job == job, tables.calls.c.id == id))
            .mappings()
            .fetchone()
        )
        if prior and (prior["name"] != name or prior["arguments"] != encode(arguments)):
            raise StateConflict("CALL_CONFLICT")
        if prior and prior["result"]:
            previous = json.loads(prior["result"])
            # Preserve the first delivery's intentional section order (tactics
            # before city menus), without rewriting or recounting a cached call.
            return result if result is not None and result == previous else previous
        db.execute(
            insert(tables.calls)
            .values(job=job, id=id, name=name, arguments=encode(arguments), result=None)
            .prefix_with("OR IGNORE")
        )
        if result is not None:
            db.execute(
                tables.calls.update()
                .where(tables.calls.c.job == job, tables.calls.c.id == id)
                .values(result=encode(result))
            )
        return result

    def delivered_records(self, job, keys):
        """Copy the job's delivered versions into an adapter cache without another read."""
        with self.transaction() as db:
            self._validate(db, job)
            snapshot = load_snapshot(
                db, db.execute(select(tables.jobs.c.snapshot).where(tables.jobs.c.id == job)).scalar_one()
            )
            return {key: snapshot["records"][key] for key in keys if key in snapshot.get("delivered", {})}

    def refresh_memory(self, job, keys, cursor=None, include_retired=False, startup=False, routing_ids=()):
        with self.transaction() as db:
            self._validate(db, job)
            row = db.execute(select(tables.jobs.c.snapshot).where(tables.jobs.c.id == job)).one()
            snapshot = load_snapshot(db, row[0])
            if startup:
                # Selection and budgeting used these exact versions. Concurrent
                # notebook edits are available through later explicit reads.
                current = {key: snapshot["records"][key] for key in snapshot["contextIds"]}
            else:
                current = RecordStore(db).all(keys=keys)
            current = {
                key: r
                for key, r in current.items()
                if r["category"] != "notice"
                and (r["category"] != "contribution" or key in snapshot.get("contributionIds", []))
            }
            chosen = {
                key: current[key]
                for key in keys
                if key in current and (include_retired or current[key]["body"].get("state") != "retired")
            }
            RecordStore.deliver(snapshot, chosen)
            if self.routing_enabled and routing_ids:
                snapshot["routingEventIds"] = sorted(set(snapshot.get("routingEventIds", [])) | set(routing_ids))
            if cursor is not None:
                snapshot["cursor"] = max(snapshot["cursor"], cursor)
            db.execute(tables.jobs.update().where(tables.jobs.c.id == job).values(snapshot=dump_snapshot(db, snapshot)))
            return chosen

    def acknowledge_messages(self, job, messages):
        """Gameplay explicitly handles delivered messages; settle only at successful finish."""
        with self.transaction() as db:
            self._validate(db, job)
            row = db.execute(select(tables.jobs).where(tables.jobs.c.id == job)).mappings().one()
            if row["purpose"] != "gameplay" or not self.routing_enabled:
                raise StateConflict("MESSAGE_ACKNOWLEDGEMENT_GAMEPLAY_ONLY")
            snapshot = load_snapshot(db, row["snapshot"])
            available = {
                record["body"]["body"]["message"]: row["epoch"] + ":" + str(record["body"]["id"])
                for key, record in snapshot["records"].items()
                if key.startswith("event:")
                and key in snapshot.get("delivered", {})
                and record["body"].get("kind") == "message"
                and record["body"].get("body", {}).get("message")
            }
            if any(message not in available for message in messages):
                raise StateConflict("MESSAGES_NOT_DELIVERED_TO_JOB")
            ids = db.scalars(
                select(tables.events.c.id).where(
                    tables.events.c.source.in_([available[message] for message in messages])
                )
            ).all()
            snapshot["handledRoutingEventIds"] = sorted(set(snapshot.get("handledRoutingEventIds", [])) | set(ids))
            db.execute(tables.jobs.update().where(tables.jobs.c.id == job).values(snapshot=dump_snapshot(db, snapshot)))

    def chat_history(self, participant=None, query=""):
        if (
            (participant is not None and not isinstance(participant, str))
            or not isinstance(query, str)
            or len(query) > 200
        ):
            raise ValueError("INVALID_ARGUMENT")
        search = "%" + query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        with self.transaction() as db:
            participants = func.json_each(tables.events.c.body, "$.body.participants").table_valued("value")
            query = (
                select(tables.events.c.body)
                .select_from(tables.events.join(tables.inbox))
                .where(tables.events.c.body.like(search, escape="\\"))
            )
            if participant is not None:
                query = query.where(
                    select(1).select_from(participants).where(participants.c.value == participant).exists()
                )
            rows = db.execute(query.order_by(tables.events.c.id.desc()).limit(21)).all()
            return {"events": [json.loads(row[0]) for row in reversed(rows[:20])], "hasMore": len(rows) > 20}

    def conversation_context(self, seeds=()):
        with self.transaction() as db:
            return conversation_context(db, self.civilization, seeds)

    def history(self, game_turn: int, offset: int = 0, *, job: str | None = None):
        if type(game_turn) is not int or game_turn < 0 or type(offset) is not int or offset < 0:
            raise ValueError("INVALID_ARGUMENT")
        with self.transaction() as db:
            if job is not None:
                self._validate(db, job)
            rows = db.execute(
                select(tables.outbox.c.decision, tables.outbox.c.transport)
                .select_from(tables.live_requests.join(tables.jobs, tables.outbox.c.job == tables.jobs.c.id))
                .where(tables.jobs.c.game_turn == game_turn)
                .order_by(tables.request_order)
                .limit(21)
                .offset(offset)
            ).fetchall()
            records = RecordStore(db).all(keys=[row[0] for row in rows[:20]])
            return HistoryReadResult.model_validate(
                {
                    "turn": game_turn,
                    "actions": [
                        action_record(row.decision, records[row.decision]["body"], transport=row.transport)
                        for row in rows[:20]
                    ],
                    "hasMore": len(rows) > 20,
                    "nextOffset": offset + min(len(rows), 20),
                }
            ).model_dump()

    def summary(self, game_turn=None, *, job=None):
        with self.transaction() as db:
            return build_summary(db, turn=game_turn, job=job)

    def export(self, workspace: Path):
        """Explicit human-readable export. Gameplay never calls this method."""
        with self.transaction() as db:
            files = notebook_files(db)
        export_files(self.root, workspace, files)

    def backup(self, destination: Path, service=None) -> str:
        if destination.exists():
            raise StateConflict("CHECKPOINT_EXISTS")
        private_directory(destination.parent)
        with self.transaction() as db:
            snapshot = {
                "civilization": self.civilization,
                "storeId": self._meta(db, "storeId"),
                "meta": [list(row) for row in db.execute(select(tables.meta).order_by(tables.meta.c.key))],
                "heads": [list(row) for row in db.execute(select(tables.heads).order_by(tables.heads.c.key))],
                "inbox": [row[0] for row in db.execute(select(tables.inbox.c.id).order_by(tables.inbox.c.id))],
                "outgoing": [row[0] for row in db.execute(select(tables.outgoing.c.id).order_by(tables.outgoing.c.id))],
                "jobs": job_states(db),
                "reviews": [dict(row) for row in db.execute(select(tables.reviews)).mappings()],
            }
            snapshot["historySha256"] = self._history_digest(db, snapshot)
            snapshot["service"] = service
        write_private_json(destination, snapshot)
        return hashlib.sha256(destination.read_bytes()).hexdigest()

    @staticmethod
    def _history_digest(db, saved):
        """Verify the current boundary's referenced evidence, without copying history."""
        checksum = hashlib.sha256()
        queries = (
            (
                "records",
                saved["heads"],
                select(tables.records).where(
                    tables.records.c.key == bindparam("ref0"), tables.records.c.version == bindparam("ref1")
                ),
            ),
            (
                "events",
                [(id,) for id in saved["inbox"]],
                select(tables.events).where(tables.events.c.id == bindparam("ref0")),
            ),
            (
                "outbox",
                [(id,) for id in saved["outgoing"]],
                select(tables.outbox, tables.jobs.c.epoch, tables.jobs.c.purpose, tables.jobs.c.game_turn)
                .select_from(tables.outbox.join(tables.jobs, tables.jobs.c.id == tables.outbox.c.job))
                .where(tables.outbox.c.id == bindparam("ref0")),
            ),
        )
        for table, references, query in queries:
            for reference in references:
                row = db.execute(query, {f"ref{i}": value for i, value in enumerate(reference)}).mappings().fetchone()
                if row is None:
                    raise StateConflict("CHECKPOINT_HISTORY_MISSING")
                if table == "outbox" and row["result"] is None:
                    raise StateConflict("CHECKPOINT_REQUEST_UNSETTLED")
                checksum.update((encode([table, dict(row)]) + "\n").encode())
        return checksum.hexdigest()

    def verify(self, source: Path, checksum: str):
        if source.is_symlink() or hashlib.sha256(source.read_bytes()).hexdigest() != checksum:
            raise StateConflict("CHECKPOINT_INVALID")
        saved = json.loads(source.read_text())
        if json.loads(dict(saved["meta"]).get("format", "null")) != tables.FORMAT_VERSION:
            raise MemoryFormatError("Unsupported memory checkpoint format; start a fresh match.")
        with self.transaction() as db:
            if saved["civilization"] != self.civilization or saved["storeId"] != self._meta(db, "storeId"):
                raise StateConflict("CHECKPOINT_INVALID")
            if saved["historySha256"] != self._history_digest(db, saved):
                raise StateConflict("CHECKPOINT_HISTORY_CHANGED")
            for job in saved["jobs"]:
                if not db.execute(select(tables.jobs.c.id).where(tables.jobs.c.id == job["id"])).first():
                    raise StateConflict("CHECKPOINT_HISTORY_MISSING")
                if "snapshot" in job:
                    expand(db, job["snapshot"])
        return saved

    def pause(self, paused: bool):
        with self.transaction() as db:
            self._set(db, "paused", paused)

    def is_paused(self) -> bool:
        with self.transaction() as db:
            return self._meta(db, "paused", False)

    def restore(self, source: Path, checksum: str, epoch: str):
        saved = self.verify(source, checksum)
        with self.transaction() as db:
            attempts = {
                job: count for job, count in db.execute(select(tables.reviews.c.job, tables.reviews.c.attempts))
            }
            for table in (tables.meta, tables.heads, tables.inbox, tables.outgoing, tables.live_jobs):
                db.execute(table.delete())
            for table, rows in (
                (tables.meta, saved["meta"]),
                (tables.heads, saved["heads"]),
                (tables.inbox, [(id,) for id in saved["inbox"]]),
                (tables.outgoing, [(id,) for id in saved["outgoing"]]),
            ):
                if rows:
                    db.execute(insert(table), [dict(zip(table.c.keys(), row, strict=True)) for row in rows])
            for job in saved["jobs"]:
                values = {"status": "INTERRUPTED" if job["status"] == "RUNNING" else job["status"]}
                if "snapshot" in job:
                    values["snapshot"] = encode(job["snapshot"])
                db.execute(tables.jobs.update().where(tables.jobs.c.id == job["id"]).values(**values))
                db.execute(insert(tables.live_jobs).values(id=job["id"]))
            db.execute(tables.reviews.delete())
            for task in saved["reviews"]:
                task["attempts"] = max(task["attempts"], attempts.get(task["job"], 0))
                db.execute(insert(tables.reviews).values(**task))
            self._set(db, "epoch", epoch)
            self._set(db, "paused", True)
            db.execute(tables.jobs.update().where(tables.jobs.c.status == "RUNNING").values(status="INTERRUPTED"))
        return [task["job"] for task in saved["reviews"] if task["status"] == "PENDING"]


def job_id() -> str:
    return "job-" + secrets.token_urlsafe(12)
