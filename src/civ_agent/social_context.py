"""Current social context; historical records remain available by explicit read."""

import copy
from typing import Literal

from civ_agent.protocol import JsonObject


def social_context(
    value: JsonObject,
    *,
    include_context: bool,
    include_history: bool,
    include_native_trades: bool,
    proposal_field: Literal["proposal", "proposalId"] = "proposal",
) -> JsonObject:
    """Filter native state or its model projection, using that boundary's reference field."""
    result = copy.deepcopy(value)
    result["chatRecipients"] = [name for name in result.pop("channels", []) if name != result["civilization"]]
    if not include_native_trades:
        result["trades"] = [trade for trade in result.get("trades", []) if trade["mode"] == "SHARED"]
    result.pop("negotiation", None)
    context = result.pop("context", {})
    result["turn"] = context.pop("turn", None)
    if include_context:
        result["context"] = context
    if include_history:
        return result
    offers = value.get("offers", [])
    latest = {}
    decisions = {}
    for offer in offers:
        key = offer[proposal_field]
        if key not in latest or offer["version"] > latest[key]["version"]:
            latest[key] = offer
    for consent in value.get("consents", []):
        decisions.setdefault((consent[proposal_field], consent["version"]), {})[consent["author"]] = consent
    turn = result["turn"]

    def current(offer):
        if offer.get("effective"):
            return True
        if offer != latest[offer[proposal_field]]:
            return False
        expiry = offer.get("expiresTurn")
        if turn is not None and expiry is not None and expiry <= turn:
            return False
        return not any(
            c["decision"] in {"withdraw", "reconsider"}
            for c in decisions.get((offer[proposal_field], offer["version"]), {}).values()
        )

    result["offers"] = [copy.deepcopy(offer) for offer in offers if current(offer)]
    selected = {(offer[proposal_field], offer["version"]) for offer in result["offers"]}
    result["consents"] = [copy.deepcopy(c) for key in sorted(selected) for c in decisions.get(key, {}).values()]
    result.pop("outcomes", None)  # Delivered as events; older outcomes are in chat_history.
    result["trades"] = [trade for trade in result.get("trades", []) if trade.get("status") == "PENDING"]
    result["agreements"] = [a for a in result.get("agreements", []) if a.get("status") == "ACTIVE"]
    return result
