"""Optional, immutable strategy reference shipped with the player service."""

from functools import cache
from importlib.resources import files


@cache
def strategy_reference() -> str:
    return files("civ_agent").joinpath("strategy_guide.md").read_text(encoding="utf-8").strip()
