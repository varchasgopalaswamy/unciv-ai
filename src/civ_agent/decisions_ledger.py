"""Durable broker accounting. This database must never enter a game checkpoint."""

import fcntl
import hashlib
import json
import os
import secrets
import time
from collections.abc import Mapping
from decimal import Decimal
from pathlib import Path
from typing import Protocol

from sqlalchemy import Column, Integer, MetaData, String, Table, UniqueConstraint, func, insert, or_, select

from civ_agent.decisions_backends import BACKENDS
from civ_agent.decisions_policy import DecisionsSettings
from civ_agent.native_broker_contracts import CloseNativeBrokerEpoch, NativeBrokerEpochClosed, NativeBrokerScope
from civ_agent.orchestration.contracts import NativeAuxiliaryTask
from civ_agent.protocol import JsonObject
from civ_agent.sqlite_database import sqlite_engine
from civ_agent.tls import private_directory, regular_file, write_private_json

# Verified 2026-10-14 against https://developers.openai.com/api/docs/guides/decisions
# Standard input $0.10 / 1M tokens, no output charge. Regional/long-context
# multipliers and tokenizer differences mean this is a LOCAL ESTIMATED ceiling.
RATE_VERSION = "decisions-backends-v1"
NANODOLLARS_PER_INPUT_TOKEN = 100
metadata = MetaData()
authority_table = Table(
    "authority", metadata, Column("key", String, primary_key=True), Column("value", String, nullable=False)
)
accounts = Table(
    "accounts",
    metadata,
    Column("principal", String, primary_key=True),
    Column("game", String, nullable=False),
    Column("player", String, nullable=False),
    Column("ceiling", Integer, nullable=False),
    Column("settings", String, nullable=False),
    UniqueConstraint("game", "player"),
)
requests = Table(
    "requests",
    metadata,
    Column("principal", String, primary_key=True),
    Column("id", String, primary_key=True),
    Column("fingerprint", String, nullable=False),
    Column("reserved", Integer, nullable=False),
    Column("spent", Integer, nullable=False),
    Column("input_tokens", Integer),
    Column("result", String),
    Column("rate_version", String, nullable=False),
    Column("created_ms", Integer, nullable=False),
)
routing_requests = Table(
    "routing_requests",
    metadata,
    Column("principal", String, primary_key=True),
    Column("id", String, primary_key=True),
    Column("operation_id", String, nullable=False),
    Column("fingerprint", String, nullable=False),
    Column("result", String, nullable=False),
    Column("input_tokens", Integer),
    Column("spent", Integer, nullable=False),
    Column("rate_version", String, nullable=False),
    Column("created_ms", Integer, nullable=False),
    UniqueConstraint("principal", "operation_id"),
)
native_owners = Table(
    "native_owners",
    metadata,
    Column("principal", String, primary_key=True),
    Column("game", String, nullable=False),
    Column("player", String, nullable=False),
    UniqueConstraint("game", "player"),
)
epoch_fences = Table(
    "native_epoch_fences",
    metadata,
    Column("principal", String, primary_key=True),
    Column("epoch", String, primary_key=True),
    Column("owner", String, nullable=False),
)


class NativeBrokerEpochFenced(ValueError):
    pass


class NativeAuxiliaryTaskUnavailable(ValueError):
    """No new reservation; the original logical allowance is not available."""

    def __init__(self, reason):
        self.reason = reason
        super().__init__(reason)


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def fingerprint(value):
    return hashlib.sha256(canonical(value).encode("utf-8")).hexdigest()


def estimate_tokens(body):
    """One token per serialized UTF-8 byte, plus framing and per-question headroom."""
    return len(canonical(body).encode("utf-8")) + 2048 + 128 * len(body["questions"])


class PaidCallPricing(Protocol):
    """Frozen adapter; the ledger owns admission and settlement, not rate discovery."""

    @property
    def rate_version(self) -> str: ...

    def reservation(self, body: JsonObject) -> int: ...

    def charge(self, result: JsonObject) -> tuple[int, int] | None:
        """Return (nanodollars, input tokens), or None for unmeasured usage."""
        ...

    def provenance(self) -> Mapping[str, object]: ...


