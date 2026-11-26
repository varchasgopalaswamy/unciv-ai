"""Trusted classifier scope and source retirement, outside model evidence."""

from typing import Literal

from pydantic import ConfigDict, Field, model_validator

from civ_agent.orchestration.contracts import Digest, OpaqueId, Scope
from civ_agent.orchestration.managed_retirement import ManagedRetirementIntent
from civ_agent.schemas import BoundaryModel, NonnegativeInt


class NativeBrokerScope(BoundaryModel):
    model_config = ConfigDict(frozen=True)
    version: Literal[1] = 1
    game: OpaqueId
    player: OpaqueId
    epoch: OpaqueId

    @classmethod
    def from_scope(cls, scope: Scope):
        return cls(game=scope.game, player=scope.player, epoch=scope.epoch)


class CloseNativeBrokerEpoch(BoundaryModel):
    scope: NativeBrokerScope
    owner: ManagedRetirementIntent

    @model_validator(mode="after")
    def binding(self):
        if (self.scope.game, self.scope.epoch) != (self.owner.game, self.owner.source_epoch):
            raise ValueError("NATIVE_BROKER_CLOSE_OWNER_CONFLICT")
        return self


class NativeBrokerEpochClosed(BoundaryModel):
    scope: NativeBrokerScope
    owner_sha256: Digest
    closed: Literal[True] = True
    local_drain_complete: Literal[True] = True
    advisory_requests: NonnegativeInt
    advisory_unknown_completions: NonnegativeInt
    routing_requests: NonnegativeInt
    routing_unknown_completions: NonnegativeInt
    routing_unknown_usage: NonnegativeInt
    unattributed_requests: NonnegativeInt | None = Field(default=None, exclude_if=lambda value: value is None)
