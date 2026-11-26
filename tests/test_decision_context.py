"""Chosen investments remain advisory, dated and confined to their own timeline."""

import copy
import json

import pytest
from pydantic import ValidationError

from civ_agent.civilization_state import CivilizationState
from civ_agent.memory_records import Strategy
from civ_agent.observations import overview
from civ_agent.review_store import ReviewStore
from tests.domain_fixtures import create, current, replacement, strategy
from tests.test_construction_estimates import snapshot
from tests.test_strategic_review import assessment


def decision(**changes):
    return {
        "id": "capacity",
        "objective": "Improve Rome's output",
        "investment": "Build a Library instead of continuing Science",
        "expectedObservations": ["Rome's science rate increases after completion"],
        "reviewAfterTurns": 5,
        "reconsiderWhen": "New threats change the investment's value",
        "subjects": ["city1"],
        **changes,
    }


def setup(tmp_path):
    memory = CivilizationState(tmp_path / "private", "Rome")
    memory.bind("g1")
    view = snapshot().view
    view["turn"] = 4
    view["research"] = {"sciencePerTurn": "20", "overflowScience": "0", "technologies": []}
    memory.start_job("game", "gameplay", {}, game_turn=4)
    memory.observe_player(view, "game", overview(view))
    key = create(memory, strategy(decisions=[decision()]))
    return memory, view, key


def act(memory, key, call, *, chosen="capacity", job="game", summary="Library queued"):
    request = memory.queue(
        job,
        "player_command",
        {"kind": "QUEUE_CONSTRUCTION", "target": "city1", "selection": "Library"},
        call,
        intent={"reason": "PRIVATE RATIONALE", "strategy": key, "decision": chosen},
    )
    memory.receipt(request["id"], {"actionSummary": summary})
    return request


def test_choice_feedback_tracks_receipts_and_changes_without_claiming_success(tmp_path):
    memory, view, key = setup(tmp_path)
    initial = copy.deepcopy(current(memory, key)["_decisionBaselines"])
    request = act(memory, key, "queue")
    assert act(memory, key, "queue")["id"] == request["id"]  # Exact retry is one action.
    unverified = act(memory, key, "unverified", chosen="unread-choice")  # Annotation never gates the command.
    assert not unverified["intentFeedback"]["linked"]
    assert act(memory, key, "unverified", chosen="unread-choice")["intentFeedback"] == unverified["intentFeedback"]
    assert "intentFeedback" not in request
    for n in range(10):
        act(memory, key, f"more-{n}")
    later = copy.deepcopy(view)
    later["turn"] = 9
    later["research"]["sciencePerTurn"] = "30"
    result = memory.decision_feedback(later)
    row = result["choices"][0]
    assert row["reviewTurn"] == 9 and row["reviewDue"] and row["state"] == "active"
    assert row["actionCounts"] == {"confirmed": 11}
    assert len(row["actions"]) == 8 and row["omittedEarlierActions"] == 3
    assert row["observedChanges"]["totals"]["sciencePerTurn"]["now"] == 30
    assert row["currentCities"]["city1"]["name"] == "Rome"
    assert "PRIVATE RATIONALE" not in json.dumps(result)
    assert not {"success", "failed", "breach"} & row.keys()
    assert current(memory, key)["_decisionBaselines"] == initial
    memory.engine.dispose()


def test_city_subject_names_are_matched_without_guessing_unrelated_tags(tmp_path):
    memory, view, key = setup(tmp_path)
    memory.patch("game", [replacement(memory, key, decisions=[decision(subjects=["ROME", "science"])])])
    feedback = memory.decision_feedback(view)
    assert list(feedback["choices"][0]["currentCities"]) == ["city1"]
    memory.patch("game", [replacement(memory, key, decisions=[decision(subjects=["science"])])])
    assert memory.decision_feedback(view)["choices"][0]["currentCities"] == {}
    memory.engine.dispose()


