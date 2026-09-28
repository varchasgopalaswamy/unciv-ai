"""Audit tamper detection; native execution is covered by the JVM scenario probe."""

import copy
import json
import sqlite3

import pytest

from civ_agent.civilization_state import CivilizationState
from civ_agent.commands import verify_agreement_scenario as verifier
from civ_agent.memory_store import RecordStore, encode
from civ_agent.protocol import JsonObject


def term(kind, name, amount=1, duration=25):
    return {"type": kind, "name": name, "amount": amount, "duration": duration}


def effects(gpt=0, iron=0):
    return {
        "relationship": {"atWar": False, "youMayEnterTheirTerritory": False, "theyMayEnterYourTerritory": False},
        "goldPerTurnWithPartner": gpt,
        "resourcesWithPartner": [{"name": "Iron", "net": iron}] if iron else [],
    }


def agreement(aid, partner, give, receive, reason, turn) -> JsonObject:
    initial = {"give": give, "receive": receive}
    record = {
        "agreement": aid,
        "partner": partner,
        "kind": "TRADE",
        "initial": initial,
        "observedTurn": 0,
        "current": {"give": [], "receive": []},
        "nativeTerms": {"give": [], "receive": []},
        "nativeSlot": None,
        "status": "ENDED",
        "version": 4,
        "endedTurn": turn,
        "effects": effects(),
    }
    record["history"] = [
        {
            **{k: record[k] for k in ("agreement", "partner", "kind", "version", "status", "effects")},
            "change": "ENDED",
            "reason": reason,
            "turn": turn,
            "before": copy.deepcopy(initial),
            "after": record["current"],
        }
    ]
    return record


def memory(root, player, events, record):
    state = CivilizationState(root / "agent-services" / player / "civilization", player)
    state.bind("epoch")
    resource = player != "France"
    key = "observation:agreement/" + record["agreement"]
    active = {**record, "status": "ACTIVE", "version": 1}
    initial_read = {"agreement": active if resource else record}
    plan = {
        "state": "active",
        "assessment": "Use Iron trade income" if player == "Rome" else "Use imported Iron",
        "subjects": [record["partner"], record["agreement"], "economy"],
        "_evidence": [{"key": key, "version": 1, "category": "observation"}],
        "_assumptions": [],
        "needsReview": [],
    }
    with state.transaction() as db:
        store = RecordStore(db, 0)
        store.write(key, "observation", initial_read, "opening")
        if resource:
            store.write("mem1", "strategy", plan, "opening")
        for name, arguments, result in (
            ("agreement_list", {}, {"agreements": [active]}),
            ("agreement_read", {"agreement": record["agreement"]}, initial_read),
        ):
            db.exec_driver_sql(
                "INSERT INTO calls VALUES (?,?,?,?,?)", ("opening", name, name, encode(arguments), encode(result))
            )
    backup = root / "before-restart-memory" / (player + ".sqlite3")
    backup.parent.mkdir(exist_ok=True)
    with sqlite3.connect(state.path) as source, sqlite3.connect(backup) as destination:
        source.backup(destination)
    # Persist received events using the same native memory schema and revision writer.
    with state.transaction() as db:
        store = RecordStore(db, 2)
        for event in events:
            cursor = db.exec_driver_sql(
                "INSERT INTO events(source,kind,body,wake) VALUES (?,?,?,?)",
                ("epoch:" + str(event["id"]), event["kind"], encode(event), True),
            )
            db.exec_driver_sql("INSERT INTO inbox VALUES (?)", (cursor.lastrowid,))
            store.write("event:" + str(event["id"]), "observation", event)
        if resource:
            end = next(e for e in events if e["body"]["agreement"] == record["agreement"])
            store.write(
                "notice:mem1",
                "notice",
                {
                    "record": "mem1",
                    "reasons": ["Agreement event needs review"],
                    "sources": [{"key": "event:" + str(end["id"]), "version": 1}],
                },
            )
            store.write(key, "observation", {"agreement": record}, "review")
            reviewed = {
                **plan,
                "assessment": "Replace lost trade income",
                "_seenNotice": 1,
                "_evidence": [{"key": key, "version": 2, "category": "observation"}],
            }
            store.write("mem1", "strategy", reviewed, "review")
            db.exec_driver_sql(
                "INSERT INTO calls VALUES (?,?,?,?,?)",
                (
                    "review",
                    "read",
                    "agreement_read",
                    encode({"agreement": record["agreement"]}),
                    encode({"agreement": record}),
                ),
            )


