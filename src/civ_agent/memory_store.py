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

    def all(self, *, categories=None, keys=None, project=True):
        query = select(tables.records).select_from(tables.current_records)
        if categories is not None:
            query = query.where(tables.records.c.category.in_(categories))
        if keys is not None:
            query = query.where(tables.records.c.key.in_(keys))
        records = {
            row["key"]: {"version": row["version"], "category": row["category"], "body": json.loads(row["body"])}
            for row in self.db.execute(query).mappings()
        }

        if project:
            notices = {k: r for k, r in records.items() if r["category"] == "notice"}
            missing = [
                "notice:" + k
                for k, r in records.items()
                if r["category"] in MEMORY_TYPES and "notice:" + k not in notices
            ]
            if missing:
                notices.update(self.all(keys=missing, project=False))
            for key, record in records.items():
                if record["category"] in MEMORY_TYPES:
                    self.project_notices(key, record, notices)
        return records

    @staticmethod
    def project_notices(key, record, notices):
        """Advisory projection: receiving news never revises notebook content."""
        notice = notices.get("notice:" + key, {})
        body = record["body"]
        pending = notice.get("version", 0) > body.get("_seenNotice", 0)
        body["needsReview"] = notice.get("body", {}).get("reasons", []) if pending else []
        body["_review"] = notice.get("body", {}).get("sources", []) if pending else []
        body["_noticeVersion"] = notice.get("version", 0)

    @staticmethod
    def deliver(snapshot, chosen):
        snapshot["records"].update(chosen)
        snapshot.setdefault("delivered", {}).update({key: record["version"] for key, record in chosen.items()})
        if not snapshot.get("memoryOnly"):
            for key, record in chosen.items():
                if record["category"] in MEMORY_TYPES:
                    body = record["body"]
                    snapshot.setdefault("notices", {})["notice:" + key] = {
                        "version": body.get("_noticeVersion", 0),
                        "body": {"reasons": body.get("needsReview", []), "sources": body.get("_review", [])},
                    }

    def write(self, key, category, body, job=None):
        if category in MEMORY_TYPES:
            body = {
                k: v for k, v in body.items() if k not in {"needsReview", "reviewSources", "_review", "_noticeVersion"}
            }
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
        """Store assessments with optional historical references; never certify their truth."""
        if len(encode(content)) > 8000:
            raise ValueError("MEMORY_TOO_LARGE")
        previous = previous or {}
        retained = previous.get("_citations", {})
        citations, missing = {}, []
        names = list(
            dict.fromkeys(
                content.get("sources", [])
                + content.get("contradictingSources", [])
                + [a["source"] for a in content.get("assumptions", []) if a.get("source")]
                + ([content["plan"]] if content.get("plan") else [])
            )
        )
        for source in names:
            try:
                citations[source] = self.pin(snapshot, [source], retained)[0]
            except EvidenceRequired:
                missing.append(source)
        return {
            **content,
            "_citations": citations,
            "_evidence": [
                citations[s]
                for s in content.get("sources", []) + content.get("contradictingSources", [])
                if s in citations
            ],
            "_assumptions": [
                citations[a["source"]] for a in content.get("assumptions", []) if a.get("source") in citations
            ],
            **({"_relatedPlan": citations[content["plan"]]} if content.get("plan") in citations else {}),
            "unverifiedSources": missing,
        }

    def flag(self, keys=(), subjects=(), reason="Evidence changed", review_keys=(), plans=()):
        self.flag_many([dict(keys=keys, subjects=subjects, reason=reason, review_keys=review_keys, plans=plans)])

    def flag_many(self, changes):
        """Publish direct advisory notices separately; no recursive dependency graph."""
        if not changes:
            return
        records = self.all(categories={"plan", "belief"}, project=False)
        notices = self.all(categories={"notice"}, project=False)
        source_keys = {key for change in changes for key in (change.get("review_keys") or change.get("keys", ()))}
        versions = dict(self.db.execute(select(tables.heads).where(tables.heads.c.key.in_(source_keys))).all())
        for key, record in records.items():
            body = record["body"]
            if body.get("state") != "active":
                continue
            prior = notices.get("notice:" + key, {})
            pending = prior.get("version", 0) > body.get("_seenNotice", 0)
            reasons = list(prior.get("body", {}).get("reasons", [])) if pending else []
            sources = {r["key"]: r["version"] for r in prior.get("body", {}).get("sources", [])} if pending else {}
            affected = False
            for change in changes:
                referenced = any(ref["key"] in change.get("keys", ()) for ref in body.get("_assumptions", []))
                relevant = record["category"] == "plan" and (
                    key in change.get("plans", ())
                    or bool(set(body.get("subjects", [])) & set(change.get("subjects", ())))
                )
                if not referenced and not relevant:
                    continue
                affected = True
                reasons.append(change["reason"])
                sources.update(
                    {k: versions[k] for k in (change.get("review_keys") or change.get("keys", ())) if k in versions}
                )
            if affected:
                self.write(
                    "notice:" + key,
                    "notice",
                    {
                        "record": key,
                        "reasons": list(dict.fromkeys(reasons))[-8:],
                        "sources": [{"key": k, "version": v} for k, v in sorted(sources.items())][-20:],
                    },
                )

    def review_plans(self):
        if self.turn is None:
            return
        due = [
            key
            for key, r in self.all(categories={"plan"}).items()
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
        for key, record in self.all(categories=MEMORY_TYPES).items():
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
        self,
        *,
        type=None,
        subject="",
        state="active",
        query="",
        history=False,
        id="",
        offset=0,
        memory_only=False,
        contribution_ids=(),
    ):
        conditions: list[ColumnElement[bool]] = [
            tables.records.c.category != "notice",
            (tables.records.c.category != "contribution") | tables.records.c.key.in_(contribution_ids),
        ]
        if memory_only:
            conditions.append(tables.records.c.category.in_(MEMORY_TYPES | {"contribution"}))
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
        if value["category"] in MEMORY_TYPES | {"contribution"} and value["body"].get("state") == "active"
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
