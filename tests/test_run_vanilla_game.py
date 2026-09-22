import json
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from scripts import run_vanilla_game as launcher
from tests import live_vanilla


def saved_run(root, **changes):
    (root / "runner").mkdir(parents=True, exist_ok=True)
    checkpoint = {
        "gameJson": json.dumps(
            {
                "civilizations": [{"civID": civ, "playerType": "Human"} for civ in live_vanilla.PLAYERS]
                + [{"civID": "Egypt"}]
            }
        ),
        "communication": {"jobs": {}},
        "handoff": 0,
    }
    (root / "exploration.json").write_text(json.dumps(checkpoint))
    state = {
        "model": "gpt-5.6-luna",
        "reasoningEffort": "medium",
        "startedAt": time.time(),
        "limits": {"maxTurns": 3, "maxInvocations": 36, "maxSeconds": 3600, "activationSeconds": 600},
        "status": "paused",
        "usage": {"input_tokens": 12345},
        **changes,
    }
    (root / "runner/run.json").write_text(json.dumps(state))
    return state


def test_full_game_defaults_and_existing_match_protection(tmp_path):
    args = launcher.arguments(["--state-dir", str(tmp_path)])
    command = launcher.launch_command(args, Path(sys.executable))
    for name, expected in {
        "max-turns": "331",
        "max-invocations": "10000",
        "max-seconds": "604800",
        "turn-timeout": "3600",
        "startup-timeout": "900",
        "model": "gpt-6-luna",
        "reasoning-effort": "medium",
    }.items():
        assert command[command.index("--" + name) + 1] == expected
    saved = saved_run(tmp_path)
    with pytest.raises(ValueError, match="already exists"):
        launcher.launch_command(args, Path(sys.executable))
    assert launcher.read_json(tmp_path / "runner/run.json") == saved


@pytest.fixture
def fake_services(monkeypatch):
    processes, scheduled = [], []

    class Process:
        def __init__(self, command, *, stdout, stderr):
            self.command, self.stopped = command, False
            stdout.write("MCP-READY:local\n" if command[1] == "host" else "AGENT-READY:local\n")
            stdout.flush()
            processes.append(self)

        def poll(self):
            return 0 if self.stopped else None

        def terminate(self):
            self.stopped = True

        def wait(self, timeout):
            return 0

    def schedule(args):
        scheduled.append(args)
        return 0

    monkeypatch.setattr(live_vanilla.subprocess, "Popen", Process)
    monkeypatch.setattr(live_vanilla.scheduler, "run", schedule)
    return processes, scheduled


def assert_agent_settings(fake_services, expected):
    from civ_agent.commands import agent_server

    processes, scheduled = fake_services
    assert [(args.model, args.reasoning_effort) for args in scheduled] == [expected]
    assert len(processes) == 4 and all(process.stopped for process in processes)
    agents = [agent_server.parser().parse_args(process.command[3:]) for process in processes[1:]]
    assert [args.civilization for args in agents] == ["Rome", "Greece", "France"]
    assert all((args.model, args.reasoning_effort) == expected for args in agents)
    return agents


@pytest.mark.parametrize(
    "flags, enabled", [([], False), (["--strategy-guide"], True), (["--no-strategy-guide"], False)]
)
@pytest.mark.parametrize("fast", [False, True])
@pytest.mark.parametrize(
    "settings, expected",
    [
        ([], ("gpt-6-luna", "medium")),
        (["--model", "gpt-6-luna", "--reasoning-effort", "high"], ("gpt-6-luna", "high")),
        (["--model", "chosen-model", "--reasoning-effort", "custom-effort"], ("chosen-model", "custom-effort")),
    ],
)
def test_launcher_options_reach_all_agent_services(
    tmp_path, monkeypatch, fake_services, flags, enabled, fast, settings, expected
):
    command = launcher.launch_command(
        launcher.arguments(["--state-dir", str(tmp_path), *flags, *settings, *(["--fast"] if fast else [])]),
        Path(sys.executable),
    )
    monkeypatch.setattr(sys, "argv", ["live_vanilla", *command[4:]])
    assert live_vanilla.main() == 0
    agents = assert_agent_settings(fake_services, expected)
    assert all(args.strategy_guide == enabled for args in agents)
    assert all(args.fast == fast for args in agents)


@pytest.mark.parametrize("via_launcher", [False, True])
def test_resume_inherits_saved_settings_before_starting_services(tmp_path, monkeypatch, fake_services, via_launcher):
    saved = saved_run(tmp_path, reasoningEffort="high")
    arguments = ["--state-dir", str(tmp_path), "--resume", "--port", "8971"]
    if via_launcher:
        args = launcher.arguments(arguments)
        arguments = launcher.launch_command(args, Path(sys.executable))[4:]
        assert (args.model, args.reasoning_effort) == ("gpt-5.6-luna", "high")
    monkeypatch.setattr(sys, "argv", ["live_vanilla", *arguments])
    assert live_vanilla.main() == 0
    assert_agent_settings(fake_services, ("gpt-5.6-luna", "high"))
    assert launcher.read_json(tmp_path / "runner/run.json") == saved


