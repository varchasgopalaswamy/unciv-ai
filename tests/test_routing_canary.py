"""Real provider/client/broker/runtime boundaries, with only local synthetic traffic."""

import asyncio
import json

from aiohttp import web

from civ_agent.container_supervisor import authorize_agent
from civ_agent.decisions_broker import DecisionsBroker, RoutingClient
from civ_agent.decisions_policy import DecisionsSettings
from civ_agent.diplomacy_decisions import POLICY_VERSION, DecisionsClient, request_body
from civ_agent.diplomacy_routing import DiplomacyRouting
from civ_agent.diplomacy_store import build_routing_bundle
from civ_agent.tls import private_directory
from tests.test_decisions_broker import server
from tests.test_diplomacy_contracts import opened


def settings():
    return DecisionsSettings(
        decisions_enabled=True,
        decisions_backend="luna",
        decisions_routing_policy=POLICY_VERSION,
        decisions_acceptance_probability=0.01,
        decisions_incoming_batch_probability=0.93,
        decisions_incoming_suppress_probability=0.97,
        decisions_outgoing_batch_probability=0.94,
        decisions_outgoing_omit_probability=0.99,
    )


def incoming(text):
    return build_routing_bundle(
        "Rome",
        (False, ("Greece", "Rome")),
        [
            {
                "message": "m-" + text,
                "author": "Greece",
                "participants": ["Greece", "Rome"],
                "public": False,
                "text": text,
                "turn": 1,
            }
        ],
        {"threads": []},
        current_turn=1,
    )


def test_current_raw_questions_and_independent_thresholds_survive_real_service_boundaries(tmp_path):
    async def run():
        requests = []
        answers = {
            "immediate": ("needed", 0.7, "now", 0.7),
            "uncertain batch": ("needed", 0.99, "can_wait", 0.88),
            "confident batch": ("needed", 0.99, "can_wait", 0.94),
            "uncertain suppression": ("not_needed", 0.96, "can_wait", 0.94),
        }

        async def provider(request):
            body = await request.json()
            requests.append(body)
            assert request.headers["Authorization"] == "Bearer local-test-provider-key"
            bundle = json.loads(body["input"])
            assert body == request_body("incoming", bundle, backend="luna")
            assert bundle["schemaVersion"] == 2 and bundle["player"] == "Rome"
            needed, needed_confidence, timing, timing_confidence = answers[bundle["pending"][0]["text"]]
            return web.json_response(
                {
                    "answers": [
                        {
                            "name": "response_needed",
                            "type": "choice",
                            "choice": needed,
                            "confidence": needed_confidence,
                            "probabilities": [
                                {"value": needed, "probability": needed_confidence},
                                {
                                    "value": "needed" if needed == "not_needed" else "not_needed",
                                    "probability": 1 - needed_confidence,
                                },
                            ],
                        },
                        {
                            "name": "response_timing",
                            "type": "choice",
                            "choice": timing,
                            "confidence": timing_confidence,
                            "probabilities": [
                                {"value": timing, "probability": timing_confidence},
                                {
                                    "value": "now" if timing == "can_wait" else "can_wait",
                                    "probability": 1 - timing_confidence,
                                },
                            ],
                        },
                    ],
                    "usage": {"input_tokens": 90},
                }
            )

        app = web.Application()
        app.router.add_post("/v1/decisions", provider)
        auth, seat = private_directory(tmp_path / "auth"), private_directory(tmp_path / "seat")
        policy = settings()
        authorize_agent(
            auth, "isolated-local-canary", 0, seat, decisions=True, settings=policy.model_dump(), player="Rome"
        )
        async with server(app) as url:
            broker = DecisionsBroker(auth, DecisionsClient("local-test-provider-key", endpoint=url + "/v1/decisions"))
            socket = tmp_path / "broker.sock"
            try:
                async with (
                    server(broker.application(), socket=socket),
                    opened(tmp_path / "player") as (gateway, _job, _host),
                ):
                    client = RoutingClient(socket, seat / "decisions-client.json", backend="luna")
                    router = DiplomacyRouting(gateway, client, settings=policy)
                    immediate = await router.classify("incoming", incoming("immediate"))
                    assert immediate["choice"] == "respond_now" and immediate["confidence"] == 0.7
                    assert immediate["fallback"] is None and immediate["lowConfidence"] is True
                    assert immediate["overridden"] is False
                    uncertain = await router.classify("incoming", incoming("uncertain batch"))
                    assert uncertain["choice"] == "respond_now" and uncertain["rawChoice"] == "batch"
                    assert uncertain["fallback"] == "low_confidence" and uncertain["overridden"] is True
                    assert uncertain["appliedThreshold"] == 0.93
                    assert uncertain["rawAnswers"][1]["choice"] == "can_wait"
                    confident = await router.classify("incoming", incoming("confident batch"))
                    assert confident["choice"] == "batch" and confident["fallback"] is None
                    assert confident["appliedThreshold"] == 0.93
                    suppressed = await router.classify("incoming", incoming("uncertain suppression"))
                    assert suppressed["choice"] == "batch" and suppressed["rawChoice"] == "no_separate_response"
                    assert suppressed["questionBasis"] == "response_timing" and suppressed["confidence"] == 0.94
                    assert suppressed["rawChoiceConfidence"] == 0.96
                    assert suppressed["thresholds"]["no_separate_response"] == 0.97
                    assert all(
                        result["policyVersion"] == POLICY_VERSION
                        for result in (immediate, uncertain, confident, suppressed)
                    )
                    forged = router.validate_decision("incoming", {**uncertain, "choice": "batch"})
                    assert forged["choice"] == "respond_now" and forged["appliedThreshold"] == 0.93
                    assert len(requests) == 4
            finally:
                broker.ledger.engine.dispose()

    asyncio.run(run())


