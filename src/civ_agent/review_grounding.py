"""Small, named facts from a review's frozen player-visible evidence."""

import copy
from typing import Annotated, Literal

from pydantic import Field, StrictBool, StrictFloat, StrictInt, StrictStr, model_validator

from civ_agent.protocol import JsonObject
from civ_agent.schemas import BoundaryModel

type Scalar = StrictBool | StrictInt | StrictFloat | StrictStr | None


class ReviewFactReference(BoundaryModel):
    id: Annotated[str, Field(min_length=1, max_length=240)]
    value: Scalar = Field(description="Exact value of this named fact, including null for unknown.")


class MaterialAssumption(BoundaryModel):
    assessment: Annotated[str, Field(min_length=1, max_length=400)]
    status: Literal["inferred", "unknown"] = Field(
        description="An interpretation of the cited facts, or an unresolved assumption; never a certified fact."
    )
    facts: Annotated[list[ReviewFactReference], Field(max_length=6)]

    @model_validator(mode="after")
    def inference_has_evidence(self):
        if self.status == "inferred" and not self.facts:
            raise ValueError("An inference must cite a supplied fact; unsupported assumptions are unknown")
        return self


def named_facts(
    observation: JsonObject, victory: JsonObject | None = None, *, civilization: str | None = None
) -> list[JsonObject]:
    """Use only the caller's permitted snapshot; unknown foreign totals stay unknown."""
    turn = observation.get("turn")
    facts = []

    def add(id, subject, metric, value, definition, source):
        facts.append(
            {
                "id": id,
                "subject": subject,
                "metric": metric,
                "value": value,
                "definition": definition,
                "observedTurn": turn,
                "source": source,
            }
        )

    population_metric = None
    if victory:
        for index, metric in enumerate(victory.get("metrics", [])):
            if metric["name"] == "Population":
                population_metric = index
                break
        for row in (victory.get("rankings") or {}).get("rows", []):
            rival = row["civilization"]
            values = row.get("values")
            if population_metric is not None and values is not None:
                add(
                    f"rival/{rival}/totalPopulation",
                    rival,
                    "totalPopulation",
                    values[population_metric],
                    "Sum of city population. This is not a count of cities.",
                    "victory_progress.rankings",
                )

    locations: dict[str, dict] = {}
    for tile in observation.get("map", {}).get("tiles", []):
        owner = tile.get("owner")
        if owner and tile.get("cityName"):
            group = locations.setdefault(owner, {"known": set(), "visible": set()})
            position = tile["position"]
            key = (position["x"], position["y"])
            group["known"].add(key)
            if tile.get("visible"):
                group["visible"].add(key)
    civilizations: set[str] = set(locations)
    for fact in facts:
        if isinstance(fact["subject"], str):
            civilizations.add(fact["subject"])
    if civilization is not None:
        civilizations.discard(civilization)
    for rival in sorted(civilizations):
        counts = locations.get(rival, {"known": set(), "visible": set()})
        add(
            f"rival/{rival}/empireCityCount",
            rival,
            "empireCityCount",
            None,
            "Total city count is not established by population rankings or partial map sightings.",
            "player_turn.map coverage",
        )
        add(
            f"rival/{rival}/visibleCityCountLowerBound",
            rival,
            "visibleCityCountLowerBound",
            len(counts["visible"]),
            "Currently visible city locations only; not the empire's total city count.",
            "player_turn.map.tiles",
        )
        add(
            f"rival/{rival}/observedCityLocationsCount",
            rival,
            "observedCityLocationsCount",
            len(counts["known"]),
            "Known locations include last observations; old sightings need not still be cities.",
            "player_turn.map.tiles",
        )
    if "cities" in observation:
        add(
            "our/empireCityCount",
            civilization or "our civilization",
            "empireCityCount",
            len(observation["cities"]),
            "Exact number of owned cities in our player overview.",
            "player_turn.cities",
        )
    for city in observation.get("cities", []):
        subject = city.get("handle", city.get("name", ""))
        development = city.get("development") or {}
        if "population" in development:
            add(
                f"city/{subject}/population",
                subject,
                "population",
                development["population"],
                "Population of this owned city.",
                "player_turn.cities.development",
            )
    for metric in ("treasury", "goldPerTurn", "happiness"):
        economy = observation.get("economy") or {}
        if metric in economy and not isinstance(economy[metric], (dict, list)):
            add(f"economy/{metric}", "our civilization", metric, economy[metric], metric, "player_turn.economy")
    return facts


def bind_assumptions(assumptions: list[JsonObject], facts: list[JsonObject]) -> list[JsonObject]:
    """Check exact references and values, not the truth of a player's interpretation."""
    catalogue = {fact["id"]: fact for fact in facts}
    bound = copy.deepcopy(assumptions)
    for assumption in bound:
        seen = set()
        for reference in assumption["facts"]:
            fact = catalogue.get(reference["id"])
            value = reference["value"]
            if (
                fact is None
                or reference["id"] in seen
                or value != fact["value"]
                or isinstance(value, bool) != isinstance(fact["value"], bool)
            ):
                raise ValueError("STRATEGIC_REVIEW_FACT: use a supplied named fact and its exact value")
            seen.add(reference["id"])
            reference.update(copy.deepcopy(fact))
    return bound
