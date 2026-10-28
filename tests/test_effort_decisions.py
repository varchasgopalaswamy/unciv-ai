"""Effort advice is bounded, replayable and separate from configured defaults."""

import asyncio
import copy
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import aiohttp
import pytest
from pydantic import ValidationError

from civ_agent.advisory_answers import parse_answers
from civ_agent.advisory_runtime import AdvisoryRuntime
from civ_agent.commands import agent_server
from civ_agent.decisions_broker import DecisionsBroker
from civ_agent.decisions_policy import DecisionsSettings, decisions_values
from civ_agent.decisions_tasks import acceptance_threshold, request_body, request_id, request_metadata
from civ_agent.effort_decisions import EFFORT_LEVELS, base_settings, brief, choose, select_effort, supported_levels
from civ_agent.game_config import load_config
from civ_agent.protocol import JsonObject
from civ_agent.startup_context import prepare_startup
from civ_agent.tls import write_private_json
from tests.test_agent_service import AgentServiceTestCase
from tests.test_decisions_broker import server
from tests.test_economy_optimization import managed


def settings(**values):
    return DecisionsSettings(decisions_enabled=True, decisions_effort_enabled=True, **values)


def evidence(levels=None):
    return {
        "player": "Rome",
        "effortSelection": {
            "role": "gameplay",
            "model": "gpt-6-luna",
            "levels": levels or list(EFFORT_LEVELS),
            "facts": {"turn": 1, "role": "main", "pending": []},
        },
    }


def answer(body, probabilities=None) -> JsonObject:
    q = body["questions"][0]
    levels = [row["value"] for row in q["choices"]]
    probabilities = probabilities or {level: 0.85 if level == "low" else 0.15 / (len(levels) - 1) for level in levels}
    winner = max(probabilities, key=lambda level: probabilities[level])
    return {
        "name": q["name"],
        "type": "choice",
        "choice": winner,
        "confidence": probabilities[winner],
        "providerConfidence": 0.99,
        "probabilities": [{"value": level, "probability": value} for level, value in probabilities.items()],
    }


class Classifier:
    def __init__(self, policy, mode="success", callback=None):
        self.policy, self.mode, self.callback = policy, mode, callback
        self.calls = []
        self.refuse_economy = False

    async def batch(self, supplied, *, timeout):
        self.calls.append(copy.deepcopy(supplied))
        if self.mode == "timeout":
            raise TimeoutError()
        if self.callback:
            self.callback()
        body = request_body(supplied, self.policy)
        raw = answer(body)
        if supplied.get("effortSelection", {}).get("role") == "economy" and getattr(self, "refuse_economy", False):
            raw = {"name": raw["name"], "type": "refusal"}
        if self.mode == "ambiguous":
            raw = answer(body, {"low": 0.35, "medium": 0.34, "high": 0.21, "xhigh": 0.05, "max": 0.05})
        elif self.mode == "refusal":
            raw = {"name": raw["name"], "type": "refusal"}
        elif self.mode == "malformed":
            malformed = answer(body)
            malformed["probabilities"][0]["probability"] = True
            raw = malformed
        return {
            **request_metadata(body, supplied),
            **parse_answers(body, {"answers": [raw]}, acceptance_threshold(supplied, self.policy)),
        }


class Models:
    def __init__(self, levels=EFFORT_LEVELS):
        self.levels, self.calls = levels, []

    async def request(self, method, params):
        self.calls.append((method, params))
        assert method == "model/list"
        return {
            "data": [
                {
                    "id": "gpt-6-luna",
                    "model": "gpt-6-luna",
                    "supportedReasoningEfforts": [{"reasoningEffort": level} for level in self.levels],
                }
            ],
            "nextCursor": None,
        }


