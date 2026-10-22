"""Provider-neutral acceptance, independent backend circuits and free accounting."""

import argparse
import asyncio
import math

import pytest
from aiohttp import web
from pydantic import ValidationError

from civ_agent.container_supervisor import authorize_agent, validate_routing_credentials
from civ_agent.decisions_backends import BACKENDS, provider_answers
from civ_agent.decisions_broker import DecisionsBroker, RoutingClient
from civ_agent.decisions_ledger import DecisionsLedger
from civ_agent.decisions_policy import DecisionsSettings, add_decisions_arguments, decisions_values, routing_thresholds
from civ_agent.decisions_tasks import request_body, request_id, request_metadata
from civ_agent.diplomacy_decisions import DecisionsClient
from civ_agent.game_config import GameConfig
from civ_agent.protocol import JsonObject
from tests.test_decisions_broker import server
from tests.test_decisions_ledger import evidence


@pytest.mark.parametrize("backend,probability,timeout", [("luna", 0.95, 5), ("jev", 0.98, 120)])
def test_backend_defaults_and_overrides_reach_cli_and_resolved_configuration(backend, probability, timeout):
    settings = DecisionsSettings(decisions_enabled=True, decisions_backend=backend)
    assert settings.decisions_acceptance_probability == probability
    assert settings.decisions_timeout_seconds == timeout
    assert set(routing_thresholds(settings, "incoming").values()) == {probability}
    assert set(routing_thresholds(settings, "outgoing").values()) == {probability}
    parser = argparse.ArgumentParser()
    add_decisions_arguments(parser)
    args = parser.parse_args(["--decisions-enabled", "--decisions-backend", backend])
    assert decisions_values(args) == settings.model_dump()
    overridden = parser.parse_args(
        [
            "--decisions-backend",
            backend,
            "--decisions-acceptance-probability",
            "0.97",
            "--decisions-outgoing-omit-probability",
            "0.999",
        ]
    )
    resolved = GameConfig(agents=decisions_values(overridden)).resolved().agents
    assert resolved.decisions_acceptance_probability == 0.97
    assert resolved.decisions_incoming_batch_probability == 0.97
    assert resolved.decisions_outgoing_omit_probability == 0.999


@pytest.mark.parametrize("value", [-0.1, 1.1, math.inf, math.nan, True, False, "0.95", None])
def test_invalid_acceptance_probability_is_rejected(value):
    with pytest.raises(ValidationError):
        DecisionsSettings(decisions_acceptance_probability=value)


def test_old_confidence_parameters_cannot_silently_change_units():
    with pytest.raises(ValidationError, match="Extra inputs"):
        DecisionsSettings.model_validate({"decisions_min_confidence": 0.9})


def test_receipt_identity_includes_the_backend_model():
    jev = request_body(evidence(), DecisionsSettings(decisions_backend="jev"))
    luna = request_body(evidence(), DecisionsSettings(decisions_backend="luna"))
    assert jev["questions"] == luna["questions"]
    assert jev["input"] == luna["input"]
    assert request_metadata(jev, evidence())["requestId"] != request_metadata(luna, evidence())["requestId"]


def test_real_jev_wire_contract_uses_probability_not_entropy_and_no_api_key(tmp_path):
    async def run():
        settings = DecisionsSettings(decisions_enabled=True, additional_decisions_budget_usd=0)
        auth, seat = tmp_path / "auth", tmp_path / "seat"
        seat.mkdir()
        authorize_agent(auth, "game", 0, seat, decisions=True, settings=settings.model_dump(), player="Rome")
        calls = []

        async def provider(request):
            assert "Authorization" not in request.headers
            body = await request.json()
            calls.append(body)
            assert set(body) == {"model", "state", "questions"}
            assert body["model"] == "Winnow-12B"
            assert body["questions"]["preservation:0"]["criteria"] == {
                "consider": "Worth considering for memory.",
                "skip": "Nothing useful to preserve.",
            }
            return web.json_response(
                {
                    "model": "Winnow-12B",
                    "answers": {
                        "preservation:0": {
                            "type": "choice",
                            "choice": "consider",
                            "confidence": 0.87,
                            "probabilities": {"consider": 0.985, "skip": 0.015},
                        }
                    },
                    "usage": {"input_tokens": 300, "output_tokens": 0},
                }
            )

        app = web.Application()
        app.router.add_post("/v1/systemone", provider)
        async with server(app) as url:
            client = DecisionsClient(backend="jev", endpoint=url + "/v1/systemone")
            broker = DecisionsBroker(auth, client)
            socket = tmp_path / "broker.sock"
            async with server(broker.application(), socket=socket):
                remote = RoutingClient(socket, seat / "decisions-client.json")
                assert (await remote.health())["classifierModel"] == "Winnow-12B"
                first = await remote.batch(evidence())
                duplicate = await remote.batch(evidence())
        assert first == duplicate and len(calls) == 1
        assert first["answers"][0]["confidence"] == 0.985
        assert first["rawAnswers"][0]["probability"] == 0.985
        assert first["rawAnswers"][0]["providerConfidence"] == 0.87
        assert first["questionResults"][0]["threshold"] == 0.98
        assert first["accounting"]["spentNanodollars"] == first["accounting"]["reservedNanodollars"] == 0
        assert first["accounting"]["inputTokens"] == 300
        assert not (auth / "decisions.key").exists()
        validate_routing_credentials(GameConfig(agents=settings.model_dump()), auth)

    asyncio.run(run())


