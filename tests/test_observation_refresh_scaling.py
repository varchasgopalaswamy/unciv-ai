"""Regressions for the turn-126 refresh bottleneck, without paid model calls."""

import copy

from sqlalchemy import event

from civ_agent import civilization_state
from civ_agent.civilization_state import CivilizationState
from civ_agent.observations import DetailQuery, detail_fingerprint, detail_scope, overview, project_detail
from civ_agent.protocol import JsonObject
from tests.domain_fixtures import Host, create, strategy
from tests.memory_helpers import stored_records


def test_past_map_queries_stay_historical_while_current_assumptions_refresh(tmp_path, monkeypatch):
    memory = CivilizationState(tmp_path / "state", "Rome")
    memory.bind("g1")
    memory.start_job("old", "gameplay", {}, game_turn=1)
    body: JsonObject = {**Host().view, "turn": 1, "epoch": "g1", "phase": "ACTIVE", "revision": "r1"}
    body["map"]["tiles"] = [
        {"position": {"x": x, "y": 0}, "visible": True, "terrain": "Grassland", "features": [], "units": []}
        for x in range(160)
    ]
    memory.observe_player(body, "old")
    queries = [DetailQuery(tool="map_region", arguments={"center": {"x": x, "y": 0}, "radius": 0}) for x in range(150)]
    for query in queries:
        memory.observe_detail(detail_scope(query), project_detail(body, query), "old", detail_fingerprint(body, query))
    watched = "observation:" + detail_scope(queries[0])
    key = create(memory, strategy(assumptions=[{"text": "Route remains clear", "source": watched}]), "old")
    memory.finish_job("old", True)
    memory.start_job("new", "gameplay", {}, game_turn=2)
    before = stored_records(memory)
    after = copy.deepcopy(body)
    after.update(turn=2, revision="r2")
    for tile in after["map"]["tiles"]:
        tile["features"] = ["Forest"]
    projections = []
    original = civilization_state.project_detail

    def projected(snapshot, query):
        projections.append(query)
        return original(snapshot, query)

    monkeypatch.setattr(civilization_state, "project_detail", projected)
    memory.observe_player(after, "new")
    current = stored_records(memory)
    assert projections == [queries[0]]
    assert current[key]["body"]["needsReview"]
    assert current[watched]["version"] > before[watched]["version"]
    for query in queries[1:]:
        scope = "observation:" + detail_scope(query)
        assert current[scope] == before[scope]
    # An explicit read still gives the current region with a new evidence version.
    query = queries[-1]
    memory.observe_detail(detail_scope(query), project_detail(after, query), "new", detail_fingerprint(after, query))
    assert stored_records(memory)["observation:" + detail_scope(query)]["version"] > 1


def test_detail_dependencies_respect_regions_sections_and_wrapping():
    body = Host().view
    body["map"].update(
        tiles=[{"position": {"x": x, "y": 0}, "visible": True, "features": []} for x in range(10)],
        wrapOffset={"x": 10, "y": 0},
    )
    region = DetailQuery(tool="map_region", arguments={"center": {"x": 0, "y": 0}, "radius": 1})
    movement = DetailQuery(tool="unit_details", arguments={"target": "unit1", "sections": ["movement"]})
    original = [detail_fingerprint(body, query) for query in (region, movement)]
    body["map"]["tiles"][5]["features"] = ["Forest"]
    body["map"]["units"][0]["worker"] = {"currentJob": "Farm"}
    assert [detail_fingerprint(body, query) for query in (region, movement)] == original
    body["map"]["tiles"][9]["features"] = ["Forest"]
    body["map"]["units"][0]["movement"] = "1"
    assert all(detail_fingerprint(body, query) != old for query, old in zip((region, movement), original, strict=True))


def test_many_changed_observations_batch_dependency_updates_and_keep_history(tmp_path, monkeypatch):
    memory = CivilizationState(tmp_path / "state", "Rome")
    memory.bind("g1")
    memory.start_job("game", "gameplay", {}, game_turn=1)
    body: JsonObject = {**Host().view, "phase": "ACTIVE", "epoch": "g1", "revision": "r1", "turn": 1}
    unit = body["map"]["units"][0]
    body["map"]["units"] = [{**unit, "handle": f"unit{i}"} for i in range(150)]
    memory.observe_player(body, "game")
    first = create(memory, strategy(assumptions=[{"text": "Healthy unit", "source": "observation:unit/unit0"}]))
    second = create(
        memory, {"type": "belief", "claim": "Past defense assessment", "uncertainty": "high", "sources": [first]}
    )
    # Hundreds of one-off queries from past turns must remain citeable without
    # being projected or rewritten when their inputs have not changed.
    for i in range(150):
        query = DetailQuery(tool="unit_details", arguments={"target": f"lost-unit{i}"})
        memory.observe_detail(detail_scope(query), project_detail(body, query), "game", detail_fingerprint(body, query))
    before = stored_records(memory)
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
    current = stored_records(memory)
    assert current[first]["body"]["needsReview"]
    assert not current[second]["body"]["needsReview"]  # No transitive proof graph.
    for key in (first, second):
        assert current[key]["version"] == before[key]["version"]
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
    key = create(memory, strategy(assumptions=[{"text": "Border covered", "source": "observation:border"}]))
    memory.observe("border", {"units": 2}, "game")
    memory.observe("border", {"units": 3}, "game")
    records = stored_records(memory)
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
    current = stored_records(memory)["observation:" + scope]
    assert current["body"]["data"]["entity"]["health"] == 50
