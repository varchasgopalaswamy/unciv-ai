"""Frozen generation facts and their explicit replacement-allowance policy."""

from typing import Annotated, Literal

from pydantic import ConfigDict, Field, model_validator

from civ_agent.orchestration.contracts import Digest, Scope
from civ_agent.schemas import BoundaryModel

type AllowancePolicy = Literal["carry", "refresh"]


class GenerationBudgetTransfer(BoundaryModel):
    model_config = ConfigDict(frozen=True)
    version: Literal[1] = 1
    source: Scope
    target: Scope
    generation_ids: Annotated[list[Digest], Field(max_length=100)]
    allowance_policy: AllowancePolicy = Field(default="carry", exclude_if=lambda value: value == "carry")

    @model_validator(mode="after")
    def binding(self):
        if (self.source.game, self.source.player) != (
            self.target.game,
            self.target.player,
        ) or self.source.epoch == self.target.epoch:
            raise ValueError("RECOVERY_BUDGET_SCOPE_CONFLICT")
        if self.generation_ids != sorted(set(self.generation_ids)):
            raise ValueError("RECOVERY_BUDGET_GENERATION_CONFLICT")
        return self

    @property
    def inherited_ids(self) -> set[str]:
        if self.allowance_policy == "refresh":
            return set()
        return {self.target.identity("rewind-generation", identity) for identity in self.generation_ids}