@pytest.fixture
def scenario(tmp_path):
    iron, gold = term("Strategic_Resource", "Iron"), term("Gold_Per_Turn", "Gold per turn", 3)
    gpt, borders = term("Gold_Per_Turn", "Gold per turn", duration=1), term("Agreement", "Open Borders", duration=1)
    records = {
        "Rome": {
            "a1": agreement("a1", "France", [gpt], [borders], "EXPIRED", 0),
            "a2": agreement("a2", "Greece", [iron], [gold], "RESOURCE_UNAVAILABLE", 2),
        },
        "Greece": {"a1": agreement("a1", "Rome", [gold], [iron], "RESOURCE_UNAVAILABLE", 2)},
        "France": {"a1": agreement("a1", "Rome", [borders], [gpt], "EXPIRED", 1)},
    }
    clauses = [{"from": "Rome", "to": "Greece", "item": iron}, {"from": "Greece", "to": "Rome", "item": gold}]
    outcome = {"proposal": "d1", "version": 1, "action": "ENACT_TRADE", "clauses": clauses, "wars": []}
    proposal = {
        "proposal": "d1",
        "version": 1,
        "author": "Rome",
        "partner": "Greece",
        "turn": 0,
        "clauses": clauses,
        "status": "ENACTED",
        "outcome": outcome,
        "consents": [{"author": p, "decision": "accept", "turn": 0} for p in ("Rome", "Greece")],
    }
    game: JsonObject = {
        "gameId": "game",
        "turns": 3,
        "currentPlayer": "Rome",
        "gameParameters": {"speed": "Quick"},
        "tileMap": {
            "mapParameters": {"name": verifier.MAP_NAME},
            "tileList": [
                {
                    "position": {"x": -2, "y": -2},
                    "resource": "Iron",
                    "resourceAmount": 1,
                    "improvement": "Mine",
                    "improvementIsPillaged": True,
                    "militaryUnit": {"owner": "France", "id": 25},
                }
            ],
        },
        "civilizations": [
            {
                "civName": p,
                "playerType": "Human",
                "diplomacy": {
                    q: {"diplomaticStatus": "War" if {p, q} == {"Rome", "France"} else "Peace"}
                    for q in verifier.PLAYERS
                    if q != p
                },
            }
            for p in verifier.PLAYERS
        ],
    }
    streams: JsonObject = {
        p: [
            {"id": n + 1, "kind": "agreement", "wake": True, "body": r["history"][0]} for n, r in enumerate(rs.values())
        ]
        for p, rs in records.items()
    }
    saved: JsonObject = {
        "epoch": "epoch",
        "handoff": 9,
        "gameJson": json.dumps(game),
        "players": {p: {"receipts": []} for p in verifier.PLAYERS},
        "communication": {
            "agreements": {"players": {p: {"records": r, "nextId": len(r) + 1} for p, r in records.items()}},
            "streams": streams,
            "trades": {"proposals": {"d1": [proposal]}},
            "outcomes": [outcome],
            "requests": {},
        },
    }
    for index, kind in enumerate(("DECLARE_WAR", "PILLAGE")):
        saved["players"]["France"]["receipts"].append(
            {
                "request": {
                    "requestId": "r" + str(index),
                    "epoch": "epoch",
                    "kind": kind,
                    "target": "Rome" if index == 0 else "u1",
                },
                "receipt": {"requestId": "r" + str(index), "epoch": "epoch", "kind": kind, "turn": 1},
            }
        )
    saved["players"]["France"]["unitHandles"] = {"25": "u1"}
    saved["players"]["France"]["receipts"][1]["receipt"]["economy"] = {
        "goldChange": 4,
        "units": [{"handle": "u1", "position": {"x": -2, "y": -2}, "removed": False}],
    }
    ledger = saved["communication"]
    settlement = {"gold": {p: 500 for p in verifier.PLAYERS}, "wars": []}
    for n, op in enumerate(("trade_offer", "trade_decide")):
        ledger["requests"][op] = {
            "request": {"op": op, "decision": "accept", "version": 1},
            "response": {"trade": proposal, "outcome": outcome},
            "audit": {
                "activeCivilization": "Rome",
                "handoff": 0,
                "timeMillis": n,
                "settlement": {"before": settlement, "after": copy.deepcopy(settlement)},
            },
        }
    earlier = copy.deepcopy(saved)
    old_game = copy.deepcopy(game)
    old_game["turns"] = 1
    old_game["tileMap"]["tileList"][0]["improvementIsPillaged"] = False
    for civilization in old_game["civilizations"]:
        for relation in civilization["diplomacy"].values():
            relation["diplomaticStatus"] = "Peace"
    earlier.update(gameJson=json.dumps(old_game), handoff=3)
    earlier["players"]["France"]["receipts"] = []
    for player in ("Rome", "Greece"):
        aid = "a2" if player == "Rome" else "a1"
        record = earlier["communication"]["agreements"]["players"][player]["records"][aid]
        current = {side: [{**item, "duration": 24} for item in record["initial"][side]] for side in ("give", "receive")}
        record.update(
            status="ACTIVE",
            version=2,
            nativeSlot=0,
            nativeTerms=current,
            current=current,
            history=[],
            endedTurn=None,
            effects=effects(3 if player == "Rome" else -3, -1 if player == "Rome" else 1),
        )
        native = next(c for c in old_game["civilizations"] if c["civName"] == player)
        native["diplomacy"][record["partner"]]["trades"] = [
            {"ourOffers": current["give"], "theirOffers": current["receive"]}
        ]
        earlier["communication"]["streams"][player] = [e for e in streams[player] if e["body"]["reason"] == "EXPIRED"]
    earlier["gameJson"] = json.dumps(old_game)
    baseline = tmp_path / "before-restart.json"
    baseline.write_text(json.dumps(earlier))
    for player in verifier.PLAYERS:
        memory(tmp_path, player, streams[player], records[player]["a2" if player == "Rome" else "a1"])
    return tmp_path, saved, earlier


