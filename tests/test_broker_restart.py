"""Restart trusted services with new images without replacing durable authority."""

import hashlib
import json
import subprocess
from contextlib import contextmanager
from copy import deepcopy
from unittest.mock import Mock

import pytest

from civ_agent import cli
from civ_agent import container_supervisor as supervisor
from civ_agent.commands import runtime
from civ_agent.tls import private_directory, write_private, write_private_json


class FakePodman:
    def __init__(self, root):
        self.root = root
        self.containers = {}
        self.calls = []
        self.held_locks = set()
        self.fail_stop = False

    def existing(self, role, *, running=True, image="old-image"):
        name = supervisor.auth_name(self.root) + ("-decisions" if role == "decisions" else "")
        self.containers[name] = {"Image": image, "State": {"Running": running}}
        write_private(self.ready(role), b'{"old":true}\n')
        return name

    def ready(self, role):
        return self.root / ("decisions-ready.json" if role == "decisions" else "ready.json")

    def inspected(self, name):
        return deepcopy(self.containers.get(name))

    @contextmanager
    def locked(self, path, *, wait=False):
        assert wait
        assert path.name not in self.held_locks
        self.held_locks.add(path.name)
        try:
            yield
        finally:
            self.held_locks.remove(path.name)

    def __call__(self, *arguments, **options):
        self.calls.append((arguments, options))
        action = arguments[0]
        if action in {"stop", "rm", "start"}:
            name = arguments[-1]
            role = "decisions" if name.endswith("-decisions") else "auth"
            lock = ".decisions-launch.lock" if role == "decisions" else ".launch.lock"
            assert lock in self.held_locks
            if action == "stop":
                assert arguments[1:3] == ("--time", "90")
                assert options.get("timeout") == 100
                assert options.get("check", True)
                if self.fail_stop:
                    raise supervisor.ContainerError("stop failed")
                self.containers[name]["State"]["Running"] = False
            elif action == "rm":
                assert not self.containers[name]["State"]["Running"]
                del self.containers[name]
            else:
                self.containers[name]["State"]["Running"] = True
                write_private(self.ready(role), b'{"ready":true}\n')
        elif action == "create":
            name = arguments[arguments.index("--name") + 1]
            image, role = arguments[-2:]
            assert not self.ready(role).exists(), "stale ready files must be cleared before starting"
            self.containers[name] = {"Image": image, "State": {"Running": False}}
        else:
            raise AssertionError(f"Unexpected Podman command: {arguments}")
        return subprocess.CompletedProcess(arguments, 0, stdout="", stderr="")


@pytest.fixture
def fake_runtime(tmp_path, monkeypatch):
    root = private_directory(tmp_path / "auth with spaces")
    fake = FakePodman(root)
    monkeypatch.setattr(supervisor, "podman", fake)
    monkeypatch.setattr(supervisor, "inspected", fake.inspected)
    monkeypatch.setattr(supervisor, "locked", fake.locked)
    monkeypatch.setattr(supervisor, "routing_broker_health", lambda *_, **__: {"status": "ready"})
    return fake


def configure(root, *roles):
    if "auth" in roles:
        write_private(root / "codex/auth.json", b'{"tokens":{"refresh_token":"synthetic-login"}}\n')
    if "decisions" in roles:
        write_private(root / "decisions.key", b"synthetic-decisions-key\n")


@pytest.mark.parametrize("role", ["auth", "decisions"])
@pytest.mark.parametrize("running", [True, False])
def test_restart_recreates_existing_container_with_selected_image(fake_runtime, role, running):
    fake = fake_runtime
    configure(fake.root, role)
    name = fake.existing(role, running=running)
    ensure = supervisor.ensure_auth if role == "auth" else supervisor.ensure_decisions
    ensure(fake.root, "new-image", restart=True)
    commands = [args[0] for args, _ in fake.calls]
    assert commands == (["stop"] if running else []) + ["rm", "create", "start"]
    assert fake.containers[name] == {"Image": "new-image", "State": {"Running": True}}


def test_default_ensure_preserves_running_auth_and_rejects_decisions_image_change(fake_runtime):
    fake = fake_runtime
    configure(fake.root, "auth", "decisions")
    fake.existing("auth")
    fake.existing("decisions")
    supervisor.ensure_auth(fake.root, "new-image")
    with pytest.raises(supervisor.ContainerError, match="different runtime image"):
        supervisor.ensure_decisions(fake.root, "new-image")
    assert fake.calls == []


@pytest.mark.parametrize("role", ["auth", "decisions"])
def test_failed_stop_does_not_remove_or_recreate_container(fake_runtime, role):
    fake = fake_runtime
    configure(fake.root, role)
    name = fake.existing(role)
    fake.fail_stop = True
    ensure = supervisor.ensure_auth if role == "auth" else supervisor.ensure_decisions
    with pytest.raises(supervisor.ContainerError, match="stop failed"):
        ensure(fake.root, "new-image", restart=True)
    assert [args[0] for args, _ in fake.calls] == ["stop"]
    assert fake.containers[name]["State"]["Running"]
    assert fake.ready(role).exists()


