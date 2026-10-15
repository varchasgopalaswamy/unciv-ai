"""Checkpoint-aware frozen advisory inputs and outputs, never notebook authority."""

import copy
import re

from sqlalchemy import select

from civ_agent import memory_schema as tables
from civ_agent.decisions_ledger import canonical, fingerprint
from civ_agent.decisions_tasks import TASK_VERSION
from civ_agent.memory_records import public_record
from civ_agent.memory_snapshots import dump_snapshot, load_snapshot
from civ_agent.memory_store import RecordStore, expired_at, select_context


def material_facts(view):
    """Exclude transport metadata, routine progress counters and stockpiles.

    These are eligibility signals, not alternate gameplay rules. The complete
    frozen context can still accompany a request once it is eligible.
    """
    economy = view.get("economy") or {}
    progress = view.get("recentProgress") or {}
    diplomacy = view.get("diplomacy", [])
    if isinstance(diplomacy, dict):
        diplomacy = diplomacy.get("knownCivilizations", [])
    return {
        "cities": [
            (
                c.get("handle"),
                c.get("name"),
                c.get("constructionQueue"),
                c.get("health", 100) < c.get("maxHealth", 100),
                {k: v for k, v in (c.get("constructionChoice") or {}).items() if k != "turnsObserved"},
            )
            for c in view.get("cities", [])
        ],
        "units": [
            (u.get("handle"), u.get("name"), u.get("workerJob", (u.get("worker") or {}).get("currentJob")))
            for u in view.get("units", view.get("map", {}).get("units", []))
        ],
        "researchQueue": view.get("researchQueue"),
        "diplomacy": [{k: r.get(k) for k in ("civilizationId", "atWar", "defeated", "status")} for r in diplomacy],
        "economicPressure": {"unhappy": economy.get("happiness", 0) < 0, "deficit": economy.get("goldPerTurn", 0) < 0},
        "resources": [(r.get("name"), r.get("available", 0) > 0) for r in economy.get("resources", [])],
        "stalledProduction": [
            (c.get("city"), p.get("construction"))
            for c in progress.get("cities", [])
            for p in c.get("retainedProduction", [])
            if p.get("activeNow") and p.get("sameProductionObservedTurns", 0) >= 3
        ],
        "stalledOrders": [
            u.get("unit")
            for u in progress.get("units", [])
            if u.get("destination") and u.get("samePositionObservedTurns", 0) >= 3
        ],
        "hostiles": (view.get("tacticalReview") or {}).get("visibleHostiles", []),
    }


def active(record, turn):
    return record["body"].get("state") == "active" and not expired_at(record, turn)


def reference(key, record):
    return {"key": key, "version": record["version"], "record": public_record(key, record)}


