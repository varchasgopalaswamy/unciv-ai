"""Durable provider usage tracking, with optional request limits for older pilots."""

import json
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pydantic import TypeAdapter
from pydantic_ai._model_request_attempts import AttemptStart, carried_attempts
from pydantic_ai.capabilities import AbstractCapability
from pydantic_ai.concurrency import AbstractConcurrencyLimiter
from pydantic_ai.exceptions import UsageLimitExceeded
from pydantic_ai.messages import ModelMessagesTypeAdapter
from pydantic_ai.models import ModelRequestParameters
from pydantic_ai.models.concurrency import ConcurrencyLimitedModel
from pydantic_ai.models.wrapper import WrapperModel
from pydantic_ai.usage import RequestUsage
from sqlalchemy import Column, Integer, MetaData, String, Table, func, insert, select, update

from civ_agent.orchestration.contracts import canonical, digest
from civ_agent.runtime.model_responses import provider_attempt_ordinal, retain_partial_attempt
from civ_agent.sqlite_database import sqlite_engine
from civ_agent.tls import private_directory

metadata = MetaData()
account = Table(
    "pilot_account",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("ceiling", Integer),
    Column("state", String, nullable=False),
)
attempts = Table(
    "pilot_attempts",
    metadata,
    Column("id", String, primary_key=True),
    Column("component", String, nullable=False),
    Column("status", String, nullable=False),
    Column("request_sha256", String, nullable=False),
    Column("usage", String),
    Column("response", String),
)


class PilotOutcomeUncertain(RuntimeError):
    pass


class PilotAdmissionRetained(RuntimeError):
    pass


def admission_limit(value: int) -> int:
    if type(value) is not int or value < 1:
        raise ValueError("PROVIDER_ADMISSION_LIMIT_MUST_BE_POSITIVE_INTEGER")
    return value


