"""Reject false claims of milestone completion and malformed native evidence."""

import copy
import json
from typing import Any

import pytest

from civ_agent import cli
from civ_agent.commands import host, verify_war_scenario


@pytest.fixture
def joint(tmp_path):
    # Synthetic audit fixture, deliberately independent of model text. The JVM probe
    # supplies the native execution coverage; this fixture tests tampering detection.
    clauses = [
        {"from": a, "to": b, "item": {"type": "WarDeclaration", "name": "France", "amount": 1, "duration": -1}}
        for a, b in (("Rome", "Greece"), ("Greece", "Rome"))
    ]
    outcome = {
        "proposal": "d1",
        "version": 1,
        "action": "ENACT_TRADE",
        "clauses": clauses,
        "wars": [
            {"declaringCivilization": a, "targetCivilization": "France", "atWar": True} for a in ("Rome", "Greece")
        ],
    }
    proposal = {
        "proposal": "d1",
        "version": 1,
        "author": "Rome",
        "partner": "Greece",
        "clauses": clauses,
        "turn": 0,
        "deadlineMillis": 1000,
        "status": "ENACTED",
        "outcome": outcome,
        "consents": [{"author": a, "decision": "accept"} for a in ("Rome", "Greece")],
    }
    game = {
        "tileMap": {"mapParameters": {"name": "Three-Cornered War: joint"}},
        "turns": 3,
        "civilizations": [
            {"civName": "Rome", "diplomacy": {"France": {}, "Greece": {"diplomaticStatus": "Peace"}}},
            {"civName": "Greece", "diplomacy": {"France": {}, "Rome": {"diplomaticStatus": "Peace"}}},
            {"civName": "France", "diplomacy": {"Rome": {}, "Greece": {}}},
        ],
    }
    saved: dict[str, Any] = {
        "gameJson": json.dumps(game),
        "communication": {
            "trades": {"proposals": {"d1": [proposal]}},
            "streams": {"Rome": [{"kind": "trade", "body": {"proposal": "d1"}}], "France": []},
            "outcomes": [outcome],
            "requests": {},
        },
    }
    for op in ("trade_offer", "trade_decide"):
        saved["communication"]["requests"][op] = {
            "request": {"op": op, "decision": "accept", "version": 1},
            "response": {"trade": copy.deepcopy(proposal), "outcome": outcome},
            "audit": {"activeCivilization": "Rome", "handoff": 0, "timeMillis": 10 if op == "trade_offer" else 20},
        }
    saved["communication"]["requests"]["trade_decide"]["audit"]["settlement"] = {
        "before": {"gold": {c: 500 for c in ("Rome", "Greece", "France")}, "wars": []},
        "after": {
            "gold": {c: 500 for c in ("Rome", "Greece", "France")},
            "wars": [
                {"declaringCivilization": a, "targetCivilization": b, "atWar": True}
                for a, b in (("Rome", "France"), ("France", "Rome"), ("Greece", "France"), ("France", "Greece"))
            ],
        },
    }
    return tmp_path, saved


def audit(fixture):
    root, saved = fixture
    (root / "exploration.json").write_text(json.dumps(saved))
    return verify_war_scenario.audit(root)


def test_joint_evidence_and_cli(joint):
    assert audit(joint)["verified"]
    root, _ = joint
    assert cli.main(["match", "verify-war-scenario", "--state-dir", str(root)]) == 0
    assert (root / "verification/war-scenario.json").stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize(
    "change",
    [
        "native_peace",
        "early",
        "missing_consent",
        "duplicate",
        "wrong_target",
        "payment",
        "handoff",
        "private_event",
        "missing_receipt",
        "stale_acceptance",
        "wrong_balance",
        "missing_snapshot",
    ],
)
def test_reject_bad_evidence(joint, change):
    _, saved = joint
    ledger = saved["communication"]
    proposal = ledger["trades"]["proposals"]["d1"][0]
    game = json.loads(saved["gameJson"])
    match change:
        case "native_peace":
            game["civilizations"][0]["diplomacy"]["France"]["diplomaticStatus"] = "Peace"
        case "early":
            game["turns"] = 2
        case "missing_consent":
            proposal["consents"].pop()
        case "duplicate":
            ledger["outcomes"].append(copy.deepcopy(ledger["outcomes"][0]))
        case "wrong_target":
            proposal["clauses"][0]["item"]["name"] = "Egypt"
        case "payment":
            proposal["clauses"].append(
                {"from": "France", "to": "Rome", "item": {"type": "Gold", "name": "Gold", "amount": 30, "duration": -1}}
            )
        case "handoff":
            ledger["requests"]["trade_decide"]["audit"]["handoff"] = 1
        case "private_event":
            ledger["streams"]["France"].append({"kind": "trade", "body": {"proposal": "d1"}})
        case "missing_receipt":
            del ledger["requests"]["trade_decide"]
        case "wrong_balance":
            ledger["requests"]["trade_decide"]["audit"]["settlement"]["after"]["gold"]["Rome"] -= 1
        case "missing_snapshot":
            del ledger["requests"]["trade_decide"]["audit"]["settlement"]
        case "stale_acceptance":
            ledger["requests"]["trade_decide"]["request"]["version"] = 0
    saved["gameJson"] = json.dumps(game)
    assert not audit(joint)["verified"]


