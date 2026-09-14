"""Allowlisted final player view. Nested engine-only fields must never reach disk."""

from typing import Annotated, Literal

from pydantic import Field, field_validator

from civ_agent.schemas import BoundaryModel, NonnegativeInt

type Text = Annotated[str, Field(max_length=65536)]


class GameResult(BoundaryModel):
    outcome: Literal["Victory", "Defeat"]
    winningCivilizationId: Text | None
    winningCivilizationName: Text | None
    victoryType: Text | None
    victoryTurn: NonnegativeInt | None
    title: Text
    paragraphs: list[Text]


class FinalMessage(BoundaryModel):
    title: Text
    paragraphs: list[Text]
    acknowledgement: Text
    quote: Text | None = None
    additionalAcknowledgements: list[Text] = Field(default_factory=list)


class Combatant(BoundaryModel):
    kind: Text
    name: Text | None
    instanceName: Text | None
    isOwn: bool


class Location(BoundaryModel):
    x: int
    y: int


class CombatReport(BoundaryModel):
    id: Annotated[str, Field(min_length=1, max_length=65536)]
    turn: NonnegativeInt
    kind: Text
    text: Text | None
    locations: list[Location]
    attacker: Combatant | None = None
    defender: Combatant | None = None
    outcome: Text | None = None
    damageReceived: NonnegativeInt | None = None
    retaliationDamage: NonnegativeInt | None = None
    attackerDefeatedByDefender: bool | None = None
    showZeroDamage: bool | None = None
    ourUnit: Combatant | None = None
    enemyUnit: Combatant | None = None
    ourDamage: NonnegativeInt | None = None
    enemyDamage: NonnegativeInt | None = None
    ourDestroyed: bool | None = None
    enemyDestroyed: bool | None = None
    isAttacking: bool | None = None
    weaponType: Text | None = None
    attackingCivilizationName: Text | None = None
    improvementName: Text | None = None
    unitType: Text | None = None
    hitOurTerritory: bool | None = None


class PlayerResult(BoundaryModel):
    epoch: Annotated[str, Field(min_length=1, max_length=128)]
    civilization: Annotated[str, Field(min_length=1, max_length=128)]
    result: GameResult
    combatReports: list[CombatReport]
    messages: list[FinalMessage]

    @field_validator("combatReports")
    @classmethod
    def unique_reports(cls, reports: list[CombatReport]) -> list[CombatReport]:
        if len({report.id for report in reports}) != len(reports):
            raise ValueError("DUPLICATE_REPORT")
        return reports