def test_configuration_cli_and_example_defaults_are_independent_of_effort_choices():
    policy = settings(decisions_effort_levels=["high", "low"])
    assert policy.decisions_effort_levels == ["low", "high"]
    for values in (
        {"decisions_effort_levels": ["low"]},
        {"decisions_effort_levels": ["low", "low"]},
        {"decisions_effort_levels": ["low", "invented"]},
        {"decisions_effort_levels": ["none", "low"]},
        {"decisions_effort_min_margin": -0.1},
        {"decisions_effort_min_probability": float("nan")},
    ):
        with pytest.raises(ValidationError):
            settings(**values)
    with pytest.raises(ValidationError):
        DecisionsSettings(decisions_effort_enabled=True)
    args = agent_server.parser().parse_args(
        [
            "Rome",
            "--decisions-enabled",
            "--decisions-effort-enabled",
            "--decisions-effort-levels",
            '["low","medium","high"]',
        ]
    )
    assert decisions_values(args)["decisions_effort_levels"] == ["low", "medium", "high"]
    for name in ("benchmark.toml", "benchmark-luna.toml", "benchmark-luna-terra.toml"):
        config = load_config(Path("examples") / name)
        assert config.agents.reasoning_effort == "medium" and config.agents.decisions_effort_enabled
        assert not any("default" in key for key in config.agents.model_dump() if key.startswith("decisions_effort"))
        player = next(p for p in config.players if p.controller == "agent")
        player.reasoning_effort = "high"
        assert config.settings(player)["reasoning_effort"] == "high"


def test_fixed_question_permissions_context_and_request_binding():
    value, policy = evidence(), settings()
    body = request_body(value, policy)
    assert [r["value"] for r in body["questions"][0]["choices"]] == list(EFFORT_LEVELS)
    assert "Future projects alone" in body["questions"][0]["instructions"]
    another_model = {**value, "effortSelection": {**value["effortSelection"], "model": "another-model"}}
    assert request_body(another_model, policy)["questions"] == body["questions"]
    assert request_id(another_model) != request_id(value)
    assert "default" not in json.loads(body["input"])
    assert acceptance_threshold(value, policy) == 0.5
    assert acceptance_threshold({}, policy) == policy.decisions_acceptance_probability
    assert request_id(value) != request_id(evidence(["low", "medium"]))
    for bad, operator in (
        (value, DecisionsSettings(decisions_enabled=True)),
        (value, settings(decisions_effort_levels=["low", "medium"])),
        ({**value, "earlyReview": {"turn": 1}}, policy),
        ({**value, "effortSelection": {**value["effortSelection"], "facts": {"large": "x" * 6000}}}, policy),
    ):
        with pytest.raises(ValueError):
            request_body(bad, operator)


@pytest.mark.parametrize(
    "mode,expected",
    [
        ("success", "low"),
        ("ambiguous", "medium"),
        ("refusal", "medium"),
        ("timeout", "medium"),
        ("malformed", "medium"),
        ("changed", "medium"),
    ],
)
def test_startup_choice_falls_back_and_replays_without_reclassifying(tmp_path, mode, expected):
    async def run():
        async with managed(tmp_path) as m:
            prepared = await prepare_startup(m.gateway, m.main)
            policy = settings()
            callback = (lambda: setattr(m.main, "role_refresh_required", True)) if mode == "changed" else None
            client = Classifier(policy, mode, callback)
            m.gateway.advisories = AdvisoryRuntime(m.gateway, client, policy)
            service = SimpleNamespace(
                gateway=m.gateway, decisions_settings=policy, civilization="Rome", _save=AsyncMock()
            )
            base = {"provider": "openai", "model": "gpt-6-luna", "reasoningEffort": "medium"}
            record = {"settings": base}
            selected = await select_effort(service, record, m.main, prepared, base, Models())
            assert selected["reasoningEffort"] == expected and record["settings"] == base
            assert len(client.calls) == 1
            assert (await select_effort(service, record, m.main, prepared, base, Models())) == selected
            assert len(client.calls) == 1
            record["effectiveSettings"] = selected
            assert base_settings(record) == base
            assert len(service._save.await_args_list) == 1
            assert "effortSelection" not in prepared and not m.host.executed

    asyncio.run(run())


