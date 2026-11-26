"""Checkpoint-aware frozen advisory inputs and outputs, never notebook authority."""

import copy
import json
import re
import time

from sqlalchemy import select

from civ_agent import memory_schema as tables
from civ_agent.advisory_delivery import delivery_basis
from civ_agent.advisory_evidence import bounded_facts, material_changes
from civ_agent.decisions_ledger import canonical, fingerprint
from civ_agent.decisions_tasks import TASK_VERSION, request_body
from civ_agent.memory_records import public_record
from civ_agent.memory_snapshots import dump_snapshot, load_snapshot
from civ_agent.memory_store import RecordStore, expired_at, select_context
from civ_agent.protocol import JsonObject
from civ_agent.telemetry import classifier_metadata

DELIVERY_CATEGORY = "advisory_delivery"
DELIVERY_VERSION = 1


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


def objective_set_current(records, objectives, turn):
    return {
        (key, record["version"])
        for key, record in records.items()
        if record["category"] == "strategy" and active(record, turn)
    } == {(source["key"], source["version"]) for source in objectives}


def reference(key, record):
    return {"key": key, "version": record["version"], "record": public_record(key, record)}


def belief_evidence_context(source):
    """Cache current fact meaning while delivery still checks its exact revision."""
    current = source.get("record", {}).get("body", {}).get("current")
    if source.get("key") == "observation:overview" and isinstance(current, dict):
        return {
            "key": source["key"],
            "current": current,
            "coverage": source.get("coverage"),
            "factsFingerprint": source.get("factsFingerprint"),
        }
    return source


def source_attempt_key(field, source, *, player, objectives, topics, new_evidence=()):
    """Bind each advisory outcome to its source and actual relevance context."""
    return fingerprint(
        {
            "version": TASK_VERSION,
            "field": field,
            "player": player,
            "source": source,
            "objectives": objectives,
            "topics": sorted(topics),
            **({"newEvidence": [belief_evidence_context(item) for item in new_evidence]} if field == "beliefs" else {}),
        }
    )


def statement_reference(key, record):
    """Expose attribution in one place while retaining the immutable original."""
    event = record["body"]
    body = event.get("body", {})
    return {
        **reference(key, record),
        "source": {
            "author": body.get("author", body.get("from")),
            "text": body.get("text"),
            "participants": copy.deepcopy(body.get("participants", [])),
            **{key: copy.deepcopy(body[key]) for key in ("public", "from", "to", "recipients") if key in body},
            "turn": event.get("turn", body.get("turn")),
            "kind": event.get("kind"),
        },
    }


def statement_order(source):
    turn = source["source"]["turn"]
    sequence = source["record"].get("body", {}).get("id")
    if type(sequence) is not int:
        suffix = source["key"].removeprefix("event:")
        sequence = int(suffix) if suffix.isdecimal() else -1
    return (turn if type(turn) is int else -1, sequence, source["key"])


TERMINAL_SOURCE_STATUSES = {"accepted", "low_confidence", "refusal", "missing", "malformed"}
UNRESOLVED_SOURCE_STATUSES = {"broker_unavailable", "malformed_broker_output", "completion_unknown"}
MEMORY_PREFIXES = {
    "retrieval": ("retrieval_include", "retrieval_priority"),
    "preservation": ("preservation",),
    "beliefs": ("beliefs",),
}


def pending_source_requests(field, source, evidence, state):
    context = {key: evidence[key] for key in ("player", "objectives", "topics")}
    identifiers = []
    for prefix in MEMORY_PREFIXES[field]:
        key = source_attempt_key(prefix, source, **context, new_evidence=evidence.get("newEvidence", []))
        attempt = state.get("sourceAttempts", {}).get(key, {})
        identifier = attempt.get("requestId")
        if attempt.get("status") in UNRESOLVED_SOURCE_STATUSES and isinstance(identifier, str):
            identifiers.append(identifier)
    return identifiers


def pending_memory_requests(evidence, state):
    """Original receipts must be reconciled before dispatching a changed bundle."""
    return list(
        dict.fromkeys(
            identifier
            for field in MEMORY_PREFIXES
            for source in evidence.get(field, [])
            for identifier in pending_source_requests(field, source, evidence, state)
        )
    )


