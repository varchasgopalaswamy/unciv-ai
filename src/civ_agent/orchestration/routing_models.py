"""Typed routing requests and results, independent of execution machinery."""

from typing import Annotated, Literal

from pydantic import Field, model_validator

from civ_agent.orchestration.contracts import Digest, NativeAuxiliaryTask, Reference, Scope
from civ_agent.orchestration.delivery_contracts import OutgoingDraft
from civ_agent.orchestration.routing_contracts import RoutingDirection
from civ_agent.protocol import JsonObject
from civ_agent.schemas import BoundaryModel, Identifier


class RoutingRun(BoundaryModel):
    version: Literal[1] = 1
    scope: Scope
    submission: Reference
    policy: Reference
    request_id: Digest
    operation_id: Identifier
    direction: RoutingDirection
    timeout_seconds: Annotated[int, Field(ge=1, le=300)]
    task: NativeAuxiliaryTask | None = Field(default=None, exclude_if=lambda value: value is None)


class RoutingReply(BoundaryModel):
    outcome: Reference


class RoutingResult(BoundaryModel):
    scope: Scope
    request_id: Digest
    outcome: Reference


class FinishRouting(BoundaryModel):
    request: RoutingRun
    reply: RoutingReply


class DraftPlan(BoundaryModel):
    routing: RoutingRun | None = None
    decision: JsonObject | None = None
    deadline: Annotated[float, Field(ge=0, allow_inf_nan=False)]


class DraftDecision(BoundaryModel):
    request: OutgoingDraft
    routing: RoutingResult | None = None
    deadline_expired: bool = False


class RoutingLink(BoundaryModel):
    scope: Scope
    id: Digest
    kind: Literal["routing", "outgoing-draft", "outgoing-delivery"]
    request: RoutingRun | None = None

    @model_validator(mode="after")
    def request_binding(self):
        if self.kind == "routing":
            if (
                self.request is None
                or self.request.scope != self.scope
                or self.id not in {self.scope.identity("task", "routing"), self.scope.identity("workflow", "routing")}
            ):
                raise ValueError("ROUTING_LINK_BINDING_CONFLICT")
        elif self.request is not None:
            raise ValueError("ROUTING_LINK_KIND_CONFLICT")
        return self
