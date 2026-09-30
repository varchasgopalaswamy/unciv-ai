"""Durable opening and periodic reviews; inputs live until a report is committed."""

import json

from sqlalchemy import insert, select

from civ_agent import memory_schema as tables
from civ_agent.memory_store import encode


class ReviewStore:
    def __init__(self, memory):
        self.memory = memory

    def pending(self):
        with self.memory.transaction() as db:
            row = db.execute(select(tables.reviews).where(tables.reviews.c.status == "PENDING")).mappings().first()
            return dict(row) if row else None

    def has_opening(self):
        with self.memory.transaction() as db:
            return db.execute(select(tables.reviews.c.job).where(tables.reviews.c.turn == 0)).first() is not None

    def prepare_opening(self, id, inputs, settings, timeout):
        with self.memory.transaction() as db:
            if db.execute(select(tables.reviews.c.job).where(tables.reviews.c.turn == 0)).first() is not None:
                return
            db.execute(
                insert(tables.reviews).values(
                    job=id,
                    turn=0,
                    status="PENDING",
                    attempts=0,
                    inputs=encode(inputs),
                    settings=encode(settings),
                    timeout=timeout,
                )
            )

    def blocking(self, epoch, turn):
        """Only read readiness metadata; frozen inputs may be large."""
        with self.memory.transaction() as db:
            if self.memory._meta(db, "epoch", epoch) != epoch:
                raise ValueError("WORKSPACE_EPOCH_CONFLICT")
            row = (
                db.execute(
                    select(
                        tables.reviews.c.job, tables.reviews.c.turn, tables.reviews.c.status, tables.reviews.c.attempts
                    )
                    .where(available_for_turn(turn), tables.reviews.c.status != "COMPLETED")
                    .order_by(tables.reviews.c.turn)
                    .limit(1)
                )
                .mappings()
                .first()
            )
            return dict(row) if row else None

    def available(self, parent):
        with self.memory.transaction() as db:
            return not db.execute(
                select(tables.reviews.c.job).where(
                    (tables.reviews.c.status == "PENDING")
                    | ((tables.reviews.c.job == parent) & (tables.reviews.c.status != "PREPARED"))
                )
            ).first()

    def prepare(self, parent, inputs, settings, timeout):
        with self.memory.transaction() as db:
            # A rejected END_TURN may be corrected and retried with a newer position.
            existing = db.execute(select(tables.reviews.c.status).where(tables.reviews.c.job == parent)).scalar()
            if existing not in (None, "PREPARED"):
                return
            values = dict(turn=inputs["turn"], inputs=encode(inputs), settings=encode(settings), timeout=timeout)
            if existing is None:
                db.execute(insert(tables.reviews).values(job=parent, status="PREPARED", attempts=0, **values))
            else:
                db.execute(tables.reviews.update().where(tables.reviews.c.job == parent).values(**values))

    def confirm(self, parent):
        with self.memory.transaction() as db:
            db.execute(
                tables.reviews.update()
                .where(tables.reviews.c.job == parent, tables.reviews.c.status == "PREPARED")
                .values(status="PENDING")
            )

    def attempt(self, parent, limit):
        with self.memory.transaction() as db:
            row = db.execute(select(tables.reviews).where(tables.reviews.c.job == parent)).mappings().one()
            if row["status"] != "PENDING" or row["attempts"] >= limit:
                raise ValueError("STRATEGIC_REVIEW_ATTEMPTS_EXHAUSTED")
            db.execute(
                tables.reviews.update().where(tables.reviews.c.job == parent).values(attempts=row["attempts"] + 1)
            )
            return {**row, "attempts": row["attempts"] + 1}

    def failed(self, parent, error):
        with self.memory.transaction() as db:
            db.execute(tables.reviews.update().where(tables.reviews.c.job == parent).values(error=error))

    def complete(self, parent, advice):
        with self.memory.transaction() as db:
            db.execute(
                tables.reviews.update()
                .where(tables.reviews.c.job == parent, tables.reviews.c.status == "PENDING")
                .values(status="COMPLETED", inputs=None, advice=encode(advice), error=None)
            )


def available_for_turn(turn):
    # Opening advice precedes the first move; periodic advice follows its source turn.
    return (tables.reviews.c.turn < turn) | ((tables.reviews.c.turn == 0) & (turn >= 0))


def latest_advice(db, turn):
    """Freeze the opening or latest earlier end-of-turn report at dispatch."""
    if turn is None:
        return None
    row = (
        db.execute(
            select(tables.reviews.c.turn, tables.reviews.c.advice)
            .where(tables.reviews.c.status == "COMPLETED", available_for_turn(turn))
            .order_by(tables.reviews.c.turn.desc())
            .limit(1)
        )
        .mappings()
        .first()
    )
    return {"asOfTurn": row["turn"], **json.loads(row["advice"])} if row else None
