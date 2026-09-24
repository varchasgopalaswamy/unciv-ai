"""Regressions for the turn-126 refresh bottleneck, without paid model calls."""

import copy

from sqlalchemy import event

from civ_agent import civilization_state
from civ_agent.civilization_state import CivilizationState
from civ_agent.observations import DetailQuery, detail_fingerprint, detail_scope, overview, project_detail
from civ_agent.protocol import JsonObject
from tests.test_proposal_gateway import Host
from tests.test_structured_memory import create, plan


def test_many_changed_observations_batch_dependency_updates_and_keep_history(tmp_path, monkeypatch):
    memory = CivilizationState(tmp_path / "state", "Rome")
    memory.bind("g1")
    memory.start_job("game", "gameplay", {}, game_turn=1)
    body: JsonObject = {**Host().view, "phase": "ACTIVE", "epoch": "g1", "revision": "r1", "turn": 1}
    unit = body["map"]["units"][0]
    body["map"]["units"] = [{**unit, "handle": f"unit{i}"} for i in range(150)]
    memory.observe_player(body, "game")
    first = create(memory, plan(assumptions=[{"text": "Healthy unit", "source": "observation:unit/unit0"}]))
    second = create(memory, plan(assumptions=[{"text": "Defense plan holds", "source": first}]))
    # Hundreds of one-off queries from past turns must remain citeable without
    # being projected or rewritten when their inputs have not changed.
    for i in range(150):
        query = DetailQuery(tool="unit_details", arguments={"target": f"lost-unit{i}"})
        memory.observe_detail(detail_scope(query), project_detail(body, query), "game", detail_fingerprint(body, query))
    before = memory.snapshot("gameplay")["records"]
    projections = []
    original = civilization_state.project_detail

    def projected(snapshot, query):
        projections.append(query)
        return original(snapshot, query)

    monkeypatch.setattr(civilization_state, "project_detail", projected)
    queries = []

    def capture(_connection, _cursor, statement, _parameters, _context, _many):
        queries.append(statement)

    event.listen(memory.engine, "before_cursor_execute", capture)
    after = copy.deepcopy(body)
    after.update(turn=2, revision="r2")
    for item in after["map"]["units"]:
        item["health"] = 50
    memory.observe_player(after, "game", overview(after))
    event.remove(memory.engine, "before_cursor_execute", capture)
    # The previous implementation performed a complete records/heads scan for
    # each changed unit and historical detail. Allow a small constant overhead.
    full_reads = [q for q in queries if "JOIN heads" in q and "WHERE" not in q]
    assert len(full_reads) <= 3, len(full_reads)
    assert projections == []
    current = memory.snapshot("gameplay")["records"]
    for key in (first, second):
        assert current[key]["body"]["needsReview"]
        assert current[key]["version"] == before[key]["version"] + 1
    for key in before:
        if key.startswith("observation:detail/"):
            assert current[key] == before[key]
    historical = memory.search_memory("game", {"id": "observation:unit/unit0", "history": True})
    assert any(row["body"]["entity"]["health"] == 100 for row in historical["records"])


def test_new_evidence_advances_review_even_when_reason_is_already_present(tmp_path):
    memory = CivilizationState(tmp_path / "state", "Rome")
    memory.bind("g1")
    memory.start_job("game", "gameplay", {})
    memory.observe("border", {"units": 1}, "game")
    key = create(memory, plan(assumptions=[{"text": "Border covered", "source": "observation:border"}]))
    memory.observe("border", {"units": 2}, "game")
    memory.observe("border", {"units": 3}, "game")
    records = memory.snapshot("gameplay")["records"]
    assert records[key]["body"]["_review"] == [{"key": "observation:border", "version": 3}]
    assert len(records[key]["body"]["needsReview"]) == 1


def test_late_cached_read_cannot_mask_newer_game_state(tmp_path):
    memory = CivilizationState(tmp_path / "state", "Rome")
    memory.bind("g1")
    memory.start_job("game", "gameplay", {})
    body: JsonObject = {**Host().view, "phase": "ACTIVE", "epoch": "g1", "revision": "r1"}
    query = DetailQuery(tool="unit_details", arguments={"target": "unit1"})
    scope = detail_scope(query)
    memory.observe_player(body, "game", overview(body))
    memory.observe_detail(scope, project_detail(body, query), "game", detail_fingerprint(body, query))
    newer = copy.deepcopy(body)
    newer["map"]["units"][0]["health"] = 50
    newer["revision"] = "r2"
    memory.observe_player(newer, "game", overview(newer))
    # Another job can still have an older permitted snapshot cached. Its late
    # read changes the detail head, even though observation:game stays at r2.
    memory.observe_detail(scope, project_detail(body, query), "game", detail_fingerprint(body, query))
    memory.observe_player(newer, "game", overview(newer))
    current = memory.snapshot("gameplay")["records"]["observation:" + scope]
    assert current["body"]["data"]["entity"]["health"] == 50
