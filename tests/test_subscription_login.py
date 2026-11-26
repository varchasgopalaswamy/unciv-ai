"""The operator login uses native OAuth without an image or Codex subprocess."""

import json
from unittest.mock import Mock

from civ_agent.commands import runtime
from civ_agent.tls import write_private_json


def test_native_login_runs_on_host_and_status_uses_its_private_file(tmp_path, monkeypatch, capsys):
    from civ_agent import subscription_auth

    root = tmp_path / "login with spaces"
    calls = []

    async def login(directory):
        calls.append(directory)
        write_private_json(directory / "openai-subscription.json", {"synthetic": "private-test-credential"})

    monkeypatch.setattr(subscription_auth, "login", login)
    monkeypatch.setattr(runtime, "inspected", lambda *_: None)
    forbidden = Mock(side_effect=AssertionError("Native login must not resolve an image or run a process"))
    monkeypatch.setattr(runtime, "resolve_image", forbidden)
    monkeypatch.setattr(runtime.subprocess, "call", forbidden)
    args = runtime.parser(group="auth", action="login").parse_args(["--auth-dir", str(root)])
    assert runtime.run(args) == 0
    assert calls == [root]
    assert not (root / "codex").exists()
    assert "private-test-credential" not in capsys.readouterr().out
    args = runtime.parser(group="auth", action="status").parse_args(["--auth-dir", str(root)])
    assert runtime.run(args) == 0
    status = json.loads(capsys.readouterr().out)
    assert status == {"auth_directory": str(root), "login_present": True, "manager_running": False}
    forbidden.assert_not_called()


def test_native_login_fences_account_change_while_manager_runs(tmp_path, monkeypatch, capsys):
    from civ_agent import subscription_auth

    login = Mock(side_effect=AssertionError("Active credentials must remain untouched"))
    monkeypatch.setattr(subscription_auth, "login", login)
    monkeypatch.setattr(runtime, "inspected", lambda *_: {"State": {"Running": True}})
    existing = {"synthetic": "private-existing-credential"}
    write_private_json(tmp_path / "openai-subscription.json", existing)
    args = runtime.parser(group="auth", action="login").parse_args(["--auth-dir", str(tmp_path)])
    assert runtime.run(args) == 2
    assert "auth stop" in capsys.readouterr().err
    assert json.loads((tmp_path / "openai-subscription.json").read_text()) == existing
    login.assert_not_called()
