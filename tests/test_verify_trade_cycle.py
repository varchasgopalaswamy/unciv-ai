"""Negative audits of a recorded real-engine run; agent claims never supply evidence."""

import copy
import gzip
import json
from pathlib import Path

import pytest

from civ_agent import cli
from civ_agent.commands import verify_trade_cycle


@pytest.fixture
def match(tmp_path):
    # Captured by TradeCycleProbe, then stripped of unrelated map/city fields and
    # compressed. Includes the engine's omitted defaults and asymmetric treaty ticks.
    data = json.loads(
        gzip.decompress(Path(__file__).with_name("fixtures").joinpath("trade-cycle.json.gz").read_bytes())
    )
    saved = data["checkpoint"]
    rounds = tmp_path / "round-checkpoints"
    rounds.mkdir()
    (rounds / f"{saved['epoch']}-round-0.json").write_text(
        json.dumps({"host": {"gameJson": json.dumps(data["initialGame"])}})
    )
    (rounds / f"{saved['epoch']}-round-10.json").write_text(
        json.dumps({"host": {"gameJson": json.dumps(data["fundingGame"])}})
    )
    write(tmp_path, saved)
    return tmp_path, saved


def write(root, saved):
    (root / "exploration.json").write_text(json.dumps(saved))


def proposals(saved):
    return list(saved["communication"]["trades"]["proposals"].values())


def test_real_engine_completion_without_any_agent_logs(match):
    root, _ = match
    report = verify_trade_cycle.audit(root)
    assert report["verified"]
    assert all(step["status"] == "PASSED" for step in report["steps"])
    assert report["cityOwners"] == {"Rome": "Rome", "Sparta": "Rome", "Athens": "Greece", "Antium": "Greece"}
    assert not (root / "agents").exists()
    assert cli.main(["match", "verify-trade-cycle", "--state-dir", str(root)]) == 0
    assert (root / "verification" / "trade-cycle.json").stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize(
    "change",
    ["city", "resources", "duration", "duplicate_native_deal", "treaty", "embassy", "unresolved_city", "too_early"],
)
def test_native_save_overrules_completed_ledger_and_optimistic_transcript(match, change):
    root, saved = match
    game = json.loads(saved["gameJson"])
    rome, greece = game["civilizations"]
    match change:
        case "city":
            city = greece["cities"].pop()
            rome["cities"].append(city)
        case "resources":
            rome["diplomacy"]["Greece"]["trades"][0]["theirOffers"][0]["duration"] = 0
        case "duration":
            rome["diplomacy"]["Greece"]["trades"][0]["theirOffers"][0]["duration"] = 25
        case "duplicate_native_deal":
            rome["diplomacy"]["Greece"]["trades"].append(copy.deepcopy(rome["diplomacy"]["Greece"]["trades"][0]))
        case "treaty":
            greece["diplomacy"]["Rome"]["flagsCountdown"].pop("ResearchAgreement")
        case "embassy":
            greece["diplomacy"]["Rome"]["diplomaticModifiers"].pop("SharedEmbassies")
        case "unresolved_city":
            greece["popupAlerts"] = [{"type": "CityTraded", "value": greece["cities"][-1]["id"]}]
        case "too_early":
            game["turns"] = 11
    saved["gameJson"] = json.dumps(game)
    write(root, saved)
    (root / "agent-transcript.md").write_text("Every trade objective is complete. Both players verified everything.")
    report = verify_trade_cycle.audit(root)
    assert report["checks"]["allElevenSteps"]
    assert not report["verified"]


@pytest.mark.parametrize(
    "change",
    [
        "counter_price",
        "extra_counter",
        "consent",
        "outcome",
        "handoff",
        "unreceipted_gift",
        "duplicate_outcome",
        "closed_wrongly",
    ],
)
def test_incorrect_or_unconfirmed_negotiation_does_not_pass(match, change):
    root, saved = match
    ledger = saved["communication"]
    deals = proposals(saved)
    match change:
        case "counter_price":
            deals[5][1]["clauses"][1]["item"]["amount"] += 1
        case "extra_counter":
            deals[5].append(copy.deepcopy(deals[5][-1]))
        case "consent":
            deals[4][-1]["consents"].pop()
        case "outcome":
            deals[4][-1]["outcome"] = None
        case "handoff":
            row = next(r for r in ledger["requests"].values() if r["request"]["op"] == "trade_decide")
            row["audit"]["handoff"] += 1
        case "unreceipted_gift":
            gift = deals[4][-1]["proposal"]
            ledger["requests"] = {
                k: r
                for k, r in ledger["requests"].items()
                if (r["response"].get("trade") or {}).get("proposal") != gift
            }
        case "duplicate_outcome":
            ledger["outcomes"].append(copy.deepcopy(ledger["outcomes"][0]))
        case "closed_wrongly":
            deals[3][-1]["status"] = "WITHDRAWN"
    write(root, saved)
    report = verify_trade_cycle.audit(root)
    assert not report["verified"]
    assert any(step["status"] == "FAILED" for step in report["steps"])


def test_late_acceptance_retry_does_not_count_as_a_second_negotiation(match):
    root, saved = match
    ledger = saved["communication"]
    accept = copy.deepcopy(
        next(
            r
            for r in ledger["requests"].values()
            if r["request"]["op"] == "trade_decide" and r["request"]["decision"] == "accept"
        )
    )
    accept["audit"]["handoff"] = 26
    accept["audit"]["activeCivilization"] = "Greece"
    accept["audit"]["timeMillis"] += 10_000
    ledger["requests"]["later-accept"] = accept
    write(root, saved)
    assert verify_trade_cycle.audit(root)["verified"]


def test_missing_funding_snapshot_does_not_establish_that_funding_was_needed(match):
    root, saved = match
    (root / "round-checkpoints" / f"{saved['epoch']}-round-10.json").unlink()
    report = verify_trade_cycle.audit(root)
    assert not report["verified"]
    assert not report["checks"]["researchFundingWasNecessary"]


def test_initial_checkpoint_reports_pending_without_inventing_success(match):
    root, saved = match
    initial = json.loads((root / "round-checkpoints" / f"{saved['epoch']}-round-0.json").read_text())["host"][
        "gameJson"
    ]
    saved["gameJson"] = initial
    saved["handoff"] = 0
    saved["communication"]["trades"]["proposals"] = {}
    saved["communication"]["streams"] = {}
    saved["communication"]["requests"] = {}
    saved["communication"]["outcomes"] = []
    write(root, saved)
    report = verify_trade_cycle.audit(root)
    assert not report["verified"]
    assert all(step["status"] == "PENDING" for step in report["steps"])
    assert cli.main(["match", "verify-trade-cycle", "--state-dir", str(root)]) == 1


def test_wrong_scenario_or_broken_checkpoint_fails_cleanly(match):
    root, saved = match
    game = json.loads(saved["gameJson"])
    game["tileMap"]["mapParameters"]["name"] = "Ordinary duel"
    saved["gameJson"] = json.dumps(game)
    write(root, saved)
    assert cli.main(["match", "verify-trade-cycle", "--state-dir", str(root)]) == 2
    (root / "exploration.json").write_text("{")
    assert cli.main(["match", "verify-trade-cycle", "--state-dir", str(root)]) == 2
