"""Direct local launches, durable settings, and process cleanup."""

import json
import signal
import subprocess
import sys
import time

import pytest

from civ_agent import cli
from civ_agent.commands import agent_server, game, host
from civ_agent.monitoring import MonitorStore, path_for, read_record
from civ_agent.tls import write_private_json


def options(root, *flags, action="start"):
    return game.parser(action=action).parse_args(["--state-dir", str(root), *flags])


def saved_run(root, **changes):
    write_private_json(root / "exploration.json", {"gameJson": "{}"})
    write_private_json(
        root / "players.json", {"players": [{"civilizationId": civ} for civ in ("Rome", "Greece", "France")]}
    )
    state = {
        "version": game.scheduler.RUN_STATE_VERSION,
        "model": "gpt-6-luna",
        "reasoningEffort": "high",
        "startedAt": time.time(),
        "limits": {
            "maxTurns": 3,
            "maxInvocations": 36,
            "maxSeconds": 3600,
            "activationSeconds": 600,
        },
        "status": "paused",
        "usage": {"input_tokens": 12345},
        **changes,
    }
    MonitorStore(path_for(root)).put("state", "run", state)
    return state


def test_resume_requires_a_native_save_before_launching(tmp_path, fake_services, capsys):
    assert game.run(options(tmp_path, action="resume")) == 2
    assert "Resume requires exploration.json" in capsys.readouterr().err
    assert fake_services[0] == []


def test_human_launch_has_no_human_agent_service(tmp_path, fake_services):
    assert game.run(options(tmp_path, "--human", "France", "--agents", "Rome", "Greece")) == 3
    processes, scheduled, _ = fake_services
    assert len(processes) == 3
    assert ["--human", "France"] == processes[0].command[processes[0].command.index("--human") :][:2]
    assert [process.command[2] for process in processes[1:]] == ["Rome", "Greece"]
    assert scheduled[0].human == "France"


def test_human_roster_is_disjoint_and_resume_restores_roles(tmp_path):
    with pytest.raises(ValueError, match="distinct"):
        game.resolve_options(options(tmp_path, "--human", "France"))
    saved_run(tmp_path)
    write_private_json(
        tmp_path / "players.json",
        {
            "players": [
                {"civilizationId": "France", "controller": "HUMAN"},
                {"civilizationId": "Rome", "controller": "AGENT"},
            ]
        },
    )
    args = options(tmp_path, action="resume")
    game.resolve_options(args)
    assert args.human == "France" and args.agents == ["Rome"]


@pytest.fixture
def fake_services(monkeypatch):
    processes, scheduled, closed = [], [], []

    class Process:
        def __init__(self, command, *, stdout, stderr, start_new_session):
            assert start_new_session
            assert command[:4] == [sys.executable, "-u", "-m", "civ_agent"]
            self.command = command[4:]
            self.returncode: int | None = None
            stdout.write("MCP-READY:local\n" if self.command[0] == "host" else "AGENT-READY:local\n")
            stdout.flush()
            processes.append(self)

        def poll(self):
            return self.returncode

        def terminate(self):
            self.returncode = 0
            closed.append(self)

        def wait(self, timeout=None):
            return self.returncode

    def schedule(args):
        scheduled.append(args)
        saved_run(args.state_dir, model=args.model, reasoningEffort=args.reasoning_effort, status="budget_exhausted")
        return 0

    monkeypatch.setattr(game.subprocess, "Popen", Process)
    monkeypatch.setattr(game.scheduler, "run", schedule)
    monkeypatch.setattr(game, "provenance", lambda _: None)
    return processes, scheduled, closed


@pytest.mark.parametrize(
    "flags, enabled, fast",
    [
        ([], False, False),
        (["--strategy-guide", "--fast"], True, True),
        (["--no-strategy-guide", "--no-fast"], False, False),
    ],
)
def test_direct_launch_and_shutdown_order(tmp_path, fake_services, flags, enabled, fast):
    assert (
        cli.main(
            [
                "game",
                "start",
                "--state-dir",
                str(tmp_path),
                "--model",
                "chosen-model",
                "--reasoning-effort",
                "max",
                *flags,
            ]
        )
        == 3
    )
    processes, scheduled, closed = fake_services
    assert len(processes) == 4 and closed == processes[::-1]
    assert len(scheduled) == 1
    assert (scheduled[0].model, scheduled[0].reasoning_effort) == ("chosen-model", "max")
    assert scheduled[0].max_turns == 331 and scheduled[0].turn_timeout == 3600
    agents = [agent_server.parser().parse_args(p.command[2:]) for p in processes[1:]]
    assert [a.civilization for a in agents] == ["Rome", "Greece", "France"]
    assert all(
        (a.model, a.reasoning_effort, a.strategy_guide, a.fast) == ("chosen-model", "max", enabled, fast)
        for a in agents
    )
    assert all((a.review_interval, a.review_attempts) == (5, 3) for a in agents)


