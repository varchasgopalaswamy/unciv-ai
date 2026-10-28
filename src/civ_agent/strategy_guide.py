"""Strategy references and era-appropriate attention in player preloads."""

from functools import cache
from importlib.resources import files
from typing import TypeGuard

from civ_agent.protocol import JsonObject

EARLY_GAME_GUIDANCE = (
    "Before a known major civilization reaches the Modern era, focus on development, expansion, diplomacy "
    "and current military opportunities. For a distant research-based victory, build science and production "
    "rather than surveilling final projects or comparing completion dates. Do not request or repeat routine "
    "reports about those distant projects. Act on credible military, cultural or diplomatic victory "
    "opportunities whenever they arise. Reassess inherited plans and reporting commitments against what "
    "can affect current decisions."
)

LATE_GAME_GUIDANCE = (
    "A known major civilization has reached the Modern era or later. Compare relevant victory timing when "
    "it could change current choices. An era or missing milestone does not establish project construction "
    "or a finish date. Seek useful new evidence and avoid repeated unchanged reports."
)

ERA_FILES = {
    "Ancient era": "ancient.md",
    "Classical era": "classical.md",
    "Medieval era": "medieval.md",
    "Renaissance era": "renaissance.md",
    "Industrial era": "industrial.md",
    "Modern era": "modern.md",
    "Future era": "future.md",
}
ROLE_SECTIONS = {
    "gameplay": ("Strategy", "Economy", "Military", "Diplomacy"),
    "review": ("Strategy", "Economy", "Military", "Diplomacy"),
    "economy": ("Economy",),
    "military": ("Military",),
    "communication": ("Diplomacy",),
}
SPACE_RACE_GUIDANCE = {
    "gameplay": (
        "Vanilla science victory needs Rocketry, Apollo, the required spaceship parts, delivery and assembly "
        "in the capital. Compare the entire remaining research and production path, including prerequisite "
        "waiting and transport. Modern-era entry alone does not make this route urgent or establish a rival start."
    ),
    "economy": (
        "For a chosen science finish, coordinate Apollo and spaceship-part production with main. Verify "
        "prerequisites, resources, queue waiting and delivery needs before displacing other useful builds."
    ),
    "military": (
        "If main assigns an Apollo or spaceship-related objective, check the evidence, approach and arrival "
        "time. Protect or disrupt a concrete target without assuming a project has begun."
    ),
    "communication": (
        "Ask about Apollo or spaceship parts only when new evidence could change a decision or agreement. "
        "An unchanged milestone is not a construction report; do not solicit routine confirmations."
    ),
}
SPACE_RACE_GUIDANCE["review"] = SPACE_RACE_GUIDANCE["gameplay"]


def era_progress(reads: JsonObject) -> JsonObject:
    return (
        (reads.get("player_turn") or {}).get("eraProgress")
        or ((reads.get("chat_read") or {}).get("context") or {}).get("eraProgress")
        or {}
    )


def modern_era_reached(reads: JsonObject) -> bool:
    """Use fresh player-visible era facts, including the communication-only preload."""
    return era_progress(reads).get("modernOrLater") is True


def current_constraints(reads: JsonObject) -> str:
    """Surface explicit current shortages; missing facts never imply a shortage."""
    view = reads.get("player_turn") or {}
    economy = view.get("economy") or ((reads.get("chat_read") or {}).get("context") or {}).get("economy") or {}
    constraints = []

    def negative(value: object) -> TypeGuard[int | float]:
        return isinstance(value, (int, float)) and value < 0

    penalty = (economy.get("unitSupply") or {}).get("productionPenaltyPercent")
    if negative(penalty):
        constraints.append(f"unit supply production penalty {penalty:g}%")
    for key, label in (("happiness", "happiness"), ("goldPerTurn", "net gold per turn")):
        value = economy.get(key)
        if negative(value):
            constraints.append(f"{label} {value:g}")
    starving = [
        city["handle"]
        for city in view.get("cities", [])
        if negative(city.get("foodPerTurn")) and isinstance(city.get("handle"), str)
    ]
    if starving:
        constraints.append(
            "negative food in "
            + ", ".join(starving[:6])
            + (f" and {len(starving) - 6} more" if len(starving) > 6 else "")
        )
    return "Current constraints from the supplied observations: " + "; ".join(constraints) + "." if constraints else ""


def era_guidance(reads: JsonObject) -> str:
    progress = era_progress(reads)
    current, highest = progress.get("current"), progress.get("highestKnownMajor")
    sections = [current_constraints(reads)]
    if current:
        sections.append(f"Our current era: {current}. Use our own capabilities for development and operations.")
    if current and highest and highest != current:
        sections.append(
            f"A known major civilization is in a later era ({highest}). Account for that threat without "
            "assuming our civilization has its capabilities or that a final project has begun."
        )
    sections.append(LATE_GAME_GUIDANCE if modern_era_reached(reads) else EARLY_GAME_GUIDANCE)
    return "\n\n".join(section for section in sections if section)


@cache
def era_brief(era: str | None, purpose: str = "gameplay") -> str:
    if era not in ERA_FILES:
        return ""
    reference = files("civ_agent").joinpath("strategy_eras", ERA_FILES[era]).read_text(encoding="utf-8")
    sections = dict(section.split("\n", 1) for section in reference.split("\n## ")[1:])
    selected = ROLE_SECTIONS[purpose]
    return f"## {era}: guidance for {purpose}\n\n" + "\n\n".join(
        f"### {heading}\n\n{sections[heading].strip()}" for heading in selected
    )


@cache
def strategy_reference(*, era: str | None = None, purpose: str = "gameplay", include_space_race: bool = False) -> str:
    reference = files("civ_agent").joinpath("strategy_guide.md").read_text(encoding="utf-8").strip()
    sections = [reference, era_brief(era, purpose)]
    if include_space_race:
        sections.append("### Science victory\n\n" + SPACE_RACE_GUIDANCE[purpose])
    return "\n\n".join(section for section in sections if section)


def player_strategy_reference(reads: JsonObject, purpose: str = "gameplay") -> str:
    return strategy_reference(
        era=era_progress(reads).get("current"), purpose=purpose, include_space_race=modern_era_reached(reads)
    )
