"""Periodic, advisory strategy reports from frozen player-visible facts."""

import copy
from typing import Annotated, Self

from pydantic import Field, model_validator

from civ_agent.player_prompts import DIPLOMACY_GUIDANCE, STRATEGY_GUIDANCE, VICTORY_HORIZON_GUIDANCE
from civ_agent.protocol import JsonObject
from civ_agent.schemas import BoundaryModel

ReviewText = Annotated[str, Field(min_length=1, max_length=600)]


class StrategicPlan(BoundaryModel):
    name: Annotated[str, Field(min_length=1, max_length=80)]
    objective: ReviewText = Field(description="Concrete improvement in our position this plan seeks.")
    investment: ReviewText = Field(description="Actions, preparation, resources and time required; what they postpone.")
    expectedObservations: Annotated[list[ReviewText], Field(min_length=1, max_length=3)] = Field(
        description="Observable effects expected within the shared horizon, with estimates and uncertainty."
    )
    firstActions: Annotated[list[ReviewText], Field(min_length=1, max_length=3)]
    relativeOutcome: ReviewText = Field(
        description="Likely gains and costs for us and rivals, including rival responses."
    )
    risks: ReviewText = Field(description="Assumptions, failure costs and information that could change this choice.")


class DiplomaticInitiative(BoundaryModel):
    plan: Annotated[int, Field(ge=1, le=3)] = Field(description="Plan this initiative supports.")
    players: Annotated[list[Annotated[str, Field(min_length=1, max_length=80)]], Field(min_length=1)] = Field(
        description="Players to approach."
    )
    objective: ReviewText = Field(
        description="Change sought in another player's behavior and how it improves our position relative to rivals."
    )
    incentives: ReviewText = Field(description="Why these players might cooperate or resist; likely responses.")
    approach: ReviewText = Field(description="Proposed terms, what to disclose or conceal, and the risks.")
    nextStep: ReviewText = Field(description="Concrete first contact or follow-up, with its timing.")
    reconsiderWhen: ReviewText = Field(description="Conditions for revising or abandoning this initiative.")


class DiplomaticAdvice(BoundaryModel):
    assessment: ReviewText = Field(description="How relationships and agreements affect our chances of winning.")
    initiatives: Annotated[list[DiplomaticInitiative], Field(max_length=2)] = Field(
        description="Up to two worthwhile initiatives. An empty list is valid."
    )


class StrategicReview(BoundaryModel):
    horizonTurns: Annotated[int, Field(ge=1, le=100)] = Field(description="Common comparison horizon for all plans.")
    outcomeAssessment: ReviewText = Field(
        description="What prior choices achieved, what is unknown, and which assumptions changed. An opening review may state that no prior choice exists."
    )
    plans: Annotated[list[StrategicPlan], Field(min_length=1, max_length=3)] = Field(
        description="Concrete competing plans, numbered from 1; normally two or three, or one if only one is credible."
    )
    preferredPlan: Annotated[int, Field(ge=1, le=3)] | None = Field(
        description="One-based plan number, or null when evidence does not support a preference."
    )
    reconsiderWhen: ReviewText = Field(description="Evidence or developments that would change the recommendation.")
    diplomacy: DiplomaticAdvice

    @model_validator(mode="after")
    def preferred_plan_exists(self) -> Self:
        if self.preferredPlan is not None and self.preferredPlan > len(self.plans):
            raise ValueError("STRATEGIC_REVIEW_PLAN: preferredPlan must reference a numbered plan.")
        if any(initiative.plan > len(self.plans) for initiative in self.diplomacy.initiatives):
            raise ValueError("STRATEGIC_REVIEW_PLAN: initiative must reference a supplied plan.")
        return self


class StrategicAdvice(BoundaryModel):
    assessment: Annotated[str, Field(min_length=1, max_length=1200)] = Field(
        description="Why the recommended plan offers the best prospects relative to alternatives, or why the choice is open."
    )
    review: StrategicReview


REVIEW_READ_TOOLS = frozenset(
    {
        "player_turn",
        "victory_progress",
        "research_details",
        "policy_details",
        "city_details",
        "unit_details",
        "construction_details",
        "player_details",
        "map_region",
        "chat_read",
    }
)


def review_facts(value: JsonObject) -> JsonObject:
    """Keep permitted game evidence, excluding private recommendations and action history."""
    # These are service-owned overview fields. Do not rewrite similarly named
    # keys inside player-authored messages or promises.
    return {
        key: item
        for key, item in value.items()
        if key not in {"memoryUpdates", "strategyReview", "turnReview", "previousTurnReview"}
    }


