"""Exercise the public entrypoint, including checkout-independent invocation."""

import os
import subprocess
import sys
from importlib.metadata import distribution
from pathlib import Path
from types import ModuleType
from unittest.mock import Mock

import pytest

from civ_agent import cli, scheduler
from civ_agent.commands import agent, agent_server, game, history, host, inspect, profile

COMMANDS = [
    ("game start", game),
    ("game resume", game),
    ("host start", host),
    ("host inspect", inspect),
    ("agent start", agent),
    ("agent-server start", agent_server),
    *((f"match {action}", scheduler) for action in ("start", "resume", "status", "pause", "stop", "manual")),
    ("history export", history),
    ("match profile", profile),
]


@pytest.mark.parametrize(("command", "handler"), COMMANDS)
def test_help_never_runs_a_handler(
    command: str, handler: ModuleType, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    run = Mock()
    monkeypatch.setattr(handler, "run", run)
    with pytest.raises(SystemExit) as error:
        cli.main([*command.split(), "--help"])
    assert error.value.code == 0
    assert f"usage: civ-agent {command}" in capsys.readouterr().out
    run.assert_not_called()


@pytest.mark.parametrize(("command", "handler"), COMMANDS)
def test_dispatch_preserves_options_and_exit_status(
    command: str, handler: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run = Mock(return_value=7)
    monkeypatch.setattr(handler, "run", run)
    args = command.split()
    if command.startswith(("agent ", "agent-server ")):
        args.append("Rome")
    args.extend(["--state-dir", str(tmp_path / "match with spaces")])
    settings = command != "match profile" and command.startswith(("agent ", "agent-server ", "match "))
    if settings:
        args.extend(["--model", "test-model", "--reasoning-effort", "high"])
    if command == "agent-server start":
        args.extend(["--strategy-guide", "--civilization-guide"])
    assert cli.main(args) == 7
    run.assert_called_once()
    options = run.call_args.args[0]
    if command == "match profile":
        assert options.state_dir == [tmp_path / "match with spaces"]
    else:
        assert Path(options.state_dir) == tmp_path / "match with spaces"
    if command.startswith("match ") and command != "match profile":
        assert options.action == command.split()[1]
    if settings:
        assert (options.model, options.reasoning_effort) == ("test-model", "high")
    if command == "agent-server start":
        assert options.strategy_guide
        assert options.civilization_guide
    assert not (tmp_path / "match with spaces").exists()


@pytest.mark.parametrize("args", [[], ["host"], ["match"], ["agent", "start"], ["host", "stop"], ["run_agents"]])
def test_incomplete_or_invalid_commands_fail_before_creating_state(
    args: list[str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = tmp_path / "match"
    monkeypatch.setenv("UNCIV_HOST_DIR", str(state))
    with pytest.raises(SystemExit) as error:
        cli.main(args)
    assert error.value.code == 2
    assert not state.exists()


def test_installed_entrypoint_and_module_work_outside_checkout(tmp_path: Path) -> None:
    package = distribution("civ-agent")
    entrypoints = [point for point in package.entry_points if point.group == "console_scripts"]
    assert [(point.name, point.value) for point in entrypoints] == [("civ-agent", "civ_agent.cli:main")]
    executable = Path(sys.executable).parent / "civ-agent"
    environment = {key: value for key, value in os.environ.items() if key != "PYTHONPATH"}
    for command in ([str(executable)], [sys.executable, "-I", "-m", "civ_agent"]):
        version = subprocess.run(
            [*command, "--version"],
            cwd=tmp_path,
            env=environment,
            capture_output=True,
            text=True,
            check=True,
        )
        assert version.stdout.strip() == f"civ-agent {package.version}"
        help_output = subprocess.run(
            [*command, "agent-server", "start", "--help"],
            cwd=tmp_path,
            env=environment,
            capture_output=True,
            text=True,
            check=True,
        )
        assert "--reasoning-effort" in help_output.stdout
        assert "--strategy-guide" in help_output.stdout and "--no-strategy-guide" in help_output.stdout
        assert "--civilization-guide" in help_output.stdout and "--no-civilization-guide" in help_output.stdout
    assert list(tmp_path.iterdir()) == []
