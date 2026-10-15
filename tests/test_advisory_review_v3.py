"""Comparable committed baselines and scheduler timing for bounded early triage."""

import asyncio
import copy
from types import SimpleNamespace
from typing import cast

import pytest

from civ_agent import memory_schema as tables
from civ_agent.advisory_evidence import bounded_facts, material_changes
from civ_agent.advisory_review import review_current, review_input
from civ_agent.agent_service import AgentService, ServiceError
from civ_agent.civilization_state import CivilizationState
from civ_agent.decisions_policy import DecisionsSettings
from civ_agent.memory_store import RecordStore
from civ_agent.review_store import ReviewStore


@pytest.fixture
def memory(tmp_path):
    state = CivilizationState(tmp_path / "memory", "Rome")
    state.bind("g1")
    yield state
    state.engine.dispose()


def position(turn=0):
    return {
        "turn": turn,
        "cities": [{"handle": "c1", "name": "Rome", "constructionQueue": ["Library"]}],
        "economy": {"happiness": 2, "goldPerTurn": 3, "treasury": 20},
    }


def complete(memory, job="opening", turn=0, view=None):
    view = position(turn) if view is None else view
    store = ReviewStore(memory)
    store.prepare(job, {"turn": turn, "observation": view, "reads": {"player_turn": view}}, {}, 60)
    store.complete(job, {})


def observe(memory, view):
    with memory.transaction() as db:
        RecordStore(db).write("observation:overview", "observation", view)


def changed_position(turn=3):
    view = position(turn)
    view["economy"]["goldPerTurn"] = -4
    view["recentProgress"] = {"cities": [{"city": "c1", "observedResult": "Library completed"}]}
    return view


def test_input_compares_matching_saved_facts_and_keeps_progress_and_round_conventions(memory):
    complete(memory)
    observe(memory, changed_position())
    evidence = review_input(memory, 3, DecisionsSettings(), 10)
    assert evidence is not None and evidence["player"] == "Rome"
    early = evidence["earlyReview"]
    assert early["baseline"]["available"] and early["baseline"]["reviewTurn"] == 0
    assert early["baseline"]["facts"]["economicPressure"]["deficit"] is False
    assert early["current"]["facts"]["economicPressure"]["deficit"] is True
    assert early["deltas"]["fields"] == [
        {
            "field": "economicPressure",
            "complete": True,
            "before": {"unhappy": False, "deficit": False},
            "current": {"unhappy": False, "deficit": True},
        }
    ]
    assert early["progress"] == changed_position()["recentProgress"]
    assert (early["completedTurn"], early["sourceTurn"], early["boundaryTurn"], early["nextReviewTurn"]) == (
        3,
        3,
        4,
        10,
    )
    assert review_current(memory, evidence)


def test_baseline_is_published_only_with_report_and_rewinds_atomically(memory, tmp_path):
    complete(memory)
    with memory.transaction() as db:
        first = memory._meta(db, "advisories")["reviewMaterialBaseline"]
    checkpoint = tmp_path / "boundary.json"
    checksum = memory.backup(checkpoint)
    newer = changed_position(5)
    store = ReviewStore(memory)
    store.prepare("later", {"turn": 5, "observation": newer, "reads": {"player_turn": newer}}, {}, 60)
    store.failed("later", "interrupted")
    with memory.transaction() as db:
        assert memory._meta(db, "advisories")["reviewMaterialBaseline"] == first
    store.complete("later", {})
    with memory.transaction() as db:
        state = memory._meta(db, "advisories")
        assert state["reviewMaterialBaseline"]["reviewTurn"] == 5
        assert state["reviewBaseline"] == state["reviewMaterialBaseline"]["fullFingerprint"]
    store.complete("opening", {})
    with memory.transaction() as db:
        assert memory._meta(db, "advisories")["reviewMaterialBaseline"]["reviewTurn"] == 5
    memory.restore(checkpoint, checksum, "restored")
    with memory.transaction() as db:
        assert memory._meta(db, "advisories")["reviewMaterialBaseline"] == first


@pytest.mark.parametrize("changed", ["source", "objective", "objective_added", "baseline"])
def test_late_answers_reject_changed_source_objective_or_completed_baseline(memory, changed):
    complete(memory)
    observe(memory, changed_position())
    objective = {"assessment": "Keep Rome solvent", "state": "active"}
    with memory.transaction() as db:
        RecordStore(db).write("objective", "strategy", objective)
    evidence = review_input(memory, 3, DecisionsSettings(), 10)
    assert evidence is not None
    assert review_current(memory, evidence)
    if changed == "source":
        observe(memory, changed_position(4))
    elif changed == "objective":
        with memory.transaction() as db:
            RecordStore(db).write("objective", "strategy", {**objective, "assessment": "Defend Rome"})
    elif changed == "objective_added":
        with memory.transaction() as db:
            RecordStore(db).write("new-objective", "strategy", {**objective, "assessment": "Expand Rome"})
    else:
        complete(memory, "new-review", 3, changed_position())
    assert not review_current(memory, evidence)


