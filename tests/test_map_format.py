"""Compact maps preserve visible facts, JSON types, and evidence semantics."""

import copy

import pytest

from civ_agent.map_format import compact_tiles, encoded, expand_tiles
from civ_agent.memory_store import semantic
from civ_agent.observations import DetailQuery, project_detail
from civ_agent.protocol import JsonObject
from tests.test_structured_memory import create, current, plan, setup


def tiles() -> list[JsonObject]:
    return [
        {
            "position": {"x": n, "y": 0},
            "terrain": "Grassland",
            "visible": n < 3,
            "units": [],
            "yields": {"food": 2},
            "owner": None,
            "resource": None,
            "road": None,
            "feature": None,
            "improvement": None,
            "isWater": False,
            "futureField": {"known": True, "values": []},
        }
        for n in range(4)
    ]


def test_compact_map_round_trip_keeps_overrides_types_and_missing_fields():
    source = tiles()
    source[0]["owner"] = "Rome"
    source[1]["resource"] = "Gold"
    source[1]["isWater"] = 0  # Distinct JSON value from false.
    source[2]["futureField"] = None
    source[3]["feature"] = "Forest"
    source[0]["optional"] = None  # Missing elsewhere must not become null.
    saved = copy.deepcopy(source)
    compact = compact_tiles(source)
    assert "tileDefaults" in compact
    assert len(encoded(compact)) < len(encoded({"tiles": source}))
    assert encoded(expand_tiles(compact)) == encoded(source)
    assert compact_tiles(source) == compact
    assert source == saved
    assert compact["tiles"][0]["optional"] is None and "optional" not in compact["tileDefaults"]
    assert "optional" not in expand_tiles(compact)[1]
    assert type(expand_tiles(compact)[1]["isWater"]) is int
    assert type(expand_tiles(compact)[0]["isWater"]) is bool
    for row in compact["tiles"]:
        assert {"position", "terrain", "visible", "units", "yields"} <= row.keys()
    expanded = expand_tiles(compact)
    expanded[0]["futureField"]["values"].append("Mutate copy")
    assert compact["tileDefaults"]["futureField"]["values"] == [] and source == saved


@pytest.mark.parametrize("source", [[], [{"position": {"x": 0, "y": 0}, "owner": None}], [{"x": 1}, {"x": 1}]])
def test_compaction_does_not_enlarge_small_regions(source):
    assert compact_tiles(source) == {"tiles": source}


@pytest.mark.parametrize("format", ["compact", "full"])
def test_map_format_follows_existing_fog_wrap_and_region_selection(format):
    source = tiles()
    source[0]["position"] = {"x": 5, "y": -5}
    source[1]["position"] = {"x": -4, "y": 5}  # Neighbor across the seam.
    source[1]["visible"] = False
    snapshot = {"turn": 1, "map": {"wrapOffset": {"x": 10, "y": -10}, "tiles": source, "frontier": []}}
    query = DetailQuery(tool="map_region", arguments={"center": source[0]["position"], "radius": 1, "format": format})
    result = project_detail(snapshot, query)
    assert encoded(expand_tiles(result["data"])) == encoded(source[:2])
    assert result["available"]
    if format == "full":
        assert "tileDefaults" not in result["data"]
    else:
        assert result["data"]["tileDefaults"]
    query.arguments["center"] = {"x": 99, "y": 99}
    unknown = project_detail(snapshot, query)
    assert not unknown["available"] and not unknown["data"]


def test_switching_old_full_map_evidence_to_compact_does_not_flag_a_plan(tmp_path):
    memory = setup(tmp_path)
    source = tiles()
    full = {"turn": 4, "data": {"tiles": source}}
    compact = {"turn": 5, "data": compact_tiles(source)}
    assert semantic(full) == semantic(compact)
    memory.observe("region", full, "game")
    key = create(memory, plan(assumptions=[{"text": "The land is known", "source": "observation:region"}]))
    memory.observe("region", compact, "game")
    assert not current(memory, key)["needsReview"]
    source[0]["resource"] = "Iron"
    memory.observe("region", {"turn": 5, "data": compact_tiles(source)}, "game")
    assert current(memory, key)["needsReview"]
