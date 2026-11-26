"""One model-input boundary for prepared role and communication observations."""

import json
from typing import Any

from pydantic import Field

from civ_agent.protocol import JsonObject
from civ_agent.schemas import BoundaryModel


class PreparedRoleContext(BoundaryModel):
    role: str
    reads: dict[str, JsonObject]
    decisionDetails: list[JsonObject] = Field(default_factory=list, max_length=48)
    coordination: JsonObject | None = None
    memory: str | None = None
    roleTask: JsonObject | None = None
    events: list[int] = Field(default_factory=list)
    waitForEconomy: bool = False
    intentHandoff: JsonObject | None = None

    @classmethod
    def from_prepared(cls, prepared: JsonObject, **fields: Any):
        from civ_agent.intent_handoffs import startup_handoff

        return cls(
            role=prepared["purpose"],
            reads=prepared["reads"],
            decisionDetails=prepared.get("decisionDetails", []),
            coordination=prepared.get("economyCoordination"),
            memory=prepared.get("memoryContext"),
            intentHandoff=startup_handoff(prepared["reads"], prepared.get("economyCoordination")),
            **fields,
        )

    def serialize(self, *, fixture: JsonObject | None = None) -> str:
        # Fixtures may add task labels, never replace prepared evidence.
        return json.dumps({**(fixture or {}), **self.model_dump(mode="json", exclude_none=True)})


class PreparedDelivery(BoundaryModel):
    access: JsonObject
    militaryReview: JsonObject | None = None
    militaryTargets: list[str] = Field(default_factory=list)

    @classmethod
    def freeze(cls, delivery):
        return cls(
            access=delivery.delivered_access,
            militaryReview=delivery.review,
            militaryTargets=sorted(delivery.review_targets),
        ).model_dump(mode="json")


def empty_prepared_access():
    return {
        "revision": None,
        "details": [],
        "warVersions": {},
        "memoryVersions": {},
        "messages": [],
        "trades": {},
        "entities": {},
    }


def delivered_prepared_context(gateway, job, catalogue, serialized: str):
    """Credit only observations present in the exact opening message we saved."""
    from civ_agent.decision_contracts import compile_decision_contract
    from civ_agent.gateway_batches import BatchExecutor

    value = json.loads(serialized)
    context = PreparedRoleContext.model_validate(
        {key: value[key] for key in PreparedRoleContext.model_fields if key in value}
    )
    delivery = BatchExecutor(gateway, job, compile_decision_contract(job.purpose, catalogue))
    delivery.delivered_revision = None
    delivery.delivered_access = empty_prepared_access()
    for reply in [*context.reads.values(), *context.decisionDetails]:
        delivery.delivered(reply)
    if context.coordination is not None:
        delivery.delivered({"economyCoordination": context.coordination})
    # A memory context is the actual rendered memory selected for this run.
    if context.memory:
        from civ_agent.memory_delivery import apply_memory_delivery

        body = json.loads(context.memory.split("\n", 1)[1])
        if "memoryDelivery" in body:
            _, records = apply_memory_delivery({}, body, scope=job.coordination_scope)
            delivery.delivered_access["memoryVersions"].update(
                {key: record["version"] for key, record in records.items()}
            )
    return delivery