def test_old_fingerprint_does_not_invent_prior_facts(memory):
    with memory.transaction() as db:
        db.execute(
            tables.reviews.insert().values(
                job="legacy", turn=0, status="COMPLETED", attempts=1, inputs=None, settings="{}", timeout=60
            )
        )
        memory._set(db, "advisories", {"reviewBaseline": bounded_facts(position())["fullFingerprint"]})
    observe(memory, changed_position())
    evidence = review_input(memory, 3, DecisionsSettings(), 10)
    assert evidence is not None
    baseline = evidence["earlyReview"]["baseline"]
    assert not baseline["available"] and "facts" not in baseline
    assert evidence["earlyReview"]["deltas"] == {"baselineAvailable": False, "comparisonComplete": False, "fields": []}
    assert review_current(memory, evidence)
    observe(memory, position(3))
    assert review_input(memory, 3, DecisionsSettings(), 10) is None


def test_stable_facts_ignore_routine_turn_and_record_churn_but_schedule_changes_invalidate(memory):
    complete(memory)
    observe(memory, changed_position(3))
    first = review_input(memory, 3, DecisionsSettings(), 20)
    later = changed_position(4)
    later["economy"]["treasury"] += 10
    observe(memory, later)
    second = review_input(memory, 4, DecisionsSettings(), 20)
    assert first is not None and second is not None
    assert first["signature"] == second["signature"]
    assert not review_current(memory, first)
    rescheduled = review_input(memory, 4, DecisionsSettings(), 25)
    assert rescheduled is not None and second["signature"] != rescheduled["signature"]


@pytest.mark.parametrize("turn,next_review", [(2, 20), (3, 3), (3, 4)])
def test_cheap_gap_or_scheduled_review_guard(memory, turn, next_review):
    complete(memory)
    observe(memory, changed_position())
    assert review_input(memory, turn, DecisionsSettings(), next_review) is None


def test_truncated_rosters_disclose_coverage_and_never_claim_removed_entities():
    view = {"cities": [{"handle": f"c{i}", "name": f"City {i}"} for i in range(70)]}
    before = bounded_facts(view)
    assert len(before["facts"]["cities"]) == 64
    assert before["coverage"]["omitted"] == {"cities": 6}
    later = copy.deepcopy(view)
    later["cities"].reverse()
    current = bounded_facts(later)
    delta = material_changes(before, current)
    assert not delta["comparisonComplete"]
    field = next(field for field in delta["fields"] if field["field"] == "cities")
    assert not field["complete"] and "removed" not in field and "added" not in field
    # A change wholly outside the excerpt is still an explicit material signal.
    later = copy.deepcopy(view)
    later["cities"][-1]["name"] = "Renamed city"
    tail = bounded_facts(later)
    assert tail["facts"] == before["facts"] and tail["fullFingerprint"] != before["fullFingerprint"]
    assert material_changes(before, tail)["fullFactsChanged"]


def test_missing_recorded_fields_cannot_establish_removal_or_pressure_reversal():
    previous = bounded_facts(
        {
            "units": [{"handle": "u1", "name": "Scout", "workerJob": None}],
            "economy": {"goldPerTurn": -3, "happiness": -1},
        }
    )
    current = bounded_facts({"turn": 4})
    assert {"units", "economicPressure"} <= set(current["coverage"]["unknownFields"])
    delta = material_changes(previous, current)
    assert not delta["comparisonComplete"]
    relevant = [row for row in delta["fields"] if row["field"] in {"units", "economicPressure"}]
    assert len(relevant) == 2
    assert all(not row["complete"] and "removed" not in row and "beforeExcerpt" in row for row in relevant)


def test_service_requires_scheduler_timing_and_converts_boundary_once():
    seen = []

    class Admission:
        async def __aenter__(self):
            return None

        async def __aexit__(self, *args):
            return None

    async def triage(turn, **kwargs):
        seen.append((turn, kwargs))
        return {"earlyReview": False}

    service = SimpleNamespace(
        chat=SimpleNamespace(epoch="g1"),
        gateway=SimpleNamespace(
            advisories=SimpleNamespace(triage=triage),
            paused=False,
            checkpoint_requested=False,
            run_failure=None,
            admit=Admission,
        ),
    )

    async def run():
        payload = {"epoch": "g1", "gameTurn": 4, "scheduledSoon": False, "nextReviewTurn": 10}
        await AgentService.review_triage(cast(AgentService, service), payload)
        assert seen == [(3, {"scheduled_soon": False, "next_review_turn": 10})]
        for bad in (
            {k: v for k, v in payload.items() if k != "nextReviewTurn"},
            {**payload, "nextReviewTurn": True},
            {**payload, "nextReviewTurn": 3},
        ):
            with pytest.raises(ServiceError):
                await AgentService.review_triage(cast(AgentService, service), bad)

    asyncio.run(run())