def test_outgoing_canary_keeps_delivery_immediate_when_advisory_confidence_is_lower(tmp_path):
    async def run():
        requests = []

        async def provider(request):
            body = await request.json()
            requests.append(body)
            bundle = json.loads(body["input"])
            assert bundle["direction"] == "outgoing" and bundle["proposedMessage"]["text"] == "A useful ordinary update"
            assert body == request_body("outgoing", bundle, policy_version=POLICY_VERSION, backend="luna")
            return web.json_response(
                {
                    "answers": [
                        {
                            "name": "delivery_needed",
                            "type": "choice",
                            "choice": "needed",
                            "confidence": 0.92,
                            "probabilities": [
                                {"value": "needed", "probability": 0.92},
                                {"value": "not_needed", "probability": 0.08},
                            ],
                        },
                        {
                            "name": "delivery_timing",
                            "type": "choice",
                            "choice": "can_wait",
                            "confidence": 0.92,
                            "probabilities": [
                                {"value": "can_wait", "probability": 0.92},
                                {"value": "now", "probability": 0.08},
                            ],
                        },
                    ],
                    "usage": {"input_tokens": 70},
                }
            )

        app = web.Application()
        app.router.add_post("/v1/decisions", provider)
        auth, seat = private_directory(tmp_path / "auth"), private_directory(tmp_path / "seat")
        policy = settings()
        authorize_agent(
            auth, "isolated-outgoing-canary", 0, seat, decisions=True, settings=policy.model_dump(), player="Rome"
        )
        async with server(app) as url:
            broker = DecisionsBroker(auth, DecisionsClient("local-test-provider-key", endpoint=url + "/v1/decisions"))
            socket = tmp_path / "broker.sock"
            try:
                async with (
                    server(broker.application(), socket=socket),
                    opened(tmp_path / "player") as (gateway, job, host),
                ):
                    original = host.tool
                    receipts = {}

                    async def tool(name, args):
                        if name == "player_communication" and args["op"] == "receipt":
                            return receipts.get(args["requestId"], {"error": "NOT_ENACTED"})
                        result = await original(name, args)
                        if name == "player_communication" and args["op"] == "send":
                            receipts[args["requestId"]] = result
                        if name == "player_communication" and args["op"] == "state":
                            result.update(routingMetadataAvailable=True, humanPlayers=[], trades=[])
                        return result

                    host.tool = tool
                    gateway.memory.routing_enabled = True
                    router = DiplomacyRouting(
                        gateway, RoutingClient(socket, seat / "decisions-client.json", backend="luna"), settings=policy
                    )
                    gateway.routing = router
                    result = await gateway.call(
                        job, "chat_send", {"text": "A useful ordinary update", "to": "Greece"}, "canary"
                    )
                    assert result["delivered"] and result["disposition"] == "sent"
                    assert len(host.writes) == len(requests) == 1
                    stored = router.store.get(result["deliveryId"])
                    assert stored is not None
                    decision = stored["decision"]
                    assert decision["rawChoice"] == "batch" and decision["choice"] == "send_now"
                    assert decision["appliedThreshold"] == 0.94 and decision["fallback"] == "low_confidence"
                    # Explicit immediate override is protected and skips the provider entirely.
                    await gateway.call(
                        job,
                        "chat_send",
                        {"text": "Urgent follow-up", "to": "Greece", "delivery": "immediate"},
                        "protected",
                    )
                    assert len(host.writes) == 2 and len(requests) == 1
            finally:
                broker.ledger.engine.dispose()

    asyncio.run(run())