def test_checkpoint_run_records_and_uses_selected_settings(tmp_path, monkeypatch, fake_services):
    source, target = tmp_path / "source", tmp_path / "target"
    saved_run(source)
    for name in ("players.json", "operator.json"):
        (source / name).write_text("{}")
    monkeypatch.setattr(
        live_vanilla.subprocess,
        "check_output",
        lambda command, **kwargs: "" if "--porcelain" in command else "revision\n",
    )
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "live_vanilla",
            "--state-dir",
            str(target),
            "--port",
            "8971",
            "--checkpoint",
            str(source / "exploration.json"),
            "--source-state",
            str(source),
            "--model",
            "chosen-model",
            "--reasoning-effort",
            "high",
        ],
    )
    assert live_vanilla.main() == 0
    assert_agent_settings(fake_services, ("chosen-model", "high"))
    provenance = launcher.read_json(target / "live-run.json")
    assert provenance is not None and (provenance["model"], provenance["reasoningEffort"]) == ("chosen-model", "high")


def test_resume_preserves_larger_limits_usage_and_original_clock(tmp_path):
    saved = saved_run(
        tmp_path, limits={"maxTurns": 500, "maxInvocations": 20000, "maxSeconds": 900000, "activationSeconds": 4000}
    )
    args = launcher.arguments(["--state-dir", str(tmp_path), "--resume", "--fast"])
    command = launcher.launch_command(args, Path(sys.executable))
    assert "--resume" in command and "--fast" in command and "--seed" not in command
    for option, (key, _) in launcher.LIMITS.items():
        assert command[command.index("--" + option.replace("_", "-")) + 1] == str(saved["limits"][key])
    assert launcher.read_json(tmp_path / "runner/run.json") == saved
    args.max_seconds = 100
    with pytest.raises(ValueError, match="cannot decrease"):
        launcher.launch_command(args, Path(sys.executable))
    args.max_seconds = None
    saved_run(tmp_path, startedAt=time.time() - 604801)
    with pytest.raises(ValueError, match="original wall-time budget has expired"):
        launcher.launch_command(args, Path(sys.executable))


def test_resume_requires_saved_state(tmp_path):
    args = launcher.arguments(["--state-dir", str(tmp_path), "--resume"])
    with pytest.raises(ValueError, match="requires exploration.json"):
        launcher.launch_command(args, Path(sys.executable))


@pytest.mark.parametrize("settings", [["--model", "gpt-6-luna"], ["--reasoning-effort", "medium"]])
def test_resume_rejects_conflicting_settings_before_starting_services(tmp_path, monkeypatch, fake_services, settings):
    saved = saved_run(tmp_path, reasoningEffort="high")
    arguments = ["--state-dir", str(tmp_path), "--resume", "--port", "8971", *settings]
    args = launcher.arguments(arguments)
    with pytest.raises(ValueError, match="Resume retains the saved model gpt-5.6-luna with high reasoning"):
        launcher.launch_command(args, Path(sys.executable))
    monkeypatch.setattr(sys, "argv", ["live_vanilla", *arguments])
    with pytest.raises(SystemExit) as stopped:
        live_vanilla.main()
    assert stopped.value.code == 2
    assert fake_services == ([], [])
    assert launcher.read_json(tmp_path / "runner/run.json") == saved


@pytest.mark.parametrize("settings", [["--model", ""], ["--reasoning-effort", "with spaces"]])
def test_invalid_model_settings_fail_before_launch(tmp_path, settings):
    args = launcher.arguments(["--state-dir", str(tmp_path), *settings])
    with pytest.raises(ValueError, match="Invalid model or reasoning effort"):
        launcher.launch_command(args, Path(sys.executable))


@pytest.mark.parametrize(
    ("status", "result", "child_code", "interrupted", "expected"),
    [
        ("complete", {"winner": "Egypt", "victoryType": "Time"}, 0, False, 0),
        ("complete", None, 0, False, 2),
        ("paused", None, 0, False, 2),
        ("needs_inspection", None, 0, False, 2),
        ("budget_exhausted", None, 0, False, 3),
        ("paused", None, 0, True, 130),
        ("paused", None, 7, False, 7),
    ],
)
def test_exit_status_distinguishes_blockers_from_native_completion(
    tmp_path, status, result, child_code, interrupted, expected
):
    saved_run(tmp_path, status=status, reason="Saved reason")
    (tmp_path / "telemetry").mkdir()
    launcher.write_json(
        tmp_path / "telemetry/game-result.json", {"status": "completed" if result else "incomplete", "result": result}
    )
    summary = launcher.stopped_state(tmp_path, child_code, interrupted)
    assert summary["exitCode"] == expected
    assert summary["result"] == result and summary["reason"] == "Saved reason"


