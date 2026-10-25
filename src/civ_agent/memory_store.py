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
            advice = snapshot.get("strategicAdvice")
            if advice:
                assumption = next(
                    (a for a in advice["review"]["materialAssumptions"] if a.get("source") == source), None
                )
                if assumption is not None:
                    evidence.append(
                        {
                            "key": source,
                            "category": "review_assumption",
                            "reviewTurn": advice["asOfTurn"],
                            "assessment": assumption,
                        }
                    )
                    continue
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
                + ([content["relatedMemory"]] if content.get("relatedMemory") else [])
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
            **(
                {"_relatedMemory": citations[content["relatedMemory"]]}
                if content.get("relatedMemory") in citations
                else {}
            ),
            "unverifiedSources": missing,
        }

    def flag(self, keys=(), subjects=(), reason="Evidence changed", review_keys=(), strategies=()):
        self.flag_many(
            [dict(keys=keys, subjects=subjects, reason=reason, review_keys=review_keys, strategies=strategies)]
        )

    def flag_many(self, changes):
        """Publish direct advisory notices separately; no recursive dependency graph."""
        if not changes:
            return
        records = self.all(categories={"strategy", "belief"}, project=False)
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
                relevant = record["category"] == "strategy" and (
                    key in change.get("strategies", ())
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

    def expire(self):
        if self.turn is None:
            return
        for key, record in self.all(categories=MEMORY_TYPES | {"contribution"}).items():
            body = record["body"]
            if body.get("state") == "active" and (
                expired_at(record, self.turn)
                or record["category"] == "handoff"
                and type(body.get("basis", {}).get("turn")) is int
                and body["basis"]["turn"] + 1 < self.turn
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
        contribution_ids=(),
    ):
        conditions: list[ColumnElement[bool]] = [
            tables.records.c.category.in_(MEMORY_TYPES | {"contribution", "decision", "observation", "statement"}),
            (tables.records.c.category != "contribution") | tables.records.c.key.in_(contribution_ids),
        ]
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


def handoff_eligible(record, turn):
    authored = record["body"].get("basis", {}).get("turn")
    return type(authored) is int and type(turn) is int and turn == authored + 1


def expired_at(record, turn):
    expiry = record["body"].get("expiresTurn")
    return type(turn) is int and type(expiry) is int and expiry <= turn


def select_context(snapshot, limit):
    turn = snapshot.get("gameTurn")
    subjects = set(snapshot.get("contextTopics", []))
    for event in snapshot["events"]:
        body = event.get("body", {})
        event_turn = event.get("turn", body.get("turn"))
        if type(turn) is int and type(event_turn) is int and event_turn < turn - 1:
            continue
        subjects.update(body.get("participants", []))
        subjects.update(
            v for v in (body.get("author"), body.get("from"), body.get("to"), body.get("partner")) if isinstance(v, str)
        )
    subjects = {subject.casefold() for subject in subjects}

    def relevant(record):
        return any(subject.casefold() in subjects for subject in record["body"].get("subjects", []))

    def rank(key):
        value = snapshot.get("advisoryRanking", {}).get(key, {})
        return value if isinstance(value, dict) else {}

    candidates = [
        (key, value)
        for key, value in snapshot["records"].items()
        if value["category"] in MEMORY_TYPES | {"contribution"}
        and value["body"].get("state") == "active"
        and (
            value["category"] != "city_plan"
            or snapshot["purpose"] == "economy"
            and value["body"].get("city", "").casefold() in subjects
        )
        and not expired_at(value, snapshot.get("gameTurn"))
        and (value["category"] != "handoff" or snapshot["purpose"] == "gameplay" and handoff_eligible(value, turn))
    ]
    available = len(candidates)
    # A spare character budget is not a reason to repeat old notes. Strategies,
    # next-turn handoffs and unhandled contributions have explicit lifecycles;
    # historical knowledge needs recency, a current subject, or a review notice.
    candidates = [
        (key, record)
        for key, record in candidates
        if (
            record["category"] not in {"belief", "note"}
            or rank(key).get("include") is not False
            or record["body"].get("needsReview")
        )
        and (
            record["category"] in {"strategy", "handoff", "contribution"}
            or rank(key).get("include") is True
            or record["body"].get("needsReview")
            or relevant(record)
            or type(turn) is not int
            or (record["body"].get("updatedTurn") or 0) >= turn - 2
        )
    ]
    candidates.sort(
        key=lambda item: (
            {"strategy": 0, "handoff": 1}.get(item[1]["category"], 2),
            not bool(item[1]["body"].get("needsReview")),
            (0 if rank(item[0]).get("priority") == "high" else 1) if item[1]["category"] in {"belief", "note"} else 2,
            not relevant(item[1]),
            -item[1]["body"].get("priority", 3),
            -(item[1]["body"].get("updatedTurn") or 0),
            item[0],
        )
    )
    chosen, used = {}, 2  # JSON array brackets, then commas between chosen records.
    for key, record in candidates:
        size = len(encode(public_record(key, record))) + bool(chosen)
        if used + size <= limit:
            chosen[key] = record
            used += size
    return chosen, available - len(chosen)
