"""Frozen peace consultation inputs and scoped publication receipts."""

from civ_agent.orchestration.contracts import (
    FinishPeace,
    FinishPeacePhase,
    PeacePhase,
    PeacePhaseResult,
    PeaceResult,
    canonical,
)
from civ_agent.orchestration.store import JournalConflict
from civ_agent.orchestration.transcripts import seal
from civ_agent.peace_context import INSTRUCTIONS, peace_prompt_inputs
from civ_agent.peace_contracts import PeaceApproval, TacticalOverview
from civ_agent.player_prompts import CAMPAIGN_GUIDANCE


def phase_scope(phase: PeacePhase):
    return phase.scope


class PeaceServices:
    def __init__(self, store, clients, *, model_factory=None):
        self.store, self.clients, self.model_factory = store, clients, model_factory

    def inputs(self, phase):
        inputs = self.store.get(phase.request.scope, phase.request.inputs)
        if inputs["position"]["epoch"] != phase.request.scope.epoch:
            raise JournalConflict("PEACE_EPOCH_CONFLICT")
        return inputs


async def prepare_phase(phase: PeacePhase, *, worker: PeaceServices) -> dict:
    frozen = worker.inputs(phase)
    inputs = {**frozen, "privateMemory": frozen["privateMemoryByRole"][phase.phase]}
    if phase.phase == "gameplay":
        if phase.military is None:
            raise JournalConflict("PEACE_MILITARY_OVERVIEW_REQUIRED")
        inputs["militaryOverview"] = TacticalOverview.model_validate(phase.military).model_dump(mode="json")
    brief = peace_prompt_inputs(inputs, phase.phase)
    role = (
        "As military, assess accepted goals, tactical position, costs of continuing and consequences of peace. You advise; Main decides."
        if phase.phase == "military"
        else "As Main, approve or decline the exact terms after considering the military overview and your current goals. Explain disagreement."
    )
    return {
        "instructions": role
        + "\n"
        + CAMPAIGN_GUIDANCE
        + "\n"
        + INSTRUCTIONS
        + "\nUse only the frozen evidence. Return submit_report.",
        "user": canonical(brief),
    }


async def finish_phase(finished: FinishPeacePhase, *, worker: PeaceServices) -> PeacePhaseResult:
    store, phase = worker.store, finished.phase
    worker.inputs(phase)
    report = finished.report
    if report is not None:
        schema = TacticalOverview if phase.phase == "military" else PeaceApproval
        report = schema.model_validate(report).model_dump(mode="json")
    body = finished.model_dump(mode="json")
    _, prior = store.reserve(phase.request.scope, "peace-phase:" + phase.phase, body)
    if prior is not None:
        return PeacePhaseResult.model_validate(store.get(phase.request.scope, prior))
    result = PeacePhaseResult(
        phase=phase.phase,
        status="COMPLETED" if report is not None else "UNAVAILABLE",
        report=report,
        history=await seal(
            store,
            phase.request.scope,
            finished.history,
            agent="peace_military_v1" if phase.phase == "military" else "peace_main_v1",
        ),
        usage=finished.usage,
    )
    store.complete(phase.request.scope, "peace-phase:" + phase.phase, body, result.model_dump(mode="json"))
    return result


async def finish_consultation(finished: FinishPeace, *, worker: PeaceServices) -> PeaceResult:
    store, request = worker.store, finished.request
    for phase in finished.phases:
        if store.receipt(request.scope, "peace-phase:" + phase.phase) != phase.model_dump(mode="json"):
            raise JournalConflict("PEACE_PHASE_RECEIPT_REQUIRED")
    ready = (
        len(finished.phases) == 2
        and [phase.phase for phase in finished.phases] == ["military", "gameplay"]
        and all(phase.status == "COMPLETED" for phase in finished.phases)
    )
    result = PeaceResult(
        scope=request.scope,
        basis=request.basis,
        status="COMPLETED" if ready else "UNAVAILABLE",
        overview=finished.phases[0].report if ready else None,
        approval=finished.phases[1].report if ready else None,
        phases=finished.phases,
    )
    body = finished.model_dump(mode="json")
    store.reserve(request.scope, "peace-result", body)
    store.complete(request.scope, "peace-result", body, result.model_dump(mode="json"))
    return result
