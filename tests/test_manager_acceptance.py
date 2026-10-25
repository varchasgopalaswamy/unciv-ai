"""Controlled acceptance runners must expose missing evidence, not infer success from scores."""

import copy
import json
import sqlite3
from argparse import Namespace
from pathlib import Path

import pytest
from pydantic import ValidationError

from civ_agent.civilization_state import CivilizationState
from civ_agent.cli import parser
from civ_agent.commands import manager_test
from civ_agent.commands import verify_manager_scenario as audit
from civ_agent.game_config import GameConfig, load_config
from civ_agent.protocol import JsonObject
from civ_agent.tls import write_private_json

REPO = Path(__file__).resolve().parents[1]


def test_profiles_have_fixed_roster_bounded_stages_and_native_scenario():
    for name in ("economy-manager", "military-manager"):
        config = load_config(REPO / "examples" / (name + "-test.toml")).resolved()
        assert [p.civilization for p in config.agent_players] == ["Rome", "Greece"]
        assert config.game.scenario == name and config.limits.max_turns == 2
        assert config.engine_arguments()[:2] == ["--scenario", name]
        assert len(config.engine_arguments()) == 4
        assert config.turn_order[-1].controller == "built_in_ai"
    with pytest.raises(ValidationError, match="fixed Rome/Greece"):
        GameConfig(game={"scenario": "economy-manager"})


def test_runner_cli_dry_run_is_nonmutating_and_has_restart(tmp_path, capsys):
    args = parser().parse_args(
        ["game", "test-managers", "--state-dir", str(tmp_path / "test"), "--dry-run", "--model", "custom"]
    )
    assert args.handler(args) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["roundBoundaries"] == [2, 6]
    assert not (tmp_path / "test").exists()
    for commands in report["commands"].values():
        assert "--model" in commands[0]
        assert commands[2][-1] == "--save-baseline"
        assert commands[3][:2] == ["game", "resume"]
        assert commands[4][:2] == ["game", "stop"]


def test_runner_refuses_existing_case_before_launch(tmp_path, monkeypatch):
    (tmp_path / "economy-manager").mkdir()
    monkeypatch.setattr(manager_test.subprocess, "run", lambda *_a, **_k: pytest.fail("Launched existing case"))
    assert (
        manager_test.run(
            Namespace(state_dir=tmp_path, case="both", repo_dir=None, dry_run=False, model=None, image=None)
        )
        == 1
    )


def test_wait_ignores_previous_launch_status_and_reports_early_budget(tmp_path, monkeypatch):
    write_private_json(tmp_path / "container-status.json", {"status": "budget_exhausted", "updated_at": 1})
    write_private_json(tmp_path / "exploration.json", {"gameJson": json.dumps({"turns": 2})})

    def advance(_):
        write_private_json(tmp_path / "container-status.json", {"status": "budget_exhausted", "updated_at": 3})
        write_private_json(tmp_path / "exploration.json", {"gameJson": json.dumps({"turns": 6})})

    monkeypatch.setattr(manager_test.time, "sleep", advance)
    manager_test.wait_boundary(tmp_path, 6, not_before=2)
    with pytest.raises(RuntimeError, match="round 6"):
        manager_test.wait_boundary(tmp_path, 7, not_before=2)


def test_runner_cleans_failed_launch_and_runs_other_case(tmp_path, monkeypatch):
    commands = []
    boundaries = []

    def execute(command, *, check):
        commands.append(command)
        if any(argument.endswith("economy-manager-test.toml") for argument in command):
            raise manager_test.subprocess.CalledProcessError(1, command)

    monkeypatch.setattr(manager_test.subprocess, "run", execute)
    monkeypatch.setattr(
        manager_test, "wait_boundary", lambda directory, rounds, **_: boundaries.append((directory.name, rounds))
    )
    monkeypatch.setattr(manager_test, "verify", lambda _: {"verified": True, "status": "PASSED"})
    args = Namespace(state_dir=tmp_path, case="both", repo_dir=None, dry_run=False, model=None, image=None)
    assert manager_test.run(args) == 2
    report = json.loads((tmp_path / "manager-tests.json").read_text())
    assert report["economy-manager"]["status"] == "FAILED"
    assert report["military-manager"]["verified"]
    assert commands[1][3:5] == ["game", "stop"]
    assert commands[1][-1] == str(tmp_path / "economy-manager")
    assert boundaries == [("military-manager", 2), ("military-manager", 6)]


