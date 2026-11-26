"""History exposes native domain facts and immutable evidence, never a fake receipt."""

import copy
import hashlib
import json

from sqlalchemy import select

from civ_agent import memory_schema as tables
from civ_agent.memory_records import (
    DecisionRecord,
    HistoryReadResult,
    MemoryReadResult,
    MemorySearchResult,
    TurnSummary,
    public_record,
)
from civ_agent.memory_snapshots import load_snapshot
from civ_agent.memory_store import RecordStore, encode
from tests.test_native_effect_outcome import counts, record, setup


def legacy(memory, call, summary):
    request = memory.queue(
        "job1", "player_command", {"kind": "SET_CITY_FOCUS", "target": "city1", "selection": "FoodFocus"}, call
    )
    receipt = {"kind": "SET_CITY_FOCUS", "actionSummary": summary, "legacyDetail": call}
    memory.receipt(request["id"], receipt)
    return request, receipt


def test_summary_and_history_include_native_and_legacy_actions_in_original_order(tmp_path):
    memory, prepared, receipt = setup(tmp_path)
    before, before_receipt = legacy(memory, "before", "Selected food focus.")
    baseline = counts(memory)
    memory.record_native_outcome("job1", prepared, receipt)
    after, after_receipt = legacy(memory, "after", "Selected food focus again.")
    for result in (memory.summary(4), memory.summary(job="job1"), memory.history(4)):
        assert [action["decision"] for action in result["actions"]] == [
            before["decision"],
            "native:request1",
            after["decision"],
        ]
        first, native, last = result["actions"]
        assert first["receipt"] == before_receipt and last["receipt"] == after_receipt
        assert native["action"] == "Granary purchased." and native["reason"] == "Improve food surplus"
        assert native["outcome"] == "confirmed" and native.get("receipt") is None
        assert native["facts"] == {"actionSummary": "Granary purchased.", "economy": {"goldChange": -180}}
        assert native["receiptReference"] == {
            "operationId": "request1",
            "transport": "player_command",
            "requestSha256": hashlib.sha256(encode(["player_command", prepared["payload"]]).encode()).hexdigest(),
            "receiptSha256": hashlib.sha256(encode(receipt).encode()).hexdigest(),
        }
        assert "NOT_A_" not in json.dumps(result)
    TurnSummary.model_validate(memory.summary(4))
    HistoryReadResult.model_validate(memory.history(4))
    current = counts(memory)
    assert current["outbox"] == baseline["outbox"] + 1
    assert current["calls"] == current["decision_journal"] == 0
    assert "receipt" not in record(memory)["body"]


def test_native_history_evidence_pins_the_exact_domain_version_across_trade_settlement(tmp_path):
    memory, prepared, _ = setup(tmp_path)
    prepared.update(
        transport="player_communication",
        payload={"requestId": "request1", "epoch": "epoch1", "op": "trade_offer", "partner": "Greece"},
        intent=None,
    )
    pending = {"trade": {"proposal": "trade1", "status": "PENDING", "mode": "SHARED"}, "rawExtra": "DO_NOT_COPY"}
    memory.record_native_outcome("job1", prepared, pending)
    first = memory.history(4, job="job1")["actions"][0]
    evidence = first["evidence"]
    assert first["action"] == "Diplomatic proposal pending" and first["receipt"] is None
    result = memory.patch(
        "job1",
        [
            {
                "op": "create",
                "content": {
                    "type": "belief",
                    "claim": "The offer remains under discussion",
                    "uncertainty": "low",
                    "sources": [evidence],
                },
            }
        ],
    )
    assert result["changes"][0]["accepted"]
    belief = result["changes"][0]["id"]
    legacy(memory, "later-native-command", "Another action after the proposal.")
    memory.observe(
        "trade/trade1",
        {"trade": {"proposal": "trade1", "status": "ENACTED", "mode": "SHARED"}, "outcome": {"raw": "DO_NOT_COPY"}},
        "job1",
    )
    later = memory.history(4, job="job1")["actions"]
    assert later[0]["decision"] == "native:request1" and later[1]["action"] == "Another action after the proposal."
    assert later[0]["action"] == "Engine agreement enacted"
    assert later[0]["receiptReference"] == first["receiptReference"]
    assert later[0]["facts"] == first["facts"] and later[0]["evidence"] != evidence
    assert later[0]["settlement"] == {
        "trade": {"proposal": "trade1", "status": "ENACTED", "mode": "SHARED"},
        "outcome": True,
    }
    with memory.transaction() as db:
        snapshot = load_snapshot(db, db.scalar(select(tables.jobs.c.snapshot).where(tables.jobs.c.id == "job1")))
        old = snapshot["historicalEvidence"][evidence]
        assert old == {"key": "native:request1", "version": 1, "category": "decision"}
        assert RecordStore.pin(snapshot, [evidence]) == [{**old, "historical": True}]
        citations = RecordStore(db).all(keys=[belief], project=False)[belief]["body"]["_citations"]
        assert citations[evidence] == {**old, "historical": True}
    assert "DO_NOT_COPY" not in json.dumps(record(memory))
    assert record(memory)["body"]["native"]["receiptSha256"] == hashlib.sha256(encode(pending).encode()).hexdigest()


def test_native_decision_public_reads_validate_as_projected_facts_with_host_reference(tmp_path):
    memory, prepared, receipt = setup(tmp_path)
    memory.record_native_outcome("job1", prepared, receipt)
    saved = record(memory)
    public = public_record("native:request1", saved)
    parsed = MemoryReadResult.model_validate({"records": [public]}).records[0]
    assert isinstance(parsed, DecisionRecord)
    assert parsed.receipt is None and parsed.receiptReference is not None
    assert parsed.receiptReference.operationId == "request1"
    assert "native" not in public and public["facts"] == saved["body"]["facts"]
    search = memory.search_memory("job1", {"id": "native:request1", "history": True})
    MemorySearchResult.model_validate(search)
    assert search["records"][0]["receiptReference"] == public["receiptReference"]
    assert search["records"][0]["evidence"].startswith("e")


def test_native_history_pagination_and_turn_summary_include_military_and_exclude_rewound_future(tmp_path):
    memory, prepared, receipt = setup(tmp_path)
    memory.start_job("military", "military", {}, game_turn=4)
    for index in range(21):
        operation, outcome = copy.deepcopy(prepared), copy.deepcopy(receipt)
        operation["operationId"] = operation["payload"]["requestId"] = outcome["requestId"] = "action" + str(index)
        operation["intent"] = None
        memory.record_native_outcome("military", operation, outcome)
    first, second = memory.history(4), memory.history(4, 20)
    assert first["hasMore"] and first["nextOffset"] == 20 and len(first["actions"]) == 20
    assert not second["hasMore"] and second["nextOffset"] == 21 and len(second["actions"]) == 1
    assert len(memory.summary(4)["actions"]) == 21
    checkpoint = tmp_path / "checkpoint.json"
    checksum = memory.backup(checkpoint)
    memory.record_native_outcome("job1", prepared, receipt)
    assert len(memory.history(4)["actions"]) == 20 and memory.history(4, 20)["nextOffset"] == 22
    memory.restore(checkpoint, checksum, "epoch2")
    assert memory.history(4, 20)["nextOffset"] == 21
    assert len(memory.summary(4)["actions"]) == 21
    assert counts(memory)["outbox"] == 0
