"""Effort advice is bounded, replayable and separate from configured defaults."""

import asyncio
import copy
import hashlib
import json
from pathlib import Path
from unittest.mock import AsyncMock

import aiohttp
import pytest
from pydantic import ValidationError

from civ_agent.advisory_answers import parse_answers
from civ_agent.commands import game
from civ_agent.decisions_broker import DecisionsBroker
from civ_agent.decisions_policy import DecisionsSettings, decisions_values
from civ_agent.decisions_tasks import acceptance_threshold, request_body, request_id, request_metadata
from civ_agent.effort_decisions import EFFORT_LEVELS, VERSION, brief, choose, question
from civ_agent.game_config import load_config
from civ_agent.protocol import JsonObject
from civ_agent.tls import write_private_json
from tests.test_decisions_broker import server


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
    args = game.parser().parse_args(
        [
            "--decisions-enabled",
            "--decisions-effort-enabled",
            "--decisions-effort-levels",
            '["low","medium","high"]',
        ]
    )
    assert decisions_values(args)["decisions_effort_levels"] == ["low", "medium", "high"]
    for name in ("pangaea.toml", "two-continents.toml"):
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


def test_brief_preserves_complete_authored_plans_or_reports_omission():
    plan = {"type": "handoff", "text": "Complete authored thought " * 300}
    prepared = {"reads": {"player_turn": {"turn": 1, "cities": [], "units": []}}}
    facts = brief(prepared, [plan], "gameplay")
    assert facts["plans"] == [] and "notebook plan omitted" in facts["coverage"]["omitted"][-1]
    assert plan["text"].endswith("thought ") and "plans" not in prepared
    prepared["reads"]["player_turn"]["cities"] = [{"name": "long" * 3000}]
    with pytest.raises(ValueError, match="EFFORT_INPUT_TOO_LARGE"):
        brief(prepared, [], "gameplay")


def test_classifier_responsibilities_include_workers_and_current_review_scope():
    main = question({"role": "gameplay", "levels": list(EFFORT_LEVELS)})
    economy = question({"role": "economy", "levels": list(EFFORT_LEVELS)})
    assert "non-worker civilians" in main["instructions"]
    assert "Economy owns cities and workers" in main["instructions"]
    assert "movement, safety and improvement replacements" in economy["instructions"]
    assert "current review scope and requests due now" in economy["instructions"]
    assert [r["value"] for r in economy["choices"]] == list(EFFORT_LEVELS)


@pytest.mark.parametrize("role", ["gameplay", "economy"])
def test_brief_retains_due_deferred_requests_and_scope_without_mutating_source(role):
    requests = [
        {"id": "pending", "status": "pending"},
        {"id": "due", "status": "deferred", "reviewTurn": 64, "text": "Authored request"},
        {"id": "overdue", "status": "deferred", "reviewTurn": 63},
        {"id": "future", "status": "deferred", "reviewTurn": 65},
        {"id": "completed", "status": "completed", "reviewTurn": 64},
        {"id": "rejected", "status": "rejected", "reviewTurn": 64},
        {"id": "unknown", "status": "deferred"},
        {"id": "null", "status": "deferred", "reviewTurn": None},
        {"id": "bool", "status": "deferred", "reviewTurn": True},
        {"id": "string", "status": "deferred", "reviewTurn": "64"},
    ]
    prepared = {
        "reads": {"player_turn": {"turn": 64}},
        "economyCoordination": {
            "requests": requests,
            "economySchedule": {"fullReview": False, "cities": ["city:1"], "workerActionRequired": True},
        },
    }
    original = copy.deepcopy(prepared)
    facts = brief(prepared, [], role)
    assert facts["coordination"]["requests"] == requests[:3]
    assert facts["coordination"]["economySchedule"] == prepared["economyCoordination"]["economySchedule"]
    facts["coordination"]["requests"][1]["text"] = "Changed only in projection"
    facts["coordination"]["economySchedule"]["cities"].clear()
    assert prepared == original


@pytest.mark.parametrize("turn", [None, True, "64"])
def test_unknown_turn_does_not_make_deferred_requests_due(turn):
    prepared = {
        "reads": {"player_turn": {"turn": turn}},
        "economyCoordination": {"requests": [{"status": "deferred", "reviewTurn": 1}]},
    }
    assert brief(prepared, [], "economy")["coordination"]["requests"] == []


def test_old_classifier_evidence_cannot_reuse_current_receipts():
    supplied = evidence()
    explicit = copy.deepcopy(supplied)
    explicit["effortSelection"]["version"] = VERSION
    assert request_id(supplied) == request_id(explicit)
    explicit["effortSelection"]["version"] = "reasoning-effort-v1"
    with pytest.raises(ValidationError):
        request_id(explicit)


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