@pytest.mark.parametrize("paused", [False, True])
def test_runner_preserves_other_case_reports_and_refreshes_failed_resume(tmp_path, monkeypatch, paused):
    earlier = {"verified": False, "status": "FAILED", "error": "Earlier economy audit failure"}
    write_private_json(tmp_path / "manager-tests.json", {"economy-manager": earlier})
    directory = tmp_path / "military-manager"

    def execute(command, *, check):
        if command[3:5] == ["game", "resume"]:
            write_private_json(directory / "verification/manager-scenario.json", {"checks": {"nativePeace": False}})

    def boundary(directory, rounds, **kwargs):
        if paused and rounds == 6:
            raise RuntimeError("Activation timed out")

    monkeypatch.setattr(manager_test.subprocess, "run", execute)
    monkeypatch.setattr(manager_test, "wait_boundary", boundary)
    monkeypatch.setattr(
        manager_test, "verify", lambda _: {"verified": True, "status": "PASSED", "checks": {"nativePeace": True}}
    )
    args = Namespace(state_dir=tmp_path, case="war", repo_dir=None, dry_run=False, model=None, image=None)
    assert manager_test.run(args) == (2 if paused else 0)
    report = json.loads((tmp_path / "manager-tests.json").read_text())
    assert report["economy-manager"] == earlier
    assert report["military-manager"]["checks"]["nativePeace"]
    if paused:
        assert report["military-manager"]["status"] == "FAILED"
        assert report["military-manager"]["error"] == "Activation timed out"
        assert json.loads((directory / "verification/manager-scenario.json").read_text()) == report["military-manager"]


