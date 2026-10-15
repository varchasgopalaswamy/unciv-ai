import copy
import json
from types import SimpleNamespace

import pytest

from civ_agent.civilization_state import CivilizationState
from civ_agent.diplomacy_store import DiplomacyStore, build_routing_bundle
from civ_agent.memory_store import RecordStore


@pytest.fixture
def memory(tmp_path):
    state = CivilizationState(tmp_path, "Rome")
    state.routing_enabled = True
    state.bind("g1")
    try:
        yield state
    finally:
        state.engine.dispose()


def event(id, *, participants=("Greece", "Rome"), public=False, text=None, turn=5):
    return {
        "id": id,
        "kind": "message",
        "wake": True,
        "turn": turn,
        "body": {
            "message": f"m{id}",
            "author": "Greece",
            "text": text or f"Message {id}",
            "public": public,
            "participants": list(participants),
            "turn": turn,
        },
    }


def messages(bundle):
    return [message for thread in bundle["recentExchanges"] for message in thread["messages"]]


def test_pending_excluded_before_history_limit_and_context_identifies_player(memory):
    events = [event(id) for id in range(1, 16)]
    memory.ingest("g1", events, 15)
    pending = [item["body"] for item in events[8:]]
    bundle = DiplomacyStore(memory).bundle((False, ("Greece", "Rome")), pending)
    assert bundle["schemaVersion"] == 2
    assert bundle["player"] == "Rome" and bundle["direction"] == "incoming" and bundle["currentTurn"] == 5
    assert bundle["pending"] == pending
    assert [item["message"] for item in messages(bundle)] == [f"m{id}" for id in range(3, 9)]
    assert bundle["hasEarlierMessages"] is True
    assert bundle["historyCoverage"] == "bounded_exact_audience_excerpt"
    assert bundle["recentExchanges"][0]["hasEarlierMessages"] is True
    assert "not proof" in bundle["evidenceSemantics"]
    assert "pending messages await this routing decision" in bundle["evidenceSemantics"]
    assert not {"read", "answered", "resolved"} & bundle.keys()


@pytest.mark.parametrize("public", [False, True])
def test_outgoing_exact_audience_recovers_quiet_history_and_limits_objectives(memory, public):
    target = (True, ()) if public else (False, ("Greece", "Rome"))
    own_history = event(1, public=public, text="OUR THREAD")
    unrelated = [
        event(2, participants=("France", "Greece", "Rome"), text="PRIVATE GROUP SECRET"),
        event(3, participants=("France", "Rome"), text="PRIVATE DIRECT SECRET"),
    ]
    # A quiet private thread remains selectable when the most recent fifty
    # events no longer contain it, while public and private evidence stay apart.
    busy = [event(id, public=not public, text="OTHER AUDIENCE") for id in range(4, 65)]
    memory.ingest("g1", [own_history, *unrelated, *busy], 64)
    with memory.transaction() as db:
        records = RecordStore(db)
        records.write("ours", "strategy", {"subjects": ["Greece"], "text": "OUR OBJECTIVE"})
        records.write("other", "note", {"subjects": ["France"], "text": "PRIVATE NOTE SECRET"})
        records.write("global", "note", {"subjects": [], "text": "UNSCOPED SECRET"})
    bundle = DiplomacyStore(memory).bundle(target, [])
    assert bundle["player"] == "Rome" and bundle["direction"] == "outgoing"
    assert [item["text"] for item in messages(bundle)] == ["OUR THREAD"]
    assert bundle["hasEarlierMessages"] is False
    assert bundle["pending"] == [] and bundle["queuedDrafts"] == []
    assert bundle["timing"] == {"maxBatchSeconds": 30}
    assert "SECRET" not in json.dumps(bundle)
    assert [item["text"] for item in bundle["playerObjectives"]] == ([] if public else ["OUR OBJECTIVE"])


