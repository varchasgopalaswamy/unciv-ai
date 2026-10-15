"""Two-question policy through the actual provider, broker and routing runtime."""

import argparse
import asyncio
import json

from aiohttp import web

from civ_agent.container_supervisor import authorize_agent
from civ_agent.decisions_broker import DecisionsBroker, RoutingClient
from civ_agent.decisions_policy import DecisionsSettings, add_decisions_arguments, decisions_values
from civ_agent.diplomacy_decisions import (
    OUTGOING_QUESTION_CHOICES,
    POLICY_VERSION,
    TWO_QUESTION_CHOICES,
    DecisionsClient,
    request_body,
)
from civ_agent.diplomacy_routing import DiplomacyRouting
from civ_agent.diplomacy_store import event_states, ready_ids
from civ_agent.game_config import GameConfig
from civ_agent.telemetry import JobAccounting
from civ_agent.tls import private_directory
from tests.test_decisions_broker import server
from tests.test_diplomacy_contracts import opened
from tests.test_diplomacy_routing import event
from tests.test_routing_canary import incoming


def binary_answer(name, choice, confidence):
    probability = (1 + confidence) / 2
    return {
        "name": name,
        "type": "choice",
        "choice": choice,
        "confidence": confidence,
        "probabilities": [
            {"value": value, "probability": probability if value == choice else 1 - probability}
            for value in {**TWO_QUESTION_CHOICES, **OUTGOING_QUESTION_CHOICES}[name]
        ],
    }


def test_current_policy_is_default_and_settings_and_cli_preserve_it():
    assert DecisionsSettings().decisions_routing_policy == POLICY_VERSION
    assert DecisionsSettings().decisions_enabled is False
    parser = argparse.ArgumentParser()
    add_decisions_arguments(parser)
    arguments = parser.parse_args(["--decisions-routing-policy", POLICY_VERSION])
    settings = DecisionsSettings.model_validate(decisions_values(arguments))
    assert settings.decisions_routing_policy == POLICY_VERSION
    assert settings.decisions_incoming_batch_confidence == settings.decisions_incoming_suppress_confidence == 0.9
    config = GameConfig(agents=settings.model_dump()).resolved()
    assert config.agents.decisions_routing_policy == POLICY_VERSION
    assert config.agents.decisions_min_confidence == 0.9


