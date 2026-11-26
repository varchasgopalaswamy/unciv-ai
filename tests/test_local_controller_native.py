"""Real native HTTPS game + local Pydantic models, without provider inference."""

import asyncio
import json
import re
import secrets
import shutil
import sqlite3
import subprocess
import time
from contextlib import contextmanager
from pathlib import Path

import pytest

from civ_agent.game_config import GameConfig
from civ_agent.operator_connection import Operator
from civ_agent.orchestration.handoff import NativeHandoffs
from civ_agent.orchestration.host_effect_service import NativeCommandService
from civ_agent.replay import ReplayArchive, perspective
from civ_agent.runtime.controller import LocalController, run_controller
from civ_agent.runtime.player import LocalPlayer
from civ_agent.tls import ensure_host_tls, private_directory, write_private_json


@contextmanager
def native_game(root, *, resume=False, record_replay=False):
    repository = Path(__file__).resolve().parents[1]
    executable = repository / "build/install/unciv-ai/bin/unciv-ai"
    if not executable.exists():
        pytest.skip("Build installDist to run the real native local-controller integration")
    private_directory(root)
    # Freeze the runtime: a concurrent build must not replace a jar while the
    # JVM is lazily loading classes during a long acceptance run.
    installed = root / "runtime"
    if not resume:
        shutil.copytree(executable.parents[1], installed)
    executable = installed / "bin/unciv-ai"
    token = secrets.token_urlsafe(32)
    write_private_json(root / "operator.json", {"token": token})
    write_private_json(
        root / "players.json",
        {
            "players": [
                {
                    "civilizationId": "Rome",
                    "token": secrets.token_urlsafe(32),
                    "controller": "AGENT",
                }
            ]
        },
    )
    tls, ca = ensure_host_tls(root, "127.0.0.1", "localhost")
    arguments = [
        str(executable),
        "--data-dir",
        str(root / "data"),
        "--mcp-config",
        str(root / "players.json"),
        "--operator-config",
        str(root / "operator.json"),
        "--tls-config",
        str(tls),
        "--port",
        "0",
    ]
    if resume:
        arguments += ["--resume-checkpoint", str(root / "exploration.json")]
    else:
        arguments += [
            "--new-game",
            "--checkpoint",
            str(root / "exploration.json"),
            "--agent-player",
            "Rome",
            "--built-in-ai",
            "Greece",
            "--city-states",
            "0",
            "--map-radius",
            "10",
            "--seed",
            "42",
        ]
    if record_replay:
        arguments.append("--record-replay")
    with (root / "host.log").open("w") as output:
        process = subprocess.Popen(arguments, stdout=output, stderr=subprocess.STDOUT, cwd=repository)
        try:
            deadline = time.monotonic() + 60
            while True:
                log = (root / "host.log").read_text()
                ready = re.search(r"MCP-READY:(https://localhost:\d+)/mcp", log)
                if ready:
                    break
                if process.poll() is not None or time.monotonic() >= deadline:
                    raise RuntimeError("Native fixture startup failed: " + log[-8000:])
                time.sleep(0.1)
            yield ready.group(1), ca, token
        finally:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)


def local_config():
    return GameConfig.model_validate(
        {
            "game": {"civilizations": ["Rome", "Greece"], "master_seed": 42, "map_radius": 10, "city_states": 0},
            "players": [{"name": "Player"}, {"name": "Native", "controller": "built_in_ai"}],
            "agents": {"model": "scripted-v1", "decisions_enabled": False},
            "runtime": {"scripted_models": True},
            "limits": {"max_turns": 1, "max_seconds": 120, "turn_timeout": 100},
        }
    ).resolved()


