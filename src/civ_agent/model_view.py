"""Remove audited native flavor from copies, never from player-authored wording.

Match owned protocol structures, not arbitrary text/description keys or phrases.
Raw host observations and receipts remain the audit source. See the field audit
in docs/Model-flavor-text-audit.md.
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
NATIVE_MESSAGES = {
    "DemandToStopSettlingCitiesNear",
    "DemandToStopSpreadingReligion",
    "DemandToStopSpyingOnUs",
    "DemandToNotAttackUs",
    "CitySettledNearOtherCivDespiteOurPromise",
    "ReligionSpreadDespiteOurPromise",
    "SpyingOnUsDespiteOurPromise",
    "AttackedUsDespitePromise",
    "BorderConflict",
    "TilesStolen",
    "Denounced",
    "AcceptingDemand",
    "RejectingDemand",
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
        native["event"] = native.get("event") or kind
        native["sender"] = native.get("sender") or native["civilizationId"]
        # Older hosts lack structured incident identities. Keep their incident
        # descriptions until those facts can be represented without dialogue.
        if kind in NATIVE_MESSAGES or native.get("incidentCityState") is not None:
            native.pop("content", None)
            native["options"] = [{k: v for k, v in option.items() if k != "text"} for option in native["options"]]
            for option in native["options"]:
                if "declaresWar" not in option:
                    option["declaresWar"] = option["name"] == "DeclareWar" or (
                        kind == "DemandToNotAttackUs" and option["name"] == "Refuse"
                    )
        value["nativeDiplomacy"] = native
    if isinstance(value.get("friendship"), dict):
        friendship = dict(value["friendship"])
        friendship["sender"] = friendship["civilizationId"]
        # On old hosts the only duration is in the native button label.
        if "duration" in friendship:
            friendship.pop("content", None)
        value["friendship"] = friendship
    return value


def model_view(value: Any) -> Any:
    """Idempotent projection used for fresh, historical and replayed model data."""
    if isinstance(value, list):
        return [model_view(item) for item in value]
    if not isinstance(value, dict):
        return value
    value = dict(value)
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