def audit(scenario):
    root, saved, earlier = scenario
    (root / "exploration.json").write_text(json.dumps(saved))
    (root / "before-restart.json").write_text(json.dumps(earlier))
    return verifier.audit(root, before_restart=root / "before-restart.json")


def test_complete_evidence_and_private_report(scenario):
    root, _, _ = scenario
    report = audit(scenario)
    assert report["verified"], report
    args = verifier.parser().parse_args(
        ["--state-dir", str(root), "--before-restart", str(root / "before-restart.json")]
    )
    assert verifier.run(args) == 0
    assert (root / "verification/agreement-scenario.json").stat().st_mode & 0o777 == 0o600
    assert "not that processes restarted" in report["evidenceNote"]


@pytest.mark.parametrize(
    "change",
    [
        "missing_consent",
        "wrong_duration",
        "wrong_price",
        "duplicate_outcome",
        "immediate_payment",
        "native_trade_remaining",
        "native_mine_intact",
        "missing_pillage_receipt",
        "wrong_pillage_turn",
        "wrong_pillage_unit",
        "wrong_pillage_position",
        "missing_pillage_economy",
        "wrong_receipt_epoch",
        "wrong_native_war",
        "wrong_ending",
        "wrong_effects",
        "duplicate_event",
        "private_leak",
        "missing_event",
        "changed_id",
        "changed_archive",
        "archive_wrong_timer",
        "archive_wrong_effects",
        "early",
        "missing_memory_baseline",
        "lost_memory",
        "no_review",
        "no_refresh",
        "unrelated_review",
        "unread_tools",
    ],
)
def test_reject_incomplete_or_tampered_evidence(scenario, change):
    root, saved, earlier = scenario
    ledger = saved["communication"]
    record = ledger["agreements"]["players"]["Rome"]["records"]["a2"]
    proposal = ledger["trades"]["proposals"]["d1"][0]
    game = json.loads(saved["gameJson"])
    match change:
        case "missing_consent":
            proposal["consents"].pop()
        case "wrong_duration":
            proposal["clauses"][0]["item"]["duration"] = 3
        case "wrong_price":
            proposal["clauses"][1]["item"]["amount"] = 99
        case "duplicate_outcome":
            ledger["outcomes"].append(copy.deepcopy(ledger["outcomes"][0]))
        case "immediate_payment":
            ledger["requests"]["trade_decide"]["audit"]["settlement"]["after"]["gold"]["Rome"] += 3
        case "native_trade_remaining":
            game["civilizations"][0]["diplomacy"]["Greece"]["trades"] = [{}]
        case "native_mine_intact":
            game["tileMap"]["tileList"][0]["improvementIsPillaged"] = False
        case "missing_pillage_receipt":
            saved["players"]["France"]["receipts"].pop()
        case "wrong_pillage_turn":
            saved["players"]["France"]["receipts"][1]["receipt"]["turn"] = 0
        case "wrong_pillage_unit":
            saved["players"]["France"]["receipts"][1]["request"]["target"] = "unit-of-a-different-player"
        case "wrong_pillage_position":
            saved["players"]["France"]["receipts"][1]["receipt"]["economy"]["units"][0]["position"]["x"] = -1
        case "missing_pillage_economy":
            saved["players"]["France"]["receipts"][1]["receipt"]["economy"] = None
        case "wrong_receipt_epoch":
            saved["players"]["France"]["receipts"][1]["receipt"]["epoch"] = "discarded-epoch"
        case "wrong_native_war":
            game["civilizations"][0]["diplomacy"]["Greece"]["diplomaticStatus"] = "War"
        case "wrong_ending":
            record["history"][0]["reason"] = "EXPIRED"
        case "wrong_effects":
            record["effects"]["goldPerTurnWithPartner"] = 3
        case "duplicate_event":
            ledger["streams"]["Rome"].append(copy.deepcopy(ledger["streams"]["Rome"][-1]))
        case "private_leak":
            ledger["streams"]["France"].append(copy.deepcopy(ledger["streams"]["Rome"][-1]))
        case "missing_event":
            ledger["streams"]["Rome"].pop()
        case "changed_id":
            record["agreement"] = "a99"
        case "changed_archive":
            earlier["epoch"] = "different"
        case "archive_wrong_timer":
            earlier["communication"]["agreements"]["players"]["Rome"]["records"]["a2"]["nativeTerms"]["give"][0][
                "duration"
            ] = 20
        case "archive_wrong_effects":
            earlier["communication"]["agreements"]["players"]["Rome"]["records"]["a2"]["effects"][
                "goldPerTurnWithPartner"
            ] = 4
        case "early":
            game["turns"] = 2
        case "missing_memory_baseline":
            (root / "before-restart-memory/Greece.sqlite3").unlink()
        case _:
            with sqlite3.connect(root / "agent-services/Rome/civilization/memory.sqlite3") as db:
                if change == "lost_memory":
                    db.execute("DELETE FROM calls WHERE job='opening'")
                elif change == "unread_tools":
                    db.execute("UPDATE calls SET name='chat_read'")
                else:
                    version = 1 if change == "unrelated_review" else 2
                    key = (
                        "observation:agreement/a2"
                        if change == "no_refresh"
                        else "mem1"
                        if change == "no_review"
                        else "notice:mem1"
                    )
                    if key == "notice:mem1":
                        version = 1
                    row = db.execute("SELECT body FROM records WHERE key=? AND version=?", (key, version)).fetchone()
                    body = json.loads(row[0])
                    if change == "no_review":
                        body["_seenNotice"] = 0
                    elif change == "no_refresh":
                        body["agreement"]["status"] = "ACTIVE"
                    else:
                        body["sources"] = [{"key": "unrelated", "version": 1}]
                    db.execute("UPDATE records SET body=? WHERE key=? AND version=?", (encode(body), key, version))
    saved["gameJson"] = json.dumps(game)
    assert not audit(scenario)["verified"], change


