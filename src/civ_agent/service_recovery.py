"""Operator-only, repeatable civilization checkpoint operations."""

import hashlib
import json
import re
import shutil

from civ_agent.agent_history import HistoryStore
from civ_agent.tls import private_directory, write_private_json
from civ_agent.turn_archive import TurnArchive


def checkpoint_path(service, id):
    if not isinstance(id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", id):
        raise ValueError("INVALID_CHECKPOINT_ID")
    return private_directory(service.state_dir / "round-checkpoints") / (id + ".json")


async def barrier(service, paused):
    service.gateway.paused = paused
    await service.io.call(service.memory.pause, paused)
    if not paused:
        deferred, service.gateway.deferred_completions = service.gateway.deferred_completions, []
        for epoch, id, completed in deferred:
            if service.match is not None and service.match["epoch"] == epoch:
                await service.io.call(service.memory.finish_job, id, completed)
        service.chat.pending.set()
    return {"paused": paused, "ready": service.gateway.in_flight == 0}


async def capture(service, id):
    if not service.gateway.paused or service.gateway.in_flight:
        raise ValueError("BARRIER_REQUIRED")
    await service.chat.reconcile()
    path = checkpoint_path(service, id)
    if path.exists():
        digest = await service.io.call(lambda: hashlib.sha256(path.read_bytes()).hexdigest())
    else:
        digest = await service.io.call(service.memory.backup, path)
    return {"id": id, "sha256": digest, "civilization": service.civilization}


async def restore(service, payload):
    if set(payload) != {"id", "sha256", "epoch"} or not service.gateway.paused:
        raise ValueError("BARRIER_REQUIRED")
    path = checkpoint_path(service, payload["id"])
    await service.io.call(service.memory.verify, path, payload["sha256"])
    if not isinstance(payload["epoch"], str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", payload["epoch"]):
        raise ValueError("INVALID_EPOCH")
    if service.active_task is not None and not service.active_task.done():
        await service.interrupt(service.active_id)
    await service.chat.close()
    await service.io.call(write_private_json, service.state_dir / "restore.json", payload)

    def install():
        archive = private_directory(service.state_dir / "abandoned" / payload["epoch"])
        # Checkpoint contains memory revisions and receipts, not copies of every
        # transcript. Old job histories stay operator-only after a rewind.
        for source in (
            service.workspace,
            service.state_dir / "turn-archive",
            service.records_dir,
            service.state_dir / "history",
            service.state_dir / "chat-jobs",
            service.results.path,
        ):
            destination = archive / ("workspace" if source == service.workspace else source.name)
            if source.exists() and not destination.exists():
                shutil.move(source, destination)
        private_directory(service.workspace)
        private_directory(service.records_dir)
        service.memory.restore(path, payload["sha256"], payload["epoch"])
        service.memory.export(service.workspace)
        service.match = {"epoch": payload["epoch"]}
        write_private_json(service.match_file, service.match)
        service.history = HistoryStore(service.state_dir / "history", service.token, auto_render=False)
        service.archive = TurnArchive(
            service.state_dir / "turn-archive",
            service.workspace,
            service.civilization,
            payload["epoch"],
            secrets=(service.token,),
        )
        service.records.clear()
        service.known_threads.clear()
        service.thread_settings.clear()
        service.thread_totals.clear()
        service.archive_cache.clear()
        service.active_id = None
        service.gateway.jobs.clear()
        service.gateway.deferred_completions.clear()
        write_private_json(service.state_dir / "restore.json", {**payload, "installed": True})

    await service.io.call(install)
    return {"restored": True, "epoch": payload["epoch"], "id": payload["id"], "sha256": payload["sha256"]}


def pending_restore(service):
    path = service.state_dir / "restore.json"
    return json.loads(path.read_text()) if path.exists() else None
