"""Launch boundaries and operator views for the single-process player runtime."""

import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, call

import pytest

from civ_agent import container_runtime, match_watch, watch_state
from civ_agent import container_supervisor as supervisor
from civ_agent.commands import batch, game
from civ_agent.container_identity import LOCAL_RUNTIME, require_local_runtime
from civ_agent.container_support import identity
from civ_agent.game_config import GameConfig, Player
from civ_agent.operator_state import local_state, projection, published_game
from civ_agent.orchestration.managed_player import NativePlayerSession, NativePlayerSettings
from civ_agent.runtime.state import AttemptRecord, ControllerState, RoleStatus
from civ_agent.tls import read_json, write_private_json


@pytest.fixture
def local_game(tmp_path):
    config = GameConfig(agents={"model": "scripted-v1"}, runtime={"scripted_models": True}).resolved()
    record = identity(tmp_path)
    record.update(
        auth_root=str(tmp_path.parent / "auth"),
        images={"runtime": "sha256:" + "a" * 64},
        launchResumeRequested=False,
    )
    write_private_json(tmp_path / "container.json", record)
    write_private_json(tmp_path / "resolved.json", config.model_dump(mode="json"))
    return tmp_path, config, record


def saved_state(local_game):
    root, config, record = local_game
    now = datetime.now(UTC)
    state = ControllerState(
        game=record["id"],
        epoch="epoch-1",
        lease_id="lease-1",
        config_sha256="a" * 64,
        initial_started_at=now,
        deadline=now + timedelta(seconds=600),
        initial_turn=1,
        turn=2,
        handoff=3,
        phase="ROLE_WORK",
        active_civilization=config.agent_players[0].civilization,
        active_controller="AGENT",
        seats=[
            {
                "civilization": player.civilization,
                "session": NativePlayerSession(
                    game=record["id"],
                    player=f"p{index}",
                    settings=NativePlayerSettings.model_validate({**config.settings(player), "scripted": True}),
                ).model_dump(mode="json"),
            }
            for index, player in enumerate(config.agent_players)
        ],
        active_attempt=AttemptRecord(
            id="attempt-1",
            player="p0",
            civilization=config.agent_players[0].civilization,
            epoch="epoch-1",
            turn=2,
            handoff=3,
            status="RUNNING",
            roles=[
                RoleStatus(purpose="gameplay", job="main", phase="ROLE_FINISHED", model_requests=3),
                RoleStatus(purpose="economy", job="economy", phase="RUNNING", model_requests=2),
                RoleStatus(purpose="military", job="military", phase="WAITING_PROVIDER", model_requests=1),
            ],
        ),
    )
    write_private_json(root / "native-runtime/controller.json", state.model_dump(mode="json"))
    write_private_json(root / "native-runtime/status.json", {**state.model_dump(mode="json"), "roles": []})
    return state


@pytest.mark.parametrize("resume", [False, True])
def test_launch_starts_only_controller_and_preserves_explicit_intent(local_game, monkeypatch, resume):
    root, _, record = local_game
    saved_state(local_game)  # The presence of saved state must never grant restart permission.
    record["launchResumeRequested"] = resume
    write_private_json(root / "container.json", record)
    run = supervisor.GameRun(root)
    started, networks, cleaned = [], [], []
    monkeypatch.setattr(supervisor, "cleanup", cleaned.append)
    monkeypatch.setattr(supervisor, "firewall", lambda *_: None)

    def network(name, bridge, **kwargs):
        networks.append((name, bridge, kwargs))
        return {"subnets": [{"subnet": "10.42.0.0/24"}]}

    def start(arguments, name, image, *entry):
        started.append((arguments, name, image, entry))
        write_private_json(root / "runtime.json", {"phase": "host_ready", "launch": run.record["launch"]})

    monkeypatch.setattr(supervisor, "create_network", network)
    monkeypatch.setattr(run, "start", start)
    monkeypatch.setattr(supervisor, "podman", Mock(side_effect=AssertionError("Unexpected infrastructure process")))
    run.setup()
    assert len(started) == 1
    arguments, name, image, entry = started[0]
    assert name == "civ-" + record["id"] + "-host" and entry == ("controller",)
    assert image == record["images"]["runtime"]
    assert len(networks) == 2 and networks[0][2] == {"internal": True}
    assert str(root) + ":/match:rw,z" in arguments
    assert cleaned == [record["id"]]
    saved = read_json(root / "container.json")
    assert saved["launchResumeRequested"] is resume and saved["servicesReady"] is True
    assert read_json(root / "control/start.json") == {"launch": saved["launch"]}
    assert not any(
        (root / "native-runtime" / name).exists() for name in ("database.json", "postgres.env", "temporal.env")
    )