def test_wrong_scenario_is_not_a_pending_success(scenario):
    root, saved, _ = scenario
    game = json.loads(saved["gameJson"])
    game["tileMap"]["mapParameters"]["name"] = "Three-Cornered War: paid"
    saved["gameJson"] = json.dumps(game)
    with pytest.raises(ValueError, match="not the agreement"):
        audit(scenario)
    assert verifier.run(verifier.parser().parse_args(["--state-dir", str(root)])) == 2


def test_missing_milestones_and_restart_never_verify(scenario):
    root, saved, _ = scenario
    saved["communication"]["trades"]["proposals"] = {}
    report = audit(scenario)
    assert not report["verified"]
    assert report["steps"][0]["status"] == "PENDING"
    assert not verifier.audit(root)["verified"]


def test_null_trade_in_rejected_request_does_not_break_audit(scenario):
    _, saved, _ = scenario
    saved["communication"]["requests"]["rejected"] = {
        "request": {"op": "trade_decide"},
        "response": {"error": "STALE_REVISION", "trade": None},
    }
    assert audit(scenario)["verified"]


def test_restart_allows_new_native_war_outcome(scenario):
    _, saved, _ = scenario
    saved["communication"]["outcomes"].append({"action": "DECLARE_WAR", "author": "France", "turn": 1})
    assert audit(scenario)["verified"]


