"""Sanitized delivery observations share the existing rewindable domain commit."""

import json
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from civ_agent import advisory_store
from civ_agent import memory_schema as tables
from civ_agent.advisory_store import DELIVERY_CATEGORY, AdvisoryStore
from civ_agent.civilization_state import CivilizationState
from civ_agent.experiment_status import classifier_summary
from civ_agent.memory_store import RecordStore
from civ_agent.orchestration.contracts import Scope, digest
from civ_agent.orchestration.native_experiments import classifier_projection


@pytest.fixture
def delivery_state(tmp_path, monkeypatch):
    memory = CivilizationState(tmp_path / "notebook", "Rome")
    memory.bind("epoch")
    memory.start_job("job", "gameplay", {}, game_turn=1)
    with memory.transaction() as db:
        memory._set(db, "nativeGatewayOwner", {"version": 1, "game": "game", "player": "p0"})
    clock = [1000]
    monkeypatch.setattr(advisory_store.time, "time_ns", lambda: clock[0] * 1_000_000)
    fixture = SimpleNamespace(
        memory=memory,
        store=AdvisoryStore(memory),
        clock=clock,
        job=SimpleNamespace(id="job"),
        scope=Scope(game="game", player="p0", epoch="epoch", activation="diagnostics"),
        root=tmp_path,
    )
    yield fixture
    memory.engine.dispose()


def observation(status="delivered"):
    return [
        {
            "requestId": digest("advisory"),
            "name": "preservation:0",
            "choice": "consider",
            "status": status,
            **({"exposure": "notice"} if status == "delivered" else {}),
            "source": {"text": "PRIVATE SOURCE"},
            "objectives": [{"text": "PRIVATE OBJECTIVE"}],
            "reason": "PRIVATE ERROR STRING",
        }
    ]


def events(fixture, *, scope=None):
    _, records, complete = classifier_projection(
        fixture.memory.path, fixture.root / "unused-workflow.sqlite3", scope or fixture.scope
    )
    assert complete
    return records


def write(fixture, source, rows):
    if source == "role":
        fixture.store.record_delivery(fixture.job, rows)
    else:
        fixture.store.save(
            reviewDelivery=rows, reviewDeliveryTurn=1, reviewDeliveryEvidence={"text": "PRIVATE EVIDENCE"}
        )


@pytest.mark.parametrize("source", ["role", "review"])
def test_delivery_commit_suppresses_ack_duplicates_and_preserves_aba_versions(delivery_state, source):
    fixture = delivery_state
    write(fixture, source, observation())
    first = events(fixture)
    assert len(first) == 1 and first[0]["atMs"] == 1000 and first[0]["source"] == source
    fixture.clock[0] = 1500
    write(fixture, source, observation())
    assert events(fixture) == first
    fixture.clock[0] = 2000
    write(fixture, source, observation("stale"))
    fixture.clock[0] = 3000
    write(fixture, source, observation())
    history = events(fixture)
    assert [event["atMs"] for event in history] == [1000, 2000, 3000]
    assert [event["deliveryResults"][0]["status"] for event in history] == ["delivered", "stale", "delivered"]
    assert len({event["eventId"] for event in history}) == 3
    with fixture.memory.transaction() as db:
        rows = db.execute(select(tables.records).where(tables.records.c.category == DELIVERY_CATEGORY)).mappings().all()
        assert [row["version"] for row in rows] == [1, 2, 3]
        assert "PRIVATE" not in json.dumps([json.loads(row["body"]) for row in rows])
    summary = classifier_summary(history)
    assert summary["calls"] == 0 and summary["usage"] == {} and summary["latest"] is None
    assert summary["advisoryDelivery"]["events"] == 3
    assert summary["advisoryDelivery"]["byTask"]["preservation"]["statuses"] == {"delivered": 2, "stale": 1}