def test_human_launch_waits_for_the_localhost_port_and_browser(local_game, monkeypatch):
    root, config, record = local_game
    config.players[0] = Player(name=config.players[0].name, controller="human")
    record["images"]["desktop"] = "sha256:" + "b" * 64
    write_private_json(root / "container.json", record)
    write_private_json(root / "resolved.json", config.model_dump())
    run = supervisor.GameRun(root)
    monkeypatch.setattr(supervisor, "cleanup", Mock())
    monkeypatch.setattr(supervisor, "firewall", Mock())
    monkeypatch.setattr(supervisor, "create_network", Mock(return_value={"subnets": [{"subnet": "10.42.0.0/24"}]}))
    browser_check = Mock(side_effect=[False, True])
    port = Mock(
        side_effect=[
            SimpleNamespace(returncode=125, stdout=""),
            SimpleNamespace(returncode=0, stdout=""),
            SimpleNamespace(returncode=0, stdout="127.0.0.1:0\n"),
            SimpleNamespace(returncode=0, stdout="127.0.0.1:42753\n"),
            SimpleNamespace(returncode=0, stdout="127.0.0.1:42753\n"),
        ]
    )
    monkeypatch.setattr(supervisor, "browser_ready", browser_check)
    monkeypatch.setattr(supervisor, "podman", port)
    monkeypatch.setattr(supervisor.time, "sleep", Mock())

    started = []

    def start(arguments, name, image, *entry):
        started.append((arguments, name, image, entry))
        if name == run.controller:
            write_private_json(root / "runtime.json", {"phase": "host_ready", "launch": run.record["launch"]})

    monkeypatch.setattr(run, "start", start)
    run.setup()

    controller, browser = started
    assert "--publish" not in controller[0] and "CIV_BROWSER_BIND=10.42.0.254" in controller[0]
    args, name, image, entry = browser
    assert args.count("--network") == 1 and args[args.index("--network") + 1] == run.control_net
    assert args[args.index("--publish") + 1] == "127.0.0.1::6080"
    assert "--volume" not in args and image == controller[2] == record["images"]["desktop"]
    assert entry[0] == "-c" and entry[-1] == "10.42.0.254"
    assert port.call_args_list == [call("port", name, "6080/tcp", check=False)] * 5
    assert browser_check.call_args_list == [call(42753)] * 2
    assert read_json(root / "browser.json")["url"] == "http://127.0.0.1:42753/vnc.html?resize=scale"


def test_old_deployment_cannot_launch_or_resume_before_effects(local_game, monkeypatch):
    root, _, record = local_game
    record.update(format=1, runtime="pydantic-temporal-v1")
    write_private_json(root / "container.json", record)
    forbidden = Mock(side_effect=AssertionError("Archive execution caused an effect"))
    monkeypatch.setattr(supervisor, "cleanup", forbidden)
    monkeypatch.setattr(supervisor, "create_network", forbidden)
    before = {path: path.read_bytes() for path in root.rglob("*") if path.is_file()}
    with pytest.raises(ValueError, match="read-only archive"):
        supervisor.GameRun(root).setup()
    with pytest.raises(ValueError, match="read-only archive"):
        game.resume_configuration(root, game.parser(action="resume").parse_args([]))
    assert all(path.read_bytes() == value for path, value in before.items())
    forbidden.assert_not_called()


