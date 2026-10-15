import json
import sqlite3
from collections import Counter

import pytest

from scripts.collect_diplomacy_routing_cases import collect, collect_database, sample_cases


def database(root):
    path = root / "batch/games/g0001/agent-services/Rome/civilization/memory.sqlite3"
    path.parent.mkdir(parents=True)
    with sqlite3.connect(path) as db:
        db.executescript("""
            CREATE TABLE events (id INTEGER PRIMARY KEY, source TEXT, kind TEXT, body TEXT);
            CREATE TABLE records (key TEXT, version INTEGER, category TEXT, body TEXT);
            CREATE TABLE calls (job TEXT, id TEXT, name TEXT, arguments TEXT, result TEXT);
            CREATE TABLE jobs (id TEXT, game_turn INTEGER, snapshot TEXT);
        """)
        for identifier, epoch, author, participants, text in [
            (1, "e1", "Rome", ["Greece", "Rome"], "Our earlier message"),
            (2, "e1", "Greece", ["Greece", "Rome"], "Earlier report"),
            (3, "e1", "Persia", ["Persia", "Rome"], "PRIVATE OTHER THREAD"),
            (4, "e1", "Greece", ["Greece", "Rome"], "Selected update"),
            (5, "e1", "Greece", ["Greece", "Rome"], "FUTURE SAME TURN"),
            (6, "e2", "Greece", ["Greece", "Rome"], "OTHER EPOCH"),
        ]:
            message = {
                "message": "m" + str(identifier),
                "author": author,
                "participants": participants,
                "public": False,
                "text": text,
                "turn": 8,
            }
            db.execute(
                "INSERT INTO events VALUES (?,?,?,?)",
                (identifier, epoch + ":" + str(identifier), "message", json.dumps({"id": identifier, "body": message})),
            )
        future = {
            "conversationContext": {
                "threads": [
                    {
                        "public": False,
                        "participants": ["Greece", "Rome"],
                        "messages": [{"text": "FUTURE READ MAY FINISH AFTER SEND"}],
                    }
                ]
            }
        }
        db.execute(
            "INSERT INTO jobs VALUES (?,?,?)",
            (
                "job",
                8,
                json.dumps({"records": {"observation:communication": future}, "strategicAdvice": "PRIVATE STRATEGY"}),
            ),
        )
        db.execute("INSERT INTO calls VALUES (?,?,?,?,?)", ("job", "read", "chat_read", "{}", json.dumps(future)))
        db.execute(
            "INSERT INTO calls VALUES (?,?,?,?,?)",
            (
                "job",
                "send",
                "chat_send",
                json.dumps({"to": "Greece", "text": "An ordinary report", "intent": {"reason": "PRIVATE INTENT"}}),
                json.dumps({"deliveryId": "d1"}),
            ),
        )
        db.execute(
            "INSERT INTO records VALUES (?,?,?,?)",
            ("d1", 1, "delivery", json.dumps({"createdAt": 100, "decision": {"choice": "batch", "confidence": 0.99}})),
        )
        db.execute(
            "INSERT INTO records VALUES (?,?,?,?)",
            ("r1", 1, "routing", json.dumps({"epoch": "e1", "coveredEventIds": [4], "decision": {"choice": "batch"}})),
        )
    return path


def test_collector_never_uses_future_other_epoch_audience_or_private_intent(tmp_path):
    root = tmp_path / "benchmarks"
    path = database(root)
    original = path.read_bytes()
    cases, outcomes, status = collect_database(path, root)
    assert status == {"databases": 1}
    selected = next(case for case in cases if case["source"].get("eventId") == 4)
    history = selected["bundle"]["recentExchanges"][0]["messages"]
    assert [message["text"] for message in history] == ["Our earlier message", "Earlier report"]
    assert selected["bundle"]["pending"][0]["text"] == "Selected update"
    assert outcomes[selected["id"]]["exactOriginalInput"] is False
    other_epoch = next(case for case in cases if case["source"].get("eventId") == 6)
    assert other_epoch["bundle"]["recentExchanges"][0]["messages"] == []
    sent = next(case for case in cases if case["direction"] == "outgoing")
    assert sent["bundle"]["recentExchanges"] == []
    assert sent["bundle"]["historyCoverage"] == "unavailable"
    assert sent["bundle"]["currentTurn"] == 8
    assert sent["source"]["timestampMs"] == 100000
    assert "PRIVATE" not in json.dumps(sent["bundle"])
    assert "FUTURE" not in json.dumps(sent["bundle"])
    assert path.read_bytes() == original
    assert len({case["id"] for case in cases}) == len(cases)
    assert len({case["group"] for case in cases}) == 1
    assert len({case["split"] for case in cases}) == 1


def test_collection_writes_blind_private_artifacts_and_keeps_outcomes_separate(tmp_path):
    root = tmp_path / "benchmarks"
    database(root)
    output = tmp_path / "private-output"
    summary = collect(root, output, sample_size=4)
    sample = json.loads((output / "sample-unlabeled.json").read_text())
    assert summary["games"] == 1 and summary["sampleCases"] == 4
    # The outgoing bucket has one case, so round-robin selection is not 50/50.
    assert summary["sampleByDirection"] == {"incoming": 3, "outgoing": 1}
    assert summary["sampleByDirection"] == dict(Counter(case["direction"] for case in sample["cases"]))
    assert sample["labelStatus"] == "unlabeled"
    assert all("decision" not in case and "expected" not in case for case in sample["cases"])
    assert (output / "observed-outcomes.json").stat().st_mode & 0o777 == 0o600
    assert output.stat().st_mode & 0o777 == 0o700
    first = (output / "sample-unlabeled.json").read_bytes()
    collect(root, output, sample_size=4)
    assert (output / "sample-unlabeled.json").read_bytes() == first


def test_sampling_balances_games_and_directions_without_using_outcomes():
    cases = [
        {
            "id": f"case-{game}-{direction}-{i}",
            "source": {"game": game},
            "direction": direction,
            "protected": False,
            "bundle": {"text": str(i)},
        }
        for game in ("a", "b", "c")
        for direction in ("incoming", "outgoing")
        for i in range(10)
    ]
    selected = sample_cases(cases, 12)
    assert Counter((case["source"]["game"], case["direction"]) for case in selected) == {
        (game, direction): 2 for game in ("a", "b", "c") for direction in ("incoming", "outgoing")
    }
    assert sample_cases(list(reversed(cases)), 12) == selected
    assert sample_cases(cases, 0) == []
    protected = {**cases[0], "id": "protected", "protected": True}
    assert protected not in sample_cases([protected, *cases], 1000)


def test_collector_rejects_output_inside_input_before_creating_anything(tmp_path):
    root = tmp_path / "benchmarks"
    database(root)
    output = root / "must-not-create"
    with pytest.raises(ValueError, match="outside"):
        collect(root, output)
    assert not output.exists()


def test_sampling_excludes_protected_buckets_and_counts_remaining_directions():
    cases = [
        {
            "id": f"case-{game}-{direction}-{i}",
            "source": {"game": game},
            "direction": direction,
            "protected": game == "b" and direction == "outgoing",
            "bundle": {"text": str(i)},
        }
        for game in ("a", "b")
        for direction in ("incoming", "outgoing")
        for i in range(4)
    ]
    selected = sample_cases(cases, 6)
    assert Counter(case["direction"] for case in selected) == {"incoming": 4, "outgoing": 2}
    assert all(not case["protected"] for case in selected)
