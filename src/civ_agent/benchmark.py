"""Generate independent, ordinary game inputs from one TOML template."""

import secrets

import tomlkit

from civ_agent.game_config import SEED_FIELDS, GameConfig, GameSetup, ResolvedGameConfig
from civ_agent.protocol import JsonObject


def generate_runs(config: GameConfig, games: int, master_seed: int | None = None) -> list[JsonObject]:
    if not 1 <= games <= 2**64:
        raise ValueError("games must be positive and fit the seed space")
    first_seed = master_seed if master_seed is not None else config.game.master_seed
    if first_seed is None:
        first_seed = secrets.randbits(63)
    GameSetup(master_seed=first_seed)  # Validate CLI seeds before writing anything.
    runs: list[JsonObject] = []
    for index in range(games):
        settings = config.model_copy(deep=True)
        # Consecutive master seeds are unique, including at the signed boundary.
        # Hash-derived child streams make adjacent masters independent.
        settings.game.master_seed = (first_seed + index + 2**63) % 2**64 - 2**63
        runs.append({"id": f"g{index + 1:04d}", "config": settings.resolved().model_dump(), "status": "pending"})
    return runs


def seeded_input(template: str, config: ResolvedGameConfig) -> str:
    """Preserve the template's settings/comments, adding all resolved seeds."""
    document = tomlkit.parse(template)
    if "game" not in document:
        document["game"] = tomlkit.table()
    for field in ("master_seed", *SEED_FIELDS):
        document["game"][field] = getattr(config.game, field)
    return tomlkit.dumps(document)
