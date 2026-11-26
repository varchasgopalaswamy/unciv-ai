"""Job storage scales with its own evidence, retaining exact immutable revisions."""

import json

from sqlalchemy import select

from civ_agent import memory_schema as tables
from civ_agent.civilization_state import CivilizationState
from civ_agent.memory_snapshots import compact, expand, load_snapshot
from civ_agent.memory_store import RecordStore, encode
from civ_agent.observations import DetailQuery, detail_scope, overview, project_detail
from civ_agent.protocol import JsonObject
from tests.domain_fixtures import Host


def test_historical_region_bodies_are_neither_loaded_nor_copied_into_new_jobs(tmp_path, monkeypatch):
    memory = CivilizationState(tmp_path / "private", "Rome")
    memory.bind("g1")
    body: JsonObject = {**Host().view, "phase": "ACTIVE", "epoch": "g1", "revision": "r1"}
    body["map"]["tiles"] = [
        {
            "position": {"x": x, "y": y},
            "visible": True,
            "terrain": "Grassland",
            "features": [],
            "naturalWonder": None,
            "resource": None,
            "resourceAmount": None,
            "owner": None,
            "cityName": None,
            "cityHealth": None,
            "improvement": None,
            "improvementPillaged": None,
            "road": "None",
            "roadPillaged": False,
            "units": [],
            "yields": [{"stat": "Food", "amount": "2"}],
            "observedTurn": 1,
        }
        for x in range(180)
        for y in range(-4, 5)
    ]
    with memory.transaction() as db:
        for x in range(150):
            query = DetailQuery(
                tool="map_region", arguments={"center": {"x": x + 4, "y": 0}, "radius": 4, "format": "full"}
            )
            RecordStore(db).write("observation:" + detail_scope(query), "observation", project_detail(body, query))
    loaded = set()
    original = RecordStore.all

    def track(store, **kwargs):
        result = original(store, **kwargs)
        loaded.update(result)
        return result

    monkeypatch.setattr(RecordStore, "all", track)
    opening = memory.start_job("new", "gameplay", {}, game_turn=20)
    assert not opening["records"] and not opening["delivered"]
    memory.finish_job("new", True)
    with memory.transaction() as db:
        saved = db.scalar(select(tables.jobs.c.snapshot).where(tables.jobs.c.id == "new"))
    assert len(saved.encode()) < 1000
    assert json.loads(saved) == opening
    memory.start_job("next", "gameplay", {}, game_turn=21)
    memory.observe_player(body, "next", overview(body))
    assert not any(key.startswith("observation:detail/") for key in loaded)
    # Ordinary explicit reads still retrieve archived detail evidence.
    key = "observation:" + detail_scope(query)
    assert memory.refresh_memory("next", [key])[key]["body"] == project_detail(body, query)
    with memory.transaction() as db:
        saved = db.scalar(select(tables.jobs.c.snapshot).where(tables.jobs.c.id == "next"))
        restored = load_snapshot(db, saved)
    assert len(saved.encode()) < 4000
    assert restored["records"][key]["body"] == project_detail(body, query)
    assert key in restored["delivered"]


def test_frozen_projection_survives_new_heads_without_copying_record_bodies(tmp_path):
    memory = CivilizationState(tmp_path / "private", "Rome")
    with memory.transaction() as db:
        store = RecordStore(db)
        body = {"text": "large frozen evidence " * 10000, "removed": 1}
        version = store.write("observation:large", "observation", body)
        snapshot = {
            "records": {
                "observation:large": {
                    "version": version,
                    "category": "observation",
                    "body": {"text": body["text"], "projection": 2},
                }
            }
        }
        saved = compact(db, snapshot)
        assert len(encode(saved)) < 250
        store.write("observation:large", "observation", {"text": "new information"})
        assert expand(db, saved) == snapshot


def test_checkpoint_excludes_completed_job_snapshots(tmp_path):
    from tests.domain_fixtures import setup

    memory = setup(tmp_path)
    memory.observe("large", {"text": "OLD_COMPLETED_PAYLOAD" * 1000}, "game")
    memory.finish_job("game", True)
    memory.observe("large", {"text": "current"})
    memory.start_job("new", "gameplay", {}, game_turn=5)
    path = tmp_path / "turn.json"
    checksum = memory.backup(path)
    saved = memory.verify(path, checksum)
    assert "OLD_COMPLETED_PAYLOAD" not in path.read_text()
    assert len(path.read_bytes()) < 12_000
    assert next(job for job in saved["jobs"] if job["id"] == "game") == {"id": "game", "status": "COMPLETED"}
    memory.restore(path, checksum, "restored")
    with memory.transaction() as db:
        saved = db.scalar(select(tables.jobs.c.snapshot).where(tables.jobs.c.id == "game"))
        assert "OLD_COMPLETED_PAYLOAD" not in saved
        assert "OLD_COMPLETED_PAYLOAD" in load_snapshot(db, saved)["records"]["observation:large"]["body"]["text"]
