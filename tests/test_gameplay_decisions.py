import asyncio
import json

import pytest
from pydantic_ai.exceptions import UsageLimitExceeded
from pydantic_ai.messages import ModelResponse, TextPart

from civ_agent.diplomacy_decisions import DecisionsClient
from civ_agent.gameplay_decisions import AccountedDecisionsClient
from civ_agent.provider_attempts import PilotOutcomeUncertain, ProviderAttemptAccount


def body(player="France"):
    return {"model": "Winnow-12B", "input": json.dumps({"player": player}), "questions": []}


def test_classifier_and_terra_share_one_ledger_and_assessment_reserve(tmp_path, monkeypatch):
    calls = []

    async def evaluate(self, value, *, timeout):
        calls.append(value)
        return {"answers": [], "usage": {}, "model": self.model, "fallback": None}

    monkeypatch.setattr(DecisionsClient, "evaluate", evaluate)
    account = ProviderAttemptAccount(tmp_path / "account.sqlite3", ceiling=3)
    client = AccountedDecisionsClient(budget=account.limited(max_attempts=1), game="owned")
    asyncio.run(client.evaluate(body()))
    assert account.report()["attempts"] == 1 and account.report()["state"] == "OPEN"
    with pytest.raises(UsageLimitExceeded):
        asyncio.run(client.evaluate(body("China")))
    assert len(calls) == 1 and account.report()["state"] == "OPEN"
    account.admit("assessment", "assessment/France", {"model": "gpt-6.1-terra"})
    account.complete("assessment", ModelResponse([TextPart("assessed")]))
    assert account.report()["attempts"] == 2


def test_unknown_classifier_submission_fences_terra_and_is_not_repeated(tmp_path, monkeypatch):
    calls = []

    async def evaluate(self, value, *, timeout):
        calls.append(value)
        return {"answers": [], "fallback": "timeout", "providerError": {"kind": "timeout"}}

    monkeypatch.setattr(DecisionsClient, "evaluate", evaluate)
    account = ProviderAttemptAccount(tmp_path / "account.sqlite3")
    client = AccountedDecisionsClient(budget=account, game="owned")
    with pytest.raises(PilotOutcomeUncertain):
        asyncio.run(client.evaluate(body()))
    with pytest.raises(PilotOutcomeUncertain):
        account.admit("terra", "player/France", {})
    with pytest.raises(PilotOutcomeUncertain):
        asyncio.run(client.evaluate(body()))
    assert len(calls) == 1 and account.report()["state"] == "UNCERTAIN"


@pytest.mark.parametrize("kind", ["timeout", "transport"])
def test_unlimited_tracking_preserves_classifier_fallback_and_later_model_work(tmp_path, monkeypatch, kind):
    async def evaluate(self, value, *, timeout):
        return {"answers": [], "fallback": kind, "providerError": {"kind": kind}}

    monkeypatch.setattr(DecisionsClient, "evaluate", evaluate)
    account = ProviderAttemptAccount(tmp_path / "requests.sqlite3", ceiling=None, pilot=False)
    client = AccountedDecisionsClient(budget=account, game="owned")
    assert asyncio.run(client.evaluate(body()))["fallback"] == kind
    account.admit("next-model-request", "gameplay", {})
    account.complete("next-model-request", ModelResponse([TextPart("Continued")]))
    assert account.report()["state"] == "OPEN" and account.report()["unknown_attempts"] == 1
    assert account.report()["attempts"] == 2


def test_circuit_fallback_sends_and_admits_nothing(tmp_path, monkeypatch):
    async def evaluate(self, value, *, timeout):
        assert self.clock() < self.open_until
        return {"answers": [], "fallback": "circuit_open"}

    monkeypatch.setattr(DecisionsClient, "evaluate", evaluate)
    account = ProviderAttemptAccount(tmp_path / "account.sqlite3")
    client = AccountedDecisionsClient(budget=account, game="owned", clock=lambda: 1)
    client.open_until = 5
    assert asyncio.run(client.evaluate(body()))["fallback"] == "circuit_open"
    assert account.report()["attempts"] == 0


def test_independent_identical_questions_count_each_upstream_send(tmp_path, monkeypatch):
    calls = []

    async def evaluate(self, value, *, timeout):
        calls.append(value)
        return {"answers": [], "usage": {"input_tokens": 2, "output_tokens": 1}, "model": self.model}

    monkeypatch.setattr(DecisionsClient, "evaluate", evaluate)
    account = ProviderAttemptAccount(tmp_path / "account.sqlite3")
    client = AccountedDecisionsClient(budget=account, game="owned")
    asyncio.run(client.evaluate(body()))
    asyncio.run(client.evaluate(body()))
    assert len(calls) == 2 and account.report()["attempts"] == 2
    assert account.report()["components"]["owned/decisions/jev/France"]["input_tokens"] == 4
