"""Durable broker accounting. This database must never enter a game checkpoint."""

import fcntl
import hashlib
import json
import os
import secrets
import time
from decimal import Decimal
from pathlib import Path

from sqlalchemy import Column, Integer, MetaData, String, Table, UniqueConstraint, func, insert, or_, select

from civ_agent.decisions_policy import DecisionsSettings
from civ_agent.protocol import JsonObject
from civ_agent.sqlite_database import sqlite_engine
from civ_agent.tls import private_directory, regular_file, write_private_json

# Verified 2026-10-14 against https://developers.openai.com/api/docs/guides/decisions
# Standard input $0.10 / 1M tokens, no output charge. Regional/long-context
# multipliers and tokenizer differences mean this is a LOCAL ESTIMATED ceiling.
RATE_VERSION = "decisions-standard-2026-10-14-v1"
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


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def fingerprint(value):
    return hashlib.sha256(canonical(value).encode("utf-8")).hexdigest()


def estimate_tokens(body):
    """One token per serialized UTF-8 byte, plus framing and per-question headroom."""
    return len(canonical(body).encode("utf-8")) + 2048 + 128 * len(body["questions"])


class DecisionsLedger:
    def __init__(self, root):
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

    def register(self, principal, game, player, settings):
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
                # Additive settings with unchanged defaults must not invalidate
                # an existing accounting identity or reset its spending.
                normalized = dict(prior)
                normalized["settings"] = canonical(
                    DecisionsSettings.model_validate_json(prior["settings"]).model_dump()
                )
                if normalized != saved:
                    raise ValueError("Decisions immutable account or policy conflict")
            if prior is None:
                db.execute(insert(accounts).values(**saved))

    def settings(self, principal):
        with self.engine.begin() as db:
            value = db.scalar(select(accounts.c.settings).where(accounts.c.principal == principal))
            if value is None:
                raise ValueError("Decisions account not registered by operator")
            return DecisionsSettings.model_validate_json(value)

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
            "unknownCompletions": sum(r["result"] is None or r["reserved"] > 0 for r in rows),
        }

    def status(self, principal):
        with self.engine.begin() as db:
            ceiling = db.scalar(select(accounts.c.ceiling).where(accounts.c.principal == principal))
            return {
                "authority": self.authority,
                "rateVersion": RATE_VERSION,
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

    def reserve(self, principal, request_id, evidence_hash, body, *, pending_result=None):
        """BEGIN IMMEDIATE serializes reservations across broker processes too.

        A reservation without a durable result has unknown completion. Never
        dispatch it again, even after a crash, lost reply, token change or rewind.
        """
        with self.engine.begin() as db:
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
            ceiling = db.scalar(select(accounts.c.ceiling).where(accounts.c.principal == principal))
            if ceiling is None:
                raise ValueError("Decisions account not registered by operator")
            totals = self._totals(db, principal)
            reservation = estimate_tokens(body) * NANODOLLARS_PER_INPUT_TOKEN
            denied = ceiling == 0 or totals["spentNanodollars"] + totals["reservedNanodollars"] + reservation > ceiling
            result = (
                {**(pending_result or {}), "answers": [], "fallback": "budget_exhausted", "requestId": request_id}
                if denied
                else None
            )
            pending = {**pending_result, "pending": True} if pending_result is not None else None
            db.execute(
                insert(requests).values(
                    principal=principal,
                    id=request_id,
                    fingerprint=evidence_hash,
                    reserved=0 if denied else reservation,
                    spent=0,
                    input_tokens=None,
                    result=canonical(result or pending) if result or pending else None,
                    rate_version=RATE_VERSION,
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
        if valid:
            values.update(reserved=0, spent=tokens * NANODOLLARS_PER_INPUT_TOKEN, input_tokens=tokens)
        with self.engine.begin() as db:
            db.execute(
                requests.update()
                .where(
                    requests.c.principal == principal,
                    requests.c.id == request_id,
                    or_(requests.c.result.is_(None), func.json_extract(requests.c.result, "$.pending") == 1),
                )
                .values(**values)
            )