def filter_pending_memory(evidence, state):
    """Keep unresolved questions out of a new request, including retrieval pairs."""
    result = copy.deepcopy(evidence)
    omitted = {}
    for field in MEMORY_PREFIXES:
        sources = evidence.get(field, [])
        result[field] = [source for source in sources if not pending_source_requests(field, source, evidence, state)]
        omitted[field] = len(sources) - len(result[field])
    result.setdefault("coverage", {})["pendingMemory"] = {"omitted": omitted}
    return result


def belief_material(view):
    """Bounded facts from an already recorded overview, not a new engine read."""
    result = bounded_facts(view, list_limit=16)
    units = [
        {key: copy.deepcopy(unit[key]) for key in ("handle", "name", "owner", "position", "health") if key in unit}
        for unit in view.get("units", view.get("map", {}).get("units", []))
    ]
    result["schema"] = "recorded-belief-material-v1"
    result["facts"]["recordedUnitFacts"] = units[:16]
    if "units" not in view and "units" not in view.get("map", {}):
        result["coverage"].setdefault("unknownFields", []).append("recordedUnitFacts")
    if len(units) > 16:
        result["coverage"]["omitted"]["recordedUnitFacts"] = len(units) - 16
    result["fullFingerprint"] = fingerprint({"material": result["fullFingerprint"], "recordedUnitFacts": units})
    return result


def fit_memory_evidence(evidence, settings):
    """Omit whole optional candidates instead of shortening attributed text."""
    result = copy.deepcopy(evidence)
    omitted = {field: 0 for field in ("retrieval", "preservation", "beliefs", "newEvidence")}
    result["coverage"]["inputBudget"] = {"omitted": omitted, "sourceTextTruncated": False}
    while any(result[field] for field in ("retrieval", "preservation", "beliefs")):
        try:
            request_body(result, settings)
            break
        except ValueError as error:
            if str(error) not in {"Decisions request byte limit exceeded", "Decisions question limit or empty batch"}:
                # Delivery's ordinary validation owns other malformed inputs.
                break
        statements = {
            (source["key"], source["version"]): source
            for source in [*result["preservation"], *result["newEvidence"]]
            if "source" in source
        }
        if result["retrieval"]:
            result["retrieval"].pop()
            omitted["retrieval"] += 1
        elif len(statements) > 1:
            oldest = min(statements, key=lambda key: statement_order(statements[key]))
            for field in ("preservation", "newEvidence"):
                kept = [source for source in result[field] if (source["key"], source["version"]) != oldest]
                omitted[field] += len(result[field]) - len(kept)
                result[field] = kept
        elif len(result["beliefs"]) > 1:
            result["beliefs"].pop()
            omitted["beliefs"] += 1
        elif statements:
            # Preserve one complete attributed source before a large overview
            # comparison; the latter is optional and remains explicitly readable.
            overview = [source for source in result["newEvidence"] if "source" not in source]
            if overview:
                result["newEvidence"] = [source for source in result["newEvidence"] if "source" in source]
                omitted["newEvidence"] += len(overview)
            else:
                oldest = next(iter(statements))
                for field in ("preservation", "newEvidence"):
                    kept = [source for source in result[field] if (source["key"], source["version"]) != oldest]
                    omitted[field] += len(result[field]) - len(kept)
                    result[field] = kept
        elif result["beliefs"]:
            result["beliefs"].pop()
            omitted["beliefs"] += 1
        else:
            break
        if not result["newEvidence"]:
            omitted["beliefs"] += len(result["beliefs"])
            result["beliefs"] = []
    return result


