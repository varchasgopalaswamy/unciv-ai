"""Rootless commands recover the owner's existing session from non-login shells."""

import os
import socket
import subprocess

import pytest

from civ_agent import container_support as support


@pytest.fixture
def session(tmp_path, monkeypatch):
    monkeypatch.setattr(support, "USER_RUNTIME_ROOT", tmp_path)
    for name in ("XDG_RUNTIME_DIR", "DBUS_SESSION_BUS_ADDRESS", "CIV_AGENT_CONTAINER"):
        monkeypatch.delenv(name, raising=False)
    runtime = tmp_path / str(os.getuid())
    runtime.mkdir(mode=0o700)
    with socket.socket(socket.AF_UNIX) as bus:
        bus.bind(str(runtime / "bus"))
        yield runtime


def test_recovers_missing_session_variables_without_changing_parent(session):
    environment = support.host_environment()
    assert environment["XDG_RUNTIME_DIR"] == str(session)
    assert environment["DBUS_SESSION_BUS_ADDRESS"] == "unix:path=" + str(session / "bus")
    assert "XDG_RUNTIME_DIR" not in os.environ
    assert "DBUS_SESSION_BUS_ADDRESS" not in os.environ


def test_preserves_explicit_session_and_other_environment(session, monkeypatch):
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(session))
    monkeypatch.setenv("DBUS_SESSION_BUS_ADDRESS", "unix:path=/explicit/session")
    monkeypatch.setenv("BUILD_TEST_OPTION", "preserved")
    environment = support.host_environment()
    assert environment["DBUS_SESSION_BUS_ADDRESS"] == "unix:path=/explicit/session"
    assert environment["XDG_RUNTIME_DIR"] == str(session)
    assert environment["BUILD_TEST_OPTION"] == "preserved"


@pytest.mark.parametrize("kind", ["missing", "regular-file"])
def test_does_not_invent_a_bus_when_socket_is_absent(session, kind):
    (session / "bus").unlink()
    if kind == "regular-file":
        (session / "bus").write_text("not a bus")
    environment = support.host_environment()
    assert environment["XDG_RUNTIME_DIR"] == str(session)
    assert "DBUS_SESSION_BUS_ADDRESS" not in environment


def test_does_not_select_a_foreign_user_session(session, monkeypatch):
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(session))
    monkeypatch.setattr(support.os, "getuid", lambda: session.stat().st_uid + 1)
    assert "DBUS_SESSION_BUS_ADDRESS" not in support.host_environment()


def test_keeps_container_environment_isolated_from_host_session(session, monkeypatch):
    monkeypatch.setenv("CIV_AGENT_CONTAINER", "1")
    environment = support.host_environment()
    assert "XDG_RUNTIME_DIR" not in environment
    assert "DBUS_SESSION_BUS_ADDRESS" not in environment


def test_host_command_receives_recovered_session_and_respects_explicit_environment(session, monkeypatch):
    environments = []

    def execute(arguments, **kwargs):
        environments.append(kwargs["env"])
        return subprocess.CompletedProcess(arguments, 0, stdout="", stderr="")

    monkeypatch.setattr(support.subprocess, "run", execute)
    support.command(["podman", "info"])
    assert environments[0]["DBUS_SESSION_BUS_ADDRESS"] == "unix:path=" + str(session / "bus")
    explicit = {"CUSTOM_ENV": "retained"}
    support.command(["podman", "info"], env=explicit)
    assert environments[1] == explicit