def test_real_native_game_runs_main_specialists_review_and_handoff(tmp_path):
    root = tmp_path / "game"
    with native_game(root, record_replay=True) as (url, ca, token):
        config = local_config()
        write_private_json(root / "resolved.json", config.model_dump(mode="json"))
        write_private_json(
            root / "container.json",
            {
                "id": "localtest",
                "runtime": "pydantic-local-v1",
                "launch": "initial",
                "native": {"leaseId": "local-test-lease"},
            },
        )
        state = asyncio.run(run_controller(root, resume=False, host_url=url, host_ca=ca))
        assert state.mode in {"STOPPED", "FINISHED"}, (
            state.reason,
            (root / "native-runtime/failure.json").read_text()
            if (root / "native-runtime/failure.json").exists()
            else "",
        )
        assert state.handoff >= 1 and state.epoch
        assert state.model_requests > 0
        assert state.completed_attempts[0].status == "COMPLETED"
        status = Operator(url, token, ca).call("status")["status"]
        assert status["handoff"] >= 1
    replay = ReplayArchive(root / "exploration.json")
    assert replay.verify()["final_world_matches"]
    assert any(event["kind"] == "turnStarted" for event in perspective(replay, "Greece"))
    assert any(
        event["kind"] in {"unitAppeared", "unitPlaced", "unitMoved", "unitDisappeared", "unitRemoved"}
        for event in perspective(replay, "Greece")
    )


def test_native_turn_admits_all_three_roles_before_any_model_run_completes(tmp_path, monkeypatch):
    root = tmp_path / "game"
    admitted = set()
    all_admitted = asyncio.Event()
    original = LocalPlayer.run_request

    async def concurrent(player, request, **kwargs):
        context = kwargs.get("context")
        if context is not None and context.ticket.turn == 1:
            admitted.add(request.purpose)
            if admitted == {"gameplay", "economy", "military"}:
                all_admitted.set()
            await asyncio.wait_for(all_admitted.wait(), 10)
        return await original(player, request, **kwargs)

    monkeypatch.setattr(LocalPlayer, "run_request", concurrent)
    with native_game(root) as (url, ca, _token):
        config = local_config()
        config.limits.max_turns = 2
        config.agents.economy_interval = 1
        config.agents.military_interval = 1
        write_private_json(root / "resolved.json", config.model_dump(mode="json"))
        write_private_json(
            root / "container.json",
            {
                "id": "localtest",
                "runtime": "pydantic-local-v1",
                "launch": "initial",
                "native": {"leaseId": "local-test-lease"},
            },
        )
        state = asyncio.run(run_controller(root, resume=False, host_url=url, host_ca=ca))
        assert admitted == {"gameplay", "economy", "military"}
        assert state.reason == "MAX_TURNS" and state.handoff >= 2


