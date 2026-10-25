"""Dated sources actually delivered to one strategic review."""

import copy
import hashlib
import json
import secrets
from typing import Annotated, Literal

from pydantic import Field, model_validator

from civ_agent.protocol import JsonObject
from civ_agent.schemas import BoundaryModel


class MaterialAssumption(BoundaryModel):
    assessment: Annotated[str, Field(min_length=1, max_length=400)]
    status: Literal["inferred", "unknown"] = Field(
        description="Use inferred for your interpretation of cited observations; otherwise use unknown."
    )
    sources: Annotated[list[Annotated[str, Field(min_length=1, max_length=40)]], Field(max_length=6)] = Field(
        description="Copy labels from the outer source field of observation envelopes you received. Inner data.evidence IDs are not review source labels."
    )

    @model_validator(mode="after")
    def inference_has_evidence(self):
        if self.status == "inferred" and not self.sources:
            raise ValueError("Cite an observation for this inference, or mark the assumption unknown")
        return self


class ReviewEvidence:
    """Bind provenance, not the truth of an adviser's interpretation.

    Payloads are kept once in the private report archive. Later player context
    receives compact dated references, rather than another copy of each read.
    """

    def __init__(self, inputs: JsonObject):
        self.turn = inputs["observation"]["turn"]
        self.namespace = secrets.token_hex(6)
        self.sources: dict[str, JsonObject] = {}
        self.delivered: dict[str, str] = {}

    def deliver(self, name: str, data: JsonObject | list, arguments: JsonObject | None = None) -> JsonObject:
        payload = {"name": name, "arguments": arguments or {}, "data": data}
        digest = hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
        ).hexdigest()
        source = self.delivered.get(digest)
        if source is None:
            source = f"s{self.namespace}-{len(self.sources) + 1}"
            self.sources[source] = {
                "id": source,
                "name": name,
                "observedTurn": self.turn,
                "sha256": digest,
                **copy.deepcopy(payload),
            }
            self.delivered[digest] = source
        return {"source": source, "data": copy.deepcopy(data)}

    def bind(self, assumptions: list[JsonObject]) -> tuple[list[JsonObject], JsonObject]:
        bound = copy.deepcopy(assumptions)
        cited: JsonObject = {}
        for index, assumption in enumerate(bound, 1):
            references = assumption["sources"]
            seen: set[str] = set()
            for reference in references:
                if reference not in self.sources:
                    raise ValueError(
                        f"STRATEGIC_REVIEW_SOURCE: assumption {index} cites {reference!r}, "
                        "which was not supplied to this review. Copy the outer source field of a delivered observation, "
                        "not its inner data.evidence ID, "
                        "or mark the assumption unknown with no sources."
                    )
                if reference in seen:
                    raise ValueError(f"STRATEGIC_REVIEW_SOURCE: assumption {index} repeats source {reference!r}")
                seen.add(reference)
                cited[reference] = copy.deepcopy(self.sources[reference])
            assumption["sources"] = [
                {key: copy.deepcopy(value) for key, value in self.sources[reference].items() if key != "data"}
                for reference in references
            ]
        return bound, cited


def source_catalogue(catalogue: list[JsonObject]) -> list[JsonObject]:
    """Describe the same read results with their review source labels."""
    tools = copy.deepcopy(catalogue)
    for tool in tools:
        data = tool.pop("outputSchema", {"type": "object"})
        definitions = data.pop("$defs", None)
        tool["outputSchema"] = {
            "type": "object",
            "properties": {"source": {"type": "string"}, "data": data},
            "required": ["source", "data"],
            "additionalProperties": False,
            **({"$defs": definitions} if definitions else {}),
        }
    return tools


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
                    "our/totalPopulation" if rival == civilization else f"rival/{rival}/totalPopulation",
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
            "The number of cities we own.",
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
