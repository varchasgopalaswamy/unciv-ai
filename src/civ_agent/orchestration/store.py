"""Private immutable artifacts, fenced service receipts and transactional notifications.

This store records immutable domain publications and their ownership.
An unfinished effect is uncertain, never permission to execute it again.
"""

import json
import os
from contextlib import contextmanager
from pathlib import Path

from sqlalchemy import Column, Integer, MetaData, String, Table, Text, func, insert, select, update

from civ_agent.orchestration.contracts import Reference, Scope, canonical, digest
from civ_agent.orchestration.recovery_budgets import GenerationBudgetTransfer
from civ_agent.protocol import JsonObject
from civ_agent.sqlite_database import sqlite_engine
from civ_agent.tls import private_directory, regular_file

metadata = MetaData()
ownership = Table(
    "ownership",
    metadata,
    Column("singleton", Integer, primary_key=True),
    Column("player", String, nullable=False),
    Column("version", Integer, nullable=False),
)
epochs = Table("epochs", metadata, Column("game", String, primary_key=True), Column("epoch", String, nullable=False))
artifacts = Table(
    "artifacts", metadata, Column("sha256", String, primary_key=True), Column("body", Text, nullable=False)
)
artifact_scopes = Table(
    "artifact_scopes",
    metadata,
    Column("scope", String, primary_key=True),
    Column("sha256", String, primary_key=True),
)
calls = Table(
    "calls",
    metadata,
    Column("id", String, primary_key=True),
    Column("scope", Text, nullable=False),
    Column("request", String, nullable=False),
    Column("result", String),
)
generation_attempts = Table(
    "generation_attempts",
    metadata,
    Column("id", String, primary_key=True),
    Column("scope", String, nullable=False),
)
outbox = Table(
    "outbox",
    metadata,
    Column("id", String, primary_key=True),
    Column("scope", Text, nullable=False),
    Column("body", Text, nullable=False),
    Column("delivered", Integer, nullable=False, default=0),
)


class JournalConflict(ValueError):
    """A stale epoch, changed request, or uncertain effect requires reconciliation."""


