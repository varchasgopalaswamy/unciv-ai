"""One game's native setup, isolated players and reproducible seed selection."""

import hashlib
import random
import secrets
import tomllib
from pathlib import Path
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from civ_agent.agent_settings import communication_effort, valid_setting
from civ_agent.civilization_guide import civilization_reference
from civ_agent.decisions_policy import DecisionsSettings
from civ_agent.protocol import JsonObject
from civ_agent.providers import Provider

Positive = Annotated[int, Field(gt=0)]
Seed = Annotated[int, Field(ge=-(2**63), lt=2**63)]
SEED_FIELDS = ("map_seed", "starting_position_seed", "assignment_seed")


def derived_seed(master_seed: int, purpose: str) -> int:
    """Separate stable streams; overriding one seed never changes another.

    Native map seed zero means 'choose the current time', so derived seeds are
    positive signed 64-bit integers. The namespace is part of the saved format.
    """
    digest = hashlib.sha256(f"civ-game-seed-v1:{master_seed}:{purpose}".encode()).digest()
    return int.from_bytes(digest[:8], "big") % (2**63 - 1) + 1


def valid_name(value):
    return bool(value and len(value) <= 128 and value.isprintable() and value == value.strip())


class Configuration(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_default=True)


class GameSetup(Configuration):
    master_seed: Seed | None = None
    map_seed: Seed | None = None
    starting_position_seed: Seed | None = None
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
        if self.map_seed == 0:
            raise ValueError("map_seed must be nonzero; omit it to derive a reproducible seed")
        return self

    @field_validator("civilizations")
    @classmethod
    def seats(cls, values):
        if not values or any(not valid_name(value) for value in values):
            raise ValueError("Choose valid civilization names")
        if len(set(values)) != len(values):
            raise ValueError("Civilizations must be distinct")
        return values


class AgentDefaults(Configuration, DecisionsSettings):
    provider: Provider = "openai"
    model: str = "gpt-6-luna"
    reasoning_effort: str = "medium"
    communication_reasoning_effort: str | None = None
    review_interval: Positive = 5
    review_attempts: Positive = 3
    strategy_guide: bool = False
    civilization_guide: bool = False
    native_advice_enabled: bool = False
    fast: bool = False

    @field_validator("model", "reasoning_effort")
    @classmethod
    def setting(cls, value):
        if not valid_setting(value):
            raise ValueError("Expected a nonempty model/effort name without whitespace")
        return value

    @field_validator("communication_reasoning_effort")
    @classmethod
    def optional_setting(cls, value):
        if value is not None and not valid_setting(value):
            raise ValueError("Expected a nonempty effort name without whitespace")
        return value


class EconomyModel(Configuration):
    provider: Provider | None = None
    model: str | None = None
    reasoning_effort: str | None = None

    @field_validator("model", "reasoning_effort")
    @classmethod
    def setting(cls, value):
        if value is not None and not valid_setting(value):
            raise ValueError("Invalid economy model setting")
        return value


class Player(Configuration):
    name: str
    controller: Literal["agent", "human", "built_in_ai"] = "agent"
    economy_model: EconomyModel | None = None
    provider: Provider | None = None
    model: str | None = None
    reasoning_effort: str | None = None
    communication_reasoning_effort: str | None = None

    @field_validator("name")
    @classmethod
    def player_name(cls, value):
        if not valid_name(value):
            raise ValueError("Invalid player name")
        return value

    @model_validator(mode="after")
    def settings(self):
        if self.economy_model is not None and self.controller != "agent":
            raise ValueError("Only agent players have economy model settings")
        if self.provider is not None and self.controller != "agent":
            raise ValueError("Only agent players have provider settings")
        for value in (self.model, self.reasoning_effort, self.communication_reasoning_effort):
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
    model_capacity_retries: Annotated[int, Field(ge=0, le=100)] = 5
    model_capacity_retry_delay: Annotated[int, Field(gt=0, le=86400)] = 30


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
        for player in self.players:
            if player.controller == "agent":
                settings = self.settings(player)
                if settings["provider"] == "openrouter" and "/" not in settings["model"]:
                    raise ValueError("OpenRouter requires an explicit model ID, such as anthropic/claude-sonnet-4.5")
        return self

    def settings(self, player) -> JsonObject:
        provider = player.provider or self.agents.provider
        gameplay_effort = player.reasoning_effort or self.agents.reasoning_effort
        economy = {
            "provider": provider,
            "model": player.model or self.agents.model,
            "reasoning_effort": gameplay_effort,
        }
        if player.economy_model is not None:
            economy.update(player.economy_model.model_dump(exclude_none=True))
        if economy["provider"] == "openrouter" and "/" not in economy["model"]:
            raise ValueError("OpenRouter economy model requires an explicit model ID")
        return {
            **self.agents.model_dump(),
            "economy_model": economy,
            "provider": provider,
            "model": player.model or self.agents.model,
            "reasoning_effort": gameplay_effort,
            "communication_reasoning_effort": communication_effort(
                gameplay_effort,
                player.communication_reasoning_effort or self.agents.communication_reasoning_effort,
            ),
            "fast": self.agents.fast and provider == "openai",
        }

    def resolved(self):
        config = self.model_copy(deep=True)
        if config.game.master_seed is None:
            config.game.master_seed = secrets.randbits(63)
        for field in SEED_FIELDS:
            if getattr(config.game, field) is None:
                setattr(config.game, field, derived_seed(config.game.master_seed, field))
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
        if any(getattr(self.game, name) is None for name in ("master_seed", *SEED_FIELDS)):
            raise ValueError("Resolved games must record every seed")
        if [a.civilization for a in self.assignments] != self.game.civilizations:
            raise ValueError("Assignments must preserve civilization order")
        if sorted(a.player for a in self.assignments) != sorted(p.name for p in self.players):
            raise ValueError("Assign every player exactly once")
        if self.agents.civilization_guide:
            for player in self.agent_players:
                civilization_reference(player.civilization)
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
                record.update(
                    provider=settings["provider"],
                    model=settings["model"],
                    reasoning_effort=settings["reasoning_effort"],
                    communication_reasoning_effort=settings["communication_reasoning_effort"],
                )
            records.append(record)
        return records

    def resolved(self):
        return self.model_copy(deep=True)

    def engine_arguments(self):
        game = self.game
        arguments = [
            "--seed",
            str(game.map_seed),
            "--starting-position-seed",
            str(game.starting_position_seed),
            "--city-states",
            str(game.city_states),
        ]
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


def load_config(
    path: Path | None, overrides: dict | None = None, *, player_overrides: dict | None = None
) -> GameConfig:
    data: JsonObject = tomllib.loads(path.read_text()) if path else {}
    for section, values in (overrides or {}).items():
        if isinstance(values, dict):
            data[section] = {**data.get(section, {}), **values}
        else:
            data[section] = values
    if player_overrides:
        players = data.setdefault("players", [player.model_dump() for player in GameConfig().players])
        for name, values in player_overrides.items():
            selected = next(
                (
                    p
                    for p in players
                    if isinstance(p, dict) and p.get("name") == name and p.get("controller", "agent") == "agent"
                ),
                None,
            )
            if selected is None:
                raise ValueError(f"{name} is not an agent player")
            selected.update(values)
    return GameConfig.model_validate(data)
