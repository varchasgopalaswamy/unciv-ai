"""Authenticated native events and notification outbox share the notebook commit."""

import json

from sqlalchemy import Column, Integer, MetaData, String, Table, Text, insert, select, update

from civ_agent import memory_schema as domain
from civ_agent.orchestration.contracts import EventNotice, Scope, canonical
from civ_agent.orchestration.store import JournalConflict

metadata = MetaData()
notifications = Table(
    "native_event_notifications",
    metadata,
    Column("id", String, primary_key=True),
    Column("scope", Text, nullable=False),
    Column("source", String, nullable=False, unique=True),
    Column("notice", Text, nullable=False),
    Column("notify", Integer, nullable=False),
    Column("signaled", Integer, nullable=False),
    Column("handled", Integer, nullable=False),
    Column("job", String),
)


def communication_scope(game, player, epoch):
    return Scope(game=game, player=player, epoch=epoch, activation="communication")


class CommunicationStore:
    def __init__(self, memory, artifacts, scope: Scope):
        self.memory, self.artifacts, self.scope = memory, artifacts, scope
        metadata.create_all(memory.engine)

    def check(self, db):
        owner = self.memory._meta(db, "nativeGatewayOwner")
        if owner != {"version": 1, "game": self.scope.game, "player": self.scope.player}:
            raise JournalConflict("COMMUNICATION_OWNER_CONFLICT")
        if self.memory._meta(db, "epoch") != self.scope.epoch:
            raise JournalConflict("COMMUNICATION_EPOCH_CONFLICT")

    def ingest(self, events, cursor):
        notices = []
        for event in events:
            if type(event.get("id")) is not int or event["id"] < 1 or type(event.get("wake")) is not bool:
                raise JournalConflict("COMMUNICATION_EVENT_INVALID")
            if not isinstance(event.get("kind"), str) or not isinstance(event.get("body"), dict):
                raise JournalConflict("COMMUNICATION_EVENT_INVALID")
            # Immutable artifacts are written before the atomic notebook commit.
            # Interrupted preparation can leave an unreachable artifact only.
            notices.append(
                EventNotice(
                    scope=self.scope,
                    id=self.scope.identity("native-event", str(event["id"])),
                    native_id=event["id"],
                    content=self.artifacts.put(self.scope, event),
                )
            )

        def enqueue(db):
            self.check(db)
            for event, notice in zip(events, notices, strict=True):
                encoded = canonical(notice.model_dump(mode="json"))
                prior = db.execute(
                    select(notifications.c.notice).where(notifications.c.id == notice.id)
                ).scalar_one_or_none()
                if prior is not None and prior != encoded:
                    raise JournalConflict("COMMUNICATION_EVENT_CHANGED")
                eligible = event["wake"] and not (event["kind"] == "trade" and event["body"].get("mode") == "NATIVE_AI")
                db.execute(
                    insert(notifications)
                    .values(
                        id=notice.id,
                        scope=canonical(self.scope.model_dump()),
                        source=self.scope.epoch + ":" + str(event["id"]),
                        notice=encoded,
                        notify=int(eligible),
                        signaled=0,
                        handled=int(not eligible),
                    )
                    .prefix_with("OR IGNORE")
                )

        self.memory.ingest(self.scope.epoch, events, cursor, after_ingest=enqueue)

    def pending(self):
        with self.memory.transaction() as db:
            self.check(db)
            rows = db.execute(
                select(notifications.c.notice).where(
                    notifications.c.scope == canonical(self.scope.model_dump()),
                    notifications.c.notify == 1,
                    notifications.c.signaled == 0,
                )
            ).scalars()
            return sorted([EventNotice.model_validate_json(row) for row in rows], key=lambda notice: notice.native_id)

    def reset_unfinished(self):
        """Reassign unfinished work only after the old controller has been fenced."""
        with self.memory.transaction() as db:
            self.check(db)
            db.execute(
                update(notifications)
                .where(notifications.c.scope == canonical(self.scope.model_dump()), notifications.c.handled == 0)
                .values(job=None, signaled=0)
            )

    def row(self, db, notice):
        self.check(db)
        if notice.scope != self.scope:
            raise JournalConflict("COMMUNICATION_SCOPE_CONFLICT")
        row = db.execute(select(notifications).where(notifications.c.id == notice.id)).mappings().one_or_none()
        if row is None or json.loads(row["notice"]) != notice.model_dump(mode="json"):
            raise JournalConflict("COMMUNICATION_NOTICE_CONFLICT")
        return row

    def signaled(self, notice):
        with self.memory.transaction() as db:
            self.row(db, notice)
            db.execute(update(notifications).where(notifications.c.id == notice.id).values(signaled=1))

    def assign(self, notices, job):
        with self.memory.transaction() as db:
            pending = []
            for notice in notices:
                row = self.row(db, notice)
                if row["handled"]:
                    continue
                if row["job"] not in {None, job}:
                    raise JournalConflict("COMMUNICATION_EVENT_ALREADY_ASSIGNED")
                db.execute(update(notifications).where(notifications.c.id == notice.id).values(job=job))
                pending.append(notice)
            return pending

    def reopen(self, notices, job):
        """A confirmed handoff releases only this settled batch's assignments."""
        with self.memory.transaction() as db:
            for notice in notices:
                row = self.row(db, notice)
                if not row["handled"] and row["job"] == job:
                    db.execute(
                        update(notifications).where(notifications.c.id == notice.id).values(job=None, signaled=0)
                    )

    def snapshot(self, notices):
        with self.memory.transaction() as db:
            sources = [self.row(db, notice)["source"] for notice in notices]
        selected = [notice.native_id for notice in notices]
        snapshot = self.memory.snapshot("communication", event_sources=sources)
        snapshot["communicationEventIds"] = selected
        return snapshot

    def finish(self, notices, job):
        with self.memory.transaction() as db:
            self.check(db)
            completed = db.execute(select(domain.jobs.c.status).where(domain.jobs.c.id == job)).scalar_one_or_none()
            if completed != "COMPLETED":
                raise JournalConflict("COMMUNICATION_JOB_UNFINISHED")
            for notice in notices:
                row = self.row(db, notice)
                if row["job"] != job:
                    raise JournalConflict("COMMUNICATION_JOB_BINDING_CONFLICT")
                db.execute(update(notifications).where(notifications.c.id == notice.id).values(handled=1, signaled=1))