def test_launcher_captures_output_and_stop_report_without_restarting(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("NO_COLOR", raising=False)
    root = tmp_path / "match with spaces"
    worker = tmp_path / "fake_worker.py"
    worker.write_text(
        "import json, sys\nfrom pathlib import Path\n"
        "root = Path(sys.argv[1]); (root/'runner').mkdir()\n"
        "(root/'runner/run.json').write_text(json.dumps({'status':'paused','reason':'Turn did not hand off'}))\n"
        "print('\\x1b[36mstdout diagnostic\\x1b[0m', flush=True)\n"
        "print('stderr diagnostic', file=sys.stderr, end='', flush=True)\n"
    )
    monkeypatch.setattr(
        launcher, "launch_command", lambda args, python: [str(python), "-u", str(worker), str(args.state_dir)]
    )
    assert (
        launcher.main(
            [
                "--state-dir",
                str(root),
                "--color",
                "always",
                "--strategy-guide",
                "--fast",
                "--model",
                "chosen-model",
                "--reasoning-effort",
                "high",
            ]
        )
        == 2
    )
    output = capsys.readouterr().out
    assert "\x1b[" in output.split("\x1b[36mstdout diagnostic")[0]
    assert "\x1b[36mstdout diagnostic\x1b[0m" in output
    assert "\x1b[" in output.split("stderr diagnostic")[1]
    launches = list((root / "launches").iterdir())
    assert len(launches) == 1
    metadata = launcher.read_json(launches[0] / "launch.json")
    assert metadata is not None and metadata["strategyGuide"]
    assert metadata["requestedServiceTier"] == "fast"
    assert (metadata["model"], metadata["reasoningEffort"]) == ("chosen-model", "high")
    console = (launches[0] / "console.log").read_text()
    assert "stdout diagnostic" in console and "stderr diagnostic" in console
    assert "\x1b" not in console
    assert "[Rome] Player: chosen-model · high reasoning" in console and "[Egypt] Player: Built-in AI" in console
    assert "Priority: Fast requested." in console
    assert "stderr diagnostic\n  Error: Stopped: paused; exit 2." in console
    stopped = launcher.read_json(launches[0] / "stop.json")
    assert stopped is not None
    assert stopped["childExitCode"] == 0 and stopped["exitCode"] == 2
    assert stopped["reason"] == "Turn did not hand off"
    assert (launches[0] / "stop.json").stat().st_mode & 0o777 == 0o600


def test_launcher_resolves_color_before_piping_child_output(tmp_path, monkeypatch):
    from io import StringIO

    class Terminal(StringIO):
        def isatty(self):
            return True

    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.setenv("TERM", "xterm-256color")
    monkeypatch.setattr(sys, "stdout", Terminal())
    args = launcher.arguments(["--state-dir", str(tmp_path)])
    command = launcher.launch_command(args, Path(sys.executable))
    assert command[command.index("--color") + 1] == "always"
    monkeypatch.setenv("NO_COLOR", "1")
    args.color = "always"
    command = launcher.launch_command(args, Path(sys.executable))
    assert command[command.index("--color") + 1] == "never"


def test_term_signal_allows_child_to_flush_and_exit(tmp_path):
    ready = tmp_path / "ready"
    worker = tmp_path / "fake_worker.py"
    worker.write_text(
        "import os, signal, sys, time\nfrom pathlib import Path\n"
        "def stop(*args):\n    print('flushed checkpoint', flush=True)\n    sys.exit(0)\n"
        "signal.signal(signal.SIGINT, stop)\n"
        "Path(sys.argv[1]).write_text(str(os.getpid()))\n"
        "while True: time.sleep(0.1)\n"
    )
    driver = (
        "import json, sys\nfrom pathlib import Path\n"
        "from scripts.run_vanilla_game import run_logged\n"
        "def announce(message, end='\\n'): print(message, end=end, flush=True)\n"
        "result = run_logged([sys.executable, '-u', sys.argv[1], sys.argv[2]], Path.cwd(), announce)\n"
        "print(json.dumps(result), flush=True)\n"
    )
    process = subprocess.Popen(
        [sys.executable, "-u", "-c", driver, str(worker), str(ready)],
        cwd=launcher.REPOSITORY,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        deadline = time.monotonic() + 10
        while not ready.exists() and process.poll() is None and time.monotonic() < deadline:
            time.sleep(0.02)
        assert ready.exists(), "The fake service did not start"
        process.send_signal(signal.SIGTERM)
        output, errors = process.communicate(timeout=10)
        assert process.returncode == 0, errors
        assert "flushed checkpoint" in output
        assert json.loads(output.splitlines()[-1]) == [0, True]
        assert not Path("/proc", ready.read_text()).exists()
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)