@pytest.mark.parametrize("later", ["moved", "removed"])
def test_completed_pillage_does_not_require_unit_staying_on_mine(scenario, later):
    _, saved, _ = scenario
    game = json.loads(saved["gameJson"])
    unit = game["tileMap"]["tileList"][0].pop("militaryUnit")
    if later == "moved":
        game["tileMap"]["tileList"].append({"position": {"x": -1, "y": -2}, "militaryUnit": unit})
    else:
        saved["players"]["France"]["unitHandles"] = {}
    saved["gameJson"] = json.dumps(game)
    assert audit(scenario)["verified"]


@pytest.mark.parametrize("change", ["explicit_retirement", "read_precedes_flag"])
def test_memory_review_handles_retirement_and_read_event_race(scenario, change):
    root, _, _ = scenario
    with sqlite3.connect(root / "agent-services/Rome/civilization/memory.sqlite3") as db:
        if change == "explicit_retirement":
            row = db.execute("SELECT body FROM records WHERE key='mem1' AND version=2").fetchone()
            body = {
                **json.loads(row[0]),
                "state": "retired",
                "retirementAssessment": "Imported resource is gone; abandon reliance on this deal",
            }
            db.execute("UPDATE records SET body=? WHERE key='mem1' AND version=2", (encode(body),))
        else:
            flag = db.execute("SELECT rowid FROM records WHERE key='notice:mem1' AND version=1").fetchone()[0]
            read = db.execute(
                "SELECT rowid FROM records WHERE key='observation:agreement/a2' AND version=2"
            ).fetchone()[0]
            db.execute("UPDATE records SET rowid=999 WHERE rowid=?", (flag,))
            db.execute("UPDATE records SET rowid=? WHERE rowid=?", (flag, read))
            db.execute("UPDATE records SET rowid=? WHERE rowid=999", (read,))
    assert audit(scenario)["verified"]
