"""Exercise only the native public entrypoint and checkout-independent invocation."""

import os
import subprocess
import sys
from importlib.metadata import distribution
from pathlib import Path
from types import ModuleType
from unittest.mock import Mock

import pytest

from civ_agent import cli, match_watch
from civ_agent.commands import (
    batch,
    chat,
    failures,
    game,
    history,
    host,
    inspect,
    profile,
    runtime,
    trade,
)

COMMANDS = [
    *((f"game {action}", game) for action in ("start", "resume", "stop", "status", "remove")),
    *((f"batch {action}", batch) for action in ("generate", "start", "resume", "stop", "status")),
    *((f"auth {action}", runtime) for action in ("login", "status", "stop", "restart")),
    ("runtime setup", runtime),
    ("image build", runtime),
    ("host start", host),
    ("host inspect", inspect),
    ("history export", history),
    ("match watch", match_watch),
    ("match profile", profile),
    ("match failures", failures),
    ("chat inbox", chat),
    ("trade list", trade),
]


@pytest.mark.parametrize(("command", "handler"), COMMANDS)
def test_native_help_never_runs_a_handler(command: str, handler: ModuleType, monkeypatch, capsys):
    run = Mock()
    monkeypatch.setattr(handler, "run", run)
    with pytest.raises(SystemExit) as error:
        cli.main([*command.split(), "--help"])
    assert error.value.code == 0
    assert f"usage: civ-agent {command}" in capsys.readouterr().out
    run.assert_not_called()


@pytest.mark.parametrize(
    ("command", "handler", "options", "state_is_list"),
    [
        ("game start", game, ["--model", "test-model", "--reasoning-effort", "high"], False),
        ("game resume", game, [], False),
        ("game stop", game, [], False),
        ("game status", game, [], False),
        ("batch status", batch, [], False),
        ("history export", history, ["Rome", "--epoch", "native-epoch"], False),
        ("match watch", match_watch, [], False),
        ("match profile", profile, [], True),
        ("match failures", failures, [], True),
        ("chat inbox", chat, ["Rome"], False),
        ("trade list", trade, ["Rome"], False),
    ],
)
def test_native_dispatch_preserves_options_and_exit_status(
    command, handler, options, state_is_list, tmp_path, monkeypatch
):
    run = Mock(return_value=7)
    monkeypatch.setattr(handler, "run", run)
    state = tmp_path / "match with spaces"
    assert cli.main([*command.split(), "--state-dir", str(state), *options]) == 7
    run.assert_called_once()
    parsed = run.call_args.args[0]
    assert parsed.state_dir == [state] if state_is_list else Path(parsed.state_dir) == state
    if command == "game start":
        assert (parsed.model, parsed.reasoning_effort) == ("test-model", "high")
    if command == "history export":
        assert parsed.civilization == "Rome" and parsed.epoch == "native-epoch"
    assert not state.exists()


@pytest.mark.parametrize(
    "args",
    [
        [],
        ["host"],
        ["match"],
        ["game"],
        ["batch"],
        ["host", "stop"],
        ["run_agents"],
        ["workflow", "worker"],
        ["game", "recover"],
        ["agent", "start"],
        ["agent-server", "start"],
        *[["match", action] for action in ("start", "resume", "status", "pause", "stop", "manual")],
    ],
)
def test_invalid_or_removed_commands_fail_before_creating_state(args, tmp_path, monkeypatch):
    state = tmp_path / "match"
    monkeypatch.setenv("UNCIV_HOST_DIR", str(state))
    with pytest.raises(SystemExit) as error:
        cli.main(args)
    assert error.value.code == 2
    assert not state.exists()


def test_native_cli_help_loads_with_removed_harness_modules_blocked():
    script = """
import importlib.abc
import sys
blocked = {
    'civ_agent.commands.agent', 'civ_agent.commands.agent_server',
    'civ_agent.agent_service', 'civ_agent.scheduler', 'civ_agent.agent_history',
    'civ_agent.runner_history', 'civ_agent.agent_results', 'civ_agent.turn_archive',
    'civ_agent.agent_settings', 'civ_agent.providers', 'civ_agent.provider_gate',
    'civ_agent.provider_usage', 'civ_agent.structured_decisions',
    'civ_agent.chat_runtime', 'civ_agent.economy_runtime', 'civ_agent.review_runtime',
    'civ_agent.peace_review', 'civ_agent.service_recovery', 'civ_agent.interactive_session',
    'civ_agent.llm_metrics', 'temporalio',
    'pydantic_ai.durable_exec.temporal', 'pydantic_ai_harness.temporal',
    'civ_agent.commands.workflow', 'civ_agent.commands.workflow_game',
}
class NativeOnly(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname in blocked:
            raise AssertionError('Native CLI imported a removed harness: ' + fullname)
sys.meta_path.insert(0, NativeOnly())
from civ_agent.cli import main
for args in (['--help'], ['game','start','--help'], ['match','watch','--help'], ['history','export','--help']):
    try: main(args)
    except SystemExit as result: assert result.code == 0
assert not blocked.intersection(sys.modules)
"""
    result = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    assert "Pydantic AI" in result.stdout and "Temporal" not in result.stdout


def test_installed_native_entrypoint_and_module_work_outside_checkout(tmp_path):
    package = distribution("civ-agent")
    entrypoints = [point for point in package.entry_points if point.group == "console_scripts"]
    assert [(point.name, point.value) for point in entrypoints] == [("civ-agent", "civ_agent.cli:main")]
    executable = Path(sys.executable).parent / "civ-agent"
    environment = {key: value for key, value in os.environ.items() if key != "PYTHONPATH"}
    for command in ([str(executable)], [sys.executable, "-I", "-m", "civ_agent"]):
        version = subprocess.run(
            [*command, "--version"], cwd=tmp_path, env=environment, capture_output=True, text=True, check=True
        )
        assert version.stdout.strip() == f"civ-agent {package.version}"
        help_output = subprocess.run(
            [*command, "game", "start", "--help"],
            cwd=tmp_path,
            env=environment,
            capture_output=True,
            text=True,
            check=True,
        )
        assert "--reasoning-effort" in help_output.stdout
        assert "--strategy-guide" in help_output.stdout and "--civilization-guide" in help_output.stdout
    assert list(tmp_path.iterdir()) == []