class DecisionsLedger:
    def __init__(self, root, *, pricing: PaidCallPricing | None = None):
        self.pricing = pricing
        self.root = private_directory(Path(root))
        # WAL setup precedes SQLite's transaction lock. Serialize it together
        # with publishing the authority file, across threads and processes.
        descriptor = os.open(self.root / ".decisions-init.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            self._initialize()
        finally:
            os.close(descriptor)

    def _initialize(self):
        authority_file, self.path = self.root / "decisions-authority.json", self.root / "decisions.sqlite3"
        if authority_file.exists():
            regular_file(authority_file, private=True)
            self.authority = json.loads(authority_file.read_text())["authority"]
            pinned_authority = self.authority
            if not self.path.is_file():
                raise ValueError("Decisions accounting ledger missing for the pinned authority")
        else:
            pinned_authority = None
            self.authority = secrets.token_hex(32)
        if self.path.is_symlink():
            raise ValueError("Unsafe Decisions ledger path")
        self.engine = sqlite_engine(self.path, wal=True)
        with self.engine.begin() as db:
            metadata.create_all(db)
            recorded = db.scalar(select(authority_table.c.value).where(authority_table.c.key == "authority"))
            if recorded is None:
                db.execute(insert(authority_table).values(key="authority", value=self.authority))
            elif pinned_authority is not None and recorded != pinned_authority:
                raise ValueError("Decisions accounting authority does not match its ledger")
            else:
                self.authority = recorded
        self.path.chmod(0o600)
        if authority_file.exists():
            regular_file(authority_file, private=True)
            if json.loads(authority_file.read_text())["authority"] != self.authority:
                raise ValueError("Decisions accounting authority does not match its ledger")
        else:
            write_private_json(authority_file, {"authority": self.authority})

    def register(self, principal, game, player, settings, *, native_player=None):
        settings = DecisionsSettings.model_validate(settings)
        ceiling = int(Decimal(str(settings.additional_decisions_budget_usd)) * 1_000_000_000)
        saved = dict(
            principal=principal,
            game=game,
            player=str(player),
            ceiling=ceiling,
            settings=canonical(settings.model_dump()),
        )
        with self.engine.begin() as db:
            identity = db.scalar(
                select(accounts.c.principal).where(accounts.c.game == game, accounts.c.player == str(player))
            )
            if identity is not None and identity != principal:
                raise ValueError("Decisions immutable game/player account conflict: cannot change principal")
            prior = db.execute(select(accounts).where(accounts.c.principal == principal)).mappings().first()
            if prior is not None:
                if dict(prior) != saved:
                    raise ValueError("Decisions immutable account or policy conflict")
            if prior is None:
                db.execute(insert(accounts).values(**saved))
            if native_player is not None:
                scope = NativeBrokerScope(game=game, player=native_player, epoch="registration")
                owner = dict(principal=principal, game=scope.game, player=scope.player)
                existing = (
                    db.execute(select(native_owners).where(native_owners.c.principal == principal)).mappings().first()
                )
                occupied = db.scalar(
                    select(native_owners.c.principal).where(
                        native_owners.c.game == game, native_owners.c.player == native_player
                    )
                )
                # Legacy calls do not participate in native epoch admission.
                # Promoting their credential could strand an already accepted
                # unscoped provider call outside the native drain lock.
                if prior is not None and existing is None:
                    raise ValueError("Native Decisions cannot promote an existing legacy account")
                if existing is not None and dict(existing) != owner or occupied is not None and occupied != principal:
                    raise ValueError("Native Decisions immutable owner conflict")
                if existing is None:
                    db.execute(insert(native_owners).values(**owner))

    def native_owner(self, principal):
        with self.engine.begin() as db:
            return db.execute(select(native_owners).where(native_owners.c.principal == principal)).mappings().first()

    @staticmethod
    def _native_scope(db, principal, scope: NativeBrokerScope | None):
        owner = db.execute(select(native_owners).where(native_owners.c.principal == principal)).mappings().first()
        if scope is None:
            if owner is not None:
                raise ValueError("Native Decisions scope required")
        elif owner is None or (owner["game"], owner["player"]) != (scope.game, scope.player):
            raise ValueError("Native Decisions owner conflict")

    @classmethod
    def _native_open(cls, db, principal, scope: NativeBrokerScope | None):
        cls._native_scope(db, principal, scope)
        if (
            scope is not None
            and db.scalar(
                select(epoch_fences.c.owner).where(
                    epoch_fences.c.principal == principal, epoch_fences.c.epoch == scope.epoch
                )
            )
            is not None
        ):
            raise NativeBrokerEpochFenced("Native Decisions source epoch closed")

    def native_dispatch_open(self, principal, scope: NativeBrokerScope):
        with self.engine.begin() as db:
            self._native_open(db, principal, scope)

    def check_native_scope(self, principal, scope: NativeBrokerScope):
        with self.engine.begin() as db:
            self._native_scope(db, principal, scope)

    @staticmethod
    def _native_task_open(db, principal, scope, task):
        if scope is None:
            if task is not None:
                raise ValueError("Native auxiliary task requires native owner")
            return
        if task is None:
            raise NativeAuxiliaryTaskUnavailable("task_attribution_unavailable")
        task = NativeAuxiliaryTask.model_validate_json(task.model_dump_json())
        parents = {}
        for encoded in db.scalars(select(epoch_fences.c.owner).where(epoch_fences.c.principal == principal)):
            owner = json.loads(encoded)
            if owner["game"] != scope.game:
                raise ValueError("Native Decisions lineage owner conflict")
            target, source = owner["target_epoch"], owner["source_epoch"]
            if target in parents and parents[target] != source:
                raise NativeAuxiliaryTaskUnavailable("task_attribution_unavailable")
            parents[target] = source
        epochs, current = set(), scope.epoch
        while current not in epochs:
            epochs.add(current)
            if current not in parents:
                break
            current = parents[current]
        else:
            raise NativeAuxiliaryTaskUnavailable("task_attribution_unavailable")
        # Existing immutable receipts are the attempt authority, including
        # unknown/denied reservations. Never copy answers or reset counters.
        for table in (requests, routing_requests):
            encoded_rows = db.scalars(
                select(table.c.result).where(
                    table.c.principal == principal,
                    func.json_extract(table.c.result, "$.nativeScope.epoch").in_(epochs),
                )
            )
            for encoded in encoded_rows:
                original = json.loads(encoded)
                prior = original.get("nativeTask")
                if prior is None:
                    # An older receipt cannot be assigned a guessed task from
                    # its context, response, text similarity or checkpoint age.
                    raise NativeAuxiliaryTaskUnavailable("task_attribution_unavailable")
                if NativeAuxiliaryTask.model_validate(prior) == task:
                    raise NativeAuxiliaryTaskUnavailable("task_allowance_exhausted")

    def close_native_epoch(self, principal, command: CloseNativeBrokerEpoch):
        command = CloseNativeBrokerEpoch.model_validate_json(command.model_dump_json())
        encoded = canonical(command.owner.model_dump(mode="json"))
        with self.engine.begin() as db:
            self._native_scope(db, principal, command.scope)
            prior = db.scalar(
                select(epoch_fences.c.owner).where(
                    epoch_fences.c.principal == principal, epoch_fences.c.epoch == command.scope.epoch
                )
            )
            if prior is not None and prior != encoded:
                raise ValueError("Native Decisions retirement owner conflict")
            if prior is None:
                db.execute(insert(epoch_fences).values(principal=principal, epoch=command.scope.epoch, owner=encoded))

    def native_epoch_status(self, principal, command: CloseNativeBrokerEpoch) -> NativeBrokerEpochClosed:
        scope = command.scope
        with self.engine.begin() as db:
            self._native_scope(db, principal, scope)
            owner = db.scalar(
                select(epoch_fences.c.owner).where(
                    epoch_fences.c.principal == principal, epoch_fences.c.epoch == scope.epoch
                )
            )
            if owner != canonical(command.owner.model_dump(mode="json")):
                raise ValueError("Native Decisions retirement owner conflict")
            advisory = (
                db.execute(
                    select(requests.c.result, requests.c.reserved).where(
                        requests.c.principal == principal,
                        func.json_extract(requests.c.result, "$.nativeScope.epoch") == scope.epoch,
                    )
                )
                .mappings()
                .all()
            )
            routing = (
                db.execute(
                    select(routing_requests.c.result, routing_requests.c.input_tokens).where(
                        routing_requests.c.principal == principal,
                        func.json_extract(routing_requests.c.result, "$.nativeScope.epoch") == scope.epoch,
                    )
                )
                .mappings()
                .all()
            )
        return NativeBrokerEpochClosed(
            scope=scope,
            owner_sha256=fingerprint(command.owner.model_dump(mode="json")),
            advisory_requests=len(advisory),
            advisory_unknown_completions=sum(
                row["reserved"] > 0 or bool(json.loads(row["result"]).get("pending")) for row in advisory
            ),
            routing_requests=len(routing),
            routing_unknown_completions=sum(bool(json.loads(row["result"]).get("pending")) for row in routing),
            routing_unknown_usage=sum(row["input_tokens"] is None for row in routing),
            unattributed_requests=sum("nativeTask" not in json.loads(row["result"]) for row in [*advisory, *routing]),
        )

    def settings(self, principal):
        with self.engine.begin() as db:
            value = db.scalar(select(accounts.c.settings).where(accounts.c.principal == principal))
            if value is None:
                raise ValueError("Decisions account not registered by operator")
            saved = json.loads(value)
            if not isinstance(saved, dict) or set(saved) != set(DecisionsSettings.model_fields):
                raise ValueError("Current Decisions account settings are required")
            return DecisionsSettings.model_validate(saved)

    def player(self, principal):
        with self.engine.begin() as db:
            value = db.scalar(select(accounts.c.player).where(accounts.c.principal == principal))
            if value is None:
                raise ValueError("Decisions account not registered by operator")
            return value

    @staticmethod
    def _totals(db, principal):
        rows = db.execute(select(requests).where(requests.c.principal == principal)).mappings().all()
        return {
            "spentNanodollars": sum(r["spent"] for r in rows),
            "reservedNanodollars": sum(r["reserved"] for r in rows),
            "requests": len(rows),
            "inputTokens": sum(r["input_tokens"] or 0 for r in rows),
            "unknownCompletions": sum(
                r["result"] is None or r["reserved"] > 0 or bool(json.loads(r["result"]).get("pending")) for r in rows
            ),
        }

    def status(self, principal):
        with self.engine.begin() as db:
            ceiling = db.scalar(select(accounts.c.ceiling).where(accounts.c.principal == principal))
            return {
                "authority": self.authority,
                "rateVersion": self.pricing.rate_version if self.pricing is not None else RATE_VERSION,
                "limitKind": "local_estimated_spending_ceiling",
                "ceilingNanodollars": ceiling,
                **self._totals(db, principal),
            }

    def lookup(self, principal, request_id):
        """Read a receipt without reserving funds or dispatching a provider call."""
        with self.engine.begin() as db:
            row = db.execute(
                select(requests.c.result).where(requests.c.principal == principal, requests.c.id == request_id)
            ).first()
            if row is None:
                return {"answers": [], "fallback": "not_submitted", "requestId": request_id}
            return (
                json.loads(row[0])
                if row[0]
                else {"answers": [], "fallback": "completion_unknown", "requestId": request_id}
            )

    def reserve(
        self,
        principal,
        request_id,
        evidence_hash,
        body,
        *,
        pending_result=None,
        native_scope: NativeBrokerScope | None = None,
        native_task: NativeAuxiliaryTask | None = None,
    ):
        """BEGIN IMMEDIATE serializes reservations across broker processes too.

        A reservation without a durable result has unknown completion. Never
        dispatch it again, even after a crash, lost reply, token change or rewind.
        """
        with self.engine.begin() as db:
            self._native_scope(db, principal, native_scope)
            prior = (
                db.execute(select(requests).where(requests.c.principal == principal, requests.c.id == request_id))
                .mappings()
                .first()
            )
            if prior:
                if prior["fingerprint"] != evidence_hash:
                    raise ValueError("Decisions request ID conflict")
                return (
                    json.loads(prior["result"])
                    if prior["result"]
                    else {"answers": [], "fallback": "completion_unknown", "requestId": request_id}
                )
            self._native_open(db, principal, native_scope)
            self._native_task_open(db, principal, native_scope, native_task)
            ceiling = db.scalar(select(accounts.c.ceiling).where(accounts.c.principal == principal))
            if ceiling is None:
                raise ValueError("Decisions account not registered by operator")
            totals = self._totals(db, principal)
            if self.pricing is None:
                spec = next(spec for spec in BACKENDS.values() if spec.model == body["model"])
                reservation = estimate_tokens(body) * spec.nanodollars_per_input_token
                rate_version = spec.rate_version
            else:
                reservation = self.pricing.reservation(body)
                rate_version = self.pricing.rate_version
                pending_result = {**(pending_result or {}), "pricing": dict(self.pricing.provenance())}
            denied = reservation > 0 and (
                ceiling == 0 or totals["spentNanodollars"] + totals["reservedNanodollars"] + reservation > ceiling
            )
            result = (
                {**(pending_result or {}), "answers": [], "fallback": "budget_exhausted", "requestId": request_id}
                if denied
                else None
            )
            pending = {**pending_result, "pending": True} if pending_result is not None else None
            if native_scope is not None:
                assert native_task is not None
                admission = {
                    "nativeScope": native_scope.model_dump(mode="json"),
                    "nativeTask": native_task.model_dump(mode="json"),
                }
                pending = {**(pending or {}), **admission, "pending": True}
                if result is not None:
                    result.update(admission)
            db.execute(
                insert(requests).values(
                    principal=principal,
                    id=request_id,
                    fingerprint=evidence_hash,
                    reserved=0 if denied else reservation,
                    spent=0,
                    input_tokens=None,
                    result=canonical(result or pending) if result or pending else None,
                    rate_version=rate_version,
                    created_ms=int(time.time() * 1000),
                )
            )
            return result

    def complete(self, principal, request_id, result):
        usage = result.get("usage", {})
        tokens = usage.get("input_tokens") if isinstance(usage, dict) else None
        valid = type(tokens) is int and 0 <= tokens <= 100_000_000
        values: JsonObject = {"result": canonical(result)}
        # No validated usage => retain the reservation, including refusals and
        # transport failures. Returned results are still cached, never retried.
        with self.engine.begin() as db:
            original = (
                db.execute(
                    select(requests.c.rate_version, requests.c.result).where(
                        requests.c.principal == principal, requests.c.id == request_id
                    )
                )
                .mappings()
                .one()
            )
            rate = original["rate_version"]
            original_result = json.loads(original["result"]) if original["result"] else {}
            admission = {
                key: original_result[key] for key in ("nativeScope", "nativeTask", "pricing") if key in original_result
            }
            values["result"] = canonical({**result, **admission})
            if self.pricing is not None:
                if rate != self.pricing.rate_version or original_result.get("pricing") != self.pricing.provenance():
                    raise ValueError("Paid call frozen pricing conflict")
                charge = self.pricing.charge(result)
                if charge is not None:
                    spent, input_tokens = charge
                    values.update(reserved=0, spent=spent, input_tokens=input_tokens)
            elif valid:
                per_token = next(
                    spec.nanodollars_per_input_token for spec in BACKENDS.values() if spec.rate_version == rate
                )
                values.update(reserved=0, spent=tokens * per_token, input_tokens=tokens)
            db.execute(
                requests.update()
                .where(
                    requests.c.principal == principal,
                    requests.c.id == request_id,
                    or_(requests.c.result.is_(None), func.json_extract(requests.c.result, "$.pending") == 1),
                )
                .values(**values)
            )

    def routing_lookup(self, principal, request_id):
        """Routing receipts never admit work or spend the advisory allowance."""
        with self.engine.begin() as db:
            value = db.scalar(
                select(routing_requests.c.result).where(
                    routing_requests.c.principal == principal, routing_requests.c.id == request_id
                )
            )
        return json.loads(value) if value is not None else {"requestId": request_id, "fallback": "not_submitted"}

    def routing_reserve(
        self,
        principal,
        operation_id,
        request_id,
        body,
        pending,
        *,
        native_scope: NativeBrokerScope | None = None,
        native_task: NativeAuxiliaryTask | None = None,
    ):
        """Persist dispatch intent; an unresolved attempt must never dispatch twice.

        Routing historically runs independently of the optional advisory ceiling.
        Its observed usage has a separate ledger, retaining that admission policy.
        """
        body_hash = fingerprint(body)
        with self.engine.begin() as db:
            self._native_scope(db, principal, native_scope)
            prior = (
                db.execute(
                    select(routing_requests).where(
                        routing_requests.c.principal == principal,
                        or_(routing_requests.c.id == request_id, routing_requests.c.operation_id == operation_id),
                    )
                )
                .mappings()
                .first()
            )
            if prior is not None:
                if (
                    prior["id"] != request_id
                    or prior["operation_id"] != operation_id
                    or prior["fingerprint"] != body_hash
                ):
                    self._native_open(db, principal, native_scope)
                    self._native_task_open(db, principal, native_scope, native_task)
                    raise ValueError("Routing immutable operation conflict")
                return json.loads(prior["result"])
            self._native_open(db, principal, native_scope)
            self._native_task_open(db, principal, native_scope, native_task)
            if db.scalar(select(accounts.c.principal).where(accounts.c.principal == principal)) is None:
                raise ValueError("Decisions account not registered by operator")
            spec = next(spec for spec in BACKENDS.values() if spec.model == body["model"])
            admission = {}
            if native_scope is not None:
                assert native_task is not None
                admission = {
                    "nativeScope": native_scope.model_dump(mode="json"),
                    "nativeTask": native_task.model_dump(mode="json"),
                }
            db.execute(
                insert(routing_requests).values(
                    principal=principal,
                    id=request_id,
                    operation_id=operation_id,
                    fingerprint=body_hash,
                    result=canonical(
                        {
                            **pending,
                            "pending": True,
                            **admission,
                        }
                    ),
                    input_tokens=None,
                    spent=0,
                    rate_version=spec.rate_version,
                    created_ms=int(time.time() * 1000),
                )
            )
        return None

    def routing_complete(self, principal, request_id, result):
        usage = result.get("usage", {})
        tokens = usage.get("input_tokens") if isinstance(usage, dict) else None
        values: JsonObject = {"result": canonical(result)}
        with self.engine.begin() as db:
            original = (
                db.execute(
                    select(routing_requests.c.rate_version, routing_requests.c.result).where(
                        routing_requests.c.principal == principal, routing_requests.c.id == request_id
                    )
                )
                .mappings()
                .first()
            )
            if original is None:
                raise ValueError("Routing dispatch intent required")
            rate = original["rate_version"]
            original_result = json.loads(original["result"])
            admission = {key: original_result[key] for key in ("nativeScope", "nativeTask") if key in original_result}
            values["result"] = canonical({**result, **admission})
            if type(tokens) is int and 0 <= tokens <= 100_000_000:
                per_token = next(
                    spec.nanodollars_per_input_token for spec in BACKENDS.values() if spec.rate_version == rate
                )
                values.update(input_tokens=tokens, spent=tokens * per_token)
            db.execute(
                routing_requests.update()
                .where(
                    routing_requests.c.principal == principal,
                    routing_requests.c.id == request_id,
                    func.json_extract(routing_requests.c.result, "$.pending") == 1,
                )
                .values(**values)
            )

    def routing_status(self, principal):
        with self.engine.begin() as db:
            rows = (
                db.execute(select(routing_requests).where(routing_requests.c.principal == principal)).mappings().all()
            )
        return {
            "authority": self.authority,
            "rateVersion": RATE_VERSION,
            "requests": len(rows),
            "inputTokens": sum(row["input_tokens"] or 0 for row in rows),
            "spentNanodollars": sum(row["spent"] for row in rows),
            "unknownCompletions": sum(bool(json.loads(row["result"]).get("pending")) for row in rows),
            "unknownUsage": sum(row["input_tokens"] is None for row in rows),
        }