class AdvisoryStore:
    def __init__(self, memory):
        self.memory = memory

    def state(self):
        with self.memory.transaction() as db:
            return self.memory._meta(db, "advisories", {})

    def save(self, *, expected_epoch=None, **values):
        with self.memory.transaction() as db:
            if expected_epoch is not None and self.memory._meta(db, "epoch") != expected_epoch:
                return False
            state = self.memory._meta(db, "advisories", {})
            state.update(copy.deepcopy(values))
            self.memory._set(db, "advisories", state)
            if "reviewDelivery" in values:
                self._record_delivery(db, "review", state.get("reviewDeliveryTurn"), values["reviewDelivery"])
            return True

    def save_attempt(self, epoch, identifier, result):
        """Merge one scoped completion without overwriting concurrent attempts."""
        with self.memory.transaction() as db:
            if self.memory._meta(db, "epoch") != epoch:
                return False
            state = self.memory._meta(db, "advisories", {})
            state.setdefault("attempts", {})[identifier] = copy.deepcopy(result)
            if "accounting" in result:
                state["accounting"] = copy.deepcopy(result["accounting"])
            if result.get("brokerReconciliation"):
                state.setdefault("reconciliations", {})[identifier] = 1
            self.memory._set(db, "advisories", state)
            return True

    def _record_delivery(self, db, source, identity, rows, *, job=None):
        """Publish sanitized exposure with its domain commit, using existing ancestry."""
        owner = self.memory._meta(db, "nativeGatewayOwner")
        if owner is None:
            return  # Native coordinators alone publish this scoped projection.
        from civ_agent.orchestration.contracts import Scope

        if set(owner) != {"version", "game", "player"} or owner["version"] != 1:
            raise ValueError("ADVISORY_DELIVERY_OWNER_CONFLICT")
        if source == "review" and (type(identity) is not int or identity < 0):
            raise ValueError("ADVISORY_DELIVERY_REVIEW_TURN_REQUIRED")
        scope = Scope(
            game=owner["game"],
            player=owner["player"],
            epoch=self.memory._meta(db, "epoch"),
            activation=fingerprint(["advisory-delivery", source, identity]),
        )
        selected = classifier_metadata("advisory_delivery", {"deliveryResults": rows})["deliveryResults"]
        selected.sort(key=lambda row: (row["requestId"] or "", row["name"], row["status"]))
        key = "advisory-delivery:" + scope.activation
        store = RecordStore(db)
        prior = store.all(categories={DELIVERY_CATEGORY}, keys=[key], project=False).get(key)
        if not selected and prior is None:
            return
        event = {
            "version": DELIVERY_VERSION,
            "scope": scope.model_dump(mode="json"),
            "source": source,
            "policyVersion": TASK_VERSION,
            "deliveryResults": selected,
        }
        if prior is not None and {key: value for key, value in prior["body"].items() if key != "atMs"} == event:
            return
        # This is when the coordinator observed the exposure, not a provider
        # completion timestamp. Failed commits retain neither update nor event.
        store.write(key, DELIVERY_CATEGORY, {**event, "atMs": time.time_ns() // 1_000_000}, job)

    def freeze(self, job, topics, settings) -> JsonObject:
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
            context = {"player": self.memory.civilization, "objectives": objectives, "topics": sorted(topics)}

            def completed(field, source, new_evidence=()):
                key = source_attempt_key(field, source, **context, new_evidence=new_evidence)
                return state.get("sourceAttempts", {}).get(key, {}).get("status") in TERMINAL_SOURCE_STATUSES

            current = RecordStore(db).all(categories={"statement", "observation"})
            statements = sorted(
                (
                    statement_reference(key, record)
                    for key, record in current.items()
                    if record["category"] == "statement"
                ),
                key=statement_order,
                reverse=True,
            )
            limit = settings.decisions_preservation_candidate_limit
            # Preservation adjudication does not establish that a claim has
            # already been considered against every active belief.
            new_evidence = list(statements[:limit])
            sources = [source for source in statements if not completed("preservation", source)][:limit]
            overview = current.get("observation:overview")
            previous = None
            material = None
            if overview:
                parent = db.scalar(
                    select(tables.records.c.parent).where(
                        tables.records.c.key == "observation:overview", tables.records.c.version == overview["version"]
                    )
                )
                row = (
                    db.execute(
                        select(tables.records.c.version, tables.records.c.body)
                        .where(
                            tables.records.c.key == "observation:overview",
                            tables.records.c.version == parent,
                        )
                        .limit(1)
                    )
                    .mappings()
                    .first()
                )
                previous = belief_material(json.loads(row["body"])) if row else None
                material = belief_material(overview["body"])
                changes = material_changes(previous, material)
                new_evidence.append(
                    {
                        "key": "observation:overview",
                        "version": overview["version"],
                        "record": {"body": {"current": material["facts"], "comparison": changes}},
                        "previousSource": {"key": "observation:overview", "version": row["version"]} if row else None,
                        "coverage": material["coverage"],
                        "factsFingerprint": material["fullFingerprint"],
                        "projection": "Bounded recorded overview facts; omitted information is unknown.",
                    }
                )
            beliefs = [reference(k, r) for k, r in candidates if r["category"] == "belief"]
            # A changed ranking context is a selection need only if optional
            # candidates compete or include older entries omitted deterministically.
            selected, _ = select_context({**snapshot, "contextTopics": sorted(topics)}, self.memory.context_limit)
            retrieval_needed = any(r["key"] not in selected for r in retrieval)
            result = {
                **context,
                "retrieval": [
                    source
                    for source in retrieval
                    if not all(completed(field, source) for field in ("retrieval_include", "retrieval_priority"))
                ]
                if retrieval_needed
                else [],
                "preservation": sources,
                "newEvidence": new_evidence,
                "beliefs": [source for source in beliefs if not completed("beliefs", source, new_evidence)][
                    : settings.decisions_belief_candidate_limit
                ]
                if new_evidence
                else [],
                "coverage": {
                    "semantics": "Bounded recorded evidence only; absence does not establish falsity or redundancy.",
                    "retrieval": {"activeCandidates": len(candidates), "shortlisted": len(retrieval)},
                    "statements": {"available": len(statements), "newEvidenceIncluded": min(len(statements), limit)},
                    "beliefEvidence": {
                        "recordedOverviewAvailable": overview is not None,
                        "previousRecordedOverviewAvailable": previous is not None,
                        "detailObservationsIncluded": False,
                        "current": material["coverage"] if material else None,
                        "previous": previous["coverage"] if previous else None,
                    },
                },
            }
            result = fit_memory_evidence(result, settings)
            result["beliefs"] = [
                source for source in result["beliefs"] if not completed("beliefs", source, result["newEvidence"])
            ]
            result["coverage"]["statements"]["newEvidenceIncluded"] = sum(
                "source" in source for source in result["newEvidence"]
            )
            return result

    def apply(self, job, evidence, result):
        with self.memory.transaction() as db:
            self.memory._validate(db, job.id)
            snapshot = load_snapshot(db, db.scalar(select(tables.jobs.c.snapshot).where(tables.jobs.c.id == job.id)))
            answers = {a["name"]: a for a in result.get("answers", [])}
            ranks = {}
            notices, preservation = [], []
            current = RecordStore(db).all()
            state = self.memory._meta(db, "advisories", {})
            cached = state.get("retrievalRankings", {})
            bindings = delivery_basis(evidence, result) if result.get("requestId") else []
            context = {k: evidence[k] for k in ("player", "objectives", "topics")}
            objectives_current = objective_set_current(
                current, evidence.get("objectives", []), snapshot.get("gameTurn")
            )
            for key, record in snapshot["records"].items():
                if (
                    not objectives_current
                    or record["category"] not in {"note", "belief"}
                    or not active(record, snapshot.get("gameTurn"))
                ):
                    continue
                source = reference(key, record)
                cache_key = source_attempt_key("retrieval_include", source, **context)
                saved = cached.get(cache_key)
                if saved and current.get(key, {}).get("version") == record["version"]:
                    ranks[key] = copy.deepcopy(saved["rank"])
            evidence_current = objectives_current and all(
                current.get(s["key"], {}).get("version") == s["version"] for s in evidence.get("newEvidence", [])
            )
            for field in ("retrieval", "preservation", "beliefs"):
                for index, source in enumerate(evidence.get(field, [])):
                    record = current.get(source["key"])
                    if not record or record["version"] != source["version"]:
                        continue
                    if field == "retrieval" and active(record, snapshot.get("gameTurn")):
                        if not objectives_current:
                            continue
                        include = answers.get(f"retrieval_include:{index}", {}).get("choice")
                        priority = answers.get(f"retrieval_priority:{index}", {}).get("choice")
                        if include not in {"include", "omit"} and priority not in {"high", "normal"}:
                            continue
                        rank = {**ranks.get(source["key"], {}), "priority": "high" if priority == "high" else "normal"}
                        if include in {"include", "omit"}:
                            rank["include"] = include == "include"
                        ranks[source["key"]] = rank
                        cache_key = source_attempt_key("retrieval_include", source, **context)
                        carried = {
                            row["name"].split(":", 1)[0]: row
                            for row in cached.get(cache_key, {}).get("bindings", [])
                            if row["name"].startswith("retrieval_include:")
                        }
                        for row in bindings:
                            name = row.get("name")
                            if (
                                isinstance(name, str)
                                and name.startswith("retrieval_")
                                and row.get("source") == {"key": source["key"], "version": source["version"]}
                                and row.get("accepted")
                            ):
                                carried[name.split(":", 1)[0]] = row
                        cached[cache_key] = {
                            "rank": rank,
                            "source": {"key": source["key"], "version": source["version"]},
                            "bindings": list(carried.values()),
                        }
                        continue
                    answer = answers.get(f"{field}:{index}")
                    if not answer:
                        continue
                    if field == "preservation" and answer["choice"] == "consider":
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
            state["retrievalRankings"] = cached
            self.memory._set(db, "advisories", state)
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
            snapshot["advisoryDeliveryBasis"] = copy.deepcopy(job.memory.get("advisoryDeliveryBasis", []))
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

    def record_delivery(self, job, rows):
        with self.memory.transaction() as db:
            self.memory._validate(db, job.id)
            snapshot = load_snapshot(db, db.scalar(select(tables.jobs.c.snapshot).where(tables.jobs.c.id == job.id)))
            snapshot["advisoryDelivery"] = copy.deepcopy(rows)
            db.execute(
                tables.jobs.update().where(tables.jobs.c.id == job.id).values(snapshot=dump_snapshot(db, snapshot))
            )
            self._record_delivery(db, "role", job.id, rows, job=job.id)

    def sources_current(self, sources):
        with self.memory.transaction() as db:
            current = RecordStore(db).all(keys=[r["key"] for r in sources])
            return all(current.get(r["key"], {}).get("version") == r["version"] for r in sources)

    def objectives_current(self, objectives, turn):
        with self.memory.transaction() as db:
            return objective_set_current(RecordStore(db).all(categories={"strategy"}), objectives, turn)

    def cached_retrieval_bindings(self, job, evidence):
        """Original answer identities accompany later exposure of a cached rank."""
        with self.memory.transaction() as db:
            self.memory._validate(db, job.id)
            snapshot = load_snapshot(db, db.scalar(select(tables.jobs.c.snapshot).where(tables.jobs.c.id == job.id)))
            current = RecordStore(db).all()
            if not objective_set_current(current, evidence.get("objectives", []), snapshot.get("gameTurn")):
                return []
            context = {key: evidence[key] for key in ("player", "objectives", "topics")}
            cached = self.memory._meta(db, "advisories", {}).get("retrievalRankings", {})
            rows = []
            for key, record in snapshot["records"].items():
                if (
                    record["category"] not in {"note", "belief"}
                    or not active(record, snapshot.get("gameTurn"))
                    or current.get(key, {}).get("version") != record["version"]
                ):
                    continue
                cache_key = source_attempt_key("retrieval_include", reference(key, record), **context)
                rows.extend(copy.deepcopy(cached.get(cache_key, {}).get("bindings", [])))
            return rows

    def review_input(self, turn, settings, next_review_turn):
        from civ_agent.advisory_review import review_input

        return review_input(self.memory, turn, settings, next_review_turn)

    def review_current(self, evidence):
        from civ_agent.advisory_review import review_current

        return review_current(self.memory, evidence)