class ProviderAttemptAccount:
    def __init__(self, path: Path, *, ceiling=1000, pilot=True):
        if ceiling is None and pilot or ceiling is not None and (ceiling < 1 or pilot and ceiling > 1000):
            raise ValueError("Pilot ceiling must be between 1 and 1000 provider attempts")
        private_directory(path.parent)
        self.engine = sqlite_engine(path)
        metadata.create_all(self.engine)
        with self.engine.begin() as db:
            row = db.execute(select(account)).mappings().first()
            if row is None:
                db.execute(insert(account).values(id=1, ceiling=ceiling, state="OPEN"))
            elif row["ceiling"] != ceiling:
                raise ValueError("PILOT_ATTEMPT_CEILING_CHANGED")
            if ceiling is not None and db.scalar(
                select(func.count()).select_from(attempts).where(attempts.c.status == "STARTED")
            ):
                db.execute(update(account).values(state="UNCERTAIN"))

    def limited(self, max_attempts: int) -> "LimitedProviderAttemptAccount":
        return LimitedProviderAttemptAccount(self, max_attempts)

    def admit(self, identity, component, request, *, max_attempts: int | None = None):
        limit = admission_limit(max_attempts) if max_attempts is not None else None
        return self._admit(identity, component, request, max_attempts=limit)

    def _admit(self, identity, component, request, *, max_attempts: int | None):
        # A verified generation wrapper may have already consumed its entire
        # phase allowance in predecessors, hence an internal remaining limit of zero.
        with self.engine.begin() as db:
            row = db.execute(select(account)).mappings().one()
            if row["state"] != "OPEN":
                raise PilotOutcomeUncertain("PILOT_PROVIDER_ADMISSION_UNCERTAIN")
            if db.scalar(select(attempts.c.id).where(attempts.c.id == identity)) is not None:
                raise PilotAdmissionRetained("PILOT_COMPLETION_ALREADY_ADMITTED")
            ceiling = row["ceiling"]
            if max_attempts is not None:
                ceiling = min(ceiling, max_attempts) if ceiling is not None else max_attempts
            count = db.execute(select(func.count()).select_from(attempts)).scalar_one()
            if ceiling is not None and count >= ceiling:
                raise UsageLimitExceeded("PILOT_SHARED_PROVIDER_ATTEMPT_CEILING")
            db.execute(
                insert(attempts).values(
                    id=identity, component=component, status="STARTED", request_sha256=digest(request)
                )
            )

    def complete(self, identity, response):
        serialized = ModelMessagesTypeAdapter.dump_python([response], mode="json")[0]
        with self.engine.begin() as db:
            result = db.execute(
                update(attempts)
                .where(attempts.c.id == identity, attempts.c.status == "STARTED")
                .values(status="COMPLETED", usage=canonical(serialized["usage"]), response=canonical(serialized))
            )
            if result.rowcount != 1:
                raise PilotAdmissionRetained("PILOT_COMPLETION_CONFLICT")

    def uncertain(self, identity, error=None, *, phase=None):
        """Retain unknown usage and return whether the account requires fencing."""
        with self.engine.begin() as db:
            fields = {"status": "UNCERTAIN"}
            if error is not None:
                # Preserve diagnosis without messages, bodies, headers or keys.
                chain, current = [], error
                while current is not None and len(chain) < 6:
                    status = getattr(current, "status_code", None)
                    chain.append(
                        {
                            "type": type(current).__module__ + "." + type(current).__qualname__,
                            "http_status": status if isinstance(status, int) else None,
                        }
                    )
                    current = current.__cause__ or current.__context__
                usage = RequestUsage()
                for attempt in carried_attempts(error):
                    if attempt.usage is not None:
                        usage.incr(attempt.usage)
                fields["usage"] = canonical(
                    {
                        **TypeAdapter(RequestUsage).dump_python(usage, mode="json"),
                        "failure_phase": phase,
                        "failure_chain": chain,
                    }
                )
            db.execute(update(attempts).where(attempts.c.id == identity).values(**fields))
            fenced = db.scalar(select(account.c.ceiling)) is not None
            if fenced:
                db.execute(update(account).values(state="UNCERTAIN"))
            return fenced

    def report(self):
        with self.engine.connect() as db:
            rows = list(db.execute(select(attempts.c.component, attempts.c.status, attempts.c.usage)).mappings())
            state = db.execute(select(account)).mappings().one()
        components = {}
        for row in rows:
            totals = components.setdefault(
                row["component"],
                {"attempts": 0, "completed": 0, "input_tokens": 0, "output_tokens": 0, "cache_read_tokens": 0},
            )
            totals["attempts"] += 1
            totals["completed"] += row["status"] == "COMPLETED"
            usage = json.loads(row["usage"]) if row["usage"] else {}
            for key in ("input_tokens", "output_tokens", "cache_read_tokens"):
                totals[key] += usage.get(key, 0)
        report = {
            "ceiling": state["ceiling"],
            "state": state["state"],
            "attempts": len(rows),
            "components": components,
            "billing": "Subscription billing is unknown. Request admission is strict. Tokens and API-equivalent cost estimates cannot impose a hard token or billed-cost ceiling; in-flight requests may overshoot estimates.",
        }
        if state["ceiling"] is None:
            unknown = sum(row["status"] != "COMPLETED" for row in rows)
            report.update(
                unknown_attempts=unknown,
                unknown_usage=bool(unknown),
                billing="Subscription billing is unknown. Failed requests retain unknown token usage. Recorded tokens are a lower bound.",
            )
        return report