def evidence(player, *, war=False, turns=2) -> JsonObject:
    plans = [
        {
            "key": f"city-{i}",
            "version": 1,
            "category": "city_plan",
            "body": {
                "type": "city_plan",
                "state": "active",
                "city": f"city{i}",
                "reasons": {"construction": "Support growth"},
                "reconsiderWhen": ["Growth changes"],
                "_confirmedActions": {"r1": {"kind": "QUEUE_CONSTRUCTION"}},
            },
        }
        for i in (1, 2)
    ]
    jobs = {
        "main": {"purpose": "gameplay", "game_turn": 0, "status": "COMPLETED"},
        "eco": {"purpose": "economy", "game_turn": 0, "status": "COMPLETED"},
    }
    if turns > 2:
        jobs["later-eco"] = {"purpose": "economy", "game_turn": 4, "status": "COMPLETED"}
        jobs["later-main"] = {"purpose": "gameplay", "game_turn": 4, "status": "COMPLETED"}

    def call(name, args, result, id="call", job="main"):
        return {"name": name, "arguments": args, "result": result, "id": id, "job": job}

    calls = [
        call(
            "player_command",
            {"kind": "END_TURN"},
            {"economyCoordination": {"rolesFinished": {"gameplay": True, "economy": True}}},
            id=str(i),
        )
        for i in range(turns)
    ]
    calls += [
        call(
            "memory_read",
            {},
            {"records": [{"type": "city_plan"}]},
            id="startup:memory:0",
            job="later-eco" if turns > 2 else "eco",
        )
    ]
    # Later preload is represented by its own native turn, even in the first-stage fixture.
    jobs["preload-eco"] = {"purpose": "economy", "game_turn": 1, "status": "COMPLETED"}
    calls[-1]["job"] = "preload-eco"
    calls += [
        call("economy_request", {"priority": "urgent"}, {}),
        call("city_override", {"city": "city2"}, {}),
        call("finish_role", {}, {"roleFinished": True}, id="first", job="eco"),
        call("finish_role", {}, {"roleFinished": True}, id="second", job="eco"),
    ]
    actions = [
        {
            "job": "eco",
            "transport": "player_command",
            "payload": {"kind": "QUEUE_CONSTRUCTION", "target": "city1"},
            "result": {},
        }
    ]
    actions += [
        {
            "job": "eco",
            "transport": "player_command",
            "payload": {"kind": "BUY_CONSTRUCTION", "selection": "Worker"},
            "result": r,
        }
        for r in ({"error": "OPTION_UNAVAILABLE"}, {})
    ]
    meta = {"epoch": "e1", "civilization": player, "storeId": player}
    if war:
        jobs["army"] = {"purpose": "military", "game_turn": 0, "status": "COMPLETED"}
        meta["war:state"] = {
            "episodes": {
                "war1": {"active": False, "version": 2, "history": [{"responses": {"gameplay": {"agree": True}}}]}
            },
            "operations": {"defense": {"plan": "Protect frontier"}},
        }
        meta["peace:approved"] = {
            "status": "complete",
            "overview": {"battlefield": "Defended"},
            "approval": {"approve": True},
        }
        calls += [call("escort_assignment", {"unit": "escort", "civilian": "worker"}, {})]
        actions += [
            {
                "job": "main",
                "transport": "player_command",
                "payload": {"kind": "MOVE_UNIT", "target": unit},
                "result": {},
            }
            for unit in ("escort", "worker")
        ]
    return {
        "meta": meta,
        "jobs": jobs,
        "calls": calls,
        "actions": actions,
        "heads": plans,
        "records": [
            *copy.deepcopy(plans),
            {"category": "observation", "body": {"economy": {"committedGold": 480, "availableGold": 20}}},
        ],
    }


def save_native(root, *, war=False, turns=2):
    game: JsonObject = {
        "gameId": "game1",
        "turns": turns,
        "currentPlayer": "Rome",
        "tileMap": {
            "mapParameters": {"name": "Military manager acceptance" if war else "Economy manager acceptance"},
            "tileList": [
                {
                    "position": {"x": 0, "y": 0},
                    "civilianUnit": {"owner": "Rome", "name": "Worker", "instanceName": "Road Crew"},
                    "militaryUnit": {"owner": "Rome", "name": "Spearman", "instanceName": "Escort Guard"},
                }
            ],
        },
        "civilizations": [
            {"civName": "Rome", "diplomacy": {"Greece": {"diplomaticStatus": "Peace"}}},
            {"civName": "Greece"},
        ],
    }
    for civ in game["civilizations"]:
        civ["cities"] = [
            {
                "name": f"City {i}",
                "population": {"population": 4},
                "cityConstructions": {"constructionQueue": ["Worker"]},
            }
            for i in (1, 2)
        ]
    write_private_json(
        root / "exploration.json",
        {
            "epoch": "e1",
            "gameJson": json.dumps(game),
            "communication": {
                "trades": {
                    "proposals": {
                        "d1": [{"nativeRequestId": "n1", "author": "Rome", "partner": "France", "status": "WITHDRAWN"}]
                    }
                }
            },
        },
    )


@pytest.mark.parametrize("war", [False, True])
def test_audit_requires_baseline_and_later_jobs_and_keeps_missing_checks(tmp_path, monkeypatch, war):
    memories = {p: evidence(p, war=war) for p in audit.PLAYERS}
    monkeypatch.setattr(audit, "read_memory", lambda path: memories[path.parents[1].name])
    save_native(tmp_path, war=war)
    report = audit.verify(tmp_path, save_baseline=True)
    assert report["status"] == "PENDING" and not report["verified"]
    for p in audit.PLAYERS:
        memories[p] = evidence(p, war=war, turns=6)
    save_native(tmp_path, war=war, turns=6)
    report = audit.verify(tmp_path)
    assert report["verified"], report["checks"]
    memories["Rome"]["records"][0]["body"]["reasons"] = {"construction": "Discarded original history"}
    report = audit.verify(tmp_path)
    assert report["status"] == "FAILED" and not report["checks"]["Rome:restartHistory"]


