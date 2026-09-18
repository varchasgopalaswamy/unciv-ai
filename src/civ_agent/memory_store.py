"""Revision and lifecycle operations within a caller-owned SQLite transaction."""

import json

from civ_agent.memory_records import MEMORY_TYPES, public_record
from civ_agent.protocol import JsonObject


def encode(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


# A restored head can only reach its own ancestors, never a discarded future.
REACHABLE = """WITH RECURSIVE reachable(key,version) AS (
    SELECT key,version FROM heads UNION
    SELECT r.key,r.parent FROM records r JOIN reachable a USING(key,version) WHERE r.parent IS NOT NULL
) """


class RecordStore:
    def __init__(self, db, turn=None):
        self.db, self.turn = db, turn

    def all(self):
        return {
            row["key"]: {"version": row["version"], "category": row["category"], "body": json.loads(row["body"])}
            for row in self.db.execute("SELECT r.* FROM records r JOIN heads h USING(key,version)")
        }

    def write(self, key, category, body, job=None):
        prior = self.db.execute(
            "SELECT r.* FROM records r JOIN heads h USING(key,version) WHERE r.key=?", (key,)
        ).fetchone()
        if prior and prior["body"] == encode(body) and prior["category"] == category:
            return prior["version"]
        version = self.db.execute("SELECT COALESCE(MAX(version),0)+1 FROM records WHERE key=?", (key,)).fetchone()[0]
        self.db.execute(
            "INSERT INTO records VALUES (?,?,?,?,?,?)",
            (key, version, category, encode(body), prior["version"] if prior else None, job),
        )
        self.db.execute("INSERT OR REPLACE INTO heads VALUES (?,?)", (key, version))
        return version

    def handle(self, prefix="mem"):
        # AUTOINCREMENT is deliberately outside the rollback manifest.
        cursor = self.db.execute("INSERT INTO handles DEFAULT VALUES")
        return prefix + str(cursor.lastrowid)

    @staticmethod
    def pin(snapshot: JsonObject, sources: list[str]) -> list[JsonObject]:
        evidence: list[JsonObject] = []
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
                raise ValueError("SOURCE_NOT_DELIVERED: read the evidence before referencing it")
            key, record = matches[0]
            if record["body"].get("requiresRefresh"):
                raise ValueError("SOURCE_NEEDS_REFRESH: read the current agreement before referencing it")
            evidence.append({"key": key, "version": record["version"], "category": record["category"]})
        return evidence

    def content(self, content, snapshot):
        if len(encode(content)) > 8000:
            raise ValueError("MEMORY_TOO_LARGE")
        sources = content.get("sources", []) + content.get("contradictingSources", [])
        assumptions = [a["source"] for a in content.get("assumptions", []) if a.get("source")]
        if content.get("plan"):
            assumptions.append(content["plan"])
        evidence = self.pin(snapshot, sources)
        pinned = self.pin(snapshot, assumptions)
        if content.get("plan") and pinned[-1]["category"] != "plan":
            raise ValueError("RELATED_PLAN_REQUIRED")
        current = self.all()
        for ref in pinned:
            if current.get(ref["key"], {}).get("version") != ref["version"]:
                raise ValueError("ASSUMPTION_CHANGED: read the current evidence before revising")
        for ref in evidence + pinned:
            if ref.get("historical") and ref not in pinned:
                continue
            value = current.get(ref["key"], {})
            if value.get("category") in MEMORY_TYPES and (
                value.get("version") != ref["version"]
                or value["body"].get("state") != "active"
                or value["body"].get("needsReview")
            ):
                raise ValueError("SOURCE_NEEDS_REVIEW: read and reconsider the referenced memory")
        return {**content, "_evidence": evidence, "_assumptions": pinned}

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
                if record["category"] not in {"plan", "belief", "note"} or body.get("state") != "active":
                    continue
                refs = body.get("_assumptions", []) + [
                    r for r in body.get("_evidence", []) if r["category"] == "belief"
                ]
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

    def search(self, *, type=None, subject="", state="active", query="", history=False, id="", offset=0):
        conditions, args = [], []
        if type:
            conditions.append("r.category=?")
            args.append(type)
        if subject:
            conditions.append("EXISTS (SELECT 1 FROM json_each(r.body,'$.subjects') WHERE value=?)")
            args.append(subject)
        if state != "all":
            conditions.append("COALESCE(json_extract(r.body,'$.state'),'active')=?")
            args.append(state)
        if id:
            conditions.append("r.key=?")
            args.append(id)
        if query:
            conditions.append("instr(lower(r.body),lower(?))>0")
            args.append(query)
        where = " WHERE " + " AND ".join(conditions) if conditions else ""
        rows = self.db.execute(
            (REACHABLE if history else "")
            + "SELECT r.*,r.rowid AS record_id FROM records r JOIN "
            + ("reachable" if history else "heads")
            + " h USING(key,version)"
            + where
            + " ORDER BY r.rowid DESC LIMIT 21 OFFSET ?",
            (*args, offset),
        ).fetchall()
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
    if snapshot["purpose"] == "gameplay":
        subjects.update(("defense", "expansion", "economy"))
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
