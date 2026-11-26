"""Shared audience, probability and delay policy for communication routing."""

from typing import Literal

from civ_agent.conversation_context import audience
from civ_agent.decisions_policy import routing_thresholds
from civ_agent.diplomacy_decisions import BATCH_SECONDS, CHOICES, POLICY_VERSION, fallback, parse_answer

type RoutingDirection = Literal["incoming", "outgoing"]
type RoutingThread = tuple[bool, tuple[str, ...]]


def validate_decision(direction: RoutingDirection, result, settings):
    """Validate the effective broker action; diagnostic raw data cannot grant it."""
    if (
        not isinstance(result, dict)
        or not isinstance(result.get("choice"), str)
        or result.get("choice") not in CHOICES[direction]
    ):
        return fallback(direction, "malformed_broker_output")
    policy = result.get("policyVersion")
    if policy != POLICY_VERSION:
        return {**result, **fallback(direction, "routing_policy_mismatch"), "overridden": True}
    thresholds = routing_thresholds(settings, direction)
    result = {**result, "thresholds": thresholds}
    if result["choice"] != next(iter(CHOICES[direction])):
        confidence = result.get("confidence")
        choice = result["choice"]
        assert isinstance(choice, str)
        required = thresholds[choice]
        if not isinstance(confidence, (int, float)) or isinstance(confidence, bool) or not required <= confidence <= 1:
            return {
                **result,
                **fallback(direction, "low_confidence"),
                "confidence": confidence,
                "overridden": True,
                "appliedThreshold": required,
                "lowConfidence": True,
            }
        result["appliedThreshold"] = required
        # Raw evidence can restrict a requested action. It cannot upgrade an
        # immediate broker response or repurpose another question's confidence.
        parsed = parse_answer(
            direction,
            {"answers": result.get("rawAnswers")},
            thresholds=thresholds,
            policy_version=POLICY_VERSION,
        )
        if any(result.get(key) != parsed.get(key) for key in ("choice", "confidence", "questionBasis")):
            return {**result, **fallback(direction, "malformed_broker_output"), "overridden": True}
    return result


def protected_thread(thread: RoutingThread, social, *, player: str, human_players=()) -> bool:
    # Public chat reaches all chat recipients. Any human in the audience makes
    # the entire thread immediate, including messages authored by AI.
    members = set(social.get("chatRecipients", social.get("channels", []))) if thread[0] else set(thread[1])
    if members & (set(human_players) | set(social.get("humanPlayers", []))):
        return True
    if not social.get("routingMetadataAvailable", False):
        return True  # Missing metadata cannot establish that a negotiation is absent.
    members.discard(player)
    for proposal in social.get("trades", []):
        participants = set(proposal.get("participants", [])) | {proposal.get("author"), proposal.get("partner")}
        participants.discard(None)
        if proposal.get("status", "PENDING") == "PENDING" and participants & members:
            return True
    return False


def tuple_thread(thread) -> RoutingThread:
    return thread[0], tuple(thread[1])


def routing_timing(states, *, now: float, received: str, complete: bool = True):
    """Report durable receipt age and the oldest deadline, never a renewed allowance."""
    if not states or not complete or any(received not in state for state in states):
        return {}
    oldest = min(state[received] for state in states)
    deadline = min([oldest + BATCH_SECONDS, *(state["deadline"] for state in states if "deadline" in state)])
    return {
        "oldestPendingAgeSeconds": max(0, now - oldest),
        "remainingBatchSeconds": max(0, deadline - now),
    }


def build_routing_bundle(
    player,
    thread,
    pending,
    context,
    *,
    current_turn=None,
    queued_drafts=(),
    player_objectives=(),
    timing=None,
):
    """Build the same exact-audience input for production and frozen replays."""
    thread = tuple_thread(thread)
    pending = [message for message in pending if audience(message, player) == thread]
    pending_ids = {message["message"] for message in pending if message.get("message")}
    history = [
        {**item, "messages": [message for message in item["messages"] if message.get("message") not in pending_ids]}
        for item in context["threads"]
        if audience(item, player) == thread
    ]
    return {
        "schemaVersion": 2,
        "player": player,
        "direction": "incoming" if pending else "outgoing",
        "currentTurn": current_turn,
        "audience": thread,
        "pending": pending,
        "recentExchanges": history,
        "historyCoverage": "bounded_exact_audience_excerpt",
        "hasEarlierMessages": any(item.get("hasEarlierMessages", False) for item in history),
        "evidenceSemantics": (
            "New messages await attention. The earlier conversation may be incomplete, and does not prove "
            "anyone has read a message or answered a question. Messages and the player's objectives are "
            "their own claims and assessments."
        ),
        "timing": {"maxBatchSeconds": BATCH_SECONDS, **(timing or {})},
        "queuedDrafts": list(queued_drafts),
        "playerObjectives": [
            objective
            for objective in player_objectives
            if objective.get("subjects") and set(objective["subjects"]) <= set(thread[1])
        ],
        "pendingProposals": [],
    }