ADVISOR_INSTRUCTIONS = (
    "You are this civilization's independent strategic adviser. Recommend how it can win from the supplied "
    "position. Your advice is for the player to judge.\n\n"
    "Compare concrete investments that could improve our chances of winning. Normally compare continued "
    "development with the strongest plausible alternatives in expansion, diplomacy or military action. "
    "Use one plan when only one is credible. Compare investments over one shared horizon, "
    "including preparation, opportunity costs, likely rival responses and what happens if rivals continue developing. "
    "Military costs include recruitment, upgrades, positioning and travel. Consider limited objectives such as taking "
    "a useful city, pillaging or forcing rival military spending, with conditions for seeking peace, rather than "
    "judging every military option as a full domination campaign. Account for benefits to uninvolved rivals. "
    "Compare the earliest feasible intervention with rival progress and the opportunity window.\n\n"
    + STRATEGY_GUIDANCE
    + " Identify uncertainties and useful ways to resolve them.\n\n"
    + VICTORY_HORIZON_GUIDANCE
    + "\n\n"
    "Explain the preference or leave it open. When undecided, recommend useful information gathering or reversible "
    "investments. Distinguish investments useful across victories from committing to a particular finish. "
    "Use decisionFeedback to compare the player's earlier expectations with observed results. Treat prior choices "
    "as hypotheses and reassess alternatives, including abandonment; do not preserve a plan merely because it exists. "
    "Distinguish a command being accepted from its intended result and correlation from causation. "
    "Compare continued production conversion or saving with feasible investments, including the cost of waiting. "
    "Use named construction_details with horizonTurns for native city investment comparisons. "
    "Give observable expectations, first actions and conditions for reconsideration. A broad victory direction can "
    "remain open; cover terminal projects only when they affect a current decision.\n\n"
    + DIPLOMACY_GUIDANCE
    + "\nConsider whether influencing another player could improve a plan or make it feasible. "
    "Connect worthwhile diplomatic initiatives to the plans being considered, accounting for existing exchanges "
    "and agreements. Explain the desired change in another player's behavior, its incentives, our relative benefit "
    "and proposed terms. "
    "Human and agent players can negotiate in chat; built-in AI responds only to native diplomatic actions and trades.\n\n"
    "The supplied observations and read-only tools describe the frozen position. "
    "All tools available to this review are defined below. Call them directly with tools.<name>(arguments) in "
    "functions.exec; no tool discovery is needed. "
    "Return one complete JSON report matching the supplied schema, preferably under 6000 characters."
)


def advice_prompt(inputs, *, strategy_guide=False, civilization_guide=False):
    from civ_agent.civilization_guide import civilization_reference
    from civ_agent.startup_context import StartupPrompt, encoded, reference_sections, tool_definitions
    from civ_agent.strategy_guide import strategy_reference

    definitions = tool_definitions(inputs["catalogue"])
    reference = strategy_reference() if strategy_guide else ""
    civ_reference = civilization_reference(inputs["civilization"]) if civilization_guide else ""
    sections = reference_sections(reference, civ_reference)
    return StartupPrompt(
        ADVISOR_INSTRUCTIONS + "\nRead tool definitions:\n" + encoded(definitions),
        ("\n\n".join(sections) + "\n\n" if sections else "")
        + (
            "Opening position before the first move, turn "
            if inputs["turn"] == 0
            else "Frozen position after all players completed round "
        )
        + str(inputs["turn"])
        + ":\n"
        + encoded(
            {
                "player_turn": frozen_read(inputs, "player_turn", {}),
                "victory_progress": frozen_read(inputs, "victory_progress", {"sections": ["rankings"]}),
                "chat_read": frozen_read(inputs, "chat_read", {}),
            }
        )
        + "\nKnown foreign cities (current visibility or last observation; hex distances are not travel times):\n"
        + encoded(review_cities(inputs["observation"], inputs["civilization"]))
        + "\nRecent accepted commands and observed effects:\n"
        + encoded(inputs["actions"]),
        strategy_reference=reference,
        civilization_reference=civ_reference,
    )


