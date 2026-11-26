"""Managed host checkpoint artifacts require the native communication barrier."""

import asyncio
import copy
import json
import stat
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest
from pydantic import ValidationError

from civ_agent.operator_connection import OperatorError
from civ_agent.orchestration.contracts import digest
from civ_agent.orchestration.managed_checkpoints import (
    HostCheckpointRequest,
    HostCheckpoints,
)
from civ_agent.orchestration.managed_host import ManagedHost
from civ_agent.orchestration.store import JournalConflict
from tests.test_managed_host import Host, authority


def request():
    return HostCheckpointRequest(game="game", epoch="e1", id="turn-0-start", turn=0, handoff=0)


def snapshot():
    return {
        "formatVersion": 9,
        "epoch": "e1",
        "handoff": 0,
        "paused": True,
        "communicationPaused": True,
        "scheduled": True,
        "gameJson": "PRIVATE_NATIVE_GAME_STATE",
        "players": {},
        "communication": {},
    }


class CheckpointHost(Host):
    def __init__(self):
        super().__init__()
        self.barrier = False
        self.snapshot = snapshot()
        self.snapshot_reply_loss = False
        self.restore_pending = False

    def call(self, operation, **fields):
        reply = super().call(operation, **fields)
        if operation == "barrier":
            self.status["phase"], self.barrier = "PAUSED", True
            reply["status"] = copy.deepcopy(self.status)
        elif operation == "unbarrier":
            if self.restore_pending:
                raise OperatorError("Restore is unfinished", "RESTORE_INCOMPLETE")
            self.barrier = False
        elif operation == "snapshot":
            if not self.barrier:
                raise OperatorError("Native communication barrier required", "BARRIER_REQUIRED")
            if self.snapshot_reply_loss:
                self.snapshot_reply_loss = False
                raise OperatorError("Lost read acknowledgement", "TRANSPORT_TIMEOUT")
            reply["checkpoint"] = copy.deepcopy(self.snapshot)
        return reply


def test_capture_retries_keep_original_boundary_private_and_verifiable_after_worker_replacement(tmp_path):
    async def run():
        host = CheckpointHost()
        root = tmp_path / "operator-checkpoints"
        owner = ManagedHost(authority(), host, checkpoint_root=root)
        try:
            await owner.barrier(authority())
            first = await owner.capture_checkpoint(request())
            assert "PRIVATE_NATIVE_GAME_STATE" not in first.model_dump_json()
            assert len(first.model_dump_json()) < 1000
            saved = HostCheckpoints(root).load(first)
            assert saved == snapshot()
            host.snapshot["gameJson"] = "SUBSEQUENT_DISCARDED_ACTIONS"
            assert await owner.capture_checkpoint(request()) == first
            assert HostCheckpoints(root).load(first) == saved
            await owner.verify_checkpoint(first)
            assert [op for op, _ in host.calls].count("acquire") == 1
            assert all(stat.S_IMODE(path.stat().st_mode) == 0o600 for path in root.iterdir())
            assert stat.S_IMODE(root.stat().st_mode) == 0o700
        finally:
            await owner.close()
        replacement = ManagedHost(authority(), host, checkpoint_root=root)
        try:
            assert await replacement.capture_checkpoint(request()) == first
            assert await replacement.verify_checkpoint(first) == first
            assert HostCheckpoints(root).load(first) == saved
            host.snapshot["communicationPaused"] = False
            with pytest.raises(ValidationError, match="BARRIER_REQUIRED"):
                await replacement.capture_checkpoint(request())
            host.snapshot["communicationPaused"] = True
            await replacement.unbarrier(authority())
            assert not host.barrier
        finally:
            await replacement.close()

    asyncio.run(run())


def test_paused_host_alone_cannot_capture_and_read_loss_publishes_no_checkpoint(tmp_path):
    async def run():
        host = CheckpointHost()
        root = tmp_path / "checkpoints"
        owner = ManagedHost(authority(), host, checkpoint_root=root)
        try:
            with pytest.raises(OperatorError, match="barrier"):
                await owner.capture_checkpoint(request())
            assert not root.exists()
            await owner.barrier(authority())
            host.snapshot_reply_loss = True
            with pytest.raises(OperatorError, match="acknowledgement"):
                await owner.capture_checkpoint(request())
            assert not root.exists()
            assert await owner.capture_checkpoint(request())
        finally:
            await owner.close()

    asyncio.run(run())