def test_missing_subscription_precedes_decisions_broker_and_setup_effects(local_game, monkeypatch):
    root, _, record = local_game
    record["auth_root"] = str(root.parent / (root.name + "-auth"))
    write_private_json(root / "container.json", record)
    config = GameConfig(agents={"decisions_enabled": True, "decisions_backend": "jev"}).resolved()
    write_private_json(root / "resolved.json", config.model_dump())
    forbidden = Mock(side_effect=AssertionError("A missing login must be reported before starting services"))
    monkeypatch.setattr(supervisor, "ensure_decisions", forbidden)
    monkeypatch.setattr(supervisor, "cleanup", forbidden)
    with pytest.raises(supervisor.ContainerError, match="auth login"):
        supervisor.GameRun(root).setup()
    assert read_json(root / "container.json") == record
    assert not (root / "launches").exists()
    forbidden.assert_not_called()


def test_batch_rejects_archived_member_before_preflight_or_writes(local_game, monkeypatch):
    root, _, record = local_game
    member = root / "games" / "one"
    write_private_json(member / "container.json", {**record, "format": 1})
    write_private_json(root / "batch.json", {"format": 2, "parallel": 1, "runs": [{"id": "one", "status": "paused"}]})
    forbidden = Mock(side_effect=AssertionError("Archive reached execution preflight"))
    monkeypatch.setattr(batch, "preflight", forbidden)
    before = (root / "batch.json").read_bytes()
    args = batch.parser(action="resume").parse_args(["--state-dir", str(root)])
    with pytest.raises(ValueError, match="read-only archive"):
        batch._resume(args)
    assert (root / "batch.json").read_bytes() == before
    forbidden.assert_not_called()


def test_local_projection_and_watch_show_specialists_after_main_finished(local_game, monkeypatch):
    root, config, _ = local_game
    state = saved_state(local_game)
    state.phase_started_at = datetime(2026, 11, 7, 1, 2, 0, tzinfo=UTC)
    assert state.active_attempt is not None
    state.active_attempt.roles[1].phase_started_at = datetime(2026, 11, 7, 1, 2, 10, tzinfo=UTC)
    write_private_json(root / "native-runtime/controller.json", state.model_dump(mode="json"))
    monkeypatch.setattr(watch_state.time, "time", lambda: datetime(2026, 11, 7, 1, 2, 30, tzinfo=UTC).timestamp())
    host_result = Mock(return_value=(None, None))
    monkeypatch.setattr("civ_agent.orchestration.native_status.host_result", host_result)
    observed = projection(root)
    assert observed.status == state and observed.completion is None
    assert published_game(root).seats[0].civilization == config.agent_players[0].civilization
    assert local_state(root) == state
    snapshot = watch_state.WatchReader(root, owner=True).read()
    assert not snapshot.errors
    flight = snapshot.run["inFlight"]
    assert flight["agentPhase"] == "ROLE_WORK"
    assert flight["phaseStartedAt"] == "2026-11-07T01:02:00+00:00"
    assert [(r["purpose"], r["phase"]) for r in flight["activeRoles"]] == [
        ("gameplay", "ROLE_FINISHED"),
        ("economy", "RUNNING"),
        ("military", "WAITING_PROVIDER"),
    ]
    specialists = {view.role: view for view in snapshot.specialists if view.civilization == state.active_civilization}
    assert specialists["economy"].status == "Running T2"
    economy_job = specialists["economy"].job
    assert economy_job is not None and economy_job["telemetry"]["phaseElapsedMs"] == 20000
    assert specialists["military"].status == "Waiting provider T2"
    assert snapshot.run["status"] == "running"


@pytest.mark.parametrize("value", [None, "", "unavailable", "2026-11-07T01:02:00"])
def test_phase_elapsed_requires_an_explicit_aware_transition_time(value):
    assert watch_state.phase_elapsed_ms(value, now=100) is None


def test_request_counters_separate_communication_and_deduplicate_held_jobs():
    main = {"id": "main", "civilization": "Rome", "purpose": "gameplay", "telemetry": {"modelRequests": 4}}
    jobs = [
        main,
        main,
        {**main, "id": "economy", "purpose": "economy", "telemetry": {"modelRequests": 3}},
        {**main, "id": "chat", "purpose": "communication", "telemetry": {"modelRequests": 2}},
        {**main, "id": "review", "purpose": "review", "telemetry": {"modelRequests": 5}},
        {**main, "id": "unreported", "telemetry": None},
    ]
    counts = watch_state.request_counts(jobs)
    assert counts == {
        "gameplay": 7,
        "review": 5,
        "communication": 2,
        "players": {"Rome": {"gameplay": 7, "review": 5, "communication": 2}},
        "missingJobs": 1,
    }


