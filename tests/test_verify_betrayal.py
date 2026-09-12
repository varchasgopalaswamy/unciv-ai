import copy
import json
import sqlite3

import pytest

from civ_agent.civilization_state import CivilizationState
from civ_agent.commands.verify_betrayal import audit
from civ_agent.tls import private_directory, write_private_json


def fixture(root):
    offer = {
        "proposal": "p1",
        "version": 1,
        "author": "Rome",
        "participants": ["Rome", "Greece"],
        "kind": "peace",
        "terms": "No war or attacks before turn 8",
        "conditions": "No unilateral release",
        "expiresTurn": 8,
        "turn": 0,
    }
    events = []

    def event(kind, body):
        events.append({"id": len(events) + 1, "kind": kind, "body": body, "wake": True})

    event("offer", offer)
    consent = {"proposal": "p1", "version": 1, "participants": ["Rome", "Greece"], "decision": "accept", "turn": 0}
    event("consent", {**consent, "author": "Rome"})
    event("consent", {**consent, "author": "Greece"})
    event(
        "consent",
        {
            **consent,
            "author": "Greece",
            "decision": "reconsider",
            "reason": "I choose to violate this promise",
            "turn": 1,
        },
    )
    event(
        "engine",
        {
            "outcome": "o1",
            "author": "Greece",
            "participants": ["Rome", "Greece"],
            "action": "DECLARE_WAR",
            "turn": 1,
            "confirmation": {"atWar": True, "source": "engine"},
        },
    )
    game = {
        "turns": 2,
        "tileMap": {"mapParameters": {"name": "Diplomacy memory exercise"}},
        "civilizations": [
            {"civName": name, "diplomacy": {other: {"diplomaticStatus": "War"}}}
            for name, other in (("Rome", "Greece"), ("Greece", "Rome"))
        ],
    }
    saved = {
        "epoch": "g1",
        "gameJson": json.dumps(game),
        "handoff": 5,
        "communication": {"streams": {"Rome": events}},
        "players": {
            "Rome": {"receipts": []},
            "Greece": {
                "receipts": [
                    {
                        "request": {"kind": "DECLARE_WAR", "target": "Rome"},
                        "receipt": {"kind": "DECLARE_WAR", "turn": 1},
                    }
                ]
            },
        },
    }
    initial = copy.deepcopy(game)
    for player in initial["civilizations"]:
        for relation in player["diplomacy"].values():
            relation["diplomaticStatus"] = "Peace"
    write_private_json(root / "round-checkpoints" / "g1-round-0.json", {"host": {"gameJson": json.dumps(initial)}})
    write_private_json(root / "exploration.json", saved)
    memory = CivilizationState(root / "agent-services" / "Rome" / "civilization", "Rome")
    memory.bind("g1")
    memory.ingest("g1", events, 5)
    memory.start_job("assess", "communication", {})
    note(memory, root, ["event:1", "event:3", "event:5"])
    return saved, memory


def note(memory, root, sources):
    memory.patch(
        "assess",
        [
            {
                "key": "relationships/Greece",
                "category": "belief",
                "body": {
                    "text": "Greece broke our unexpired non-aggression promise by declaring war. Future assurances deserve less trust.",
                    "sources": sources,
                    "uncertainty": "medium",
                },
            }
        ],
    )
    memory.export(private_directory(root / "agents" / "Rome"))


def test_audit_checks_native_receipts_and_sourced_memory_without_mutating_live_jobs(tmp_path):
    _, memory = fixture(tmp_path)
    report = audit(tmp_path)
    assert report["verified"] and report["referencedHostEvents"] == [1, 3, 5]
    with sqlite3.connect(memory.path) as db:
        assert db.execute("SELECT status FROM jobs WHERE id='assess'").fetchone()[0] == "RUNNING"


@pytest.mark.parametrize("missing", ["war", "terms", "consent"])
def test_confident_narrative_without_observed_evidence_does_not_pass(tmp_path, missing):
    _, memory = fixture(tmp_path)
    sources = {"terms": "event:1", "consent": "event:3", "war": "event:5"}
    note(memory, tmp_path, [source for kind, source in sources.items() if kind != missing])
    assert not audit(tmp_path)["verified"]


@pytest.mark.parametrize(
    "change",
    ["native_peace", "expired", "no_expiration", "released", "engine_treaty", "missing_receipt", "forged_evidence"],
)
def test_engine_and_ledger_disagreements_override_agent_reports(tmp_path, change):
    saved, _ = fixture(tmp_path)
    stream = saved["communication"]["streams"]["Rome"]
    if change == "native_peace":
        game = json.loads(saved["gameJson"])
        game["civilizations"][0]["diplomacy"]["Greece"]["diplomaticStatus"] = "Peace"
        saved["gameJson"] = json.dumps(game)
    elif change == "expired":
        stream[0]["body"]["expiresTurn"] = 1
    elif change == "no_expiration":
        stream[0]["body"]["expiresTurn"] = None
    elif change == "released":
        stream.insert(3, {"id": 3.5, "kind": "consent", "body": {**stream[1]["body"], "decision": "withdraw"}})
    elif change == "engine_treaty":
        saved["players"]["Rome"]["receipts"].append({"request": {"kind": "PROPOSE_PEACE"}})
    elif change == "missing_receipt":
        saved["players"]["Greece"]["receipts"].clear()
    else:
        stream[2]["body"]["decision"] = "withdraw"
    write_private_json(tmp_path / "exploration.json", saved)
    assert not audit(tmp_path)["verified"]


def test_native_json_default_is_war_but_missing_relationship_is_not_evidence(tmp_path):
    saved, _ = fixture(tmp_path)
    game = json.loads(saved["gameJson"])
    for player in game["civilizations"]:
        for other, relation in player["diplomacy"].items():
            relation.pop("diplomaticStatus")
            relation["otherCivName"] = other
    saved["gameJson"] = json.dumps(game)
    write_private_json(tmp_path / "exploration.json", saved)
    assert audit(tmp_path)["verified"]
    game["civilizations"][0]["diplomacy"] = {}
    saved["gameJson"] = json.dumps(game)
    write_private_json(tmp_path / "exploration.json", saved)
    assert not audit(tmp_path)["checks"]["nativeSaveConfirmsWarBothWays"]


def test_complete_interface_observation_can_source_terms_consent_and_conduct(tmp_path):
    saved, memory = fixture(tmp_path)
    events = saved["communication"]["streams"]["Rome"]
    memory.observe_player(
        {
            "turn": 2,
            "communication": {
                "offers": [{**events[0]["body"], "effective": False}],
                "consents": [e["body"] for e in events if e["kind"] == "consent"],
                "outcomes": [events[-1]["body"]],
            },
        },
        "assess",
    )
    note(memory, tmp_path, ["observation:game"])
    assert audit(tmp_path)["verified"]


def test_future_uncertainty_does_not_require_a_categorical_trust_verdict(tmp_path):
    _, memory = fixture(tmp_path)
    memory.patch(
        "assess",
        [
            {
                "key": "relationships/Greece",
                "category": "belief",
                "body": {
                    "text": "Greece breached its accepted promise by declaring war. Future conduct remains uncertain.",
                    "sources": ["event:1", "event:3", "event:5"],
                    "uncertainty": "medium",
                },
            }
        ],
    )
    memory.export(tmp_path / "agents" / "Rome")
    report = audit(tmp_path)
    assert report["verified"] and not report["explicitTrustLanguage"]
