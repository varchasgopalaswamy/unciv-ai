"""Private assessments keep history and provenance without enforcing strategy."""

import json

from civ_agent.observations import DetailQuery, detail_scope
from tests.domain_fixtures import create, current, replacement, setup, strategy


def test_notes_can_describe_unreviewed_and_retired_plans(tmp_path):
    memory = setup(tmp_path)
    memory.observe("border", {"turn": 4, "units": ["Warrior"]}, "game")
    key = create(memory, strategy(assumptions=[{"text": "Warrior remains nearby", "source": "observation:border"}]))
    memory.observe("border", {"turn": 5, "units": []})
    note = create(memory, {"type": "note", "text": "This strategy failed to protect the border", "relatedMemory": key})
    assert current(memory, note)["_relatedMemory"]["key"] == key
    assert current(memory, key)["needsReview"]
    memory.patch("game", [{"op": "retire", "id": key, "reason": "abandoned"}])
    second = create(memory, {"type": "note", "text": "Remember why we abandoned this strategy", "relatedMemory": key})
    assert not current(memory, second)["needsReview"]
    assert not current(memory, note)["needsReview"]


def test_retired_plan_and_unproven_belief_do_not_gate_commands(tmp_path):
    memory = setup(tmp_path)
    key = create(memory, strategy())
    memory.patch("game", [{"op": "retire", "id": key, "reason": "completed"}])
    belief = create(memory, {"type": "belief", "claim": "We could still win", "uncertainty": "high"})
    assert current(memory, belief)["sources"] == []
    queued = memory.queue(
        "game", "player_command", {"kind": "ATTACK"}, "attack", {"strategy": key, "reason": "Defend Rome"}
    )
    assert queued["id"] and memory.pending()


def test_revisions_keep_citations_without_granting_new_claims_unread_evidence(tmp_path):
    memory = setup(tmp_path)
    query = DetailQuery(tool="unit_details", arguments={"target": "u1"})
    _, detail = memory.observe_detail(
        detail_scope(query), {"query": query.model_dump(), "turn": 4, "data": {"health": 40}}, "game"
    )
    content = strategy(sources=[detail["evidence"]])
    key = create(memory, content)
    original = current(memory, key)["_evidence"]
    memory.finish_job("game", True)
    memory.start_job("next", "gameplay", {}, game_turn=5)
    memory.refresh_memory("next", [key])
    assert memory.patch("next", [replacement(memory, key, **{"reasons": ["Withdraw"]})])["changes"][0]["accepted"]
    assert current(memory, key)["_evidence"] == original
    fresh = {"type": "belief", "claim": "Historical unit was hurt", "uncertainty": "low", "sources": content["sources"]}
    unread = memory.patch("next", [{"op": "create", "content": fresh}])["changes"][0]
    assert unread["accepted"] and unread["warnings"][0]["code"] == "UNVERIFIED_REFERENCES"
    assert current(memory, unread["id"])["_evidence"] == []


def test_advisory_notices_do_not_change_notebook_versions_or_require_evidence_reads(tmp_path):
    memory = setup(tmp_path)
    memory.observe("border", {"turn": 4, "units": ["Warrior"]}, "game")
    key = create(memory, strategy(assumptions=[{"text": "Warrior is at the border", "source": "observation:border"}]))
    original = memory.snapshot("gameplay")["records"][key]
    memory.observe("border", {"turn": 4, "units": []})
    changed = memory.snapshot("gameplay")["records"][key]
    assert changed["version"] == original["version"] and changed["body"]["needsReview"]
    memory.refresh_memory("game", [key])
    assert memory.patch("game", [replacement(memory, key, **{"reasons": ["Reassess"]})])["changes"][0]["accepted"]
    assert not current(memory, key)["needsReview"]
    assert current(memory, key)["_assumptions"][0]["version"] == 1


def test_old_evidence_is_immutable_and_unknown_references_reveal_no_new_state(tmp_path):
    memory = setup(tmp_path)
    query = DetailQuery(tool="unit_details", arguments={"target": "u1"})
    scope = detail_scope(query)
    body = {"query": query.model_dump(), "turn": 4, "data": {"health": 40}}
    _, first = memory.observe_detail(scope, body, "game")
    assert memory.observe_detail(scope, body, "game")[1]["evidence"] == first["evidence"]
    _, second = memory.observe_detail(scope, {**body, "data": {"health": 20}}, "game")
    assert first["evidence"] != second["evidence"]
    remembered = create(memory, strategy(sources=[first["evidence"]]))
    assert current(memory, remembered)["_evidence"][0]["version"] == 1
    memory.start_job("chat", "communication", {})
    _, unseen = memory.observe_detail(scope, {**body, "data": {"secret": "NEW_INFORMATION"}}, "chat")
    result = memory.patch("game", [replacement(memory, remembered, **{"sources": ["not-a-reference"]})])
    assert result["changes"][0]["accepted"]
    assert unseen["evidence"] not in json.dumps(result) and "NEW_INFORMATION" not in json.dumps(result)
    assert current(memory, remembered)["_evidence"] == []
