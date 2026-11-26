"""Recover read-only startup before a job is admitted to model workers."""

import json

from sqlalchemy import select

from civ_agent import memory_schema as tables
from civ_agent.memory_snapshots import load_snapshot
from civ_agent.orchestration.gateway_registry import job_key
from civ_agent.orchestration.store import JournalConflict
from civ_agent.proposal_gateway import JobAccess


async def prepare_job(root, binding, connection, *, turn=None, snapshot=None):
    root.check_owner(binding.scope)
    key = "nativePreparing:" + binding.job
    settings = root.job_settings(binding.purpose)

    def recover():
        with root.memory.transaction() as db:
            if root.memory._meta(db, "epoch") != binding.scope.epoch:
                raise JournalConflict("PREPARATION_EPOCH_CONFLICT")
            identity = binding.model_dump(mode="json")
            prior = root.memory._meta(db, key)
            row = db.execute(select(tables.jobs).where(tables.jobs.c.id == binding.job)).mappings().first()
            if (
                root.memory._meta(db, job_key(binding)) is not None
                or prior is not None
                and prior["identity"] != identity
                or row is not None
                and prior is None
            ):
                raise JournalConflict("PREPARATION_BINDING_CONFLICT")
            attempt = prior["attempt"] + 1 if prior is not None else 0
            saved = None
            if row is not None:
                calls = db.execute(select(tables.calls.c.id).where(tables.calls.c.job == binding.job)).scalars()
                effects = db.scalar(select(tables.outbox.c.id).where(tables.outbox.c.job == binding.job).limit(1))
                if (
                    (row["epoch"], row["purpose"]) != (binding.scope.epoch, binding.purpose)
                    or json.loads(row["settings"]) != settings
                    or row["status"] not in {"RUNNING", "INTERRUPTED"}
                    or effects is not None
                    or any(not call.startswith("startup:") for call in calls)
                ):
                    raise JournalConflict("PREPARATION_ALREADY_ADMITTED")
                saved = load_snapshot(db, row["snapshot"])
                db.execute(tables.jobs.update().where(tables.jobs.c.id == binding.job).values(status="RUNNING"))
            root.memory._set(db, key, {"identity": identity, "attempt": attempt})
            return saved, attempt

    saved, attempt = await root.io.call(recover)
    if saved is None:
        job = await root.gateway.open(
            binding.job, binding.purpose, binding.scope.epoch, connection, settings, turn, memory_snapshot=snapshot
        )
    else:
        for token, old in list(root.gateway.jobs.items()):
            if old.id == binding.job:
                old.closed = True
                root.gateway.jobs.pop(token)
        job = JobAccess(binding.job, binding.purpose, binding.scope.epoch, connection, saved)
        if binding.purpose == "communication":
            job.overview_view = "communication"
        job.outgoing_start = await root.io.call(root.memory.outgoing_position)
        root.gateway.jobs[job.token] = job
    # Startup contains permitted reads only. Refresh every read after an interrupted
    # preparation: replaying public receipts cannot reconstruct private observations
    # or delivery scopes. No model has been admitted and no effect may be present.
    return job, "startup:" + str(attempt) + ":"
