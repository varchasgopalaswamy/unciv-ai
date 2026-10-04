"""Optional, pre-written advice for the player's own Vanilla civilization."""

from functools import cache
from importlib.resources import files

CIVILIZATION_GUIDES = {
    "Inca": "inca.md",
    "France": "france.md",
    "Greece": "greece.md",
    "Arabia": "arabia.md",
    "China": "china.md",
    "Persia": "persia.md",
}


class UnsupportedCivilizationGuideError(ValueError):
    """The operator enabled advice for a civilization without a written guide."""


@cache
def civilization_reference(civilization: str | None) -> str:
    if civilization not in CIVILIZATION_GUIDES:
        raise UnsupportedCivilizationGuideError(
            f"civilization_guide is not implemented for {civilization!r}. "
            f"Supported civilizations: {', '.join(sorted(CIVILIZATION_GUIDES))}."
        )
    advice = (
        files("civ_agent")
        .joinpath("civilization_guides", CIVILIZATION_GUIDES[civilization])
        .read_text(encoding="utf-8")
        .strip()
    )
    return (
        f"# Optional civilization strategy reference: {civilization}\n\n"
        "These are suggestions, not instructions, rules, or a checklist. They describe opportunities in the "
        "Vanilla ruleset, not a prescribed victory type or a requirement to fight. Use the current "
        "civilizationProfile, unit details, construction details, and combat previews to verify capabilities "
        "and availability; current engine observations take precedence over this reference.\n\n"
        + advice
        + "\n\nConsider which civilization-specific advantage you can exploit now or prepare for next. "
        "Could it enable a profitable conquest before opponents catch up, and how does that compare with "
        "peaceful development? Account for preparation, travel, losses, happiness, rival responses, and "
        "benefits to uninvolved rivals. Use technology and readiness rather than fixed turn numbers, "
        "especially on Quick speed. Reassess when the opportunity changes; do not wait for a scheduled "
        "review if a decision is needed sooner."
    )