@pytest.mark.parametrize("damage", ["game", "epoch", "turn", "handoff", "phase", "scheduled", "lease"])
def test_wrong_authority_or_native_position_rejects_before_snapshot(tmp_path, damage):
    async def run():
        host = CheckpointHost()
        owner = ManagedHost(authority(), host, checkpoint_root=tmp_path / "checkpoints")
        command = request()
        if damage in {"game", "epoch"}:
            command = command.model_copy(update={damage: "foreign"})
        else:
            host.status[{"lease": "leaseId", "phase": "phase"}.get(damage, damage)] = {
                "turn": 1,
                "handoff": 1,
                "phase": "ACTIVE",
                "scheduled": False,
                "lease": "foreign-owner",
            }[damage]
        try:
            with pytest.raises(JournalConflict):
                await owner.capture_checkpoint(command)
            assert "snapshot" not in [op for op, _ in host.calls]
        finally:
            await owner.close()

    asyncio.run(run())


@pytest.mark.parametrize(
    "field,value",
    [
        ("epoch", "other"),
        ("handoff", 1),
        ("paused", False),
        ("communicationPaused", False),
        ("scheduled", False),
        ("communicationPaused", 1),
        ("formatVersion", 8),
        ("gameJson", ""),
    ],
)
def test_native_snapshot_requires_exact_epoch_handoff_and_strict_barrier(tmp_path, field, value):
    checkpoints = HostCheckpoints(tmp_path / "checkpoints")
    with pytest.raises(ValidationError):
        checkpoints.save(request(), {**snapshot(), field: value})
    assert not (tmp_path / "checkpoints").exists()


@pytest.mark.parametrize("damage", ["reference_hash", "body", "request", "symlink"])
def test_saved_host_checkpoint_tampering_and_replacement_are_rejected(tmp_path, damage):
    checkpoints = HostCheckpoints(tmp_path / "checkpoints")
    ref = checkpoints.save(request(), snapshot())
    path = next(checkpoints.root.iterdir())
    if damage == "reference_hash":
        ref = ref.model_copy(update={"sha256": digest("wrong")})
    elif damage == "body":
        raw = json.loads(path.read_text())
        raw["checkpoint"]["gameJson"] = "TAMPERED"
        path.write_text(json.dumps(raw))
    elif damage == "request":
        raw = json.loads(path.read_text())
        raw["request"]["game"] = "foreign"
        path.write_text(json.dumps(raw))
    else:
        moved = tmp_path / "moved-private.json"
        path.rename(moved)
        path.symlink_to(moved)
    with pytest.raises((JournalConflict, ValueError)):
        checkpoints.load(ref)
    if damage == "body":
        with pytest.raises(ValidationError, match="HOST_CHECKPOINT_CONTENT_CONFLICT"):
            checkpoints.existing(request())


def test_store_never_replaces_a_published_boundary_and_restore_pending_cannot_release_barrier(tmp_path):
    async def run():
        checkpoints = HostCheckpoints(tmp_path / "checkpoints")
        ref = checkpoints.save(request(), snapshot())
        with pytest.raises(JournalConflict, match="REPLACEMENT_REJECTED"):
            checkpoints.save(request(), {**snapshot(), "gameJson": "OTHER_BOUNDARY"})
        assert checkpoints.load(ref) == snapshot()
        host = CheckpointHost()
        owner = ManagedHost(authority(), host, checkpoint_root=checkpoints.root)
        try:
            await owner.barrier(authority())
            host.restore_pending = True
            with pytest.raises(OperatorError, match="unfinished"):
                await owner.unbarrier(authority())
            assert host.barrier
        finally:
            await owner.close()

    asyncio.run(run())


def test_overlapping_activity_workers_cannot_replace_the_first_published_host_boundary(tmp_path, monkeypatch):
    checkpoints = HostCheckpoints(tmp_path / "checkpoints")
    original = checkpoints.existing
    simultaneous = threading.Barrier(2)

    def before_publish(command):
        result = original(command)
        if result is None:
            simultaneous.wait(timeout=5)
        return result

    monkeypatch.setattr(checkpoints, "existing", before_publish)

    def save(label):
        try:
            return checkpoints.save(request(), {**snapshot(), "gameJson": label})
        except JournalConflict as error:
            assert str(error) == "HOST_CHECKPOINT_REPLACEMENT_REJECTED"
            return None

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(save, ["FIRST_CANDIDATE", "SECOND_CANDIDATE"]))
    accepted = [result for result in results if result is not None]
    assert len(accepted) == 1
    assert checkpoints.load(accepted[0])["gameJson"] in {"FIRST_CANDIDATE", "SECOND_CANDIDATE"}
    assert original(request()) == accepted[0]
    assert len(list(checkpoints.root.iterdir())) == 1


def test_checkpoint_id_cannot_capture_a_second_changed_boundary(tmp_path):
    checkpoints = HostCheckpoints(tmp_path / "checkpoints")
    first = checkpoints.save(request(), snapshot())
    changed = request().model_copy(update={"turn": 1, "handoff": 1})
    with pytest.raises(JournalConflict, match="REQUEST_CONFLICT"):
        checkpoints.save(changed, {**snapshot(), "handoff": 1})
    assert checkpoints.load(first) == snapshot()
    assert len(list(checkpoints.root.iterdir())) == 1
