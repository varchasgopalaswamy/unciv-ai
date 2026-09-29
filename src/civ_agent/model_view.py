"""Project gameplay facts, omitting native flavor and controller internals.

Match owned protocol structures, not arbitrary text/description keys or phrases.
Raw host observations and receipts remain the audit source. See the projection
contract and linked field audit in docs/architecture/Agent-context.md.
"""

import json
from typing import Any

from civ_agent.protocol import JsonObject

NARRATIVE_ALERTS = {
    "StartIntro",
    "FirstContact",
    "GoldenAge",
    "WarDeclaration",
    "Defeated",
    "GameHasBeenWon",
}


def popup_content(content: JsonObject, kind: str | None = None) -> JsonObject:
    value = dict(content)
    kind = value.get("kind") or kind
    if kind:
        value["kind"] = kind
    gameplay = value.pop("gameplayParagraphs", None)
    if gameplay is not None:
        value["paragraphs"] = gameplay
    elif kind in NARRATIVE_ALERTS:
        value["paragraphs"] = []
    omitted = ["quote"]
    if gameplay is not None or kind in NARRATIVE_ALERTS | {"TechResearched", "WonderBuilt"}:
        omitted += ["acknowledgement", "additionalAcknowledgements"]
    for key in omitted:
        value.pop(key, None)
    return value


def native_options(native: JsonObject, recipient: str) -> JsonObject:
    value = dict(native)
    if "demands" in value:
        value["availableDemandsToSend"] = [
            {"type": demand["name"], "recipient": recipient, **demand["action"]} for demand in value.pop("demands")
        ]
    if "promises" in value:
        value["promises"] = [{k: v for k, v in promise.items() if k != "text"} for promise in value["promises"]]
    return value


def player_alert(alert: JsonObject) -> JsonObject:
    value = dict(alert)
    kind = value["kind"]
    if isinstance(value.get("content"), dict):
        value["content"] = popup_content(value["content"], kind)
    if isinstance(value.get("firstContact"), dict):
        value["firstContact"] = {
            k: v for k, v in value["firstContact"].items() if k not in {"message", "acknowledgement"}
        }
    if isinstance(value.get("nativeDiplomacy"), dict):
        native = dict(value["nativeDiplomacy"])
        native.pop("content", None)
        native["options"] = [{k: v for k, v in option.items() if k != "text"} for option in native["options"]]
        value["nativeDiplomacy"] = native
    if isinstance(value.get("friendship"), dict):
        friendship = dict(value["friendship"])
        friendship["sender"] = friendship["civilizationId"]
        friendship.pop("content", None)
        value["friendship"] = friendship
    return value


def trade_inspection(proposal: str) -> JsonObject:
    return {"tool": "trade_read", "arguments": {"proposal": proposal}}


