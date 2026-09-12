"""Installed host commands locate a chosen JVM checkout before touching a match."""

from pathlib import Path
from unittest.mock import Mock

import pytest

from civ_agent import checkout, cli
from civ_agent.commands import host


def make_checkout(root: Path) -> Path:
    (root / "src/main/kotlin/uncivai").mkdir(parents=True)
    (root / "gradlew").touch()
    return root


def test_checkout_precedence(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    explicit = make_checkout(tmp_path / "explicit")
    environment = make_checkout(tmp_path / "environment")
    current = make_checkout(tmp_path / "current")
    source = make_checkout(tmp_path / "editable")
    monkeypatch.chdir(current / "src/main")
    monkeypatch.setattr(checkout, "__file__", str(source / "src/civ_agent/checkout.py"))
    monkeypatch.setenv("CIV_AGENT_REPO", str(environment))
    assert checkout.repository_root(explicit) == explicit
    assert checkout.repository_root() == environment
    monkeypatch.delenv("CIV_AGENT_REPO")
    assert checkout.repository_root() == current
    monkeypatch.chdir(tmp_path)
    assert checkout.repository_root() == source


def test_invalid_explicit_checkout_does_not_fall_back_or_touch_match(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(make_checkout(tmp_path / "valid"))
    state = tmp_path / "match"
    build = Mock()
    monkeypatch.setattr(host.subprocess, "run", build)
    assert cli.main(["host", "start", "--repo-dir", str(tmp_path / "invalid"), "--state-dir", str(state)]) == 2
    assert "No game host checkout" in capsys.readouterr().err
    assert not state.exists()
    build.assert_not_called()


def test_missing_checkout_explains_selection(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CIV_AGENT_REPO", raising=False)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(checkout, "__file__", str(tmp_path / "site-packages/civ_agent/checkout.py"))
    with pytest.raises(ValueError, match="--repo-dir or set CIV_AGENT_REPO"):
        checkout.repository_root()