@pytest.mark.parametrize(
    "flags, expected, scheduler_settings",
    [
        (
            ["--agent-model", "Rome=rome-model"],
            [("rome-model", "high"), ("default-model", "high"), ("default-model", "high")],
            (None, "high"),
        ),
        (
            ["--agent-reasoning-effort", "Greece=max"],
            [("default-model", "high"), ("default-model", "max"), ("default-model", "high")],
            ("default-model", None),
        ),
        (
            [
                "--agent-model",
                "Rome=rome-model",
                "--agent-model",
                "Greece=greek-model",
                "--agent-reasoning-effort",
                "Greece=max",
                "--agent-reasoning-effort",
                "France=medium",
            ],
            [("rome-model", "high"), ("greek-model", "max"), ("default-model", "medium")],
            (None, None),
        ),
    ],
)
def test_per_civ_settings_reach_services_output_and_metadata(
    tmp_path, fake_services, capsys, flags, expected, scheduler_settings
):
    assert (
        cli.main(
            [
                "game",
                "start",
                "--state-dir",
                str(tmp_path),
                "--color",
                "never",
                "--model",
                "default-model",
                "--reasoning-effort",
                "high",
                *flags,
            ]
        )
        == 3
    )
    processes, scheduled, _ = fake_services
    agents = [agent_server.parser().parse_args(p.command[2:]) for p in processes[1:]]
    assert [(a.model, a.reasoning_effort) for a in agents] == expected
    # Non-null scheduler settings override services, including opening reviews.
    assert (scheduled[0].model, scheduled[0].reasoning_effort) == scheduler_settings
    metadata = json.loads(next((tmp_path / "launches").glob("*/launch.json")).read_text())
    assert metadata["agentSettings"] == {
        civ: {"model": model, "reasoningEffort": effort}
        for civ, (model, effort) in zip(("Rome", "Greece", "France"), expected, strict=True)
    }
    output = capsys.readouterr().out
    for civ, (model, effort) in zip(("Rome", "Greece", "France"), expected, strict=True):
        assert f"[{civ}] Player: {model} · {effort} reasoning" in output


@pytest.mark.parametrize("scheduler_started", [True, False])
@pytest.mark.parametrize("repeat_flags", [[], ["--agent-model", "Rome=rome-model"]])
def test_resume_restores_all_per_civ_settings(tmp_path, fake_services, scheduler_started, repeat_flags):
    assert (
        game.run(
            options(
                tmp_path,
                "--model",
                "default-model",
                "--reasoning-effort",
                "high",
                "--agent-model",
                "Rome=rome-model",
                "--agent-model",
                "Greece=greek-model",
                "--agent-reasoning-effort",
                "Greece=max",
            )
        )
        == 3
    )
    saved = json.loads((tmp_path / "launch-settings.json").read_text())
    assert saved["agent_models"] == {"Rome": "rome-model", "Greece": "greek-model"}
    assert saved["agent_reasoning_efforts"] == {"Greece": "max"}
    if not scheduler_started:
        # A saved board and launcher settings suffice after initial startup fails.
        path_for(tmp_path).unlink()
    resumed = options(tmp_path, *repeat_flags, action="resume")
    game.resolve_options(resumed)
    assert (resumed.model, resumed.reasoning_effort) == ("default-model", "high")
    assert resumed.scheduler_action == ("resume" if scheduler_started else "start")
    assert game.run_services(resumed, game.TerminalOutput(color="never")) == 0
    processes, scheduled, _ = fake_services
    first = [agent_server.parser().parse_args(p.command[2:]) for p in processes[1:4]]
    later = [agent_server.parser().parse_args(p.command[2:]) for p in processes[5:8]]
    assert [(a.model, a.reasoning_effort) for a in later] == [(a.model, a.reasoning_effort) for a in first]
    assert (scheduled[1].model, scheduled[1].reasoning_effort) == (None, None)