class AdaptiveEffortServiceTests(AgentServiceTestCase):
    async def test_native_threads_use_selection_without_changing_next_turn_or_role_defaults(self):
        await self.service.close()
        self.service = self.make_service(
            model="gpt-6-luna", reasoning_effort="medium", economy_model={"reasoning_effort": "high"}
        )
        self.service.decisions_settings = policy = settings()
        client = Classifier(policy)
        client.refuse_economy = True
        runtime = AdvisoryRuntime(self.service.gateway, client, policy)
        runtime.startup = AsyncMock()
        runtime.finish_delivery = AsyncMock()
        self.service.gateway.advisories = runtime
        await self.service.start(self.payload())
        first = await self.terminal()
        self.assertEqual("COMPLETED", first["phase"])
        self.assertEqual("medium", first["settings"]["reasoningEffort"])
        self.assertEqual("low", first["effectiveSettings"]["reasoningEffort"])
        self.assertEqual("medium", first["configuredSettings"]["reasoningEffort"])
        assert self.service.match is not None
        self.assertEqual("medium", self.service.match["effectiveSettings"]["reasoningEffort"])
        economies = [r for r in self.service.records.values() if r.get("purpose") == "economy"]
        self.assertTrue(economies)
        self.assertTrue(all(r["effectiveSettings"]["reasoningEffort"] == "high" for r in economies))
        first_turn = next(p for method, p in self.factory.calls if method == "turn/start")
        self.assertEqual("low", first_turn["effort"])
        await self.service.finalize(
            dict(activationId=first["activationId"], epoch="epoch-one", gameTurn=0, committedHandoff=1)
        )
        client.mode = "ambiguous"
        self.factory.calls.clear()
        await self.service.start(self.payload("next-turn", gameTurn=1, handoff=2))
        second = await self.terminal()
        self.assertEqual("COMPLETED", second["phase"])
        self.assertEqual("medium", second["settings"]["reasoningEffort"])
        self.assertEqual("medium", second["effectiveSettings"]["reasoningEffort"])
        self.assertEqual(first["configuredSettings"], second["configuredSettings"])
        second_turn = next(p for method, p in self.factory.calls if method == "turn/start")
        self.assertEqual("medium", second_turn["effort"])


def test_supported_levels_and_provider_scope_preserve_operator_and_role_defaults(tmp_path):
    async def run():
        async with managed(tmp_path) as m:
            prepared = await prepare_startup(m.gateway, m.main)
            policy = settings(decisions_effort_levels=["low", "medium", "high"])
            client = Classifier(policy)
            m.gateway.advisories = AdvisoryRuntime(m.gateway, client, policy)
            service = SimpleNamespace(
                gateway=m.gateway, decisions_settings=policy, civilization="Rome", _save=AsyncMock()
            )
            base = {"provider": "openai", "model": "gpt-6-luna", "reasoningEffort": "high"}
            # One remaining supported choice is not a meaningful classification.
            record = {"settings": base}
            assert await select_effort(service, record, m.main, prepared, base, Models(["high", "max"])) == base
            assert record["effortSelection"]["reason"] == "insufficient_supported_levels" and not client.calls
            for provider in ("openrouter",):
                external = {**base, "provider": provider}
                assert (
                    await select_effort(service, {"settings": external}, m.main, prepared, external, Models())
                    == external
                )
                assert not client.calls
            service.decisions_settings = DecisionsSettings()
            process = Models()
            assert await select_effort(service, {"settings": base}, m.main, prepared, base, process) == base
            assert not process.calls
            service.decisions_settings = policy
            record = {"settings": base}
            chosen = await select_effort(
                service, record, m.main, prepared, base, Models(["none", "low", "medium", "max"])
            )
            assert chosen["reasoningEffort"] == "low"
            assert client.calls[0]["effortSelection"]["levels"] == ["low", "medium"]
            assert record["effortSelection"]["levels"] == ["low", "medium"]

    asyncio.run(run())


