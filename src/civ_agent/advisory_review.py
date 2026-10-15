"""Early-review inputs from committed, player-visible records and review baselines."""

import copy
import re

from sqlalchemy import select

from civ_agent import memory_schema as tables
from civ_agent.advisory_evidence import MATERIAL_SCHEMA, bounded_facts, material_changes
from civ_agent.decisions_ledger import canonical, fingerprint
from civ_agent.decisions_tasks import TASK_VERSION
from civ_agent.memory_store import RecordStore
from civ_agent.protocol import JsonObject


def completed_baseline(parent, inputs) -> JsonObject:
    """Publish this only in the transaction that commits the corresponding report."""
    view = inputs.get("reads", {}).get("player_turn", inputs["observation"])
    baseline = {
        **bounded_facts(view),
        "available": True,
        "reviewTurn": inputs["turn"],
        "sourceTurn": view.get("turn"),
        "source": {"kind": "completed_review", "job": parent},
    }
    return {**baseline, "identity": fingerprint(baseline)}


def _last_completed(db):
    return db.scalar(
        select(tables.reviews.c.turn)
        .where(tables.reviews.c.status == "COMPLETED")
        .order_by(tables.reviews.c.turn.desc())
        .limit(1)
    )


def _baseline(state, last) -> JsonObject | None:
    baseline = state.get("reviewMaterialBaseline")
    if (
        isinstance(baseline, dict)
        and baseline.get("schema") == MATERIAL_SCHEMA
        and baseline.get("reviewTurn") == last
        and baseline.get("identity") == fingerprint({k: v for k, v in baseline.items() if k != "identity"})
    ):
        return copy.deepcopy(baseline)
    return None


def review_input(memory, turn, settings, next_review_turn) -> JsonObject | None:
    """No host reads; turn and next_review_turn both count completed rounds."""
    from civ_agent.advisory_store import active, reference

    if type(next_review_turn) is not int or next_review_turn <= turn + 1:
        return None
    with memory.transaction() as db:
        last = _last_completed(db)
        state = memory._meta(db, "advisories", {})
        if last is None or turn - last < settings.early_review_min_gap_turns:
            return None
        early = state.get("lastEarlyTurn")
        if early is not None and turn - early < settings.early_review_min_gap_between_early_turns:
            return None
        records = RecordStore(db).all(categories={"strategy", "observation", "statement"})
        overview = records.get("observation:overview")
        if overview is None:
            return None
        current = bounded_facts(overview["body"])
        baseline = _baseline(state, last)
        if baseline is None:
            return None
        objectives = [reference(k, r) for k, r in records.items() if r["category"] == "strategy" and active(r, turn)]
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
        if current["fullFingerprint"] == baseline.get("fullFingerprint") and not statements:
            return None
        source = {"key": "observation:overview", "version": overview["version"]}
        current.update(source=source, sourceTurn=overview["body"].get("turn"))
        # Stable schedule identity matters; routine turn/version increments do not
        # trigger another paid classification of the same material evidence.
        signature = fingerprint(
            {
                "baselineIdentity": baseline["identity"],
                "currentFacts": current["fullFingerprint"],
                "currentCoverage": current["coverage"],
                "statements": statements,
                "objectives": objectives,
                "nextReviewTurn": next_review_turn,
            }
        )
        return {
            "version": TASK_VERSION,
            "player": memory.civilization,
            "objectives": objectives,
            "coverage": {
                "reviewBaselineAvailable": baseline["available"],
                "currentObservationTurnKnown": type(current["sourceTurn"]) is int,
                "statements": "bounded_objective_word_overlap_since_last_review",
                "omittedInformationDoesNotEstablishAbsence": True,
            },
            "earlyReview": {
                "baseline": baseline,
                "current": current,
                "deltas": material_changes(baseline if baseline["available"] else None, current),
                "lastReviewTurn": last,
                "sourceTurn": current["sourceTurn"],
                "boundaryTurn": turn + 1,
                "completedTurn": turn,
                "nextReviewTurn": next_review_turn,
                "statements": statements,
                "progress": copy.deepcopy(overview["body"].get("recentProgress", {})),
            },
            "signature": signature,
        }


def review_current(memory, evidence):
    from civ_agent.advisory_store import active

    review = evidence["earlyReview"]
    refs = [review["current"]["source"], *evidence.get("objectives", []), *review.get("statements", [])]
    with memory.transaction() as db:
        current = RecordStore(db).all(keys=[r["key"] for r in refs])
        state = memory._meta(db, "advisories", {})
        objectives = RecordStore(db).all(categories={"strategy"})
        objective_versions = {
            key: record["version"] for key, record in objectives.items() if active(record, review["completedTurn"])
        }
        baseline = _baseline(state, _last_completed(db))
        return (
            objective_versions == {r["key"]: r["version"] for r in evidence.get("objectives", [])}
            and baseline is not None
            and baseline["identity"] == review["baseline"]["identity"]
            and all(current.get(r["key"], {}).get("version") == r["version"] for r in refs)
        )


def current_review_binding(memory, turn, settings, next_review_turn, signature):
    """Rebind a judgment only to current evidence with the same semantic context.

    The original request remains immutable. Routine record-version changes can
    update these local delivery bindings without another provider classification.
    """
    current = review_input(memory, turn, settings, next_review_turn)
    if current is None or current.pop("signature") != signature:
        return None
    return current if review_current(memory, current) else None