@pytest.mark.parametrize("roles", [("auth",), ("decisions",), ("auth", "decisions")])
def test_restart_starts_configured_roles_and_skips_unconfigured_roles(fake_runtime, roles):
    fake = fake_runtime
    configure(fake.root, *roles)
    report = supervisor.restart_brokers(fake.root, "new-image")
    assert report["auth_directory"] == str(fake.root)
    assert report["image"] == "new-image"
    for role in ("auth", "decisions"):
        if role in roles:
            name = supervisor.auth_name(fake.root) + ("-decisions" if role == "decisions" else "")
            assert report[role]["status"] == "ready"
            assert report[role]["container"] == name
            assert fake.containers[name]["Image"] == "new-image"
        else:
            assert report[role] == {"status": "not_configured"}
    if "decisions" in roles:
        assert report["decisions"]["health"]["status"] == "ready"
    assert [args[0] for args, _ in fake.calls].count("create") == len(roles)


@pytest.mark.parametrize("invalid", ["empty", "permissions"])
def test_invalid_decisions_key_preserves_both_running_services(fake_runtime, invalid):
    fake = fake_runtime
    configure(fake.root, "auth", "decisions")
    fake.existing("auth")
    fake.existing("decisions")
    key = fake.root / "decisions.key"
    if invalid == "empty":
        write_private(key, b"\n")
    else:
        key.chmod(0o644)
    with pytest.raises((ValueError, supervisor.ContainerError)):
        supervisor.restart_brokers(fake.root, "new-image")
    assert fake.calls == []
    assert all(row["State"]["Running"] for row in fake.containers.values())


@pytest.mark.parametrize("missing_role", ["auth", "decisions"])
def test_missing_credential_for_existing_service_preserves_both(fake_runtime, missing_role):
    fake = fake_runtime
    configure(fake.root, "decisions" if missing_role == "auth" else "auth")
    fake.existing("auth")
    fake.existing("decisions")
    with pytest.raises((ValueError, supervisor.ContainerError)):
        supervisor.restart_brokers(fake.root, "new-image")
    assert fake.calls == []


def test_no_configured_services_fails_without_mutating_containers(fake_runtime):
    with pytest.raises((ValueError, supervisor.ContainerError)):
        supervisor.restart_brokers(fake_runtime.root, "new-image")
    assert fake_runtime.calls == []


@pytest.mark.parametrize("directory", ["ipc", "decisions-ipc"])
def test_invalid_ipc_permissions_preserve_both_running_services(fake_runtime, directory):
    fake = fake_runtime
    configure(fake.root, "auth", "decisions")
    fake.existing("auth")
    fake.existing("decisions")
    path = fake.root / directory
    path.mkdir()
    path.chmod(0o755)
    with pytest.raises(ValueError, match="owner-only directory"):
        supervisor.restart_brokers(fake.root, "new-image")
    assert fake.calls == []
    assert all(row["State"]["Running"] for row in fake.containers.values())


def test_cli_resolves_image_once_restarts_both_and_preserves_authority_files(fake_runtime, monkeypatch, capsys):
    fake = fake_runtime
    configure(fake.root, "auth", "decisions")
    fake.existing("auth")
    fake.existing("decisions")
    write_private_json(fake.root / "operator/decisions-client.json", {"token": "existing-operator-token"})
    write_private_json(fake.root / "clients.json", {"game-seat": "auth-client-digest"})
    write_private_json(
        fake.root / "decisions-clients.json",
        {
            "game-seat": "decisions-client-digest",
            "operator-health": hashlib.sha256(b"existing-operator-token").hexdigest(),
        },
    )
    write_private(fake.root / "decisions.sqlite3", b"opaque-durable-ledger\x00\xff")
    write_private(fake.root / "decisions-authority.json", b'{"authority":"unchanged"}\n')
    files = [
        "codex/auth.json",
        "decisions.key",
        "clients.json",
        "decisions-clients.json",
        "operator/decisions-client.json",
        "decisions.sqlite3",
        "decisions-authority.json",
    ]
    before = {name: (fake.root / name).read_bytes() for name in files}
    resolve = Mock(return_value="sha256:new-immutable-image")
    monkeypatch.setattr(runtime, "resolve_image", resolve)
    assert cli.main(["auth", "restart", "--auth-dir", str(fake.root), "--image", "local:test-runtime"]) == 0
    resolve.assert_called_once_with("local:test-runtime")
    report = json.loads(capsys.readouterr().out)
    assert report["image"] == "sha256:new-immutable-image"
    assert report["auth"]["status"] == report["decisions"]["status"] == "ready"
    assert all(row["Image"] == "sha256:new-immutable-image" for row in fake.containers.values())
    assert {name: (fake.root / name).read_bytes() for name in files} == before


def test_cli_image_resolution_failure_preserves_running_services(fake_runtime, monkeypatch, capsys):
    fake = fake_runtime
    configure(fake.root, "auth", "decisions")
    fake.existing("auth")
    fake.existing("decisions")
    resolve = Mock(side_effect=supervisor.ContainerError("selected image not found"))
    monkeypatch.setattr(runtime, "resolve_image", resolve)
    assert cli.main(["auth", "restart", "--auth-dir", str(fake.root), "--image", "missing:image"]) == 2
    assert "selected image not found" in capsys.readouterr().err
    assert fake.calls == []


def test_restart_cli_has_no_provider_option(tmp_path):
    with pytest.raises(SystemExit) as error:
        cli.main(["auth", "restart", "--auth-dir", str(tmp_path), "--provider", "openrouter"])
    assert error.value.code == 2