class AdvisoryStore:
    def __init__(self, memory):
        self.memory = memory

    def state(self):
        with self.memory.transaction() as db:
            return self.memory._meta(db, "advisories", {})

    def save(self, **values):
        with self.memory.transaction() as db:
            state = self.memory._meta(db, "advisories", {})
            state.update(copy.deepcopy(values))
            self.memory._set(db, "advisories", state)

    def freeze(self, job, topics, settings):
        """Current reachable sources only; classifier/notices never become news."""
        with self.memory.transaction() as db:
            self.memory._validate(db, job.id)
            snapshot = load_snapshot(db, db.scalar(select(tables.jobs.c.snapshot).where(tables.jobs.c.id == job.id)))
            records, turn = snapshot["records"], snapshot.get("gameTurn")
            objectives = [
                reference(k, r) for k, r in records.items() if r["category"] == "strategy" and active(r, turn)
            ]
            words = set(re.findall(r"\w{3,}", canonical([sorted(topics), objectives]).casefold()))
            candidates = [(k, r) for k, r in records.items() if r["category"] in {"belief", "note"} and active(r, turn)]
            candidates.sort(
                key=lambda item: (
                    -len(words & set(re.findall(r"\w{3,}", canonical(public_record(*item)).casefold()))),
                    -item[1]["body"].get("priority", 3),
                    item[0],
                )
            )
            retrieval = [reference(k, r) for k, r in candidates[: settings.decisions_memory_candidate_limit]]
            state = self.memory._meta(db, "advisories", {})
            seen = state.get("seenSources", {})
            current = RecordStore(db).all(categories={"statement", "observation"})
            sources = []
            for key, record in current.items():
                if seen.get(key) == record["version"] or key == "observation:native_advice":
                    continue
                # Statements are retained verbatim. Facts use recorded material
                # deltas/signals, never full review capture or a hidden engine read.
                if record["category"] == "statement":
                    sources.append(reference(key, record))
            sources.sort(key=lambda s: (s["record"].get("turn", 0) or 0, s["key"]), reverse=True)
            sources = sources[: settings.decisions_preservation_candidate_limit]
            new_evidence = list(sources)
            overview = current.get("observation:overview")
            if overview:
                facts = material_facts(overview["body"])
                if fingerprint(facts) != state.get("seenFacts"):
                    new_evidence.append(
                        {
                            "key": "observation:overview",
                            "version": overview["version"],
                            "record": {"body": facts},
                            "projection": "Recorded material facts only",
                        }
                    )
            beliefs = [reference(k, r) for k, r in candidates if r["category"] == "belief"]
            # A changed ranking context is a selection need only if optional
            # candidates compete or include older entries omitted deterministically.
            selected, _ = select_context({**snapshot, "contextTopics": sorted(topics)}, self.memory.context_limit)
            retrieval_needed = any(r["key"] not in selected for r in retrieval)
            return {
                "objectives": objectives,
                "topics": sorted(topics),
                "retrieval": retrieval if retrieval_needed else [],
                "preservation": sources,
                "newEvidence": new_evidence,
                "beliefs": beliefs[: settings.decisions_belief_candidate_limit] if new_evidence else [],
            }

    def apply(self, job, evidence, result):
        with self.memory.transaction() as db:
            self.memory._validate(db, job.id)
            snapshot = load_snapshot(db, db.scalar(select(tables.jobs.c.snapshot).where(tables.jobs.c.id == job.id)))
            answers = {a["name"]: a for a in result.get("answers", [])}
            ranks = {}
            notices, preservation = [], []
            current = RecordStore(db).all()
            evidence_current = all(
                current.get(s["key"], {}).get("version") == s["version"] for s in evidence.get("newEvidence", [])
            )
            for field in ("retrieval", "preservation", "beliefs"):
                for index, source in enumerate(evidence.get(field, [])):
                    answer = answers.get(f"{field}:{index}")
                    record = current.get(source["key"])
                    if not answer or not record or record["version"] != source["version"]:
                        continue
                    if field == "retrieval" and active(record, snapshot.get("gameTurn")):
                        ranks[source["key"]] = answer["choice"]
                    elif field == "preservation" and answer["choice"] == "consider":
                        preservation.append(source)
                    elif (
                        field == "beliefs"
                        and evidence_current
                        and answer["choice"] == "reconsider"
                        and active(record, snapshot.get("gameTurn"))
                    ):
                        notices.append(
                            {
                                "belief": source,
                                "sources": evidence.get("newEvidence", []),
                                "confidence": answer["confidence"],
                                "advisoryOnly": True,
                            }
                        )
            snapshot["advisories"] = {
                "preservationCandidates": preservation,
                "beliefNotices": notices,
                "requestId": result.get("requestId"),
            }
            snapshot["advisoryRanking"] = ranks
            db.execute(
                tables.jobs.update().where(tables.jobs.c.id == job.id).values(snapshot=dump_snapshot(db, snapshot))
            )
            return snapshot["advisories"], ranks

    def city_delivery(self, job, notices):
        """Persist only the final advisory view; internal previews are not reads."""
        with self.memory.transaction() as db:
            self.memory._validate(db, job.id)
            snapshot = load_snapshot(db, db.scalar(select(tables.jobs.c.snapshot).where(tables.jobs.c.id == job.id)))
            snapshot["advisories"] = copy.deepcopy(notices)
            snapshot["cityObjectiveSources"] = copy.deepcopy(job.memory.get("cityObjectiveSources", []))
            db.execute(
                tables.jobs.update().where(tables.jobs.c.id == job.id).values(snapshot=dump_snapshot(db, snapshot))
            )
            state = self.memory._meta(db, "advisories", {})
            state["cityDelivery"] = {
                **notices.get("cityAdviceStatus", {}),
                "deliveredCities": len(notices.get("cityRecommendations", [])),
                "deliveredQuestions": sum(
                    field in row
                    for row in notices.get("cityRecommendations", [])
                    for field in ("focus", "construction")
                ),
                "gameTurn": snapshot.get("gameTurn"),
            }
            self.memory._set(db, "advisories", state)

    def sources_current(self, sources):
        with self.memory.transaction() as db:
            current = RecordStore(db).all(keys=[r["key"] for r in sources])
            return all(current.get(r["key"], {}).get("version") == r["version"] for r in sources)

    def review_input(self, turn, settings):
        """Read previously recorded signals; never open a player/review connection."""
        with self.memory.transaction() as db:
            last = db.scalar(
                select(tables.reviews.c.turn)
                .where(tables.reviews.c.status == "COMPLETED")
                .order_by(tables.reviews.c.turn.desc())
                .limit(1)
            )
            state = self.memory._meta(db, "advisories", {})
            if last is None or turn - last < settings.early_review_min_gap_turns:
                return None
            early = state.get("lastEarlyTurn")
            if early is not None and turn - early < settings.early_review_min_gap_between_early_turns:
                return None
            records = RecordStore(db).all(categories={"strategy", "observation", "statement"})
            overview = records.get("observation:overview")
            if overview is None:
                return None
            facts = material_facts(overview["body"])
            # A stable material signature never creates a repeated paid call.
            objectives = [
                reference(k, r) for k, r in records.items() if r["category"] == "strategy" and active(r, turn)
            ]
            words = set(re.findall(r"\w{4,}", canonical(objectives).casefold()))
            statements = []
            for key, record in records.items():
                body = record["body"]
                event_turn = body.get("turn", body.get("body", {}).get("turn"))
                text = body.get("body", {}).get("text", "")
                if (
                    record["category"] == "statement"
                    and type(event_turn) is int
                    and last < event_turn <= turn
                    and len(text.split()) >= 6
                    and words & set(re.findall(r"\w{4,}", text.casefold()))
                ):
                    statements.append(reference(key, record))
            statements = statements[-settings.decisions_preservation_candidate_limit :]
            if fingerprint(facts) == state.get("reviewBaseline") and not statements:
                return None
            signature = fingerprint({"facts": facts, "statements": statements, "objectives": objectives})
            return {
                "version": TASK_VERSION,
                "objectives": objectives,
                "earlyReview": {
                    "facts": facts,
                    "source": {"key": "observation:overview", "version": overview["version"]},
                    "lastReviewTurn": last,
                    "statements": statements,
                    "progress": overview["body"].get("recentProgress", {}),
                },
                "signature": signature,
            }

    def review_current(self, evidence):
        refs = [
            evidence["earlyReview"]["source"],
            *evidence.get("objectives", []),
            *evidence["earlyReview"].get("statements", []),
        ]
        with self.memory.transaction() as db:
            current = RecordStore(db).all(keys=[r["key"] for r in refs])
            return all(current.get(r["key"], {}).get("version") == r["version"] for r in refs)