@pytest.mark.parametrize("source", ["role", "review"])
def test_delivery_projection_failure_rolls_back_the_same_domain_transaction(delivery_state, monkeypatch, source):
    fixture = delivery_state
    with fixture.memory.transaction() as db:
        snapshot = db.scalar(select(tables.jobs.c.snapshot).where(tables.jobs.c.id == "job"))
    previous_state = fixture.store.state()
    original = RecordStore.write

    def fail(self, key, category, body, job=None):
        if category == DELIVERY_CATEGORY:
            raise OSError("injected projection write failure")
        return original(self, key, category, body, job)

    monkeypatch.setattr(RecordStore, "write", fail)
    with pytest.raises(OSError, match="injected projection"):
        write(fixture, source, observation())
    with fixture.memory.transaction() as db:
        assert db.scalar(select(tables.jobs.c.snapshot).where(tables.jobs.c.id == "job")) == snapshot
        assert not RecordStore(db).all(categories={DELIVERY_CATEGORY})
    assert fixture.store.state() == previous_state


@pytest.mark.parametrize("source", ["role", "review"])
def test_delivery_history_follows_reachable_heads_after_rewind(delivery_state, source):
    fixture = delivery_state
    write(fixture, source, observation())
    checkpoint = fixture.root / "checkpoint.json"
    checksum = fixture.memory.backup(checkpoint)
    fixture.clock[0] = 2000
    write(fixture, source, observation("stale"))
    fixture.clock[0] = 3000
    write(fixture, source, observation())
    assert len(events(fixture)) == 3
    fixture.memory.restore(checkpoint, checksum, "rewound")
    scope = fixture.scope.model_copy(update={"epoch": "rewound"})
    restored = events(fixture, scope=scope)
    assert len(restored) == 1 and restored[0]["atMs"] == 1000
    assert restored[0]["originEpoch"] == "epoch"
    if source == "review":
        fixture.memory.pause(False)
        fixture.clock[0] = 4000
        write(fixture, source, observation("stale"))
        branched = events(fixture, scope=scope)
        assert [event["atMs"] for event in branched] == [1000, 4000]
        with fixture.memory.transaction() as db:
            rows = db.execute(
                select(tables.records.c.version, tables.records.c.parent)
                .where(tables.records.c.category == DELIVERY_CATEGORY)
                .order_by(tables.records.c.version)
            ).all()
        assert rows == [(1, None), (2, 1), (3, 2), (4, 1)]


def test_internal_delivery_records_never_reach_player_memory_tools_or_exports(delivery_state):
    fixture = delivery_state
    write(fixture, "role", observation())
    with fixture.memory.transaction() as db:
        key = next(iter(RecordStore(db).all(categories={DELIVERY_CATEGORY})))
    for history in (False, True):
        result = fixture.memory.search_memory("job", {"id": key, "history": history, "state": "all"})
        assert result["records"] == []
    assert key not in fixture.memory.snapshot("gameplay")["records"]
    workspace = fixture.root / "export"
    fixture.memory.export(workspace)
    assert all(key not in path.read_text() for path in workspace.rglob("*") if path.is_file())


def test_delivery_reader_never_loads_job_snapshots_or_authored_sources(delivery_state, monkeypatch):
    fixture = delivery_state
    write(fixture, "role", observation())
    with fixture.memory.transaction() as db:
        db.execute(tables.jobs.update().values(snapshot="PRIVATE SNAPSHOT"))
        fixture.memory._set(db, "advisories", {"privateEvidence": "PRIVATE AUTHORED BODY"})
    original = json.loads

    def safe_loads(value, *args, **kwargs):
        assert "PRIVATE" not in value
        return original(value, *args, **kwargs)

    monkeypatch.setattr(json, "loads", safe_loads)
    history = events(fixture)
    assert len(history) == 1 and "PRIVATE" not in json.dumps(history)


def test_native_city_change_outcomes_survive_the_existing_fixed_schema_sanitizer(delivery_state):
    fixture = delivery_state
    rows = [
        {"requestId": digest("city"), "name": name, "choice": "change", "status": "delivered", "exposure": "advice"}
        for name in ("city_focus_change:0", "city_construction_change:0")
    ]
    write(fixture, "role", rows)
    history = events(fixture)
    assert [item["name"] for item in history[0]["deliveryResults"]] == [
        "city_construction_change:0",
        "city_focus_change:0",
    ]


def test_pre_projection_review_metadata_remains_explicitly_partial(delivery_state):
    fixture = delivery_state
    with fixture.memory.transaction() as db:
        fixture.memory._set(db, "advisories", {"reviewDelivery": observation(), "reviewDeliveryTurn": 1})
    _, history, complete = classifier_projection(fixture.memory.path, fixture.root / "unused", fixture.scope)
    assert complete is False and len(history) == 1 and history[0]["atMs"] is None
