"""Operator-only, repeatable civilization checkpoint operations."""

import hashlib
import json
import re
import shutil

from civ_agent.agent_history import HistoryStore
from civ_agent.tls import private_directory, write_private_json


def checkpoint_path(service, id):
    if not isinstance(id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", id):
        raise ValueError("INVALID_CHECKPOINT_ID")
    return private_directory(service.state_dir / "turn-recovery") / (id + ".json")


async def barrier(service, paused):
    gateway = service.gateway
    if paused:
        gateway.reviewing = False
        gateway.request_checkpoint()
        # Existing calls finish all their writes/receipts before memory is
        # frozen. A model still computing, or waiting to call a tool, need not.
        if gateway.calls_in_flight or gateway.in_flight:
            return {"paused": True, "ready": False}
        gateway.paused = True
        await service.io.call(service.memory.pause, True)
    else:
        await service.io.call(service.memory.pause, False)
        while gateway.deferred_completions:
            deferred, gateway.deferred_completions = gateway.deferred_completions, []
            for epoch, id, completed in deferred:
                if service.match is not None and service.match["epoch"] == epoch:
                    await service.io.call(service.memory.finish_job, id, completed)
        gateway.paused = False
        gateway.release_checkpoint()
        service.chat.pending.set()
    return {"paused": paused, "ready": True}


async def capture(service, id):
    if not service.gateway.paused or service.gateway.in_flight or service.gateway.calls_in_flight:
        raise ValueError("BARRIER_REQUIRED")
    await service.chat.reconcile()
    path = checkpoint_path(service, id)
    if path.exists():
        digest = await service.io.call(lambda: hashlib.sha256(path.read_bytes()).hexdigest())
    else:
        # Summaries are reproducible from immutable database evidence. Never
        # duplicate every completed turn's observations in the current boundary.
        metadata = json.loads(
            json.dumps(
                {
                    "match": service.match,
                    "records": list(service.records.values()),
                }
            )
        )

        def save():
            metadata["histories"] = {}
            for history in (service.state_dir / "history").glob("*/events.jsonl"):
                metadata["histories"][history.parent.name] = history.stat().st_size
            return service.memory.backup(path, metadata)

        digest = await service.io.call(save)
    return {"id": id, "sha256": digest, "civilization": service.civilization}


async def retain(service, id):
    keep = checkpoint_path(service, id)
    if not keep.exists():
        raise ValueError("CHECKPOINT_MISSING")

    def prune():
        for path in keep.parent.glob("*.json"):
            if path != keep:
                path.unlink()

    await service.io.call(prune)
    return {"retained": id}


async def restore(service, payload):
    # A late finalization request must finish against the old archive before
    # paths and epoch are replaced. No writer may resume into the restored tree.
    for task in list(service.finalization_tasks.values()):
        await task
    async with service.guard:
        return await _restore(service, payload)


async def _restore(service, payload):
    if set(payload) != {"id", "sha256", "epoch"} or not service.gateway.paused:
        raise ValueError("BARRIER_REQUIRED")
    path = checkpoint_path(service, payload["id"])
    saved = await service.io.call(service.memory.verify, path, payload["sha256"])
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
        # Keep only the history prefix that existed at the boundary. Completed
        # histories remain available; messages from discarded actions do not.
        for directory, length in saved["service"]["histories"].items():
            source = archive / "history" / directory / "events.jsonl"
            target = private_directory(service.state_dir / "history" / directory) / "events.jsonl"
            with source.open("rb") as reader, target.open("wb") as writer:
                remaining = length
                while remaining:
                    block = reader.read(min(remaining, 1024 * 1024))
                    if not block:
                        raise ValueError("CHECKPOINT_HISTORY_MISSING")
                    writer.write(block)
                    remaining -= len(block)
            target.chmod(0o600)
        pending = service.memory.restore(path, payload["sha256"], payload["epoch"])
        service.match = {**(saved["service"]["match"] or {}), "epoch": payload["epoch"]}
        write_private_json(service.match_file, service.match)
        service.history = HistoryStore(service.state_dir / "history", service.token)
        service.gateway.history = service.history
        service.records.clear()
        for record in saved["service"]["records"]:
            record["epoch"] = payload["epoch"]
            if record["phase"] not in {"COMPLETED", "FAILED", "INTERRUPTED"}:
                record.update(phase="FAILED", error="SERVICE_RESTARTED")
            service.records[record["activationId"]] = record
            write_private_json(
                service.records_dir / (hashlib.sha256(record["activationId"].encode()).hexdigest() + ".json"), record
            )
        service.known_threads.clear()
        service.finalization_tasks.clear()
        service.finalization_errors.clear()
        service.active_id = None
        service.reviews.failure = None
        service.gateway.jobs.clear()
        service.gateway.deferred_completions.clear()
        service.gateway.run_failure = service.memory.run_failure()
        if service.gateway.run_failure:
            service.gateway.failed.set()
        else:
            service.gateway.failed.clear()
        write_private_json(service.state_dir / "restore.json", {**payload, "installed": True})
        return pending

    pending = await service.io.call(install)
    return {
        "restored": True,
        "epoch": payload["epoch"],
        "id": payload["id"],
        "sha256": payload["sha256"],
        "pendingReviews": pending,
    }
