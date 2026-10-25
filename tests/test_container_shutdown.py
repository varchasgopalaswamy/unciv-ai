"""Stopping a batch fences all games and avoids serial, redundant cleanup."""

import threading
from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from civ_agent import container_support as support
from civ_agent.commands import game
from civ_agent.tls import read_json, write_private_json


@pytest.mark.parametrize("failed_cleanup", [False, True])
def test_batch_publishes_all_stops_then_cleans_independent_games_concurrently(tmp_path, monkeypatch, failed_cleanup):
    support.identity(tmp_path)
    roots = [tmp_path / "games" / name for name in ("g0001", "g0002", "g0003")]
    records = [support.identity(root) for root in roots]
    write_private_json(tmp_path / "batch.json", {"runs": [{"id": root.name} for root in roots]})
    ready = threading.Barrier(len(roots))
    cleaned = []

    def command(arguments, **kwargs):
        assert all(read_json(root / "control/request.json") == {"action": "stop"} for root in roots)
        if arguments[-1].startswith("civ-batch-"):
            return SimpleNamespace(returncode=0)
        # Batch-owned games have no individual systemd units. Even this fallback
        # must run concurrently, rather than waiting for the first game's lock.
        ready.wait(timeout=5)
        return SimpleNamespace(returncode=1)

    def cleanup(identifier):
        cleaned.append(identifier)
        if failed_cleanup and identifier == records[0]["id"]:
            raise support.ContainerError("network cleanup failed")

    monkeypatch.setattr(game, "command", command)
    monkeypatch.setattr(game, "cleanup", cleanup)
    expected = (
        pytest.raises(support.ContainerError, match="network cleanup failed") if failed_cleanup else nullcontext()
    )
    with expected:
        game.stop_game(tmp_path, role="batch")
    assert set(cleaned) == {record["id"] for record in records}


def test_empty_batch_does_not_clean_up_an_unrelated_game(tmp_path, monkeypatch):
    support.identity(tmp_path)
    write_private_json(tmp_path / "batch.json", {"runs": [{"id": "not-started"}]})
    monkeypatch.setattr(game, "command", lambda *a, **k: SimpleNamespace(returncode=1))
    cleanup = Mock()
    monkeypatch.setattr(game, "cleanup", cleanup)
    game.stop_game(tmp_path, role="batch")
    cleanup.assert_not_called()


@pytest.mark.parametrize("running", [[], ["live-player"]])
def test_cleanup_only_stops_live_containers_and_removes_exited_ones(monkeypatch, running):
    stopped = Mock()
    commands = []

    def podman(*arguments, **kwargs):
        commands.append(arguments)
        if arguments[0] == "ps":
            return SimpleNamespace(
                stdout="exited-controller live-player" if "--all" in arguments else " ".join(running)
            )
        return SimpleNamespace(stdout="")

    monkeypatch.setattr(support, "podman", podman)
    monkeypatch.setattr(support, "stop_containers", stopped)
    monkeypatch.setattr(support, "firewall", Mock())
    support.cleanup("match")
    stopped.assert_called_once_with(running)
    assert ("rm", "exited-controller", "live-player") in commands
