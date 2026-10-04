"""Create reproducible, ordinary single-game inputs for benchmark runs."""

from __future__ import annotations

import hashlib
import itertools
import secrets
from collections.abc import Iterable

import tomlkit

from civ_agent.game_config import GameConfig, GameSetup, ResolvedGameConfig
from civ_agent.protocol import JsonObject

_BATCH_SEED_NAMESPACE = "civ-batch-seed-v1"
_MAX_GAMES = 2**64


def _validate_count(games: int) -> None:
    if not 1 <= games <= _MAX_GAMES:
        raise ValueError("games must be positive and fit the seed space")


def _validate_seed(value: int, name: str) -> int:
    try:
        GameSetup(master_seed=value)
    except (TypeError, ValueError) as error:
        raise ValueError(f"{name} must be a signed 64-bit integer") from error
    return value


def choose_batch_seed(batch_seed: int | None = None) -> int:
    """Return a valid batch seed without consulting a template's game seed."""

    return _validate_seed(batch_seed if batch_seed is not None else secrets.randbits(63), "batch seed")


def derive_master_seed(batch_seed: int, game_index: int, used: Iterable[int] = ()) -> int:
    """Derive a distinct signed master for one one-based game index.

    The attempt number is part of the hash input, so a collision retry is stable
    and adding later games cannot change any earlier game's seed.
    """

    _validate_seed(batch_seed, "batch seed")
    if game_index < 0:
        raise ValueError("game index must be nonnegative")
    occupied = set(used)
    for attempt in itertools.count():
        digest = hashlib.sha256(f"{_BATCH_SEED_NAMESPACE}:{batch_seed}:{game_index}:{attempt}".encode("ascii")).digest()
        candidate = int.from_bytes(digest[:8], "big", signed=True)
        if candidate not in occupied:
            return candidate
    raise RuntimeError("Unable to derive a distinct game master seed")


def generate_runs(config: GameConfig, games: int, batch_seed: int | None = None) -> list[JsonObject]:
    """Build an in-memory preview with the same seed schedule as generation.

    The command-line generator does not persist this resolved preview. Keeping
    this helper available is useful to callers that want to inspect assignments
    without writing a run directory.
    """

    _validate_count(games)
    batch_seed = choose_batch_seed(batch_seed)
    runs: list[JsonObject] = []
    masters: list[int] = []
    for index in range(games):
        master_seed = derive_master_seed(batch_seed, index + 1, masters)
        masters.append(master_seed)
        settings = config.model_copy(deep=True)
        settings.game.master_seed = master_seed
        runs.append(
            {
                "id": f"g{index + 1:04d}",
                "batch_seed": batch_seed,
                "game_index": index + 1,
                "config": settings.resolved().model_dump(),
                "status": "pending",
            }
        )
    return runs


def seeded_input(
    template: str,
    config: GameConfig | ResolvedGameConfig | int,
    *,
    batch_seed: int | None = None,
    game_index: int | None = None,
) -> str:
    """Set only the master seed while preserving TOML settings and comments.

    Omitted component seeds intentionally remain omitted. They are derived by
    the normal single-game resolver when the input is launched.
    """

    if isinstance(config, int):
        master_seed = config
    else:
        master_seed = config.game.master_seed
    if master_seed is None:
        raise ValueError("A generated input needs a master seed")
    _validate_seed(master_seed, "master seed")

    document = tomlkit.parse(template)
    if "game" not in document:
        document["game"] = tomlkit.table()
    document["game"]["master_seed"] = master_seed
    rendered = tomlkit.dumps(document)
    if batch_seed is None and game_index is None:
        return rendered
    if batch_seed is None or game_index is None:
        raise ValueError("batch_seed and game_index must be supplied together")
    return f"# Batch seed: {batch_seed}\n# Game index: {game_index}\n{rendered}"


def input_plans(
    template: str, config: GameConfig, games: int, batch_seed: int | None = None
) -> tuple[int, list[JsonObject]]:
    """Return generated TOML contents without touching the filesystem."""

    _validate_count(games)
    batch_seed = choose_batch_seed(batch_seed)
    plans: list[JsonObject] = []
    masters: list[int] = []
    width = max(4, len(str(games)))
    for index in range(games):
        master_seed = derive_master_seed(batch_seed, index + 1, masters)
        masters.append(master_seed)
        plans.append(
            {
                "name": f"g{index + 1:0{width}d}.toml",
                "game_index": index + 1,
                "master_seed": master_seed,
                "content": seeded_input(
                    template,
                    master_seed,
                    batch_seed=batch_seed,
                    game_index=index + 1,
                ),
            }
        )
    return batch_seed, plans
