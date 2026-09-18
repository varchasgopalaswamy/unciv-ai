"""Private planning subjects affected by authenticated native agreement changes."""

from civ_agent.protocol import JsonObject


def lifecycle_terms(record: JsonObject) -> JsonObject:
    """Routine countdowns alone do not change subject-specific strategies."""
    return {
        "status": record.get("status"),
        **{
            direction: [
                {
                    **{key: value for key, value in term.items() if key not in {"duration", "displayText"}},
                    "active": term.get("duration") != 0,
                }
                for term in record.get(direction, [])
            ]
            for direction in ("give", "receive")
        },
    }


def affected_subjects(body: JsonObject) -> set[str]:
    subjects = {value for key in ("agreement", "partner") if isinstance(value := body.get(key), str)}
    terms = [
        term
        for state in ("before", "after")
        for direction in ("give", "receive")
        for term in body.get(state, {}).get(direction, [])
    ]
    if body.get("kind") == "RESEARCH_AGREEMENT" or any(
        term.get("type") in {"Gold_Per_Turn", "Luxury_Resource", "Strategic_Resource"}
        or term.get("name") == "Research Agreement"
        for term in terms
    ):
        subjects.add("economy")
    if body.get("kind") == "DEFENSIVE_PACT" or any(
        term.get("name") in {"Peace Treaty", "Defensive Pact", "Open Borders"} for term in terms
    ):
        subjects.add("defense")
    if any(term.get("name") == "Open Borders" for term in terms):
        subjects.add("expansion")
    return subjects
