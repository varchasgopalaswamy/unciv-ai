"""Dated review briefs and factual reconsideration, without judging authored plans."""

import copy


def factual_baseline(observation):
    diplomacy = observation.get("diplomacy")
    relations = (
        [*diplomacy.get("knownCivilizations", []), *diplomacy.get("cityStates", [])]
        if isinstance(diplomacy, dict) and {"knownCivilizations", "cityStates"} & diplomacy.keys()
        else diplomacy
        if isinstance(diplomacy, list)
        else None
    )
    units = (observation.get("map") or {}).get("units", observation.get("units"))
    # Missing surfaces stay unknown, especially in older checkpoints.
    return {
        "relations": {
            row["civilizationId"]: {key: copy.deepcopy(row[key]) for key in ("status", "atWar") if key in row}
            | {
                "treaties": {
                    key: (value > 0 if key.endswith("Turns") and isinstance(value, int) else value)
                    for key, value in (
                        row.get("bilateral") or {"peaceTreatyTurns": row.get("peaceTreatyTurns")}
                    ).items()
                }
            }
            for row in relations or []
            if "civilizationId" in row
        }
        if relations is not None
        else None,
        "cities": {row["handle"]: row.get("name") for row in observation.get("cities", [])}
        if "cities" in observation
        else None,
        "units": {row["handle"]: row.get("name") for row in units or []} if units is not None else None,
    }


def review_brief(advice, observation, purpose):
    if not advice or "asOfTurn" not in advice:
        return None
    turn = advice["asOfTurn"]
    result = {
        "asOfTurn": turn,
        "read": {"tool": "history_read", "arguments": {"turn": turn, "includeReview": True}},
        "basis": "Historical advice; current strategy, city plans and accepted objectives remain authored decisions.",
    }
    baseline = advice.get("_contextFacts")
    if baseline is None:
        result.update(
            status="BASELINE_UNAVAILABLE",
            instruction="This older review has no factual baseline. Retrieve it and compare against current observations before using its recommendations.",
        )
        return result
    current = factual_baseline(observation)
    changed = [
        {"surface": key, "before": old, "current": current.get(key)}
        for key, old in baseline.items()
        if old != current.get(key)
    ]
    if changed:
        result.update(
            status="RECONSIDER",
            changedFacts=changed,
            instruction="Facts have changed since this review. Retrieve it if useful and reconsider its recommendations. This does not declare an assessment false, retire an accepted plan, or restrict legal actions.",
        )
    elif purpose in {"gameplay", "communication"}:
        review = advice.get("review", {})
        result.update(
            status="CURRENT_BASELINE",
            assessment=advice.get("assessment"),
            preferredPlan=review.get("preferredPlan"),
            reconsiderWhen=review.get("reconsiderWhen"),
            plans=[
                {key: plan[key] for key in ("name", "objective") if key in plan} for plan in review.get("plans", [])
            ],
        )
    else:
        result["status"] = "HISTORICAL_REVIEW_AVAILABLE"
    return result