@pytest.mark.parametrize("variant", ["war-paid", "war-joint"])
def test_launcher_accepts_war_variants(variant, tmp_path):
    args = host.parser().parse_args(["--new", "--scenario", variant, "--state-dir", str(tmp_path)])
    assert args.scenario == variant


@pytest.mark.parametrize("reconsider_time", [None, 20, 25])
def test_paid_counter_join_and_restart_archive_does_not_require_reconsideration(joint, reconsider_time):
    root, saved = joint
    game = json.loads(saved["gameJson"])
    game["tileMap"]["mapParameters"]["name"] = "Three-Cornered War: paid"
    game.update(gameId="game", currentPlayer="Rome")
    saved.update(epoch="epoch", gameJson=json.dumps(game))
    ledger = saved["communication"]
    template = copy.deepcopy(ledger["trades"]["proposals"]["d1"][0])
    ledger.update(
        outcomes=[],
        requests={},
        offers={"p1": [{"proposal": "p1", "participants": ["Greece", "France"], "kind": "peace"}]},
    )
    ledger["trades"]["proposals"] = {}
    ledger["streams"]["Rome"] = []

    def version(proposal_id, number, author, declarer, payer, price):
        proposal = copy.deepcopy(template)
        proposal.update(
            proposal=proposal_id, version=number, author=author, partner="Greece" if author == "Rome" else "Rome"
        )
        proposal["clauses"] = [
            {
                "from": declarer,
                "to": "Greece" if declarer == "Rome" else "Rome",
                "item": {"type": "WarDeclaration", "name": "France", "amount": 1, "duration": -1},
            },
            {
                "from": payer,
                "to": "Greece" if payer == "Rome" else "Rome",
                "item": {"type": "Gold", "name": "Gold", "amount": price, "duration": -1},
            },
        ]
        proposal["outcome"].update(
            proposal=proposal_id,
            version=number,
            clauses=proposal["clauses"],
            wars=[{"declaringCivilization": declarer, "targetCivilization": "France", "atWar": True}],
        )
        return proposal

    initial = version("paid", 1, "Rome", "Greece", "Rome", 30)
    initial.update(status="SUPERSEDED", outcome=None)
    counter = version("paid", 2, "Greece", "Greece", "Rome", 47)
    joining = version("join", 1, "Greece", "Rome", "Greece", 17)
    for pid, versions, active, handoff in (("paid", [initial, counter], "Rome", 0), ("join", [joining], "Greece", 1)):
        ledger["trades"]["proposals"][pid] = versions
        ledger["streams"]["Rome"].append({"kind": "trade", "body": {"proposal": pid}})
        ledger["outcomes"].append(versions[-1]["outcome"])
        for p in versions:
            request: dict[str, Any] = {"op": "trade_offer"}
            if p["version"] > 1:
                request.update(proposal=pid, version=1)
            ledger["requests"][f"{pid}-{p['version']}"] = {
                "request": request,
                "response": {"trade": p},
                "audit": {"activeCivilization": active, "handoff": handoff, "timeMillis": 20 + p["version"]},
            }
        final = versions[-1]
        ledger["requests"][f"{pid}-accept"] = {
            "request": {"op": "trade_decide", "decision": "accept", "version": final["version"]},
            "response": {"trade": final, "outcome": final["outcome"]},
            "audit": {"activeCivilization": active, "handoff": handoff, "timeMillis": 30},
        }
    if reconsider_time is not None:
        ledger["requests"]["Greece:reconsider"] = {
            "request": {"op": "reconsider", "proposal": "p1"},
            "response": {"decision": "reconsider"},
            "audit": {"timeMillis": reconsider_time},
        }
    first = {"gold": {"Rome": 500, "Greece": 500, "France": 500}, "wars": []}
    middle = {
        "gold": {"Rome": 453, "Greece": 547, "France": 500},
        "wars": [
            {"declaringCivilization": a, "targetCivilization": b, "atWar": True}
            for a, b in (("Greece", "France"), ("France", "Greece"))
        ],
    }
    final = {
        "gold": {"Rome": 470, "Greece": 530, "France": 500},
        "wars": middle["wars"]
        + [
            {"declaringCivilization": a, "targetCivilization": b, "atWar": True}
            for a, b in (("Rome", "France"), ("France", "Rome"))
        ],
    }
    ledger["requests"]["paid-accept"]["audit"]["settlement"] = {"before": first, "after": middle}
    ledger["requests"]["join-accept"]["audit"]["settlement"] = {"before": middle, "after": final}
    report = audit(joint)
    assert report["checks"]["allMilestones"]
    assert not report["verified"]  # An archive is mandatory for the paid variant.
    archived = copy.deepcopy(saved)
    archived["gameJson"] = json.dumps(dict(game, turns=1))
    baseline = root / "before-restart.json"
    baseline.write_text(json.dumps(archived))
    assert verify_war_scenario.audit(root, before_restart=baseline)["verified"]
    saved["gameJson"] = json.dumps(dict(game, turns=2))
    audit(joint)
    continued = verify_war_scenario.audit(root, before_restart=baseline)
    assert continued["checks"]["preservedAcrossRestartArchive"]
    assert not continued["checks"]["twoRoundsAfterRestartArchive"]
    assert not continued["verified"]
    saved["gameJson"] = json.dumps(game)
    # Sponsorship must leave Rome neutral at the first settlement.
    middle["wars"].append({"declaringCivilization": "Rome", "targetCivilization": "France", "atWar": True})
    assert not audit(joint)["checks"]["allMilestones"]