def test_revision_identity_changes_only_with_choice_and_remembers_ended_choices(tmp_path):
    memory, view, key = setup(tmp_path)
    first = current(memory, key)["_decisionBaselines"]["capacity"]
    act(memory, key, "original")
    assert memory.patch("game", [replacement(memory, key, assessment="Different prose")])["changes"][0]["accepted"]
    assert current(memory, key)["_decisionBaselines"]["capacity"] == first
    assert memory.patch("game", [replacement(memory, key, decisions=[decision(investment="Save for a purchase")])])[
        "changes"
    ][0]["accepted"]
    second = current(memory, key)["_decisionBaselines"]["capacity"]
    assert first["authoredTurn"] == second["authoredTurn"] and first["id"] != second["id"]
    act(memory, key, "revised")
    choices = memory.decision_feedback(view)["choices"]
    assert [c["state"] for c in choices] == ["active", "ended"]
    assert [c["actionCounts"] for c in choices] == [{"confirmed": 1}, {"confirmed": 1}]
    assert choices[0]["baseline"] == second["id"] and choices[1]["baseline"] == first["id"]
    memory.patch("game", [{"op": "retire", "id": key, "reason": "abandoned"}])
    assert all(c["state"] == "ended" for c in memory.decision_feedback(view)["choices"])
    memory.engine.dispose()


def test_choices_and_evidence_survive_review_restart_and_rewind(tmp_path):
    memory, view, key = setup(tmp_path)
    act(memory, key, "before")
    review = ReviewStore(memory)
    review.prepare("first-review", {"turn": 4, "observation": view}, {}, 60)
    review.complete("first-review", assessment())
    saved = tmp_path / "checkpoint.json"
    checksum = memory.backup(saved)
    original = memory.decision_feedback(view)
    act(memory, key, "future", summary="DISCARDED FUTURE")
    memory.patch("game", [replacement(memory, key, decisions=[decision(objective="Discarded objective")])])
    later = {**view, "turn": 10}
    review.prepare("later-review", {"turn": 10, "observation": later}, {}, 60)
    review.complete("later-review", assessment())
    memory.engine.dispose()
    memory = CivilizationState(memory.root, "Rome")
    assert len(memory.decision_feedback(later)["choices"]) == 1
    memory.restore(saved, checksum, "g2")
    memory.pause(False)
    assert memory.decision_feedback(view) == original
    assert ReviewStore(memory).last_completed_turn() == 4
    memory.start_job("resumed", "gameplay", {}, game_turn=10)
    memory.observe_player(later, "resumed", overview(later))
    memory.refresh_memory("resumed", [key])
    act(memory, key, "new-future", job="resumed")
    feedback = memory.decision_feedback(later)
    assert feedback["choices"][0]["actionCounts"] == {"confirmed": 2}
    assert "DISCARDED" not in json.dumps(feedback) and "Discarded objective" not in json.dumps(feedback)
    review = ReviewStore(memory)
    review.prepare("next-review", {"turn": 10, "observation": later, "reads": {"decisionFeedback": feedback}}, {}, 60)
    review.complete("next-review", assessment())
    assert current(memory, key)["decisions"] == [decision()]
    memory.engine.dispose()


def test_unread_snapshot_cannot_supply_a_choice_baseline_or_communication_overwrite_it(tmp_path):
    memory = CivilizationState(tmp_path / "private", "Rome")
    memory.bind("g1")
    memory.start_job("game", "gameplay", {}, game_turn=4)
    view = {**snapshot().view, "turn": 4}
    memory.observe_player(view, "game")  # Raw cache is not a delivered overview.
    key = create(memory, strategy(decisions=[decision()]))
    feedback = memory.decision_feedback({**view, "turn": 9})
    assert not feedback["choices"][0]["observedChanges"]["available"]
    memory.start_job("chat", "communication", {}, game_turn=9)
    assert not memory.patch("chat", [replacement(memory, key, decisions=[])])["changes"][0]["accepted"]
    assert current(memory, key)["decisions"] == [decision()]
    memory.engine.dispose()


def test_choice_schema_bounds_structure_without_requiring_a_playstyle():
    Strategy.model_validate(strategy(decisions=[decision(investment="Continue Science conversion")]))
    Strategy.model_validate(strategy(decisions=[]))
    result = Strategy.model_validate(strategy(decisions=[decision(id=str(i)) for i in range(4)]))
    assert len(result.decisions) == 4
    for decisions in (
        [decision(), decision()],
        [decision(reviewAfterTurns=0)],
    ):
        with pytest.raises(ValidationError):
            Strategy.model_validate(strategy(decisions=decisions))