@pytest.mark.parametrize(
    "flags",
    [
        ["--model", "changed-model"],
        ["--reasoning-effort", "low"],
        ["--agent-model", "Rome=changed-model"],
        ["--agent-model", "France=changed-model"],
        ["--agent-reasoning-effort", "Greece=low"],
        ["--agent-reasoning-effort", "France=low"],
    ],
)
def test_resume_rejects_changed_per_civ_settings_before_starting(tmp_path, fake_services, capsys, flags):
    assert (
        game.run(options(tmp_path, "--agent-model", "Rome=rome-model", "--agent-reasoning-effort", "Greece=max")) == 3
    )
    settings = (tmp_path / "launch-settings.json").read_bytes()
    state = read_record(path_for(tmp_path), "state", "run")
    assert game.run(options(tmp_path, *flags, action="resume")) == 2
    assert "Resume retains" in capsys.readouterr().err
    assert len(fake_services[0]) == 4 and len(fake_services[1]) == 1
    assert (tmp_path / "launch-settings.json").read_bytes() == settings
    assert read_record(path_for(tmp_path), "state", "run") == state


@pytest.mark.parametrize("flag", ["--agent-model", "--agent-reasoning-effort"])
@pytest.mark.parametrize("value", ["Rome", "=value", " =value", "Rome=", "Rome=two words"])
def test_per_civ_settings_require_civ_and_valid_value(tmp_path, flag, value):
    with pytest.raises(SystemExit):
        options(tmp_path, flag, value)


@pytest.mark.parametrize("flag", ["--agent-model", "--agent-reasoning-effort"])
def test_duplicate_per_civ_settings_are_rejected(tmp_path, flag):
    with pytest.raises(SystemExit):
        options(tmp_path, flag, "Rome=first", flag, "Rome=second")


@pytest.mark.parametrize("civilization", ["Egypt", "Atlantis", "rome"])
@pytest.mark.parametrize("flag", ["--agent-model", "--agent-reasoning-effort"])
def test_overrides_require_an_agent_in_the_roster(tmp_path, fake_services, capsys, civilization, flag):
    assert game.run(options(tmp_path, flag, f"{civilization}=value")) == 2
    assert "requires an agent civilization" in capsys.readouterr().err
    assert fake_services == ([], [], [])


def test_overrides_support_custom_agent_rosters(tmp_path, fake_services):
    assert game.run(options(tmp_path, "--agents", "Rome", "Germany", "--agent-model", "Germany=german-model")) == 3
    agents = [agent_server.parser().parse_args(p.command[2:]) for p in fake_services[0][1:]]
    assert [(a.civilization, a.model, a.reasoning_effort) for a in agents] == [
        ("Rome", "gpt-6-luna", "medium"),
        ("Germany", "german-model", "medium"),
    ]


def test_review_cadence_and_attempt_limit_are_forwarded_and_retained(tmp_path, fake_services):
    assert game.run(options(tmp_path, "--review-interval", "7", "--review-attempts", "2")) == 3
    agents = [agent_server.parser().parse_args(p.command[2:]) for p in fake_services[0][1:]]
    assert all((a.review_interval, a.review_attempts) == (7, 2) for a in agents)
    resume = options(tmp_path, action="resume")
    game.resolve_options(resume)
    assert (resume.review_interval, resume.review_attempts) == (7, 2)


@pytest.mark.parametrize("flag", ["--review-interval", "--review-attempts"])
@pytest.mark.parametrize("value", ["0", "-1"])
def test_review_options_require_positive_integers(tmp_path, flag, value):
    with pytest.raises(SystemExit):
        options(tmp_path, flag, value)
    with pytest.raises(SystemExit):
        agent_server.parser().parse_args(["Rome", flag, value])


def test_map_and_roster_options_reach_native_launcher(tmp_path, fake_services):
    args = options(
        tmp_path,
        "--agents",
        "Rome",
        "Germany",
        "--built-in-ai",
        "Egypt",
        "China",
        "--map-radius",
        "26",
        "--city-states",
        "8",
        "--seed",
        "-42",
    )
    assert game.run(args) == 3
    processes, _, _ = fake_services
    native = host.parser().parse_args(processes[0].command[2:])
    assert native.agent_player == ["Rome", "Germany"] and native.built_in_ai == ["Egypt", "China"]
    assert (native.map_radius, native.map_size, native.city_states, native.seed) == (26, None, 8, -42)
    assert len(processes) == 3


