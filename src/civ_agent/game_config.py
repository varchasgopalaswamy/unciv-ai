"""One configuration for native setup, isolated players, and benchmark expansion."""

import itertools
import secrets
import tomllib
from pathlib import Path
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from civ_agent.agent_settings import valid_setting

Positive = Annotated[int, Field(gt=0)]


class Configuration(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_default=True)


class GameSetup(Configuration):
    seed: Annotated[int, Field(ge=-(2**63), lt=2**63)] | None = None
    map_type: str = "Pangaea"
    map_size: str | None = None
    map_radius: Annotated[int, Field(ge=2, le=500)] | None = None
    difficulty: str = "Prince"
    speed: str = "Quick"
    starting_era: str = "Ancient era"
    city_states: Annotated[int, Field(ge=0, le=41)] = 2
    victories: list[str] | None = None

    @model_validator(mode="after")
    def size(self):
        if self.map_size is not None and self.map_radius is not None:
            raise ValueError("Choose map_size or map_radius, not both")
        return self


class AgentDefaults(Configuration):
    model: str = "gpt-6-luna"
    reasoning_effort: str = "medium"
    review_interval: Positive = 5
    review_attempts: Positive = 3
    strategy_guide: bool = False
    fast: bool = False

    @field_validator("model", "reasoning_effort")
    @classmethod
    def setting(cls, value):
        if not valid_setting(value):
            raise ValueError("Expected a nonempty model/effort name without whitespace")
        return value


class Player(Configuration):
    civilization: str
    controller: Literal["agent", "human", "built_in_ai"] = "agent"
    model: str | None = None
    reasoning_effort: str | None = None

    @field_validator("civilization")
    @classmethod
    def civilization_name(cls, value):
        if not value or len(value) > 128 or not value.isprintable() or value != value.strip():
            raise ValueError("Invalid civilization name")
        return value

    @model_validator(mode="after")
    def settings(self):
        for value in (self.model, self.reasoning_effort):
            if value is not None and not valid_setting(value):
                raise ValueError("Invalid model or reasoning effort")
            if value is not None and self.controller != "agent":
                raise ValueError("Only agent players have model settings")
        return self


class Limits(Configuration):
    max_turns: Positive | None = None
    max_invocations: Positive = 10000
    max_seconds: Positive = 604800
    turn_timeout: Annotated[int, Field(gt=0, le=86400)] = 3600
    startup_timeout: Positive = 900
    max_concurrent_jobs: Positive = 4


class ContainerResources(Configuration):
    memory_mib: Annotated[int, Field(ge=128)] = 2048
    cpus: Annotated[float, Field(gt=0)] = 1
    pids: Annotated[int, Field(ge=32)] = 256


class Resources(Configuration):
    controller: ContainerResources = Field(
        default_factory=lambda: ContainerResources(memory_mib=4096, cpus=2, pids=512)
    )
    desktop: ContainerResources = Field(default_factory=lambda: ContainerResources(memory_mib=6144, cpus=2, pids=512))
    agent: ContainerResources = Field(default_factory=ContainerResources)


class Runtime(Configuration):
    image: str = "localhost/civ-agent:runtime"
    desktop_image: str = "localhost/civ-agent:desktop"
    browser_port: Annotated[int, Field(ge=0, le=65535)] = 0


class Batch(Configuration):
    seeds: list[Annotated[int, Field(ge=-(2**63), lt=2**63)]] = Field(default_factory=list)
    repetitions: Positive = 1
    parallel: Positive = 1
    rotate_models: bool = False


class GameConfig(Configuration):
    game: GameSetup = Field(default_factory=GameSetup)
    agents: AgentDefaults = Field(default_factory=AgentDefaults)
    players: list[Player] = Field(
        default_factory=lambda: (
            [Player(civilization=name) for name in ("Rome", "Greece", "France")]
            + [Player(civilization="Egypt", controller="built_in_ai")]
        )
    )
    limits: Limits = Field(default_factory=Limits)
    resources: Resources = Field(default_factory=Resources)
    runtime: Runtime = Field(default_factory=Runtime)
    batch: Batch = Field(default_factory=Batch)

    @model_validator(mode="after")
    def roster(self):
        names = [p.civilization for p in self.players]
        if len(names) != len(set(names)):
            raise ValueError("Civilizations must be distinct")
        if not self.agent_players:
            raise ValueError("Choose at least one agent player")
        if sum(p.controller == "human" for p in self.players) > 1:
            raise ValueError("The native desktop supports one human per game")
        return self

    @property
    def agent_players(self):
        return [p for p in self.players if p.controller == "agent"]

    @property
    def human(self):
        return next((p.civilization for p in self.players if p.controller == "human"), None)

    def settings(self, player):
        return {
            **self.agents.model_dump(),
            "model": player.model or self.agents.model,
            "reasoning_effort": player.reasoning_effort or self.agents.reasoning_effort,
        }

    def resolved(self):
        config = self.model_copy(deep=True)
        if config.game.seed is None:
            config.game.seed = secrets.randbits(63)
        return config

    def engine_arguments(self):
        game = self.game
        arguments = ["--seed", str(game.seed), "--city-states", str(game.city_states)]
        for key in ("map_type", "difficulty", "speed", "starting_era"):
            arguments += ["--" + key.replace("_", "-"), getattr(game, key)]
        arguments += (
            ["--map-radius", str(game.map_radius)]
            if game.map_radius is not None
            else ["--map-size", game.map_size or "Small"]
        )
        # Keep the native desktop's human-first turn order identical in both launch modes.
        if self.human:
            arguments += ["--agent-player", self.human]
        for player in self.players:
            if player.controller != "human":
                arguments += [
                    "--agent-player" if player.controller == "agent" else "--built-in-ai",
                    player.civilization,
                ]
        if game.victories is not None:
            arguments += ["--victories", ",".join(game.victories)]
        return arguments


def load_config(path: Path | None, overrides: dict | None = None) -> GameConfig:
    data = tomllib.loads(path.read_text()) if path else {}
    for section, values in (overrides or {}).items():
        if isinstance(values, dict):
            data[section] = {**data.get(section, {}), **values}
        else:
            data[section] = values
    return GameConfig.model_validate(data)


def expand_batch(config: GameConfig):
    """Freeze assignments before launching; rotations change models, never native seats."""
    config = config.resolved()
    seeds = config.batch.seeds or [config.game.seed]
    settings = [
        (p.model or config.agents.model, p.reasoning_effort or config.agents.reasoning_effort)
        for p in config.agent_players
    ]
    rotations = []
    for offset in range(len(settings) if config.batch.rotate_models else 1):
        rotated = settings[offset:] + settings[:offset]
        if rotated not in [r for _, r in rotations]:
            rotations.append((offset, rotated))
    result = []
    for seed, repetition, (rotation, assignment) in itertools.product(
        seeds, range(config.batch.repetitions), rotations
    ):
        game = config.model_copy(deep=True)
        game.game.seed = seed
        game.batch = Batch()
        for player, (model, effort) in zip(game.agent_players, assignment, strict=True):
            player.model, player.reasoning_effort = model, effort
        result.append(
            {
                "id": f"g{len(result) + 1:04d}",
                "seed": seed,
                "repetition": repetition,
                "rotation": rotation,
                "config": game.model_dump(),
                "status": "pending",
            }
        )
    return result
