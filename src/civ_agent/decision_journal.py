"""Private durable operation identities, receipts and epoch fences."""

import json

from sqlalchemy import insert, select

from civ_agent import memory_schema as tables
from civ_agent.civilization_state import StateConflict
from civ_agent.strict_schema import encoded

EVENT_FIELDS = (
    "usage",
    "cumulativeUsage",
    "requestFailure",
    "modelFailure",
    "providerRequests",
    "providerRejections",
    "generationStopped",
)


def journal(memory, identifier, body=None, *, event=False):
    with memory.transaction() as db:
        value = db.execute(
            select(tables.decision_journal.c.body).where(tables.decision_journal.c.id == identifier)
        ).scalar_one_or_none()
        prior = json.loads(value) if value is not None else None
        if body is not None:
            if prior is not None:
                if event:
                    body = {
                        **prior,
                        **{k: v for k, v in body.items() if k in EVENT_FIELDS or prior["status"] == "generating"},
                    }
                else:
                    body = {**body, **{k: prior[k] for k in EVENT_FIELDS if k in prior}}
                    # A failure can arrive before turn/start's caller binds the
                    # turn. Binding must not revive that rejected response.
                    if prior["status"] == "generation_failed" and body["status"] == "generating":
                        body.update({k: prior[k] for k in ("status", "category", "error") if k in prior})
            db.execute(
                insert(tables.decision_journal)
                .values(
                    id=identifier,
                    job=body["job"],
                    epoch=body["epoch"],
                    purpose=body["purpose"],
                    body=encoded(body),
                )
                .prefix_with("OR REPLACE")
            )
            return body
        return prior


def completed_call(memory, job, identity):
    """Look up a prior receipt without registering an unexecuted operation."""
    with memory.transaction() as db:
        return (
            db.scalar(select(tables.calls.c.result).where(tables.calls.c.job == job, tables.calls.c.id == identity))
            is not None
        )


def check_epoch(memory, epoch):
    with memory.transaction() as db:
        if memory._meta(db, "epoch") != epoch:
            raise StateConflict("DECISION_CONTEXT_EXPIRED")
