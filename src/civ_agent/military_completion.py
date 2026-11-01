"""A factual completion review; choosing to hold or accept risk remains legal."""

from typing import Annotated

from pydantic import Field

from civ_agent.decisions_ledger import fingerprint
from civ_agent.military_schedule import military_facts
from civ_agent.protocol import JsonObject
from civ_agent.schemas import BoundaryModel
from civ_agent.unit_ownership import unit_owner
from civ_agent.war_state import goal_versions, war_opponents


class MilitaryReviewRead(BoundaryModel):
    cursor: int = Field(default=0, ge=0)
    limit: int = Field(default=20, ge=1, le=100)


class MilitaryDisposition(BoundaryModel):
    targets: list[Annotated[str, Field(min_length=1, max_length=128)]] = Field(min_length=1)
    reason: str = Field(
        min_length=1,
        max_length=500,
        pattern=r"\S",
        description="Why these units/cities should hold their current position or leave the remaining opportunities unused. Deliberate risk is permitted.",
    )


class MilitaryCompletion(BoundaryModel):
    basis: str = Field(min_length=1, max_length=64)
    decisions: list[MilitaryDisposition] = Field(default_factory=list)


def completion_review(view, state, saved=None) -> JsonObject:
    units = (view.get("map") or {}).get("units", view.get("units", []))
    controlled = {u["handle"]: u for u in units if unit_owner(u, state) == "military"}
    captures = {}
    for tile in (view.get("map") or {}).get("tiles", []):
        occupants = tile.get("units", [])
        if not tile.get("visible") or tile.get("cityName") or any(u.get("military") for u in occupants):
            continue
        if not any(u.get("atWar") and u.get("capture") for u in occupants):
            continue
        for handle, unit in controlled.items():
            if any(move.get("destination") == tile.get("position") for move in unit.get("moves", [])):
                captures.setdefault(handle, []).append(tile["position"])
    items, evidence = [], {}
    for handle, unit in controlled.items():
        combat = unit.get("combat") or {}
        threats, attacks = unit.get("nearbyHostiles", []), combat.get("attacks", [])
        if not threats and not attacks and handle not in captures:
            continue
        items.append(
            {
                "target": handle,
                "kind": "unit",
                "health": unit.get("health"),
                "position": unit.get("position"),
                "movement": unit.get("movement"),
                "order": (combat.get("order") or {}).get("action"),
                "nearbyHostiles": len(threats),
                "attackOptions": len(attacks),
                "captureDestinations": captures.get(handle, []),
                "read": {
                    "tool": "unit_details",
                    "arguments": {"target": handle, "sections": ["combat", "movement", "orders"]},
                },
            }
        )
        evidence[handle] = {"threats": threats, "combat": combat, "moves": unit.get("moves", [])}
    for city in view.get("cities", []):
        attacks = city.get("attacks", [])
        if attacks:
            handle = city["handle"]
            items.append(
                {
                    "target": handle,
                    "kind": "city",
                    "attackOptions": len(attacks),
                    "read": {"tool": "city_details", "arguments": {"target": handle, "sections": ["combat"]}},
                }
            )
            evidence[handle] = attacks
    operations = {k: v for k, v in state.get("operations", {}).items() if v.get("status") == "active"}
    basis = fingerprint(
        {
            "epoch": view.get("epoch"),
            "turn": view.get("turn"),
            "items": items,
            "evidence": evidence,
            "wars": sorted(war_opponents(view)),
            "goals": goal_versions(state),
            "operations": operations,
        }
    )
    saved = saved or {}
    previous = saved.get("baseline") or {}
    current = military_facts(view, state)
    progress = None
    if previous:
        old_units, new_units = previous.get("units", {}), current["units"]
        old_cities, new_cities = previous.get("cities", {}), current["cities"]
        progress = {
            "sinceTurn": saved.get("lastReviewTurn"),
            "unitsAddedOrAssigned": len(new_units.keys() - old_units.keys()),
            "unitsLostOrReassigned": len(old_units.keys() - new_units.keys()),
            "unitsMoved": sum(
                new_units[k]["position"] != old_units[k].get("position") for k in new_units.keys() & old_units.keys()
            ),
            "unitsWithChangedHealth": sum(
                new_units[k]["health"] != old_units[k].get("health") for k in new_units.keys() & old_units.keys()
            ),
            "citiesGained": len(new_cities.keys() - old_cities.keys()),
            "citiesLost": len(old_cities.keys() - new_cities.keys()),
            "history": {"tool": "history_read", "arguments": {"turn": saved.get("lastReviewTurn")}},
        }
    return {
        "basis": basis,
        "items": items,
        "observedProgress": progress,
        "operations": {
            "active": len(operations),
            "read": {"tool": "war_goals", "arguments": {"section": "operations", "status": "active"}},
        },
        "instruction": "Resolve these exposures or opportunities with native actions, or include reasoned hold decisions for every remaining target in finish.arguments.decisions. The host binds this delivered review. Group targets when the reason is shared. Compare observed progress with accepted objectives and update military_operation only when its assessment or plan changes.",
        "limits": "Visible proximity is not enemy reach; unseen threats remain unknown. A hold may deliberately accept risk. Counts do not establish an operation's success, and lost/reassigned units do not establish combat losses.",
    }


def review_page(review, cursor=0, limit=20):
    items = review["items"]
    end = min(cursor + limit, len(items))
    return {
        **review,
        "items": items[cursor:end],
        "total": len(items),
        "nextCursor": end if end < len(items) else None,
        "read": {"tool": "military_review", "arguments": {"cursor": end if end < len(items) else 0, "limit": limit}},
    }


def completion_problem(review, supplied):
    if not review["items"] and supplied is None:
        return None
    targets = [target for decision in (supplied or {}).get("decisions", []) for target in decision["targets"]]
    if (
        (supplied or {}).get("basis") == review["basis"]
        and len(targets) == len(set(targets))
        and set(targets) == {item["target"] for item in review["items"]}
    ):
        return None
    return {
        "error": "MILITARY_COMPLETION_REVIEW_REQUIRED",
        "applied": False,
        "recoverable": True,
        "militaryCompletion": review_page(review),
        "recovery": {
            "instruction": "Use the returned completion review; retrieve remaining pages if nextCursor is present. "
            "Issue any chosen actions, then use a fresh basis and reasoned holds for the remaining targets. "
            "Do not repeat confirmed actions.",
        },
    }