@dataclass(frozen=True)
class LimitedProviderAttemptAccount:
    """A phase ceiling over one shared ledger; completions and fences are shared."""

    account: ProviderAttemptAccount
    max_attempts: int

    def __post_init__(self):
        admission_limit(self.max_attempts)

    @property
    def engine(self):
        return self.account.engine

    def limited(self, max_attempts: int) -> "LimitedProviderAttemptAccount":
        admission_limit(max_attempts)
        return LimitedProviderAttemptAccount(self.account, min(self.max_attempts, max_attempts))

    def admit(self, identity, component, request, *, max_attempts: int | None = None):
        limit = self.max_attempts if max_attempts is None else min(self.max_attempts, admission_limit(max_attempts))
        return self.account.admit(identity, component, request, max_attempts=limit)

    def complete(self, identity, response):
        return self.account.complete(identity, response)

    def uncertain(self, identity, error=None, *, phase=None):
        return self.account.uncertain(identity, error, phase=phase)

    def report(self):
        # Keep the durable global ceiling and total truthful; the caller can
        # inspect max_attempts separately without inventing a second account.
        return self.account.report()


class AdmittedModel(WrapperModel):
    def __init__(self, wrapped, *, budget, identity, component):
        super().__init__(wrapped)
        self.budget, self.identity, self.component = budget, identity, component

    def admit(self, messages, settings, parameters):
        ordinal = provider_attempt_ordinal.get()
        identity = self.identity if ordinal == 1 else digest([self.identity, "provider-wait", ordinal])
        self.budget.admit(
            identity,
            self.component,
            {
                "model": self.model_name,
                "messages": ModelMessagesTypeAdapter.dump_python(messages, mode="json"),
                "settings": settings,
                "parameters": TypeAdapter(ModelRequestParameters).dump_python(parameters, mode="json"),
            },
        )
        return identity

    async def request(self, messages, model_settings, model_request_parameters):
        identity = self.admit(messages, model_settings, model_request_parameters)
        try:
            response = await self.wrapped.request(messages, model_settings, model_request_parameters)
            self.budget.complete(identity, response)
            return response
        except BaseException as error:
            fenced = self.budget.uncertain(identity, error, phase="request_or_persistence")
            if fenced and isinstance(error, Exception):
                raise PilotOutcomeUncertain("PILOT_PROVIDER_OUTCOME_UNCERTAIN") from None
            raise

    @asynccontextmanager
    async def request_stream(self, messages, model_settings, model_request_parameters, run_context=None):
        identity = self.admit(messages, model_settings, model_request_parameters)
        stream = None
        started = AttemptStart()
        try:
            async with self.wrapped.request_stream(
                messages, model_settings, model_request_parameters, run_context
            ) as stream:
                yield stream
            self.budget.complete(identity, stream.get())
        except BaseException as error:
            retain_partial_attempt(error, stream, self, started)
            fenced = self.budget.uncertain(identity, error, phase="stream_or_persistence")
            if fenced and isinstance(error, Exception):
                raise PilotOutcomeUncertain("PILOT_PROVIDER_OUTCOME_UNCERTAIN") from None
            raise


@dataclass(kw_only=True)
class AttemptAdmission(AbstractCapability[Any]):
    budget: ProviderAttemptAccount | LimitedProviderAttemptAccount
    namespace: str
    limiter: AbstractConcurrencyLimiter | None = None
    id: str | None = "disposable-provider-admission-v1"
    identity: str | None = field(default=None, init=False)

    async def for_run(self, ctx):
        return type(self)(budget=self.budget, namespace=self.namespace, limiter=self.limiter)

    async def prepare_model_request(self, ctx, request_context):
        if not ctx.run_id:
            raise ValueError("PILOT_RUN_ID_REQUIRED")
        self.identity = digest([self.namespace, ctx.run_id, ctx.run_step, request_context.attempt])
        return request_context

    def get_wrapper_model(self, model):
        if self.identity is None:
            raise ValueError("PILOT_REQUEST_ID_REQUIRED")
        admitted = AdmittedModel(model, budget=self.budget, identity=self.identity, component=self.namespace)
        # Acquire capacity before admission. A queued request has not been sent
        # and must not become an uncertain admission if its waiter is canceled.
        return ConcurrencyLimitedModel(admitted, self.limiter) if self.limiter is not None else admitted
