"""Explicit choices for Economy workers left idle at the end of a pass."""

from typing import Annotated

from pydantic import Field

from civ_agent.economy_schedule import worker_facts
from civ_agent.schemas import BoundaryModel


class WorkerSkip(BoundaryModel):
    targets: list[Annotated[str, Field(min_length=1, max_length=128)]] = Field(min_length=1, max_length=500)
    reason: str = Field(
        min_length=1,
        max_length=500,
        pattern=r"\S",
        description="Why these idle Economy workers should skip further work this pass. Group workers sharing a reason; deliberate holds are permitted.",
    )


def worker_completion(view, state):
    return {
        "idleWorkers": sorted(handle for handle, facts in worker_facts(view, state).items() if facts["needsOrders"]),
        "instruction": "Give idle workers useful work or travel, or explicitly choose to skip them in finish.arguments.workerSkips with targets and a reason. Cover every remaining idle worker exactly once. Existing jobs, destinations and standing orders need no skip. Skips record this pass's decision without issuing native orders.",
    }


def worker_completion_problem(view, state, supplied):
    review = worker_completion(view, state)
    targets = [target for skip in supplied for target in skip["targets"]]
    if len(targets) == len(set(targets)) and set(targets) == set(review["idleWorkers"]):
        return None
    return {
        "error": "ECONOMY_WORKER_SKIPS_REQUIRED",
        "applied": False,
        "recoverable": True,
        "roleCompletion": review,
        "recovery": {
            "instruction": "Account for the remaining idle workers using actions or reasoned workerSkips before finishing. Refresh player_turn if their state changed; do not repeat confirmed actions.",
        },
    }