def test_native_committed_action_survives_bug_and_fresh_attempt_without_rewind(tmp_path, monkeypatch):
    root = tmp_path / "game"
    with native_game(root) as (url, ca, token):
        config = local_config()
        manifest = {
            "id": "localtest",
            "runtime": "pydantic-local-v1",
            "launch": "initial",
            "native": {"leaseId": "local-test-lease"},
        }
        write_private_json(root / "resolved.json", config.model_dump(mode="json"))
        write_private_json(root / "container.json", manifest)
        accepted = []
        original = NativeCommandService.execute

        async def lost_reply(service, effect):
            receipt = await original(service, effect)
            accepted.append((effect.prepared.payload, receipt))
            raise RuntimeError("INJECTED_AFTER_NATIVE_COMMIT")

        with monkeypatch.context() as patch:
            patch.setattr(NativeCommandService, "execute", lost_reply)
            failed = asyncio.run(run_controller(root, resume=False, host_url=url, host_ca=ca))
        assert failed.mode == "FAILED"
        # Concurrent siblings may also report the dispatch fence raised by the
        # injected bug. The group retains those failures for reconciliation.
        assert failed.reason in {"RuntimeError", "ExceptionGroup"}
        assert len(accepted) == 1 and failed.active_attempt is not None
        before_attempt, before_epoch, deadline = failed.active_attempt.id, failed.epoch, failed.deadline
        operator = Operator(url, token, ca)
        before = operator.call("snapshot", leaseId=failed.lease_id)["checkpoint"]
        # The same launch cannot silently recover an application bug.
        blocked = asyncio.run(run_controller(root, resume=False, host_url=url, host_ca=ca))
        assert blocked.reason == "EXPLICIT_RESUME_REQUIRED"
        manifest["launch"] = "explicit-resume"
        write_private_json(root / "container.json", manifest)
        recovered_positions = []

        class InspectRecovery(LocalController):
            async def _attempt(self, status):
                saved = await self.op("snapshot", leaseId=self.store.current.lease_id)
                recovered_positions.append(saved["checkpoint"]["gameJson"])
                return await super()._attempt(status)

        resumed = asyncio.run(InspectRecovery(root, config, manifest, operator, host_ca=ca).run(resume=True))
        assert resumed.mode in {"STOPPED", "FINISHED"}, resumed.reason
        assert recovered_positions[0] == before["gameJson"]
        assert resumed.epoch == before_epoch and resumed.deadline == deadline
        assert resumed.completed_attempts[0].id == before_attempt
        assert resumed.completed_attempts[0].status == "INTERRUPTED"
        assert resumed.completed_attempts[-1].id != before_attempt
        assert resumed.handoff >= 1
        after = operator.call("snapshot", leaseId=failed.lease_id)["checkpoint"]
        receipts = [entry for player in after["players"].values() for entry in player["receipts"]]
        payload, receipt = accepted[0]
        matches = [entry for entry in receipts if entry["request"]["requestId"] == payload["requestId"]]
        assert len(matches) == 1 and matches[0]["receipt"] == receipt
        # Exact queued effects are settled, while their committed records stay inspectable.
        domain = root / "coordinator/Rome/notebook/memory.sqlite3"
        assert domain.exists()
        assert json.loads((root / "native-runtime/status.json").read_text())["epoch"] == before_epoch


def test_finished_native_checkpoint_settles_committed_receipt_without_models_or_activation(tmp_path, monkeypatch):
    root = tmp_path / "game"
    requested = local_config()
    manifest = {
        "id": "localtest",
        "runtime": "pydantic-local-v1",
        "launch": "initial",
        "native": {"leaseId": "local-test-lease"},
    }
    accepted = []
    execute = NativeCommandService.execute

    async def interrupted(service, effect):
        receipt = await execute(service, effect)
        accepted.append((effect.prepared.payload, receipt))
        raise RuntimeError("INJECTED_AFTER_NATIVE_COMMIT")

    with native_game(root) as (url, ca, _):
        write_private_json(root / "resolved.json", requested.model_dump(mode="json"))
        write_private_json(root / "container.json", manifest)
        with monkeypatch.context() as patch:
            patch.setattr(NativeCommandService, "execute", interrupted)
            failed = asyncio.run(run_controller(root, resume=False, host_url=url, host_ca=ca))
        assert failed.mode == "FAILED" and len(accepted) == 1

    # Model the native boundary after an action wins mid-turn: the accepted
    # receipt is durable, and further player activation is now prohibited.
    checkpoint_path = root / "exploration.json"
    checkpoint = json.loads(checkpoint_path.read_text())
    game = json.loads(checkpoint["gameJson"])
    game["victoryData"] = {"winningCiv": "Rome", "victoryType": "Scientific", "victoryTurn": game.get("turns", 0)}
    checkpoint["gameJson"] = json.dumps(game)
    write_private_json(checkpoint_path, checkpoint)
    manifest["launch"] = "terminal-resume"
    write_private_json(root / "container.json", manifest)

    with native_game(root, resume=True) as (url, ca, token):
        operator = Operator(url, token, ca)
        assert operator.call("status")["status"]["phase"] == "FINISHED"
        calls = []
        original_call = Operator.call

        def tracked_call(client, operation, **fields):
            calls.append(operation)
            return original_call(client, operation, **fields)

        def forbidden_model(*args, **kwargs):
            raise AssertionError("Terminal receipt recovery must make no model calls.")

        with monkeypatch.context() as patch:
            patch.setattr(Operator, "call", tracked_call)
            patch.setattr("civ_agent.orchestration.scripted_model.scripted_model", forbidden_model)
            completed = asyncio.run(run_controller(root, resume=True, host_url=url, host_ca=ca))
        assert completed.mode == "FINISHED" and completed.phase == "COMPLETED", completed.reason
        assert completed.epoch == failed.epoch and completed.handoff == failed.handoff
        assert completed.model_requests == failed.model_requests and completed.deadline == failed.deadline
        assert completed.active_attempt is None and completed.completed_attempts[-1].status == "COMPLETED"
        assert "resolve_request" in calls and "activate" not in calls and "unbarrier" not in calls
        saved = json.loads(checkpoint_path.read_text())
        payload, receipt = accepted[0]
        matches = [
            row
            for player in saved["players"].values()
            for row in player["receipts"]
            if row["request"]["requestId"] == payload["requestId"]
        ]
        assert len(matches) == 1 and matches[0]["receipt"] == receipt
        with sqlite3.connect(root / "coordinator/Rome/notebook/memory.sqlite3") as db:
            assert db.execute(
                "SELECT count(*) FROM meta WHERE key LIKE 'localNativeEffect:%' AND value != 'null'"
            ).fetchone() == (0,)


