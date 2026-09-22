"""Revision and lifecycle operations within a caller-owned SQLite transaction."""

import json

from sqlalchemy import ColumnElement, func, insert, select

from civ_agent import memory_schema as tables
from civ_agent.map_format import expand_tiles
from civ_agent.memory_records import MEMORY_TYPES, public_record
from civ_agent.protocol import JsonObject


def encode(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


class EvidenceRequired(ValueError):
    """A rejected memory edit with the precise evidence needed for recovery."""

    def __init__(self, message: str, keys: list[str]):
        super().__init__(message)
        self.keys = sorted(set(keys))


class RecordStore:
    def __init__(self, db, turn=None):
        self.db, self.turn = db, turn

    def all(self):
        return {
            row["key"]: {"version": row["version"], "category": row["category"], "body": json.loads(row["body"])}
            for row in self.db.execute(select(tables.records).select_from(tables.current_records)).mappings()
        }

    def write(self, key, category, body, job=None):
        prior = (
            self.db.execute(
                select(tables.records).select_from(tables.current_records).where(tables.records.c.key == key)
            )
            .mappings()
            .fetchone()
        )
        if prior and prior["body"] == encode(body) and prior["category"] == category:
            return prior["version"]
        version = self.db.execute(
            select(func.coalesce(func.max(tables.records.c.version), 0) + 1).where(tables.records.c.key == key)
        ).scalar_one()
        self.db.execute(
            insert(tables.records).values(
                key=key,
                version=version,
                category=category,
                body=encode(body),
                parent=prior["version"] if prior else None,
                job=job,
            )
        )
        self.db.execute(insert(tables.heads).values(key=key, version=version).prefix_with("OR REPLACE"))
        if job is not None:
            self.db.execute(
                insert(tables.export_jobs)
                .from_select(["id"], select(tables.jobs.c.id).where(tables.jobs.c.id == job))
                .prefix_with("OR IGNORE")
            )
        return version

    def handle(self, prefix="mem"):
        # AUTOINCREMENT is deliberately outside the rollback manifest.
        cursor = self.db.execute(insert(tables.handles).values())
        return prefix + str(cursor.lastrowid)

    @staticmethod
    def pin(snapshot: JsonObject, sources: list[str], retained: JsonObject | None = None) -> list[JsonObject]:
        evidence: list[JsonObject] = []
        missing = []
        for source in sources:
            if source in snapshot.get("historicalEvidence", {}):
                evidence.append({**snapshot["historicalEvidence"][source], "historical": True})
                continue
            matches = [
                (key, record)
                for key, record in snapshot["records"].items()
                if snapshot.get("delivered", {}).get(key) == record["version"]
                and (
                    key == source
                    or record["category"] == "statement"
                    and record["body"].get("body", {}).get("message") == source
                )
            ]
            if len(matches) != 1:
                if retained and source in retained:
                    evidence.append(retained[source])
                else:
                    missing.append(source)
                continue
            key, record = matches[0]
            evidence.append({"key": key, "version": record["version"], "category": record["category"]})
        if missing:
            raise EvidenceRequired("SOURCE_NOT_DELIVERED: use delivered evidence or read the source first", missing)
        return evidence

    def content(self, content: JsonObject, snapshot: JsonObject, previous: JsonObject | None = None) -> JsonObject:
        if len(encode(content)) > 8000:
            raise ValueError("MEMORY_TOO_LARGE")
        sources = content.get("sources", []) + content.get("contradictingSources", [])
        assumptions = [a["source"] for a in content.get("assumptions", []) if a.get("source")]
        previous = previous or {}
        retained = dict(
            zip(
                previous.get("sources", []) + previous.get("contradictingSources", []),
                previous.get("_evidence", []),
                strict=False,
            )
        )
        # A revision may retain its own citations without re-reading the original
        # observations. Only explicit assumptions require current evidence.
        references: list[list[JsonObject]] = []
        missing: list[str] = []
        for names, prior in ((sources, retained), (assumptions, None)):
            try:
                references.append(self.pin(snapshot, names, prior))
            except EvidenceRequired as error:
                missing.extend(error.keys)
        if missing:
            raise EvidenceRequired("SOURCE_NOT_DELIVERED: use delivered evidence or read the source first", missing)
        evidence, pinned = references
        related = []
        if content.get("plan"):
            try:
                related = self.pin(
                    snapshot,
                    [content["plan"]],
                    {previous["plan"]: previous["_relatedPlan"]} if previous.get("_relatedPlan") else None,
                )
            except ValueError:
                raise ValueError(
                    "RELATED_PLAN_REQUIRED: note.plan must be an existing plan ID already read with memory_read or memory_search. Omit plan for an unrelated note; put prose in text."
                ) from None
            if related[0]["category"] != "plan":
                raise ValueError(
                    "RELATED_PLAN_REQUIRED: note.plan must reference a plan record. Omit plan for an unrelated note."
                )
        current = self.all()
        refresh = [
            ref["key"]
            for ref in pinned
            if not snapshot.get("memoryOnly") and current.get(ref["key"], {}).get("body", {}).get("requiresRefresh")
        ]
        if refresh:
            raise EvidenceRequired("SOURCE_NEEDS_REFRESH: read the current agreement before assuming it", refresh)
        changed = [
            ref["key"]
            for ref in pinned
            if current.get(ref["key"], {}).get("version") != ref["version"]
            and not (snapshot.get("memoryOnly") and ref["category"] not in MEMORY_TYPES)
        ]
        if changed:
            raise EvidenceRequired("ASSUMPTION_CHANGED: read the current evidence before revising", changed)
        review = []
        for ref in pinned:
            value = current.get(ref["key"], {})
            if value.get("category") in MEMORY_TYPES and (
                value.get("version") != ref["version"]
                or value["body"].get("state") != "active"
                or value["body"].get("needsReview")
            ):
                review.append(ref["key"])
        if review:
            raise EvidenceRequired("SOURCE_NEEDS_REVIEW: read and reconsider the referenced memory", review)
        return {
            **content,
            "_evidence": evidence,
            "_assumptions": pinned,
            **({"_relatedPlan": related[0]} if related else {}),
        }

    def flag(self, keys=(), subjects=(), reason="Evidence changed", review_keys=(), plans=()):
        """Flag only declared assumptions or relevant subject-specific plans."""
        changed = set(keys)
        # Propagate through beliefs used to support plans; bounded by record count.
        records = self.all()
        review = [{"key": key, "version": records[key]["version"]} for key in (review_keys or keys) if key in records]
        while True:
            added = set()
            for key, record in records.items():
                body = record["body"]
                if record["category"] not in {"plan", "belief"} or body.get("state") != "active":
                    continue
                refs = body.get("_assumptions", [])
                affected = any(ref["key"] in changed for ref in refs)
                affected |= record["category"] == "plan" and key in plans
                affected |= record["category"] == "plan" and bool(set(body.get("subjects", [])) & set(subjects))
                if affected and reason not in body.get("needsReview", []):
                    body = {
                        **body,
                        "needsReview": [*body.get("needsReview", []), reason],
                        "updatedTurn": self.turn,
                        "_review": body.get("_review", []) + review,
                    }
                    self.write(key, record["category"], body)
                    records[key] = {**record, "body": body}
                    added.add(key)
            if not added - changed:
                break
            changed.update(added)

    def review_plans(self):
        if self.turn is None:
            return
        due = [
            key
            for key, r in self.all().items()
            if r["category"] == "plan"
            and r["body"].get("state") == "active"
            and r["body"].get("reviewAfterTurn") is not None
            and r["body"]["reviewAfterTurn"] <= self.turn
        ]
        if due:
            self.flag(
                plans=due, reason="Scheduled progress review: check success criteria, then revise or retire this plan"
            )

    def expire(self):
        if self.turn is None:
            return
        for key, record in self.all().items():
            body = record["body"]
            if (
                record["category"] in MEMORY_TYPES
                and body.get("state") == "active"
                and body.get("expiresTurn") is not None
                and body["expiresTurn"] <= self.turn
            ):
                self.write(
                    key,
                    record["category"],
                    {**body, "state": "retired", "retirementReason": "expired", "updatedTurn": self.turn},
                )
                self.flag(keys=[key], reason=f"{key} expired")

    def search(
        self, *, type=None, subject="", state="active", query="", history=False, id="", offset=0, memory_only=False
    ):
        conditions: list[ColumnElement[bool]] = []
        if memory_only:
            conditions.append(tables.records.c.category.in_(MEMORY_TYPES))
        if type:
            conditions.append(tables.records.c.category == type)
        if subject:
            subjects = func.json_each(tables.records.c.body, "$.subjects").table_valued("value")
            conditions.append(select(1).select_from(subjects).where(subjects.c.value == subject).exists())
        if state != "all":
            conditions.append(func.coalesce(func.json_extract(tables.records.c.body, "$.state"), "active") == state)
        if id:
            conditions.append(tables.records.c.key == id)
        if query:
            conditions.append(func.instr(func.lower(tables.records.c.body), func.lower(query)) > 0)
        rows = (
            self.db.execute(
                select(tables.records, tables.record_order.label("record_id"))
                .select_from(tables.record_versions(tables.reachable) if history else tables.current_records)
                .where(*conditions)
                .order_by(tables.record_order.desc())
                .limit(21)
                .offset(offset)
            )
            .mappings()
            .all()
        )
        # Historical revisions are evidence only; they never replace a job's current version.
        return {
            "records": [
                public_record(row["key"], {"category": row["category"], "body": json.loads(row["body"])})
                | ({"evidence": "e" + str(row["record_id"]), "historical": True} if history else {})
                for row in rows[:20]
            ],
            "hasMore": len(rows) > 20,
            "nextOffset": offset + min(20, len(rows)),
        }, [row["key"] for row in rows[:20]]


def semantic(value):
    """Ignore transport clocks, preserving game facts and entity status."""
    if isinstance(value, dict):
        if "tileDefaults" in value and isinstance(value.get("tiles"), list):
            value = {
                **{key: part for key, part in value.items() if key != "tileDefaults"},
                "tiles": expand_tiles(value),
            }
        return {
            key: semantic(part)
            for key, part in value.items()
            if key not in {"turn", "revision", "epoch", "cursor", "updatedTurn", "observedTurn"}
        }
    if isinstance(value, list):
        return [semantic(part) for part in value]
    return value


def select_context(snapshot, limit, topics=()):
    subjects = set(topics)
    for event in snapshot["events"]:
        body = event.get("body", {})
        subjects.update(body.get("participants", []))
        subjects.update(
            v for v in (body.get("author"), body.get("from"), body.get("to"), body.get("partner")) if isinstance(v, str)
        )
    candidates = [
        (key, value)
        for key, value in snapshot["records"].items()
        if value["category"] in MEMORY_TYPES and value["body"].get("state") == "active"
    ]
    candidates.sort(
        key=lambda item: (
            item[1]["category"] != "soul",
            not bool(set(item[1]["body"].get("subjects", [])) & subjects),
            not bool(item[1]["body"].get("needsReview")),
            -item[1]["body"].get("priority", 3),
            item[0],
        )
    )
    chosen, used = {}, 0
    for key, record in candidates:
        size = len(encode(public_record(key, record))) + 1
        if used + size <= limit:
            chosen[key] = record
            used += size
    return chosen, len(candidates) - len(chosen)