class WorkflowStore:
    def __init__(self, root: Path, *, player: str):
        self.player = player
        self.root = root = private_directory(root)
        path = root / "workflow.sqlite3"
        try:
            descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            pass
        else:
            os.close(descriptor)
        regular_file(path, private=True)
        # DELETE journaling avoids creating sidecar files under a permissive umask.
        self.engine = sqlite_engine(path)
        metadata.create_all(self.engine)
        with self.engine.begin() as db:
            db.execute(insert(ownership).values(singleton=1, player=player, version=1).prefix_with("OR IGNORE"))
            owner = db.execute(select(ownership).where(ownership.c.singleton == 1)).mappings().one()
            if owner["player"] != player or owner["version"] != 1:
                raise JournalConflict("WORKFLOW_STORE_INCOMPATIBLE")

    def close(self):
        self.engine.dispose()

    def bind_epoch(self, game: str, epoch: str, *, previous: str | None = None):
        """Operator operation. Advancing an epoch fences every old worker call."""
        with self.engine.begin() as db:
            current = db.scalar(select(epochs.c.epoch).where(epochs.c.game == game))
            if current == epoch:
                return
            if current != previous:
                raise JournalConflict("WORKFLOW_EPOCH_CONFLICT")
            if current is None:
                db.execute(insert(epochs).values(game=game, epoch=epoch))
            else:
                db.execute(update(epochs).where(epochs.c.game == game).values(epoch=epoch))

    def _check(self, db, scope: Scope):
        if scope.player != self.player:
            raise JournalConflict("WORKFLOW_PLAYER_CONFLICT")
        if db.scalar(select(epochs.c.epoch).where(epochs.c.game == scope.game)) != scope.epoch:
            raise JournalConflict("WORKFLOW_EPOCH_CONFLICT")

    @contextmanager
    def transaction(self, scope: Scope):
        with self.engine.begin() as db:
            self._check(db, scope)
            yield db

    @staticmethod
    def _put(db, scope: Scope, value: object) -> Reference:
        body = canonical(value)
        reference = Reference(sha256=digest(value))
        db.execute(insert(artifacts).values(sha256=reference.sha256, body=body).prefix_with("OR IGNORE"))
        db.execute(
            insert(artifact_scopes)
            .values(
                scope=digest(scope.model_dump()),
                sha256=reference.sha256,
            )
            .prefix_with("OR IGNORE")
        )
        return reference

    def put(self, scope: Scope, value: object) -> Reference:
        with self.transaction(scope) as db:
            return self._put(db, scope, value)

    def get(self, scope: Scope, reference: Reference):
        with self.transaction(scope) as db:
            body = db.scalar(
                select(artifacts.c.body)
                .join(
                    artifact_scopes,
                    artifacts.c.sha256 == artifact_scopes.c.sha256,
                )
                .where(artifacts.c.sha256 == reference.sha256, artifact_scopes.c.scope == digest(scope.model_dump()))
            )
            if body is None:
                raise JournalConflict("WORKFLOW_ARTIFACT_MISSING")
            value = json.loads(body)
            if digest(value) != reference.sha256:
                raise JournalConflict("WORKFLOW_ARTIFACT_CORRUPT")
            return value

    def reserve(
        self, scope: Scope, key: str, request: object, *, generation_limit: int | None = None
    ) -> tuple[bool, Reference | None]:
        """Return (new reservation, recorded result); (False, None) is uncertain."""
        identity = scope.identity("call", key)
        fingerprint = digest(request)
        with self.transaction(scope) as db:
            row = db.execute(select(calls).where(calls.c.id == identity)).mappings().first()
            if row:
                if row["request"] != fingerprint or row["scope"] != canonical(scope.model_dump()):
                    raise JournalConflict("WORKFLOW_REQUEST_CHANGED")
                return False, Reference(sha256=row["result"]) if row["result"] else None
            if generation_limit is not None:
                used = db.scalar(
                    select(func.count())
                    .select_from(generation_attempts)
                    .where(
                        generation_attempts.c.scope == canonical(scope.model_dump()),
                    )
                )
                assert used is not None
                if used >= generation_limit:
                    raise JournalConflict("MODEL_REQUEST_LIMIT")
                db.execute(insert(generation_attempts).values(id=identity, scope=canonical(scope.model_dump())))
            db.execute(insert(calls).values(id=identity, scope=canonical(scope.model_dump()), request=fingerprint))
            return True, None

    @staticmethod
    def _generations(db, scope: Scope) -> list[str]:
        return list(
            db.scalars(
                select(generation_attempts.c.id)
                .where(generation_attempts.c.scope == canonical(scope.model_dump()))
                .order_by(generation_attempts.c.id)
            )
        )

    def prepare_generation_transfer(self, source: Scope, target: Scope) -> GenerationBudgetTransfer:
        """Trusted rewind preparation: include completed and uncertain reservations."""
        with self.transaction(source) as db:
            return GenerationBudgetTransfer(source=source, target=target, generation_ids=self._generations(db, source))

    def install_generation_transfer(self, transfer: GenerationBudgetTransfer) -> int:
        """Carry debits in the existing budget table, before the new scope is admitted.

        The recovery coordinator owns the quiescence/admission permit. This
        method never copies responses, permits an uncertain generation again,
        advances the epoch, or changes the source graph's accounting.
        """
        transfer = GenerationBudgetTransfer.model_validate_json(transfer.model_dump_json())
        source, target = transfer.source, transfer.target
        with self.engine.begin() as db:
            current = db.scalar(select(epochs.c.epoch).where(epochs.c.game == source.game))
            if current not in {source.epoch, target.epoch}:
                raise JournalConflict("RECOVERY_BUDGET_EPOCH_CONFLICT")
            self._check(db, source if current == source.epoch else target)
            if self._generations(db, source) != transfer.generation_ids:
                raise JournalConflict("RECOVERY_BUDGET_SOURCE_CHANGED")
            encoded = canonical(target.model_dump())
            inherited = transfer.inherited_ids
            proof = {
                "kind": "generation_budget_transfer",
                "version": 1,
                "transfer": transfer.model_dump(mode="json"),
            }
            recorded = list(
                db.execute(
                    select(artifacts.c.sha256, artifacts.c.body)
                    .join(artifact_scopes, artifacts.c.sha256 == artifact_scopes.c.sha256)
                    .where(
                        artifact_scopes.c.scope == digest(target.model_dump()),
                        func.json_extract(artifacts.c.body, "$.kind") == proof["kind"],
                    )
                ).mappings()
            )
            target_generations = set(self._generations(db, target))
            if recorded:
                if len(recorded) != 1:
                    raise JournalConflict("RECOVERY_BUDGET_TARGET_CONFLICT")
                row = recorded[0]
                if row["sha256"] != digest(proof) or row["body"] != canonical(proof):
                    raise JournalConflict("RECOVERY_BUDGET_TARGET_CONFLICT")
                if not inherited <= target_generations:
                    raise JournalConflict("RECOVERY_BUDGET_TARGET_CONFLICT")
                # An exact committed installation remains read-only after new
                # target requests. Missing inherited debits are never repaired.
                return len(inherited)
            # Startup reads and effort fallback receipts may exist before a
            # role's allowance is bound. Actual generation reservations, not
            # unrelated calls, establish paid model admission. Older transfers
            # without this exact proof are never inferred after admission.
            if target_generations:
                raise JournalConflict("RECOVERY_BUDGET_TARGET_ADMITTED")
            if (
                db.scalar(select(generation_attempts.c.id).where(generation_attempts.c.id.in_(inherited)).limit(1))
                is not None
            ):
                raise JournalConflict("RECOVERY_BUDGET_TARGET_CONFLICT")
            for identity in sorted(inherited):
                db.execute(insert(generation_attempts).values(id=identity, scope=encoded).prefix_with("OR IGNORE"))
            self._put(db, target, proof)
            return len(inherited)

    def complete(self, scope: Scope, key: str, request: object, result: object) -> Reference:
        """Record a confirmed result and its outbox event in the same transaction."""
        identity = scope.identity("call", key)
        with self.transaction(scope) as db:
            row = db.execute(select(calls).where(calls.c.id == identity)).mappings().one()
            if row["request"] != digest(request) or row["scope"] != canonical(scope.model_dump()):
                raise JournalConflict("WORKFLOW_REQUEST_CHANGED")
            reference = self._put(db, scope, result)
            if row["result"] and row["result"] != reference.sha256:
                raise JournalConflict("WORKFLOW_RECEIPT_CHANGED")
            db.execute(update(calls).where(calls.c.id == identity).values(result=reference.sha256))
            self.enqueue(db, scope, identity, {"kind": "call_completed", "result": reference.model_dump()})
            return reference

    def receipt(self, scope: Scope, key: str):
        with self.transaction(scope) as db:
            result = db.scalar(select(calls.c.result).where(calls.c.id == scope.identity("call", key)))
        return self.get(scope, Reference(sha256=result)) if result is not None else None

    def enqueue(self, db, scope: Scope, event_id: str, body: JsonObject):
        self._check(db, scope)
        identity = scope.identity("event", event_id)
        prior = db.scalar(select(outbox.c.body).where(outbox.c.id == identity))
        encoded = canonical(body)
        if prior is not None and prior != encoded:
            raise JournalConflict("WORKFLOW_EVENT_CHANGED")
        if prior is None:
            db.execute(
                insert(outbox).values(id=identity, scope=canonical(scope.model_dump()), body=encoded, delivered=0)
            )

    def pending_events(self, scope: Scope) -> list[JsonObject]:
        with self.transaction(scope) as db:
            rows = db.execute(
                select(outbox)
                .where(
                    outbox.c.scope == canonical(scope.model_dump()),
                    outbox.c.delivered == 0,
                )
                .order_by(outbox.c.id)
            ).mappings()
            return [{"id": row["id"], "body": json.loads(row["body"])} for row in rows]

    def acknowledge(self, scope: Scope, event_id: str):
        with self.transaction(scope) as db:
            db.execute(
                update(outbox)
                .where(
                    outbox.c.id == event_id,
                    outbox.c.scope == canonical(scope.model_dump()),
                )
                .values(delivered=1)
            )
