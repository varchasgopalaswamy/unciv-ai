"""Real provider/client/broker/runtime boundaries, with only local synthetic traffic."""

import asyncio
import json

from aiohttp import web

from civ_agent.container_supervisor import authorize_agent
from civ_agent.decisions_broker import DecisionsBroker, RoutingClient
from civ_agent.decisions_policy import DecisionsSettings
from civ_agent.diplomacy_decisions import CANDIDATE_POLICY_VERSION, DecisionsClient, request_body
from civ_agent.diplomacy_routing import DiplomacyRouting
from civ_agent.diplomacy_store import build_routing_bundle
from civ_agent.tls import private_directory
from tests.test_decisions_broker import server
from tests.test_diplomacy_contracts import opened


def settings():
    return DecisionsSettings(
        decisions_enabled=True,
        decisions_routing_policy=CANDIDATE_POLICY_VERSION,
        decisions_min_confidence=0.01,
        decisions_incoming_batch_confidence=0.93,
        decisions_incoming_suppress_confidence=0.97,
        decisions_outgoing_batch_confidence=0.94,
        decisions_outgoing_omit_confidence=0.99,
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


def test_candidate_raw_choices_and_independent_thresholds_survive_real_service_boundaries(tmp_path):
    async def run():
        requests = []
        answers = {
            "immediate": ("respond_now", 0.4),
            "uncertain batch": ("batch", 0.88),
            "confident batch": ("batch", 0.94),
            "uncertain suppression": ("no_separate_response", 0.96),
        }

        async def provider(request):
            body = await request.json()
            requests.append(body)
            assert request.headers["Authorization"] == "Bearer local-test-provider-key"
            bundle = json.loads(body["input"])
            assert body == request_body("incoming", bundle, policy_version=CANDIDATE_POLICY_VERSION)
            assert bundle["schemaVersion"] == 2 and bundle["player"] == "Rome"
            choice, confidence = answers[bundle["pending"][0]["text"]]
            probabilities = [
                {"value": "respond_now", "probability": 0.3},
                {"value": "batch", "probability": 0.5},
                {"value": "no_separate_response", "probability": 0.2},
            ]
            return web.json_response(
                {
                    "answers": [
                        {
                            "name": "delivery",
                            "type": "choice",
                            "choice": choice,
                            "confidence": confidence,
                            "probabilities": probabilities,
                        }
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
                    client = RoutingClient(socket, seat / "decisions-client.json")
                    router = DiplomacyRouting(gateway, client, settings=policy)
                    immediate = await router.classify("incoming", incoming("immediate"))
                    assert immediate["choice"] == "respond_now" and immediate["confidence"] == 0.4
                    assert immediate["fallback"] is None and immediate["lowConfidence"] is True
                    assert immediate["overridden"] is False
                    uncertain = await router.classify("incoming", incoming("uncertain batch"))
                    assert uncertain["choice"] == "respond_now" and uncertain["rawChoice"] == "batch"
                    assert uncertain["fallback"] == "low_confidence" and uncertain["overridden"] is True
                    assert uncertain["appliedThreshold"] == 0.93
                    assert uncertain["rawAnswer"]["choice"] == "batch"
                    assert (
                        uncertain["probabilities"]
                        == uncertain["rawAnswer"]["probabilities"]
                        == [
                            {"value": "respond_now", "probability": 0.3},
                            {"value": "batch", "probability": 0.5},
                            {"value": "no_separate_response", "probability": 0.2},
                        ]
                    )
                    confident = await router.classify("incoming", incoming("confident batch"))
                    assert confident["choice"] == "batch" and confident["fallback"] is None
                    assert confident["appliedThreshold"] == 0.93
                    suppressed = await router.classify("incoming", incoming("uncertain suppression"))
                    assert suppressed["choice"] == "respond_now" and suppressed["rawChoice"] == "no_separate_response"
                    assert suppressed["appliedThreshold"] == 0.97
                    assert all(
                        result["policyVersion"] == CANDIDATE_POLICY_VERSION
                        for result in (immediate, uncertain, confident, suppressed)
                    )
                    # Raw metadata cannot authorize an effective action at the runtime boundary.
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
            assert body == request_body("outgoing", bundle, policy_version=CANDIDATE_POLICY_VERSION)
            return web.json_response(
                {
                    "answers": [
                        {
                            "name": "delivery",
                            "type": "choice",
                            "choice": "batch",
                            "confidence": 0.92,
                            "probabilities": [
                                {"value": "send_now", "probability": 0.07},
                                {"value": "batch", "probability": 0.92},
                                {"value": "omit", "probability": 0.01},
                            ],
                        }
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
                        gateway, RoutingClient(socket, seat / "decisions-client.json"), settings=policy
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
