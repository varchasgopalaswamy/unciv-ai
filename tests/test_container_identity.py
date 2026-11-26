"""Restored deployments own distinct resources while native identities stay frozen."""

import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from civ_agent import container_support as support
from civ_agent.commands import game
from civ_agent.container_identity import deployment_identity
from civ_agent.tls import read_json, write_private_json

LOGICAL = "c0492c2198"
PHYSICAL = "7df350dce1"


def manifest(root, **fields):
    record = {"format": 1, "id": LOGICAL, "deployment": {"version": 1, "id": PHYSICAL, "game": LOGICAL}, **fields}
    write_private_json(root / "container.json", record)
    return record


def test_default_physical_identity_remains_the_existing_game_id():
    selected = deployment_identity({"id": LOGICAL})
    assert selected.id == selected.game == LOGICAL


@pytest.mark.parametrize(
    "deployment",
    [
        {"version": 1, "id": "../unsafe", "game": LOGICAL},
        {"version": 1, "id": "7DF350DCE1", "game": LOGICAL},
        {"version": 1, "id": "7df350dce1\n", "game": LOGICAL},
        {"version": 1, "id": "7df350dce", "game": LOGICAL},
        {"version": 1, "id": 123, "game": LOGICAL},
        {"version": 2, "id": PHYSICAL, "game": LOGICAL},
        {"version": 1, "id": PHYSICAL, "game": "aaaaaaaaaa"},
        {"version": 1, "id": PHYSICAL, "game": LOGICAL, "unsafe": True},
        {"version": 1, "id": PHYSICAL},
    ],
)
def test_invalid_physical_bindings_are_rejected(deployment):
    with pytest.raises(ValueError):
        deployment_identity({"id": LOGICAL, "deployment": deployment})


def test_restored_deployment_cannot_reuse_the_original_postgres_volume():
    with pytest.raises(ValueError, match="physical deployment identity"):
        deployment_identity(
            {
                "id": LOGICAL,
                "deployment": {"version": 1, "id": PHYSICAL, "game": LOGICAL},
                "native": {"postgresVolume": "civ-" + LOGICAL + "-postgres"},
            }
        )


def test_systemd_launch_uses_physical_identity_without_rewriting_the_logical_manifest(tmp_path, monkeypatch):
    manifest(tmp_path)
    original = json.loads((tmp_path / "container.json").read_bytes())
    command = Mock(return_value=SimpleNamespace(returncode=0, stdout=""))
    monkeypatch.setattr(support, "require_restart_systemd", lambda: None)
    monkeypatch.setattr(support, "unit_state", lambda _: {"LoadState": "not-found"})
    monkeypatch.setattr(support, "command", command)
    assert support.launch_background(tmp_path, "game") == "civ-game-" + PHYSICAL
    arguments = command.call_args.args[0]
    assert arguments[arguments.index("--unit") + 1] == "civ-game-" + PHYSICAL
    saved = json.loads((tmp_path / "container.json").read_bytes())
    assert saved.pop("supervision") == {"backend": "systemd-existing-cid-v1", "unit": "civ-game-" + PHYSICAL}
    assert saved == original


@pytest.mark.parametrize("action", ["stop-fallback", "remove"])
def test_restore_stop_and_remove_never_target_original_resources(tmp_path, monkeypatch, action):
    manifest(tmp_path)
    command = Mock(return_value=SimpleNamespace(returncode=1 if action == "stop-fallback" else 0))
    cleanup = Mock()
    monkeypatch.setattr(game, "command", command)
    monkeypatch.setattr(game, "cleanup", cleanup)
    if action == "stop-fallback":
        game.stop_game(tmp_path)
    else:
        assert game.run(game.parser(action="remove").parse_args(["--state-dir", str(tmp_path)])) == 0
    assert command.call_args.args[0] == ["systemctl", "--user", "stop", "civ-game-" + PHYSICAL]
    cleanup.assert_called_once_with(PHYSICAL)
    assert read_json(tmp_path / "container.json")["id"] == LOGICAL


def test_invalid_restore_binding_fails_before_stop_requests_or_cleanup(tmp_path, monkeypatch):
    manifest(tmp_path, native={"postgresVolume": "civ-" + LOGICAL + "-postgres"})
    command, cleanup = Mock(), Mock()
    monkeypatch.setattr(game, "command", command)
    monkeypatch.setattr(game, "cleanup", cleanup)
    with pytest.raises(ValueError, match="physical deployment identity"):
        game.stop_game(tmp_path)
    command.assert_not_called()
    cleanup.assert_not_called()
    assert not (tmp_path / "control").exists()