def test_saved_position_comparison_uses_shared_services(tmp_path, fake_services, monkeypatch):
    from tests import live_saved_position

    source, target = tmp_path / "source", tmp_path / "comparison"
    saved_run(source)
    write_private_json(
        source / "exploration.json",
        {
            "gameJson": json.dumps(
                {
                    "civilizations": [{"civID": civ, "playerType": "Human"} for civ in ("Rome", "Greece", "France")]
                    + [{"civID": "Egypt"}]
                }
            ),
            "communication": {"jobs": {}},
            "handoff": 0,
        },
    )
    write_private_json(source / "operator.json", {})
    monkeypatch.setattr(live_saved_position.subprocess, "check_output", lambda *args, **kwargs: "revision\n")
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "live_saved_position",
            "--checkpoint",
            str(source / "exploration.json"),
            "--source-state",
            str(source),
            "--state-dir",
            str(target),
            "--port",
            "9000",
            "--model",
            "chosen-model",
            "--reasoning-effort",
            "max",
        ],
    )
    assert live_saved_position.main() == 0
    processes, scheduled, closed = fake_services
    assert len(processes) == 4 and closed == processes[::-1] and scheduled[0].model == "chosen-model"
    metadata = game.read_json(target / "live-run.json")
    assert metadata is not None and metadata["model"] == "chosen-model"


def test_resume_retains_runtime_settings_and_accounting(tmp_path, fake_services):
    previous = saved_run(tmp_path)
    write_private_json(
        tmp_path / "launch-settings.json", {"port": 19000, "fast": True, "strategy_guide": True, "startup_timeout": 800}
    )
    args = options(tmp_path, action="resume")
    game.resolve_options(args)
    assert (args.port, args.fast, args.strategy_guide, args.startup_timeout) == (19000, True, True, 800)
    assert (args.model, args.reasoning_effort) == ("gpt-6-luna", "high")
    assert args.max_turns == 3 and args.turn_timeout == 600
    assert read_record(path_for(tmp_path), "state", "run") == previous
    assert game.run(args) == 3
    processes, scheduled, _ = fake_services
    assert "--new" not in processes[0].command and "--seed" not in processes[0].command
    assert scheduled[0].action == "resume"
    assert all("--fast" in p.command and "--strategy-guide" in p.command for p in processes[1:])


def test_resume_rejects_older_recovery_state_before_starting_services(tmp_path, fake_services):
    previous = saved_run(tmp_path, version=2)
    with pytest.raises(game.scheduler.RunnerError, match="use the old code"):
        game.resolve_options(options(tmp_path, action="resume"))
    assert read_record(path_for(tmp_path), "state", "run") == previous
    assert fake_services[0] == []


def test_resume_infers_host_port_and_can_disable_options(tmp_path):
    saved_run(tmp_path)
    write_private_json(tmp_path / "endpoints.json", {"hostUrl": "https://localhost:10971"})
    write_private_json(tmp_path / "launch-settings.json", {"fast": True, "strategy_guide": True})
    args = options(tmp_path, "--no-fast", "--no-strategy-guide", action="resume")
    game.resolve_options(args)
    assert args.port == 10971 and not args.fast and not args.strategy_guide


@pytest.mark.parametrize(
    "flags, message",
    [
        (["--max-seconds", "100"], "cannot decrease"),
        (["--model", "other-model"], "Resume retains"),
        (["--turn-timeout", "86401"], "cannot exceed"),
    ],
)
def test_invalid_resume_preserves_state(tmp_path, fake_services, flags, message, capsys):
    before = saved_run(tmp_path)
    assert game.run(options(tmp_path, *flags, action="resume")) == 2
    assert message in capsys.readouterr().err
    assert fake_services == ([], [], [])
    assert read_record(path_for(tmp_path), "state", "run") == before


def test_expired_budget_and_overwrite_protection(tmp_path):
    saved_run(tmp_path, startedAt=time.time() - 4000)
    with pytest.raises(ValueError, match="already exists"):
        game.resolve_options(options(tmp_path))
    with pytest.raises(ValueError, match="wall-time budget has expired"):
        game.resolve_options(options(tmp_path, action="resume"))
    extended = options(tmp_path, "--max-seconds", "8000", action="resume")
    game.resolve_options(extended)
    assert extended.max_seconds == 8000


def test_resume_after_initial_service_failure(tmp_path):
    saved_run(tmp_path)
    path_for(tmp_path).unlink()
    args = options(tmp_path, action="resume")
    game.resolve_options(args)
    assert args.scheduler_action == "start" and args.action == "resume"


def test_startup_failure_cleans_up_and_records_the_actual_cause(tmp_path, fake_services, monkeypatch):
    original = game.subprocess.Popen

    def failing(command, **kwargs):
        process = original(command, **kwargs)
        if command[4:7] == ["agent-server", "start", "Greece"]:
            process.returncode = 42
        return process

    monkeypatch.setattr(game.subprocess, "Popen", failing)
    assert game.run(options(tmp_path)) == 2
    processes, scheduled, closed = fake_services
    assert scheduled == [] and closed == processes[:2][::-1]
    report = next((tmp_path / "launches").glob("*/stop.json"))
    stopped = game.read_json(report)
    assert stopped is not None and "Greece exited with 42" in stopped["reason"]
    # A failure before the first board is saved can be retried without deleting logs.
    game.resolve_options(options(tmp_path))