def model_view(value: Any) -> Any:
    """Idempotent projection used for fresh, historical and replayed model data."""
    if isinstance(value, list):
        return [model_view(item) for item in value]
    if not isinstance(value, dict):
        return value
    value = dict(value)
    # Match owned response shapes: words such as "deadline" and "mode" can also
    # occur in player-authored promises and must not be stripped indiscriminately.
    if {"partner", "give", "receive", "canInitiate"} <= value.keys():
        for key in ("mode", "initiationRule", "negotiation"):
            value.pop(key, None)
    if {"proposal", "author", "partner", "status"} <= value.keys():
        # Summaries and chat events provide an exact read command. Only explicit
        # availability from an inspected trade can produce decision templates.
        value.pop("inspection", None)
        value.pop("responses", None)
        if value["status"] == "PENDING":
            value["inspection"] = trade_inspection(value["proposal"])
            responses = [
                {"tool": "trade_decide", "arguments": {"proposal": value["proposal"], "decision": decision}}
                for decision in ("accept", "decline", "withdraw")
                if value.get(decision + "Available") is True
            ]
            if responses:
                value["responses"] = responses
        for key in (
            "mode",
            "deadlineMillis",
            "counterofferLimit",
            "counteroffersRemaining",
            "nativeRequestId",
            "awaitingNativeTurn",
            "pausesGameplay",
        ):
            value.pop(key, None)
        if value.get("failure") == "The negotiation deadline elapsed.":
            value["failure"] = "This proposal expired."
        if value.get("unavailableReason") == "The negotiation deadline elapsed.":
            value["unavailableReason"] = "This proposal expired."
        if value.get("unavailableReason") == "Awaiting the built-in AI's native turn.":
            value["unavailableReason"] = "Awaiting a response."
    if {"civilizationId", "missingLuxuriesOffered", "spareLuxuriesToOffer"} <= value.keys():
        value.pop("mode", None)
    if {"id", "kind", "body", "wake"} <= value.keys():
        value.pop("wake")
    if "id" in value and isinstance(value.get("type"), str):
        if value["type"] in {"strategy", "handoff", "belief", "note"}:
            value.pop("basis", None)
        elif value["type"] == "contribution" and isinstance(value.get("body"), dict):
            value["body"] = {k: v for k, v in value["body"].items() if k != "basis"}
    if {"turn", "canEndTurn"} <= value.keys() or {"offers", "consents", "trades"} <= value.keys():
        value.pop("negotiation", None)
    if value.get("kind") == "delta" and isinstance(value.get("set"), dict):
        value["set"] = {k: v for k, v in value["set"].items() if k != "negotiation"}
    if {"queries", "commands"} <= value.keys():
        for key in ("protocolVersion", "completeGameplay", "durableRecovery"):
            value.pop(key, None)
    if isinstance(value.get("trade"), dict) and "error" not in value:
        value.pop("instruction", None)
    if isinstance(value.get("error"), dict) and value["error"].get("code") == "TURN_REVIEW_REQUIRED":
        value["error"] = {
            **value["error"],
            "message": "Read player_turn for the current turn review, then reconsider END_TURN.",
        }
        value.update(
            applied=False,
            recoverable=True,
            recovery={
                "reads": [{"tool": "player_turn", "arguments": {}}],
                "instruction": "Refresh the turn review, then issue END_TURN with gameplay choices only.",
            },
        )
    if {"handle", "kind", "canAcknowledge"} <= value.keys():
        value = player_alert(value)
    if {"civilizationId", "native"} <= value.keys() and isinstance(value["native"], dict):
        value["native"] = native_options(value["native"], value["civilizationId"])
    if {"title", "paragraphs", "acknowledgement"} <= value.keys():
        value = popup_content(value)
    if {"researchNow", "freeChoice", "gameplayDescription"} <= value.keys():
        gameplay = value.pop("gameplayDescription")
        if gameplay is not None:
            value["description"] = gameplay
    if {"outcome", "winningCivilizationId", "victoryType", "paragraphs"} <= value.keys():
        value.pop("paragraphs")
    if {"name", "influenceReward", "objective"} <= value.keys() and value["objective"] is not None:
        value.pop("description", None)
        # In these two quests the named civilization is the recent bully, not
        # the recipient of the requested gift/protection (the issuer is).
        if value["name"] in {"Give Gold", "Pledge to Protect"} and "targetCivilization" in value:
            value["recentBully"] = value.pop("targetCivilization")
    # These are native UI trade requests, not player-written chat or offers.
    if {"civilizationId", "leaderName", "give", "receive", "message"} <= value.keys():
        value.pop("message")
    projected = {key: model_view(item) for key, item in value.items()}
    # Archived MCP replies may contain the same native object in both forms.
    # Rewrite only text blocks proven to be that object, never arbitrary strings
    # in chat, model prose or tool output.
    structured = value.get("structuredContent")
    if isinstance(structured, dict) and structured != projected["structuredContent"]:
        for block in projected.get("content", []):
            if not isinstance(block, dict) or block.get("type") != "text":
                continue
            try:
                duplicate = json.dumps(json.loads(block["text"]), sort_keys=True) == json.dumps(
                    structured, sort_keys=True
                )
            except (KeyError, TypeError, ValueError):
                continue
            if duplicate:
                block["text"] = json.dumps(projected["structuredContent"], ensure_ascii=False, separators=(",", ":"))
    return projected
