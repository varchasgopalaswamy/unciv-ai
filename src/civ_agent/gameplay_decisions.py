"""Account actual local classifier submissions in a disposable game's ledger."""

import json
from uuid import uuid4

from pydantic_ai.messages import ModelResponse, TextPart
from pydantic_ai.usage import RequestUsage

from civ_agent.diplomacy_decisions import DecisionsClient
from civ_agent.provider_attempts import PilotOutcomeUncertain


class AccountedDecisionsClient(DecisionsClient):
    """The broker deduplicates first; this boundary counts its upstream call.

    Inherited classify() calls evaluate(), so routing and advisory requests use
    the same accounting. Circuit-only fallbacks send nothing and admit nothing.
    Unknown submissions fence the shared account instead of being resent.
    """

    def __init__(self, *, budget, game: str, endpoint=None, clock=None):
        kwargs = {"clock": clock} if clock is not None else {}
        super().__init__(backend="jev", endpoint=endpoint, **kwargs)
        self.budget, self.game = budget, game

    async def evaluate(self, body, *, timeout=120):
        if self.clock() < self.open_until:
            return await super().evaluate(body, timeout=timeout)
        if body.get("model") != self.model:
            raise ValueError("EVALUATION_DECISIONS_MODEL_CONFLICT")
        supplied = json.loads(body["input"])
        player = supplied.get("player", "routing")
        # Broker task identities already deduplicate logical consultations.
        # Two independent admitted tasks can have byte-identical questions;
        # count both actual upstream sends rather than deduplicating by body.
        identity = "decisions:" + uuid4().hex
        component = f"{self.game}/decisions/{self.backend}/{player}"
        self.budget.admit(identity, component, body)
        try:
            result = await super().evaluate(body, timeout=timeout)
            failure = result.get("providerError") or {}
            if failure.get("kind") in {"timeout", "transport"}:
                error = PilotOutcomeUncertain("EVALUATION_DECISIONS_OUTCOME_UNCERTAIN")
                if self.budget.uncertain(identity, error, phase="classifier"):
                    raise error
                return result
            raw_usage = result.get("usage") or {}
            reported = all(
                type(raw_usage.get(key)) is int and raw_usage[key] >= 0 for key in ("input_tokens", "output_tokens")
            )
            usage = RequestUsage(
                input_tokens=raw_usage["input_tokens"] if reported else 0,
                output_tokens=raw_usage["output_tokens"] if reported else 0,
                details={"classifier_token_usage_reported": int(reported)},
            )
            self.budget.complete(
                identity,
                ModelResponse(
                    parts=[TextPart(json.dumps(result, ensure_ascii=False))],
                    model_name=self.model,
                    usage=usage,
                    metadata={"classifier_token_usage_unknown": not reported, "backend": self.backend},
                ),
            )
            return result
        except BaseException as error:
            self.budget.uncertain(identity, error, phase="classifier_or_persistence")
            raise