@pytest.mark.parametrize(
    "probabilities,expected",
    [
        ({"low": 0.60, "medium": 0.25, "high": 0.15, "xhigh": 0, "max": 0}, "selected"),
        ({"low": 0.51, "medium": 0.49, "high": 0, "xhigh": 0, "max": 0}, "not_standout"),
        ({"low": 0.45, "medium": 0.20, "high": 0.20, "xhigh": 0.10, "max": 0.05}, "not_standout"),
    ],
)
def test_winning_probability_and_runner_up_margin_are_independent(probabilities, expected):
    body = request_body(evidence(), settings())
    raw = answer(body, probabilities)
    result = parse_answers(body, {"answers": [raw]}, 0.5)
    selected = choose(result, "main_effort", list(EFFORT_LEVELS), settings())
    assert selected["reason"] == expected
    assert selected["candidate"] == "low"


def test_model_catalogue_pagination_and_unusable_metadata():
    async def run():
        models = Models()
        page = await models.request("model/list", {})
        process = SimpleNamespace(request=AsyncMock(side_effect=[{"data": [], "nextCursor": "next-page"}, page]))
        assert await supported_levels(process, "gpt-6-luna") == set(EFFORT_LEVELS)
        assert process.request.await_args_list[-1].args[1]["cursor"] == "next-page"
        for malformed in (
            None,
            {"data": [None]},
            {"data": [{"model": "gpt-6-luna", "supportedReasoningEfforts": [None]}]},
        ):
            process = SimpleNamespace(request=AsyncMock(return_value=malformed))
            assert await supported_levels(process, "gpt-6-luna") == set()

    asyncio.run(run())


def test_brief_preserves_complete_authored_plans_or_reports_omission():
    plan = {"type": "handoff", "text": "Complete authored thought " * 300}
    prepared = {"reads": {"player_turn": {"turn": 1, "cities": [], "units": []}}}
    facts = brief(prepared, [plan], "gameplay")
    assert facts["plans"] == [] and "notebook plan omitted" in facts["coverage"]["omitted"][-1]
    assert plan["text"].endswith("thought ") and "plans" not in prepared
    prepared["reads"]["player_turn"]["cities"] = [{"name": "long" * 3000}]
    with pytest.raises(ValueError, match="EFFORT_INPUT_TOO_LARGE"):
        brief(prepared, [], "gameplay")


def test_broker_effort_threshold_is_scoped_and_receipts_deduplicate(tmp_path):
    async def run():
        policy = settings()
        write_private_json(tmp_path / "decisions-clients.json", {"seat": hashlib.sha256(b"token").hexdigest()})
        upstream = AsyncMock()
        value = evidence()
        body = request_body(value, policy)
        upstream.evaluate.return_value = {"answers": [answer(body)], "fallback": None, "usage": {"input_tokens": 20}}
        broker = DecisionsBroker(tmp_path, upstream)
        broker.ledger.register("seat", "game", "Rome", policy.model_dump())
        async with server(broker.application()) as url, aiohttp.ClientSession() as session:
            envelope = {"requestId": request_id(value), "evidence": value}

            async def call():
                async with session.post(
                    url + "/batch", json=envelope, headers={"Authorization": "Bearer token"}
                ) as response:
                    assert response.status == 200
                    return await response.json()

            first = await call()
            assert first["questionResults"][0]["status"] == "accepted"
            assert first["questionResults"][0]["threshold"] == 0.5
            assert (await call())["rawAnswers"] == first["rawAnswers"]
            assert upstream.evaluate.await_count == 1
            assert first["accounting"]["requests"] == 1

    asyncio.run(run())
