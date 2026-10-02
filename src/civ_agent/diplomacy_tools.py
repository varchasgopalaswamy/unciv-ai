"""Small model-facing contracts for social actions; native legality stays in the host."""

from pydantic import ValidationError

from civ_agent.protocol import JsonObject
from civ_agent.schemas import argument_issues, argument_shape


def argument_error(model, arguments, error: ValidationError) -> JsonObject:
    schema = model.model_json_schema()
    shape = schema
    discriminator = schema.get("discriminator", {})
    tag = arguments.get(discriminator.get("propertyName"))
    ref = discriminator.get("mapping", {}).get(tag) if isinstance(tag, str) else None
    if ref:
        shape = schema["$defs"][ref.rsplit("/", 1)[1]]
    return {
        "error": "INVALID_TOOL_ARGUMENTS",
        "applied": False,
        "recoverable": True,
        "invalidFields": argument_issues(error, schema),
        "expectedArguments": argument_shape(shape, schema.get("$defs", {})),
        "instruction": "Correct the indicated arguments. Nothing was dispatched; no game-state refresh is needed.",
    }


def unavailable(field: str, instruction: str, **details) -> JsonObject:
    return {
        "error": "INVALID_TOOL_ARGUMENTS",
        "applied": False,
        "recoverable": True,
        "invalidFields": [{"fields": [field]}],
        "instruction": instruction,
        **details,
    }


def commitment_problem(civilization: str, arguments: JsonObject, social: JsonObject) -> JsonObject | None:
    participants = arguments["participants"]
    recipients = social.get("chatRecipients", [])
    if len(set(participants)) != len(participants) or civilization not in participants:
        return unavailable("participants", "List every participant once, including your own civilization.")
    if any(name != civilization and name not in recipients for name in participants):
        return unavailable("participants", "Use players who receive ordinary chat.", chatRecipients=recipients)
    reference = arguments.get("reviseProposalId")
    if reference:
        offers = [offer for offer in social.get("offers", []) if offer["proposal"] == reference]
        if not offers:
            return unavailable(
                "reviseProposalId", "Copy an existing commitment ID, or omit this field to create a new offer."
            )
        if offers[-1]["author"] != civilization or offers[-1]["participants"] != participants:
            return unavailable(
                "reviseProposalId",
                "You can revise your own offer with the same participants. Create a new offer for different participants.",
            )
    if "expiresTurn" in arguments and arguments["expiresTurn"] <= social["turn"]:
        return unavailable("expiresTurn", "Choose a future turn, or omit expiresTurn.")
    return None


def commitment_responses(social: JsonObject) -> None:
    """Copyable explicit decisions; never supply private intent or public explanations."""
    civilization = social.get("civilization")
    decisions = {(c["proposal"], c["version"], c["author"]): c["decision"] for c in social.get("consents", [])}
    latest = {}
    effective = {}
    for offer in social.get("offers", []):
        latest[offer["proposal"]] = max(latest.get(offer["proposal"], 0), offer["version"])
        if offer.get("effective"):
            effective[offer["proposal"]] = offer["version"]
    for offer in social.get("offers", []):
        reference = (offer["proposal"], offer["version"])
        own = decisions.get((*reference, civilization))
        expired = (
            offer.get("expiresTurn") is not None
            and social["turn"] is not None
            and offer["expiresTurn"] <= social["turn"]
        )
        withdrawn = any(
            decisions.get((*reference, player)) in {"withdraw", "reconsider"} for player in offer["participants"]
        )
        can_change = offer["version"] == effective.get(offer["proposal"], latest[offer["proposal"]])
        choices = (
            ["withdraw", "reconsider"]
            if own == "accept" and not expired and can_change
            else ["accept"]
            if own != "accept" and not expired and not withdrawn and offer["version"] == latest[offer["proposal"]]
            else []
        )
        offer["responses"] = [
            {
                "tool": "commitment_decide",
                "arguments": {"proposalId": offer["proposal"], "decision": decision},
                **({"requiredFields": ["publicReason"]} if decision != "accept" else {}),
            }
            for decision in choices
        ]
