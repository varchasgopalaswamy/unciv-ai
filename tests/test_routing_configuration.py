import json
from unittest.mock import Mock

import pytest

from civ_agent import container_supervisor as supervisor
from civ_agent.commands.game import initialize
from civ_agent.game_config import GameConfig
from civ_agent.tls import private_directory, write_private


def test_missing_key_rejects_enabled_game_before_images_or_launch(tmp_path, monkeypatch):
    resolve = Mock(side_effect=AssertionError("Must reject credentials before resolving images"))
    monkeypatch.setattr("civ_agent.commands.game.resolve_image", resolve)
    config = GameConfig(agents={"decisions_enabled": True}).resolved()
    with pytest.raises(supervisor.ContainerError, match="separate OpenAI API key"):
        initialize(tmp_path / "game", config, tmp_path / "auth")
    assert not (tmp_path / "game").exists()
    resolve.assert_not_called()


def test_key_permissions_and_independent_client_credentials(tmp_path):
    auth, player = private_directory(tmp_path / "auth"), private_directory(tmp_path / "seat")
    disabled = GameConfig().resolved()
    supervisor.validate_routing_credentials(disabled, auth)
    enabled = GameConfig(agents={"decisions_enabled": True}).resolved()
    write_private(auth / "decisions.key", b"only-the-trusted-broker-has-this-key\n")
    supervisor.validate_routing_credentials(enabled, auth)
    (auth / "decisions.key").chmod(0o644)
    with pytest.raises(ValueError):
        supervisor.validate_routing_credentials(enabled, auth)
    (auth / "decisions.key").chmod(0o600)
    supervisor.authorize_agent(auth, "test", 0, player)
    supervisor.authorize_agent(auth, "test", 0, player, decisions=True)
    provider = json.loads((player / "auth-client.json").read_text())
    routing = json.loads((player / "decisions-client.json").read_text())
    assert provider["token"] != routing["token"]
    assert (auth / "clients.json").read_text() != (auth / "decisions-clients.json").read_text()
    mounted = " ".join(supervisor.decisions_arguments(auth, "Rome"))
    assert "decisions-ipc" in mounted and "decisions-client.json" in mounted
    assert "decisions.key" not in mounted and "/auth:" not in mounted


def test_broker_image_mismatch_preserves_existing_shared_process(tmp_path, monkeypatch):
    monkeypatch.setattr(supervisor, "inspected", lambda _: {"Image": "old-image", "State": {"Running": True}})
    mutate = Mock(side_effect=AssertionError("Must not replace a broker serving another game"))
    monkeypatch.setattr(supervisor, "podman", mutate)
    with pytest.raises(supervisor.ContainerError, match="different runtime image"):
        supervisor.ensure_decisions(tmp_path, "new-image")
    mutate.assert_not_called()


@pytest.mark.parametrize(
    "health",
    [
        {"status": "ready", "upstreamStatus": "unchecked"},
        {
            "status": "incompatible",
            "reason": "broker_version_mismatch",
            "versionMismatches": {"protocolVersion": {"expected": 3, "actual": 2}},
        },
        {"status": "unavailable", "reason": "broker_access_denied"},
    ],
)
def test_ready_file_requires_authenticated_compatible_socket_preflight(tmp_path, monkeypatch, health):
    monkeypatch.setattr(supervisor, "inspected", lambda _: {"Image": "image", "State": {"Running": True}})
    write_private(tmp_path / "decisions-ready.json", b'{"ready":true}\n')
    monkeypatch.setattr(supervisor, "podman", Mock(side_effect=AssertionError("Already running")))

    def check(root, *, timeout):
        assert timeout == 5
        token = json.loads((root / "operator/decisions-client.json").read_text())["token"]
        import hashlib

        registry = json.loads((root / "decisions-clients.json").read_text())
        assert registry["operator-health"] == hashlib.sha256(token.encode()).hexdigest()
        return health

    monkeypatch.setattr(supervisor, "routing_broker_health", check)
    if health["status"] == "ready":
        report = supervisor.ensure_decisions(tmp_path, "image")
        assert report["image"] == "image" and report["upstreamStatus"] == "unchecked"
        assert json.loads((tmp_path / "decisions-preflight.json").read_text()) == report
    else:
        with pytest.raises(supervisor.ContainerError, match=health["reason"]) as error:
            supervisor.ensure_decisions(tmp_path, "image")
        if health["status"] == "incompatible":
            assert "protocolVersion expected 3, found 2" in str(error.value)
            assert "civ-agent image build" in str(error.value)
        assert not (tmp_path / "decisions-preflight.json").exists()