def test_two_question_provider_broker_runtime_preserves_binary_evidence_and_batch_deadline(tmp_path):
    async def run():
        requests = []
        decisions = {
            "uncertain response, safe wait": ("not_needed", 0.4, "can_wait", 0.95),
            "uncertain wait": ("needed", 0.99, "can_wait", 0.8),
            "no response": ("not_needed", 0.99, "can_wait", 0.95),
            "urgent": ("needed", 0.99, "now", 0.99),
        }

        async def provider(request):
            body = await request.json()
            requests.append(body)
            assert request.headers["Authorization"] == "Bearer local-two-question-test-key"
            bundle = json.loads(body["input"])
            assert body == request_body(bundle["direction"], bundle, policy_version=POLICY_VERSION)
            if bundle["direction"] == "incoming":
                assert [question["name"] for question in body["questions"]] == list(TWO_QUESTION_CHOICES)
                needed, needed_confidence, timing, timing_confidence = decisions[bundle["pending"][0]["text"]]
                answers = [
                    binary_answer("response_needed", needed, needed_confidence),
                    binary_answer("response_timing", timing, timing_confidence),
                ]
            else:
                assert body == request_body("outgoing", bundle, policy_version=POLICY_VERSION)
                answers = [
                    binary_answer("delivery_needed", "needed", 0.95),
                    binary_answer("delivery_timing", "can_wait", 0.95),
                ]
            for answer in answers:
                answer["providerText"] = "PRIVATE PROVIDER TEXT"
            return web.json_response({"answers": answers, "usage": {"input_tokens": 90}})

        app = web.Application()
        app.router.add_post("/v1/decisions", provider)
        auth, seat = private_directory(tmp_path / "auth"), private_directory(tmp_path / "seat")
        settings = DecisionsSettings(decisions_enabled=True, decisions_routing_policy=POLICY_VERSION)
        authorize_agent(
            auth, "isolated-two-question-test", 0, seat, decisions=True, settings=settings.model_dump(), player="Rome"
        )
        async with server(app) as url:
            broker = DecisionsBroker(
                auth, DecisionsClient("local-two-question-test-key", endpoint=url + "/v1/decisions")
            )
            accounting = JobAccounting(tmp_path / "telemetry")
            try:
                async with (
                    server(broker.application(), socket=tmp_path / "broker.sock"),
                    opened(tmp_path / "player") as (gateway, job, host),
                ):
                    gateway.accounting = accounting
                    clock = [100.0]
                    router = DiplomacyRouting(
                        gateway,
                        RoutingClient(tmp_path / "broker.sock", seat / "decisions-client.json"),
                        settings=settings,
                        clock=lambda: clock[0],
                    )
                    result = await router.classify("incoming", incoming("uncertain response, safe wait"))
                    assert result["choice"] == "batch" and result["rawChoice"] == "no_separate_response"
                    assert result["confidence"] == 0.95 and result["rawChoiceConfidence"] == 0.4
                    assert result["questionBasis"] == "response_timing"
                    assert result["fallback"] == "low_confidence" and result["lowConfidence"] and result["overridden"]
                    assert result["probabilities"] is None and "rawAnswer" not in result
                    assert len(result["rawAnswers"]) == 2 and "PRIVATE" not in json.dumps(result)
                    uncertain = await router.classify("incoming", incoming("uncertain wait"))
                    assert uncertain["choice"] == "respond_now" and uncertain["fallback"] == "low_confidence"
                    suppressed = await router.classify("incoming", incoming("no response"))
                    assert suppressed["choice"] == "no_separate_response" and suppressed["confidence"] == 0.99
                    assert suppressed["questionBasis"] == "response_needed" and suppressed["fallback"] is None
                    urgent = await router.classify("incoming", incoming("urgent"))
                    assert urgent["choice"] == "respond_now" and urgent["fallback"] is None
                    assert urgent["questionBasis"] == "response_timing"

                    forged = router.validate_decision("incoming", {**result, "confidence": 0.89})
                    assert forged["choice"] == "respond_now" and forged["fallback"] == "low_confidence"
                    # A real incoming bundle can batch despite an uncertain skip
                    # answer, and keeps its durable thirty-second wait bound.
                    gateway.memory.routing_enabled = True
                    gateway.memory.ingest("g1", [event(1, text="uncertain response, safe wait")], 1)
                    with gateway.memory.transaction() as db:
                        gateway.memory._set(db, "routing:events", {"1": {"receivedAt": 100.0}})
                    social = {"routingMetadataAvailable": True, "humanPlayers": [], "trades": []}
                    await router.route_incoming("g1", social)
                    with gateway.memory.transaction() as db:
                        state = event_states(gateway.memory, db)["1"]
                        assert state["choice"] == "batch" and state["deadline"] == 130.0
                        assert ready_ids(gateway.memory, db, now=129.0) == []
                        assert ready_ids(gateway.memory, db, now=130.0) == [1]

                    # Outgoing independently judges delivery usefulness and timing.
                    original = host.tool
                    receipts = {}

                    async def tool(name, args):
                        if name == "player_communication" and args["op"] == "receipt":
                            return receipts.get(args["requestId"], {"error": "NOT_ENACTED"})
                        value = await original(name, args)
                        if name == "player_communication" and args["op"] == "send":
                            receipts[args["requestId"]] = value
                        if name == "player_communication" and args["op"] == "state":
                            value.update(social)
                        return value

                    host.tool, gateway.routing = tool, router
                    await gateway.call(job, "chat_read", {}, "read-before-send")
                    queued = await gateway.call(job, "chat_send", {"text": "Useful update", "to": "Greece"}, "send")
                    assert queued["disposition"] == "queued" and not host.writes
                    clock[0] = 130.0
                    await router.flush_due("g1", host)
                    assert len(host.writes) == 1 and host.writes[0]["text"] == "Useful update"
                    before = len(requests)
                    await gateway.call(
                        job, "chat_send", {"text": "Urgent", "to": "Greece", "delivery": "immediate"}, "override"
                    )
                    assert len(host.writes) == 2 and len(requests) == before
                    reports = accounting.classifier_reports()
                    assert reports[0]["confidence"] == 0.95 and reports[0]["rawChoiceConfidence"] == 0.4
                    assert len(reports[0]["rawAnswers"]) == 2 and reports[0]["probabilities"] is None
                    assert "PRIVATE" not in json.dumps(reports)
            finally:
                accounting.engine.dispose()
                broker.ledger.engine.dispose()

    asyncio.run(run())
