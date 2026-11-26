"""Shared routing policy keeps exact audiences and bounded independent judgments."""

import copy

import pytest

from civ_agent.decisions_policy import DecisionsSettings
from civ_agent.diplomacy_decisions import POLICY_VERSION, parse_answer
from civ_agent.routing_policy import build_routing_bundle, protected_thread, routing_timing, validate_decision


def classified(direction, *, needed="needed", timing="can_wait", confidence=0.99):
    names = (
        ("response_needed", "response_timing") if direction == "incoming" else ("delivery_needed", "delivery_timing")
    )
    return {
        **parse_answer(
            direction,
            {
                "answers": [
                    {"name": names[0], "type": "choice", "choice": needed, "confidence": confidence},
                    {"name": names[1], "type": "choice", "choice": timing, "confidence": confidence},
                ]
            },
        ),
        "policyVersion": POLICY_VERSION,
    }


@pytest.mark.parametrize(
    "direction,immediate,suppress",
    [("incoming", "respond_now", "no_separate_response"), ("outgoing", "send_now", "omit")],
)
def test_current_thresholds_and_question_basis_control_delayed_or_suppressed_attention(direction, immediate, suppress):
    settings = DecisionsSettings(
        decisions_incoming_batch_probability=0.995,
        decisions_outgoing_batch_probability=0.995,
        decisions_incoming_suppress_probability=0.985,
        decisions_outgoing_omit_probability=0.985,
    )
    delayed = classified(direction)
    before = copy.deepcopy(delayed)
    result = validate_decision(direction, delayed, settings)
    assert result["choice"] == immediate and result["fallback"] == "low_confidence"
    assert result["appliedThreshold"] == 0.995
    assert delayed == before

    unnecessary = classified(direction, needed="not_needed", timing="now")
    assert validate_decision(direction, unnecessary, settings)["choice"] == suppress
    forged = {**delayed, "choice": suppress, "questionBasis": unnecessary["questionBasis"]}
    # A high value for the timing question cannot grant suppression.
    forged["confidence"] = 1.0
    checked = validate_decision(direction, forged, settings)
    assert checked["choice"] == immediate and checked["fallback"] == "malformed_broker_output"


@pytest.mark.parametrize("direction,immediate", [("incoming", "respond_now"), ("outgoing", "send_now")])
def test_diagnostics_cannot_upgrade_an_immediate_result_or_bypass_policy_version(direction, immediate):
    result = classified(direction)
    immediate_result = {**result, "choice": immediate, "fallback": "routing_deadline"}
    checked = validate_decision(direction, immediate_result, DecisionsSettings())
    assert checked["choice"] == immediate and checked["fallback"] == "routing_deadline"
    checked = validate_decision(direction, {**result, "policyVersion": "old-policy"}, DecisionsSettings())
    assert checked["choice"] == immediate and checked["fallback"] == "routing_policy_mismatch"


@pytest.mark.parametrize(
    "thread,social,humans,protected",
    [
        ((False, ("Greece", "Rome")), {"routingMetadataAvailable": True}, (), False),
        ((False, ("Greece", "Rome")), {}, (), True),
        ((False, ("Greece", "Rome")), {"routingMetadataAvailable": True}, ("Greece",), True),
        ((False, ("Greece", "Rome")), {"routingMetadataAvailable": True, "humanPlayers": ["Greece"]}, (), True),
        ((False, ("Greece", "Rome")), {"routingMetadataAvailable": True, "humanPlayers": ["France"]}, (), False),
        ((True, ()), {"routingMetadataAvailable": True, "chatRecipients": ["Greece", "France"]}, ("France",), True),
        ((True, ()), {"routingMetadataAvailable": True, "channels": ["Greece", "France"]}, ("France",), True),
        ((True, ()), {"routingMetadataAvailable": True, "chatRecipients": ["Greece"]}, ("France",), False),
    ],
)
def test_human_protection_uses_the_entire_exact_audience(thread, social, humans, protected):
    before = copy.deepcopy(social)
    assert protected_thread(thread, social, player="Rome", human_players=humans) is protected
    assert social == before


@pytest.mark.parametrize(
    "proposal,protected",
    [
        ({"author": "Rome", "partner": "Greece", "status": "PENDING"}, True),
        ({"participants": ["Greece", "Rome"]}, True),
        ({"participants": ["Greece", "France"], "status": "PENDING"}, True),
        ({"author": "Rome", "partner": "France", "status": "PENDING"}, False),
        ({"author": "Rome", "partner": "Greece", "status": "ACCEPTED"}, False),
        ({"author": "Rome", "partner": "Greece", "status": "REJECTED"}, False),
    ],
)
def test_pending_negotiation_protects_its_participant_audience(proposal, protected):
    assert (
        protected_thread(
            (False, ("Greece", "Rome")),
            {"routingMetadataAvailable": True, "trades": [proposal]},
            player="Rome",
        )
        is protected
    )


@pytest.mark.parametrize("received", ["receivedAt", "createdAt"])
def test_coalescing_and_later_classification_never_extend_the_oldest_deadline(received):
    states = [{received: 100.0, "deadline": 125.0}, {received: 120.0, "deadline": 150.0}]
    assert routing_timing(states, now=122, received=received) == {
        "oldestPendingAgeSeconds": 22.0,
        "remainingBatchSeconds": 3.0,
    }
    assert routing_timing(states, now=130, received=received) == {
        "oldestPendingAgeSeconds": 30.0,
        "remainingBatchSeconds": 0,
    }
    # Even a stored deadline greater than the receipt's ordinary window cannot
    # enlarge the policy allowance.
    assert (
        routing_timing([{received: 100.0, "deadline": 999.0}], now=129, received=received)["remainingBatchSeconds"]
        == 1.0
    )


@pytest.mark.parametrize("states,complete", [([], True), ([{}], True), ([{"receivedAt": 100}], False)])
def test_unknown_receipt_timing_does_not_establish_another_delay_window(states, complete):
    assert routing_timing(states, now=120, received="receivedAt", complete=complete) == {}


def test_routing_bundle_excludes_other_audiences_pending_duplicates_and_unscoped_objectives():
    pending = {"message": "m2", "public": False, "participants": ["Greece", "Rome"], "text": "New exact text"}
    prior = {**pending, "message": "m1", "text": "Prior exact text"}
    other = {**pending, "message": "secret", "participants": ["France", "Rome"], "text": "PRIVATE SECRET"}
    context = {
        "threads": [
            {"public": False, "participants": ["Greece", "Rome"], "messages": [prior, pending]},
            {"public": False, "participants": ["France", "Rome"], "messages": [other]},
        ]
    }
    before = copy.deepcopy(context)
    objective = {"subjects": ["Greece"], "text": "Player-authored intent"}
    result = build_routing_bundle(
        "Rome",
        (False, ("Greece", "Rome")),
        [pending, other],
        context,
        player_objectives=[objective, {"subjects": ["France"], "text": "PRIVATE OBJECTIVE"}, {"text": "Unscoped"}],
        timing={"oldestPendingAgeSeconds": 25, "remainingBatchSeconds": 5},
    )
    assert result["pending"] == [pending]
    assert result["recentExchanges"][0]["messages"] == [prior]
    assert len(result["recentExchanges"]) == 1 and result["playerObjectives"] == [objective]
    assert result["timing"] == {"maxBatchSeconds": 30, "oldestPendingAgeSeconds": 25, "remainingBatchSeconds": 5}
    assert context == before