@pytest.mark.parametrize("flags", [["--agents", "Rome", "Rome"], ["--built-in-ai", "Rome"], ["--port", "65534"]])
def test_invalid_roster_or_port_fails_before_services(tmp_path, fake_services, flags):
    assert game.run(options(tmp_path, *flags)) == 2
    assert fake_services == ([], [], [])


@pytest.mark.parametrize(
    "status, result, code, interrupted, expected",
    [
        ("complete", {"winner": "Egypt"}, 0, False, 0),
        ("complete", None, 0, False, 2),
        ("paused", None, 0, False, 2),
        ("needs_inspection", None, 0, False, 2),
        ("budget_exhausted", None, 0, False, 3),
        ("paused", None, 0, True, 130),
        ("paused", None, 7, False, 7),
    ],
)
def test_exit_status_requires_native_completion(tmp_path, status, result, code, interrupted, expected):
    saved_run(tmp_path, status=status, reason="Saved reason")
    MonitorStore(path_for(tmp_path)).put(
        "state", "result", {"status": "completed" if result else "incomplete", "result": result}
    )
    summary = game.stopped_state(tmp_path, code, interrupted)
    assert summary["exitCode"] == expected and summary["reason"] == "Saved reason"


def test_colored_output_plain_log_and_stop_report(tmp_path, monkeypatch, capsys):
    def schedule(args, _output):
        saved_run(args.state_dir, reason="Turn did not hand off")
        print("\x1b[36mstdout diagnostic\x1b[0m")
        print("stderr diagnostic", file=sys.stderr)
        return 0

    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.setattr(game, "run_services", schedule)
    assert game.run(options(tmp_path, "--color", "always", "--fast", "--strategy-guide")) == 2
    output = capsys.readouterr().out
    assert "\x1b[" in output and "stdout diagnostic" in output and "stderr diagnostic" in output
    launch = next((tmp_path / "launches").iterdir())
    assert "\x1b" not in (launch / "console.log").read_text()
    stop = game.read_json(launch / "stop.json")
    assert stop is not None and stop["reason"] == "Turn did not hand off"
    assert (launch / "stop.json").stat().st_mode & 0o777 == 0o600
    metadata = game.read_json(launch / "launch.json")
    assert metadata is not None and metadata["requestedServiceTier"] == "fast"


def test_term_during_startup_flushes_service_and_records_pause(tmp_path):
    worker = tmp_path / "worker.py"
    worker.write_text(
        "import signal, time, sys\nfrom pathlib import Path\n"
        "def stop(*args):\n Path(sys.argv[1]).write_text('flushed'); sys.exit(0)\n"
        "signal.signal(signal.SIGTERM, stop)\nprint('MCP-READY:local', flush=True)\n"
        "while True: time.sleep(.01)\n"
    )
    driver = tmp_path / "driver.py"
    driver.write_text(
        "import time, sys, subprocess\nfrom pathlib import Path\n"
        "from civ_agent.commands import game\nreal = subprocess.Popen\n"
        "game.subprocess.Popen = lambda command, **kw: real([sys.executable, sys.argv[1], sys.argv[2]], **kw)\n"
        "game.provenance = lambda path: None\n"
        "def play(args, output):\n"
        " with game.services(args.state_dir, 5, output.line) as start:\n"
        "  start([], 'host', 'MCP-READY:')\n"
        "  Path(sys.argv[3]).write_text('ready')\n"
        "  while True: time.sleep(.01)\n"
        "game.run_services = play\n"
        "raise SystemExit(game.run(game.parser().parse_args(['--state-dir', sys.argv[4]])))\n"
    )
    ready, flushed, root = tmp_path / "ready", tmp_path / "flushed", tmp_path / "match"
    process = subprocess.Popen(
        [sys.executable, str(driver), str(worker), str(flushed), str(ready), str(root)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        deadline = time.monotonic() + 10
        while not ready.exists() and process.poll() is None and time.monotonic() < deadline:
            time.sleep(0.02)
        assert ready.exists()
        process.send_signal(signal.SIGTERM)
        output, errors = process.communicate(timeout=10)
        assert process.returncode == 130, output + errors
        assert flushed.read_text() == "flushed"
        report = next((root / "launches").glob("*/stop.json"))
        stopped = game.read_json(report)
        assert stopped is not None and stopped["interrupted"] is True
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)