def test_native_committed_handoff_is_not_replayed_after_lost_acknowledgement(tmp_path, monkeypatch):
    root = tmp_path / "game"
    with native_game(root) as (url, ca, token):
        config = local_config()
        manifest = {
            "id": "localtest",
            "runtime": "pydantic-local-v1",
            "launch": "initial",
            "native": {"leaseId": "local-test-lease"},
        }
        write_private_json(root / "resolved.json", config.model_dump(mode="json"))
        write_private_json(root / "container.json", manifest)
        original = NativeHandoffs.execute
        committed = []

        async def lost_reply(service, effect):
            result = await original(service, effect)
            committed.append(result)
            raise RuntimeError("INJECTED_AFTER_HANDOFF_COMMIT")

        with monkeypatch.context() as patch:
            patch.setattr(NativeHandoffs, "execute", lost_reply)
            failed = asyncio.run(run_controller(root, resume=False, host_url=url, host_ca=ca))
        assert (failed.mode, failed.reason) == ("FAILED", "RuntimeError") and len(committed) == 1
        assert failed.active_attempt is not None
        status = Operator(url, token, ca).call("status")["status"]
        assert status["handoff"] > failed.active_attempt.handoff
        manifest["launch"] = "explicit-resume"
        write_private_json(root / "container.json", manifest)
        resumed = asyncio.run(run_controller(root, resume=True, host_url=url, host_ca=ca))
        assert resumed.mode in {"STOPPED", "FINISHED"}, resumed.reason
        assert resumed.attempts == failed.attempts
        assert resumed.model_requests == failed.model_requests
        assert resumed.epoch == failed.epoch
        assert resumed.completed_attempts[-1].status == "COMPLETED"