def test_fresh_manifest_and_status_have_no_workflow_authority(local_game, monkeypatch, capsys):
    root, _, record = local_game
    state = saved_state(local_game)
    require_local_runtime(record)
    assert record["format"] == 2 and record["runtime"] == LOCAL_RUNTIME
    monkeypatch.setattr("civ_agent.orchestration.native_status.host_result", lambda *_args, **_kwargs: (None, None))
    assert game.run(game.parser(action="status").parse_args(["--state-dir", str(root)])) == 0
    status = json.loads(capsys.readouterr().out)
    assert state.active_attempt is not None
    assert status["runtime_status"]["active_attempt"]["id"] == state.active_attempt.id
    assert status["read_only_archive"] is False
    assert not any("workflow" in key or "recovery" in key for key in status)


def test_archived_dashboard_uses_installed_reader_without_starting_an_old_image(local_game, monkeypatch):
    root, _, record = local_game
    write_private_json(root / "container.json", {**record, "format": 1})
    viewer = Mock()
    launch = Mock(side_effect=AssertionError("Archive must not start its retired image"))
    monkeypatch.setattr(match_watch, "MatchWatch", viewer)
    monkeypatch.setattr("subprocess.call", launch)
    monkeypatch.setattr(match_watch.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(match_watch.sys.stdout, "isatty", lambda: True)
    assert match_watch.run(match_watch.parser().parse_args(["--state-dir", str(root)])) == 0
    viewer.return_value.run.assert_called_once_with()
    launch.assert_not_called()


@pytest.mark.parametrize(
    "mode,reason,expected",
    [
        ("FAILED", "ORIGINAL_APPLICATION_FAILURE", "failed"),
        ("STOPPED", "MODEL_INVOCATION_LIMIT", "budget_exhausted"),
        ("STOPPED", "MODEL_REQUEST_LIMIT", "budget_exhausted"),
        ("STOPPED", "TURN_TIMEOUT", "budget_exhausted"),
        ("PAUSED", "OPERATOR_STOP", "paused"),
        ("FINISHED", None, "complete"),
    ],
)
def test_container_entrypoint_preserves_handled_terminal_results(local_game, monkeypatch, mode, reason, expected):
    root, _, record = local_game
    record["launch"] = "a" * 12
    write_private_json(root / "container.json", record)
    write_private_json(root / "control/start.json", {"launch": record["launch"]})
    write_private_json(root / "endpoints.json", {"caFile": str(root / "ca.pem")})
    provenance = root / "provenance.json"
    write_private_json(provenance, {"source": {"revision": "host"}, "engine": {"revision": "engine"}})

    class Native:
        def __init__(self, _arguments, *, stdout, **_):
            stdout.write("MCP-READY: synthetic\n")
            stdout.flush()

        def poll(self):
            return None

    def ready(predicate, _process, _timeout):
        assert predicate()

    operation = AsyncMock(return_value=SimpleNamespace(mode=mode, reason=reason))
    stopped = Mock()
    monkeypatch.setattr("civ_agent.runtime.controller.run_controller", operation)
    monkeypatch.setattr(container_runtime, "ROOT", root)
    monkeypatch.setattr(container_runtime, "PROVENANCE", provenance)
    monkeypatch.setattr(container_runtime, "prepare", lambda _: root / "tls.json")
    monkeypatch.setattr(container_runtime, "wait_for", ready)
    monkeypatch.setattr(container_runtime.subprocess, "Popen", Native)
    monkeypatch.setattr(container_runtime, "stop", stopped)
    assert container_runtime.controller() == 0
    terminal = read_json(root / "runtime.json")
    assert terminal == {"phase": "stopped", "launch": record["launch"], "runner_status": expected, "reason": reason}
    operation.assert_awaited_once_with(root, resume=False, host_url="https://game:8765", host_ca=root / "ca.pem")
    stopped.assert_called_once()