def review_cities(snapshot: JsonObject, civilization: str) -> list[JsonObject]:
    """Summarize only the player's permitted map; never resolve cities by name."""
    from civ_agent.mechanics_overview import region_distance

    world = snapshot.get("map", {})
    ours = [
        {"name": city["name"], "position": city["development"]["position"]}
        for city in snapshot.get("cities", [])
        if (city.get("development") or {}).get("position") is not None
    ]
    result = []
    for tile in world.get("tiles", []):
        if tile.get("cityName") is None or tile.get("owner") in (None, civilization):
            continue
        city = {
            "name": tile["cityName"],
            "civilization": tile["owner"],
            "position": copy.deepcopy(tile["position"]),
            "visible": tile["visible"],
            "observedTurn": tile.get("observedTurn"),
            "originalMajorCapital": tile.get("cityIsOriginalMajorCapital"),
        }
        if tile["visible"]:
            city.update(
                {
                    key: tile[source]
                    for key, source in (
                        ("population", "cityPopulation"),
                        ("health", "cityHealth"),
                        ("maxHealth", "cityMaxHealth"),
                        ("defensiveStrength", "cityDefensiveStrength"),
                    )
                    if tile.get(source) is not None
                }
            )
        if ours:
            nearest = min(
                ours,
                key=lambda own: (
                    region_distance(own["position"], tile["position"], world.get("wrapOffset")),
                    own["name"],
                ),
            )
            city["nearestOwnCity"] = nearest["name"]
            city["distanceInHexes"] = region_distance(nearest["position"], tile["position"], world.get("wrapOffset"))
        result.append(city)
    return sorted(
        result,
        key=lambda city: (
            city["originalMajorCapital"] is not True,
            city.get("distanceInHexes", 0),
            city["civilization"],
            city["position"]["x"],
            city["position"]["y"],
        ),
    )


def review_due(turn: int, interval: int = 5) -> bool:
    if type(interval) is not int or interval < 1:
        raise ValueError("Review interval must be a positive integer")
    return turn >= interval and turn % interval == 0


def frozen_read(inputs: JsonObject, name: str, arguments: JsonObject) -> JsonObject:
    """No host connection or notebook access exists along this path."""
    from civ_agent.observations import DETAIL_MODELS, DetailQuery, project_detail
    from civ_agent.social_context import social_context
    from civ_agent.tool_schemas import TOOL_MODELS
    from civ_agent.victory_snapshot import victory_read

    if name not in REVIEW_READ_TOOLS or name == "player_details" and arguments.get("section") == "turn_history":
        return {"error": "REVIEW_READ_ONLY", "applied": False}
    if name in TOOL_MODELS:
        arguments = TOOL_MODELS[name].model_validate(arguments).model_dump(exclude_unset=True)
    if name in DETAIL_MODELS:
        return project_detail(inputs["observation"], DetailQuery.model_validate({"tool": name, "arguments": arguments}))
    if name == "victory_progress":
        return victory_read(inputs["reads"][name], arguments, civilization=inputs.get("civilization"))
    value = copy.deepcopy(inputs["reads"][name])
    if name == "chat_read":
        # Review capture has already applied the model projection to these records.
        return social_context(
            value,
            include_context=arguments.get("includeContext", False),
            include_history=arguments.get("includeHistory", False),
            include_native_trades=True,
            proposal_field="proposalId",
        )
    if name in {"research_details", "policy_details"}:
        names = arguments.get("names", [])
        field = "technologies" if name == "research_details" else "policies"
        if (
            set(arguments) - {"names"}
            or not isinstance(names, list)
            or len(names) > 16
            or any(not isinstance(n, str) for n in names)
        ):
            raise ValueError("INVALID_ARGUMENT")
        rows = {row["name"]: row for row in value[field]}
        if len(set(names)) != len(names) or any(n not in rows for n in names):
            return {"error": "OPTION_UNAVAILABLE", "applied": False}
        value[field] = (
            [rows[n] for n in names]
            if names
            else [
                {
                    k: v
                    for k, v in row.items()
                    if k
                    not in {
                        "description",
                        "gameplayDescription",
                        "prerequisiteQueue",
                        "queueAvailable",
                        "branchCompletionDescription",
                    }
                }
                for row in rows.values()
            ]
        )
    elif arguments:
        raise ValueError("INVALID_ARGUMENT")
    return value


def parse_advice(text: str, observation: JsonObject) -> JsonObject:
    advice = StrategicAdvice.model_validate_json(text).model_dump()
    if type(observation.get("turn")) is not int:
        raise ValueError("STRATEGIC_REVIEW_POSITION_MISMATCH")
    return advice


def advice_schema(observation: JsonObject) -> JsonObject:
    """Validate usable choices and references, without prescribing victory coverage."""
    return StrategicAdvice.model_json_schema()