def test_legacy_request_cap_does_not_stop_review_corrections_or_native_handoff(tmp_path, monkeypatch):
    import sqlite3

    from pydantic_ai.messages import ModelResponse, ToolCallPart
    from pydantic_ai.models.function import FunctionModel
    from pydantic_ai_harness.step_persistence import SqliteStepStore

    from civ_agent.orchestration import scripted_model
    from civ_agent.player_paths import PlayerPaths
    from civ_agent.runtime.review_cycles import ReviewCycle

    supplied = []
    original = scripted_model.scripted_review_model

    def invalid_review(request):
        base = original(request)
        assert base.function is not None

        def respond(messages, info):
            response = base.function(messages, info)
            assert isinstance(response, ModelResponse)
            report = response.parts[0]
            assert isinstance(report, ToolCallPart)
            body = report.args_as_dict()
            if len(supplied) < 3:
                body["review"]["materialAssumptions"][0].update(status="inferred", sources=["invented-source"])
            supplied.append(request)
            return ModelResponse(parts=[ToolCallPart("submit_report", body, "invalid-report")])

        return FunctionModel(respond, model_name="scripted-v1")

    monkeypatch.setattr(scripted_model, "scripted_review_model", invalid_review)
    root = tmp_path / "game"
    with native_game(root) as (url, ca, _token):
        config = local_config()
        config.agents.request_limit = 1
        write_private_json(root / "resolved.json", config.model_dump(mode="json"))
        write_private_json(
            root / "container.json",
            {
                "id": "localtest",
                "runtime": "pydantic-local-v1",
                "launch": "initial",
                "native": {"leaseId": "local-test-lease"},
            },
        )
        state = asyncio.run(run_controller(root, resume=False, host_url=url, host_ca=ca))
        assert state.mode == "STOPPED" and state.reason == "MAX_TURNS"
        assert state.active_attempt is None and state.handoff >= 1
        assert state.model_requests > 2
        assert len(supplied) == 4
        paths = PlayerPaths(root, 0, "Rome")
        database = paths.coordinator / "notebook/memory.sqlite3"
        with sqlite3.connect(database.as_uri() + "?mode=ro", uri=True) as db:
            row = db.execute("SELECT value FROM meta WHERE key = 'nativeReviewCycle:0'").fetchone()
        assert row is not None
        cycle = ReviewCycle.model_validate_json(row[0])
        assert cycle.kind == "opening" and cycle.status == "READY" and cycle.committed
        assert cycle.request == supplied[0]

        async def recorded_requests():
            steps = SqliteStepStore(database=paths.store / "agent-steps.sqlite3")
            runs = await steps.list_runs(
                conversation_id=supplied[0].scope.identity("conversation", "strategic_review_v1")
            )
            assert len(runs) == 2
            events = [event for run in runs for event in await steps.list_events(run_id=run.run_id)]
            assert sum(event.kind == "model_request_completed" for event in events) == 4
            assert all(run.metadata["review_cycle_id"] == cycle.id for run in runs)

        asyncio.run(recorded_requests())
        assert not (root / "native-runtime/failure.json").exists()


def test_real_native_64_turns_reuse_player_objects_and_bound_transient_permits(tmp_path, monkeypatch):
    from civ_agent.runtime.player import LocalPlayer

    contexts, players = [], set()
    original = LocalPlayer.begin

    async def begin(player, attempt):
        players.add(id(player))
        context = await original(player, attempt)
        assert context.role_passes == {"gameplay": 0, "economy": 0, "military": 0}
        assert context.handoff_pass == 0 and not context.allowances
        assert len(player.permits) <= 3
        contexts.append(context)
        return context

    monkeypatch.setattr(LocalPlayer, "begin", begin)
    root = tmp_path / "game"
    with native_game(root) as (url, ca, _token):
        config = local_config()
        config.limits.max_turns = 64
        config.limits.max_seconds = 1800
        config.limits.turn_timeout = 120
        config.limits.max_invocations = 1000
        write_private_json(root / "resolved.json", config.model_dump(mode="json"))
        write_private_json(
            root / "container.json",
            {
                "id": "local64",
                "runtime": "pydantic-local-v1",
                "launch": "initial",
                "native": {"leaseId": "local64-lease"},
            },
        )
        result = asyncio.run(run_controller(root, resume=False, host_url=url, host_ca=ca))
        assert result.mode == "STOPPED" and result.reason == "MAX_TURNS", (
            result.reason,
            (root / "native-runtime/failure.json").read_text()
            if (root / "native-runtime/failure.json").exists()
            else "",
        )
        assert result.turn >= 64 and result.handoff >= 64
        assert len(players) == 1 and len(contexts) >= 64
        assert all(context.handoff_pass <= 32 for context in contexts)