def test_stale_purchase_failure_does_not_count_as_reserved_funds_test(tmp_path, monkeypatch):
    memories = {p: evidence(p, turns=6) for p in audit.PLAYERS}
    monkeypatch.setattr(audit, "read_memory", lambda path: memories[path.parents[1].name])
    save_native(tmp_path, turns=6)
    memories["Rome"]["actions"][1]["result"]["error"] = "STALE_REVISION"
    assert not audit.verify(tmp_path)["checks"]["reservedPurchaseRejected"]


def test_audit_accepts_observations_without_an_economy_section(tmp_path, monkeypatch):
    memories = {p: evidence(p) for p in audit.PLAYERS}
    memories["Rome"]["records"].insert(0, {"category": "observation", "body": {"economy": None}})
    monkeypatch.setattr(audit, "read_memory", lambda path: memories[path.parents[1].name])
    save_native(tmp_path)
    report = audit.verify(tmp_path, save_baseline=True)
    assert report["checks"]["reservedPurchaseRejected"]
    assert report["status"] == "PENDING"


def test_audit_uses_native_tile_units_and_excludes_other_civilizations(tmp_path, monkeypatch):
    memories = {p: evidence(p, war=True) for p in audit.PLAYERS}
    monkeypatch.setattr(audit, "read_memory", lambda path: memories[path.parents[1].name])
    save_native(tmp_path, war=True)
    report = audit.verify(tmp_path)
    assert report["checks"]["escortSurvived"]
    assert {u["label"] for u in report["evidence"]["survivingUnits"]["Rome"]} == {"Road Crew", "Escort Guard"}
    assert not report["evidence"]["survivingUnits"]["Greece"]
    path = tmp_path / "exploration.json"
    saved = json.loads(path.read_text())
    game = json.loads(saved["gameJson"])
    game["tileMap"]["tileList"][0]["militaryUnit"]["owner"] = "Greece"
    saved["gameJson"] = json.dumps(game)
    write_private_json(path, saved)
    assert not audit.verify(tmp_path)["checks"]["escortSurvived"]


def test_audit_reads_sqlite_without_creating_missing_database(tmp_path):
    missing = tmp_path / "missing.sqlite3"
    with pytest.raises(sqlite3.OperationalError):
        audit.read_memory(missing)
    assert not missing.exists()
    memory = CivilizationState(tmp_path / "state", "Rome")
    memory.bind("e1")
    assert audit.read_memory(memory.path)["meta"]["civilization"] == "Rome"


def test_audit_excludes_abandoned_jobs_and_receipts_after_restore(tmp_path):
    memory = CivilizationState(tmp_path / "state", "Rome")
    memory.bind("e1")

    def completed_job(name):
        memory.start_job(name, "gameplay", {})
        request = memory.queue(name, "player_command", {"kind": "MOVE_UNIT", "target": "unit1"}, "move")
        memory.receipt(request["id"], {"kind": "MOVE_UNIT", "moved": True})
        memory.call(name, "done", "finish_role", {}, {"roleFinished": True})
        memory.finish_job(name, True)

    completed_job("retained")
    checkpoint = tmp_path / "round.sqlite3"
    checksum = memory.backup(checkpoint)
    completed_job("abandoned")
    memory.restore(checkpoint, checksum, "e2")
    saved = audit.read_memory(memory.path)
    assert set(saved["jobs"]) == {"retained"}
    assert {c["job"] for c in saved["calls"]} == {"retained"}
    assert {a["job"] for a in saved["actions"]} == {"retained"}
