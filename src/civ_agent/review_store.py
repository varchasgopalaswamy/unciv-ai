"""Durable opening and periodic reviews; inputs live until a report is committed."""

import json

from sqlalchemy import insert, select

from civ_agent import memory_schema as tables
from civ_agent.memory_store import encode
from civ_agent.review_context import review_position


class ReviewStore:
    def __init__(self, memory):
        self.memory = memory

    def pending(self):
        with self.memory.transaction() as db:
            row = db.execute(select(tables.reviews).where(tables.reviews.c.status == "PENDING")).mappings().first()
            return dict(row) if row else None

    def last_position(self):
        with self.memory.transaction() as db:
            return self.memory._meta(db, "reviewPosition")

    def at_turn(self, turn):
        with self.memory.transaction() as db:
            row = (
                db.execute(
                    select(
                        tables.reviews.c.job, tables.reviews.c.turn, tables.reviews.c.status, tables.reviews.c.attempts
                    ).where(tables.reviews.c.turn == turn)
                )
                .mappings()
                .first()
            )
            return dict(row) if row else None

    def last_completed_turn(self):
        with self.memory.transaction() as db:
            return db.execute(
                select(tables.reviews.c.turn)
                .where(tables.reviews.c.status == "COMPLETED")
                .order_by(tables.reviews.c.turn.desc())
                .limit(1)
            ).scalar()

    def prepare(self, id, inputs, settings, timeout):
        with self.memory.transaction() as db:
            if (
                db.execute(select(tables.reviews.c.job).where(tables.reviews.c.turn == inputs["turn"])).first()
                is not None
            ):
                return
            db.execute(
                insert(tables.reviews).values(
                    job=id,
                    turn=inputs["turn"],
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
            inputs = db.execute(
                select(tables.reviews.c.inputs).where(
                    tables.reviews.c.job == parent, tables.reviews.c.status == "PENDING"
                )
            ).scalar_one_or_none()
            if inputs is None:
                return
            # Publish the factual baseline with its report. It participates in the
            # existing metadata checkpoint and is rewound with that report.
            observation = json.loads(inputs)["observation"]
            self.memory._set(db, "reviewPosition", review_position(observation))
            # Conversations resume before this civ's next gameplay observation.
            # Make the new report available from the trusted boundary snapshot.
            self.memory._advance_turn(db, observation["turn"])
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
