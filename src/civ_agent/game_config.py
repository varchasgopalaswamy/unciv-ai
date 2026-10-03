"""One configuration for native setup, isolated players, and benchmark expansion."""

import itertools
import random
import secrets
import tomllib
from pathlib import Path
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from civ_agent.agent_settings import valid_setting

Positive = Annotated[int, Field(gt=0)]
Seed = Annotated[int, Field(ge=-(2**63), lt=2**63)]


def valid_name(value):
    return bool(value and len(value) <= 128 and value.isprintable() and value == value.strip())


class Configuration(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_default=True)


class GameSetup(Configuration):
    seed: Seed | None = None
    assignment_seed: Seed | None = None
    randomize: bool = False
    civilizations: list[str] = Field(default_factory=lambda: ["Rome", "Greece", "France", "Egypt"])
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

    @field_validator("civilizations")
    @classmethod
    def seats(cls, values):
        if not values or any(not valid_name(value) for value in values):
            raise ValueError("Choose valid civilization names")
        if len(set(values)) != len(values):
            raise ValueError("Civilizations must be distinct")
        return values


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
    name: str
    controller: Literal["agent", "human", "built_in_ai"] = "agent"
    model: str | None = None
    reasoning_effort: str | None = None

    @field_validator("name")
    @classmethod
    def player_name(cls, value):
        if not valid_name(value):
            raise ValueError("Invalid player name")
        return value

    @model_validator(mode="after")
    def settings(self):
        for value in (self.model, self.reasoning_effort):
            if value is not None and not valid_setting(value):
                raise ValueError("Invalid model or reasoning effort")
            if value is not None and self.controller != "agent":
                raise ValueError("Only agent players have model settings")
        return self


class Assignment(Configuration):
    civilization: str
    player: str


class AssignedPlayer(Player):
    civilization: str


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
    seeds: list[Seed] = Field(default_factory=list)
    repetitions: Positive = 1
    parallel: Positive = 1
    rotate_players: bool = False


class GameConfig(Configuration):
    game: GameSetup = Field(default_factory=GameSetup)
    agents: AgentDefaults = Field(default_factory=AgentDefaults)
    players: list[Player] = Field(
        default_factory=lambda: (
            [Player(name=f"Agent {index}") for index in range(1, 4)] + [Player(name="Unciv", controller="built_in_ai")]
        )
    )
    limits: Limits = Field(default_factory=Limits)
    resources: Resources = Field(default_factory=Resources)
    runtime: Runtime = Field(default_factory=Runtime)
    batch: Batch = Field(default_factory=Batch)

    @model_validator(mode="after")
    def roster(self):
        names = [p.name for p in self.players]
        if len(names) != len(set(names)):
            raise ValueError("Player names must be distinct")
        if len(names) != len(self.game.civilizations):
            raise ValueError("Choose exactly one player for each civilization")
        if not any(p.controller == "agent" for p in self.players):
            raise ValueError("Choose at least one agent player")
        if sum(p.controller == "human" for p in self.players) > 1:
            raise ValueError("The native desktop supports one human per game")
        return self

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
        if config.game.assignment_seed is None:
            config.game.assignment_seed = secrets.randbits(63)
        names = [p.name for p in config.players]
        if config.game.randomize:
            random.Random(config.game.assignment_seed).shuffle(names)
        return ResolvedGameConfig(
            **config.model_dump(),
            assignments=[
                Assignment(civilization=civ, player=name)
                for civ, name in zip(config.game.civilizations, names, strict=True)
            ],
        )


class ResolvedGameConfig(GameConfig):
    """Persisted player assignments are authoritative on every restart."""

    assignments: list[Assignment]

    @model_validator(mode="after")
    def complete_assignment(self):
        if self.game.seed is None or self.game.assignment_seed is None:
            raise ValueError("Resolved games must record both seeds")
        if [a.civilization for a in self.assignments] != self.game.civilizations:
            raise ValueError("Assignments must preserve civilization order")
        if sorted(a.player for a in self.assignments) != sorted(p.name for p in self.players):
            raise ValueError("Assign every player exactly once")
        return self

    @property
    def assigned_players(self):
        players = {p.name: p for p in self.players}
        return [AssignedPlayer(**players[a.player].model_dump(), civilization=a.civilization) for a in self.assignments]

    @property
    def turn_order(self):
        # Assignment is complete before ordering turns. Keep the local human
        # first, followed by agents, then every built-in AI (stable within groups).
        priorities = {"human": 0, "agent": 1, "built_in_ai": 2}
        return sorted(self.assigned_players, key=lambda p: priorities[p.controller])

    @property
    def agent_players(self):
        return [p for p in self.turn_order if p.controller == "agent"]

    @property
    def human(self):
        return next((p.civilization for p in self.assigned_players if p.controller == "human"), None)

    def player_records(self):
        records = []
        for player in self.turn_order:
            record = player.model_dump(exclude_none=True)
            if player.controller == "agent":
                settings = self.settings(player)
                record.update(model=settings["model"], reasoning_effort=settings["reasoning_effort"])
            records.append(record)
        return records

    def resolved(self):
        return self.model_copy(deep=True)

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
        for player in self.turn_order:
            arguments += [
                "--built-in-ai" if player.controller == "built_in_ai" else "--agent-player",
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
    """Rotate complete player profiles across fixed civilization seats."""
    config = config.resolved()
    seeds = config.batch.seeds or [config.game.seed]
    rotations = range(len(config.players) if config.batch.rotate_players else 1)
    # The same assignment schedule is used on every map seed. Repetitions get
    # independent, reproducible shuffles without consuming the map's randomness.
    rng = random.Random(config.game.assignment_seed)
    assignment_seeds = [config.game.assignment_seed] + [
        rng.getrandbits(63) for _ in range(config.batch.repetitions - 1)
    ]
    result = []
    for seed, repetition, rotation in itertools.product(seeds, range(config.batch.repetitions), rotations):
        settings = GameConfig.model_validate(config.model_dump(exclude={"assignments"}))
        settings.game.seed = seed
        settings.game.assignment_seed = assignment_seeds[repetition]
        settings.batch = Batch()
        game = settings.resolved()
        names = [a.player for a in game.assignments]
        names = names[rotation:] + names[:rotation]
        for assignment, name in zip(game.assignments, names, strict=True):
            assignment.player = name
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