def test_incoming_timing_uses_original_receipt_and_persisted_coalesced_deadline(memory):
    now = [100.0]
    store = DiplomacyStore(memory, clock=lambda: now[0])
    first, second = event(1), event(2)
    memory.ingest("g1", [first], 1)
    with memory.transaction() as db:
        memory._set(db, "routing:events", {"1": {"receivedAt": 100.0}})
    store.route("g1", [1], {"choice": "batch", "confidence": 1}, "first")
    now[0] = 120.0
    memory.ingest("g1", [second], 2)
    with memory.transaction() as db:
        states = memory._meta(db, "routing:events")
        states["2"]["receivedAt"] = 120.0
        memory._set(db, "routing:events", states)
    thread, pending = (False, ("Greece", "Rome")), [first["body"], second["body"]]
    now[0] = 125.0
    assert store.bundle(thread, pending)["timing"] == {
        "maxBatchSeconds": 30,
        "oldestPendingAgeSeconds": 25.0,
        "remainingBatchSeconds": 5.0,
    }
    store.route("g1", [1, 2], {"choice": "batch", "confidence": 1}, "coalesced")
    now[0] = 129.0
    assert store.bundle(thread, pending)["timing"]["remainingBatchSeconds"] == 1.0
    now[0] = 131.0
    assert store.bundle(thread, pending)["timing"]["remainingBatchSeconds"] == 0


def test_unknown_or_incomplete_incoming_timing_is_not_a_new_delay_allowance(memory):
    pending = [event(1)["body"], event(2)["body"]]
    store = DiplomacyStore(memory, clock=lambda: 120)
    memory.ingest("g1", [event(1)], 1)
    with memory.transaction() as db:
        memory._set(db, "routing:events", {"1": {"receivedAt": 100}})
    assert store.bundle((False, ("Greece", "Rome")), pending)["timing"] == {"maxBatchSeconds": 30}
    with memory.transaction() as db:
        memory._set(db, "routing:events", {})
    assert store.bundle((False, ("Greece", "Rome")), pending[:1])["timing"] == {"maxBatchSeconds": 30}


def test_outgoing_timing_covers_current_draft_without_treating_it_as_prior_duplicate(memory):
    now = [90.0]
    store = DiplomacyStore(memory, clock=lambda: now[0])
    memory.start_job("chat", "communication", {})
    job = SimpleNamespace(id="chat")
    other = store.draft(job, "other", {"text": "PRIVATE DRAFT SECRET"}, (False, ("France", "Rome")))
    store.update("g1", other["deliveryId"], status="queued")
    now[0] = 100.0
    thread = (False, ("Greece", "Rome"))
    first = store.draft(job, "first", {"text": "First"}, thread)
    store.update("g1", first["deliveryId"], status="queued")
    now[0] = 120.0
    current = store.draft(job, "current", {"text": "Current"}, thread)
    bundle = store.bundle(thread, [], exclude_delivery=current["deliveryId"])
    assert current["deadline"] == first["deadline"] == 130.0
    assert bundle["timing"] == {
        "maxBatchSeconds": 30,
        "oldestPendingAgeSeconds": 20.0,
        "remainingBatchSeconds": 10.0,
    }
    assert bundle["queuedDrafts"] == [{"text": "First", "status": "queued"}]
    assert "SECRET" not in json.dumps(bundle)
    now[0] = 131.0
    assert store.bundle(thread, [], exclude_delivery=current["deliveryId"])["timing"]["remainingBatchSeconds"] == 0


def test_shared_builder_filters_exact_audience_without_modifying_source_context():
    pending, other = event(2)["body"], event(3, participants=("France", "Rome"))["body"]
    context = {
        "threads": [
            {"public": False, "participants": ["Greece", "Rome"], "messages": [event(1)["body"], pending]},
            {"public": False, "participants": ["France", "Rome"], "messages": [other]},
        ]
    }
    original = copy.deepcopy(context)
    bundle = build_routing_bundle("Rome", (False, ("Greece", "Rome")), [pending, other], context)
    assert context == original
    assert bundle["pending"] == [pending]
    assert [item["message"] for item in messages(bundle)] == ["m1"]
    assert "France" not in json.dumps(bundle)
