"""Capture permitted victory data once and answer filtered reads without live access."""

import copy
from typing import Annotated, Literal

from pydantic import Field, model_validator

from civ_agent.protocol import JsonObject
from civ_agent.schemas import BoundaryModel, validate

type Section = Literal["our_status", "global_status", "rankings", "charts", "demographics"]
SECTIONS: list[Section] = ["our_status", "global_status", "rankings", "charts", "demographics"]
DEFAULT_SECTIONS = SECTIONS[:3]
type Name = Annotated[str, Field(min_length=1, max_length=256, pattern=r"\S")]
type Turn = Annotated[int, Field(ge=0, le=2147483647)]


class VictoryQuery(BoundaryModel):
    names: Annotated[list[Name], Field(max_length=16)] = []
    sections: Annotated[
        list[Section],
        Field(min_length=1, max_length=5),
    ] = DEFAULT_SECTIONS
    metrics: Annotated[
        list[
            Literal[
                "Population",
                "Growth",
                "Production",
                "Gold",
                "Territory",
                "Force",
                "Happiness",
                "Technologies",
                "Culture",
                "TilesExplored",
            ]
        ],
        Field(max_length=10),
    ] = []
    civilizations: Annotated[list[Name], Field(max_length=64)] = []
    fromTurn: Turn | None = None
    toTurn: Turn | None = None

    @model_validator(mode="after")
    def unique_lists(self):
        if any(
            len(value) != len(set(value)) for value in (self.names, self.sections, self.metrics, self.civilizations)
        ):
            raise ValueError("INVALID_ARGUMENT")
        if (self.fromTurn is not None or self.toTurn is not None) and "charts" not in self.sections:
            raise ValueError("INVALID_ARGUMENT")
        return self


async def capture_victory(read, turn):
    """Native pages enforce visibility and bound each read; retain all permitted history."""
    value = await read("victory_progress", {"sections": SECTIONS, "fromTurn": 0, "toTurn": turn})
    if value["turn"] != turn:
        raise ValueError("STRATEGIC_REVIEW_POSITION_CHANGED")
    charts = value.get("charts")
    while charts and charts["nextFromTurn"] is not None:
        start = charts["nextFromTurn"]
        page = await read("victory_progress", {"sections": ["charts"], "fromTurn": start, "toTurn": turn})
        next_charts = page["charts"]
        if page["turn"] != value["turn"] or next_charts["turns"][0] != start:
            raise ValueError("STRATEGIC_REVIEW_POSITION_CHANGED")
        charts["turns"].extend(next_charts["turns"])
        series = {row["civilization"]: row for row in next_charts["series"]}
        for row in charts["series"]:
            following = series[row["civilization"]]
            row["history"].extend(following["history"])
            if following["currentValues"] is not None:
                row["currentValues"] = following["currentValues"]
        if next_charts["currentTurn"] is not None:
            charts["currentTurn"] = next_charts["currentTurn"]
        charts["nextFromTurn"] = next_charts["nextFromTurn"]
    return value


def victory_read(snapshot: JsonObject, arguments: JsonObject, *, civilization: str | None = None) -> JsonObject:
    query = validate(VictoryQuery, arguments)
    value = copy.deepcopy(snapshot)
    turn = value["turn"]
    end = query.toTurn if query.toTurn is not None else turn
    start = query.fromTurn if query.fromTurn is not None else max(0, end - 19)
    names = {row["name"] for row in value["victories"]}
    metrics = value.get("metrics", [])
    known = {row["civilization"] for row in value.get("rivals", [])}
    for section, field in (("rankings", "rows"), ("charts", "series")):
        known.update(row["civilization"] for row in (value.get(section) or {}).get(field, []))
    if civilization:
        known.add(civilization)
    if (
        start > end
        or end > turn
        or set(query.names) - names
        or set(query.civilizations) - known
        or metrics
        and set(query.metrics) - {m["name"] for m in metrics}
    ):
        raise ValueError("INVALID_ARGUMENT")

    value["sections"] = query.sections
    # A frozen read never invents a section that was not captured or relaxes native visibility.
    for section in SECTIONS:
        if section not in snapshot["sections"]:
            value["availability"][section] = {"available": False, "reason": "NOT_CAPTURED"}

    def includes(section):
        return section in query.sections and value["availability"][section]["available"]

    def chosen(row):
        return not query.civilizations or row["civilization"] in query.civilizations

    indices = [i for i, metric in enumerate(metrics) if not query.metrics or metric["name"] in query.metrics]

    def columns(values):
        return [values[i] for i in indices] if values is not None else None

    value["metrics"] = [metrics[i] for i in indices] if any(includes(s) for s in SECTIONS[2:]) else []
    value["victories"] = (
        [r for r in value["victories"] if not query.names or r["name"] in query.names] if includes("our_status") else []
    )
    value["rivals"] = [r for r in value.get("rivals", []) if chosen(r)] if includes("global_status") else []
    for rival in value["rivals"]:
        rival["nextMilestones"] = {
            k: v for k, v in rival["nextMilestones"].items() if not query.names or k in query.names
        }
    for section in SECTIONS[2:]:
        if not includes(section):
            value[section] = None
    if rankings := value.get("rankings"):
        rankings["rows"] = [row for row in rankings["rows"] if chosen(row)]
        for row in rankings["rows"]:
            row["values"], row["ranks"] = columns(row["values"]), columns(row["ranks"])
            if row.get("trend"):
                row["trend"]["changes"] = columns(row["trend"]["changes"])
    if charts := value.get("charts"):
        charts["series"] = [row for row in charts["series"] if chosen(row)]
        # Match the engine's turn/cell page bounds, after column and civilization selection.
        size = min(50, max(1, 2000 // (len(indices) * max(1, len(charts["series"]))) - 1))
        page_end = min(end, start + size - 1)
        positions = [i for i, t in enumerate(charts["turns"]) if start <= t <= page_end]
        charts["turns"] = [charts["turns"][i] for i in positions]
        charts["currentTurn"] = turn if start <= turn <= page_end else None
        charts["nextFromTurn"] = page_end + 1 if page_end < end else None
        charts["requestedToTurn"] = end
        for row in charts["series"]:
            row["history"] = [columns(row["history"][i]) for i in positions]
            row["currentValues"] = columns(row["currentValues"]) if charts["currentTurn"] is not None else None
    if demographics := value.get("demographics"):
        demographics["rows"] = [
            row for row in demographics["rows"] if not query.metrics or row["metric"] in query.metrics
        ]
    return value