@pytest.mark.parametrize("backend", ["luna", "jev"])
def test_provider_confidence_never_substitutes_for_missing_or_low_probability(backend):
    body = request_body(evidence(), DecisionsSettings(decisions_backend=backend))

    def normalize(probabilities):
        raw: JsonObject = {"type": "choice", "choice": "consider", "confidence": 0.999}
        if probabilities is not None:
            raw["probabilities"] = (
                probabilities
                if backend == "jev"
                else [{"value": key, "probability": value} for key, value in probabilities.items()]
            )
        responses = {"preservation:0": raw} if backend == "jev" else [{"name": "preservation:0", **raw}]
        return provider_answers(body, {"model": body["model"], "answers": responses}, backend)

    assert normalize({"consider": 0.6, "skip": 0.4})[0]["confidence"] == 0.6
    assert normalize(None) == [{"name": "preservation:0"}]
    assert normalize({"consider": 0.9, "skip": 0.9}) == [{"name": "preservation:0"}]
    assert normalize({"consider": 0.2, "skip": 0.8}) == [{"name": "preservation:0"}]


def test_mixed_backend_broker_health_and_failure_circuits_are_independent(tmp_path):
    (tmp_path / "decisions.key").write_text("synthetic-luna-api-key\n")
    (tmp_path / "decisions.key").chmod(0o600)
    broker = DecisionsBroker(tmp_path)
    jev = broker._client("jev")
    luna = broker._client("luna")
    jev._failed("transport")
    assert luna.health_snapshot()["upstreamStatus"] == "unchecked"
    assert broker._client("jev") is jev and broker._client("luna") is luna
    assert jev.model == BACKENDS["jev"].model and luna.model == BACKENDS["luna"].model

    async def run():
        clients = {}
        for backend in ("luna", "jev"):
            seat = tmp_path / backend
            seat.mkdir()
            settings = DecisionsSettings(decisions_enabled=True, decisions_backend=backend)
            authorize_agent(tmp_path, backend, 0, seat, decisions=True, settings=settings.model_dump(), player="Rome")
            clients[backend] = RoutingClient(tmp_path / "broker.sock", seat / "decisions-client.json", backend=backend)
        async with server(broker.application(), socket=tmp_path / "broker.sock"):
            luna_health = await clients["luna"].health()
            jev_health = await clients["jev"].health()
        assert luna_health["classifierModel"] == "gpt-6-luna"
        assert luna_health["upstreamStatus"] == "unchecked"
        assert jev_health["classifierModel"] == "Winnow-12B"
        assert jev_health["upstreamStatus"] == "unavailable"

    asyncio.run(run())


def test_free_unknown_completion_and_recorded_paid_rate_survive_restarts(tmp_path):
    ledger = DecisionsLedger(tmp_path)
    settings = DecisionsSettings(decisions_enabled=True, additional_decisions_budget_usd=0)
    ledger.register("seat", "game", "Rome", settings.model_dump())
    body = request_body(evidence(), settings)
    identifier = request_id(evidence())
    assert ledger.reserve("seat", identifier, "input", body, pending_result={"requestId": identifier}) is None
    assert ledger.status("seat")["unknownCompletions"] == 1
    ledger.complete("seat", identifier, {"usage": {"input_tokens": 1000}})
    assert ledger.status("seat")["spentNanodollars"] == 0
    assert ledger.status("seat")["unknownCompletions"] == 0

    paid = DecisionsSettings(decisions_enabled=True, decisions_backend="luna")
    ledger.register("paid", "paid-game", "Rome", paid.model_dump())
    assert ledger.reserve("paid", "paid-request", "input", request_body(evidence(), paid)) is None
    restarted = DecisionsLedger(tmp_path)
    restarted.complete("paid", "paid-request", {"usage": {"input_tokens": 1000}})
    assert restarted.status("paid")["spentNanodollars"] == 100000
