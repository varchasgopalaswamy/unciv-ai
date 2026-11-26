"""Review preparation and publication are idempotent, epoch-fenced activities."""

from civ_agent.orchestration.contracts import ReviewRequest, ReviewResult
from civ_agent.orchestration.review import ReviewServices, validate_report
from civ_agent.orchestration.store import JournalConflict
from civ_agent.orchestration.transcripts import seal
from civ_agent.orchestration.workflows_types import CheckReview, FinishReview, PreparedReview, ReviewValidation
from civ_agent.review_feedback import correction_message
from civ_agent.strategic_review import ADVISOR_INSTRUCTIONS


async def prepare_review(request: ReviewRequest, *, worker: ReviewServices) -> PreparedReview:
    from civ_agent.civilization_guide import civilization_reference
    from civ_agent.runtime.preload import build_preload
    from civ_agent.strategy_guide import era_guidance, player_strategy_reference

    inputs, _, prompt = worker.prepare(request, guides_in_user=False)
    reference = player_strategy_reference(inputs["reads"], "review") if request.strategy_guide else ""
    era = era_guidance(inputs["reads"])
    preload = build_preload(
        "review",
        instructions=ADVISOR_INSTRUCTIONS.replace("inspect decisions", "function tools"),
        strategy_guide="\n\n".join(part for part in (reference, era) if part),
        civilization_guide=civilization_reference(inputs["civilization"]) if request.civilization_guide else "",
    )
    return PreparedReview(
        instructions=preload.instructions,
        user=prompt.user,
    )


async def check_review(check: CheckReview, *, worker: ReviewServices) -> ReviewValidation:
    try:
        validate_report(check.request, check.report, check.reads, worker=worker)
    except JournalConflict:
        raise
    except ValueError as error:
        return ReviewValidation(accepted=False, feedback=correction_message(error))
    return ReviewValidation(accepted=True)


async def finish_review(finished: FinishReview, *, worker: ReviewServices) -> ReviewResult:
    store, request = worker.store, finished.request
    key = "report"
    body = finished.model_dump()
    # Invalid reports never reserve the publication key, so one bounded correction
    # can replace them. Epoch/storage failures still stop instead of becoming advice.
    advice = (
        validate_report(request, finished.report, finished.reads, worker=worker)
        if finished.report is not None
        else None
    )
    _, receipt = store.reserve(request.scope, key, body)
    if receipt is not None:
        return ReviewResult.model_validate(store.get(request.scope, receipt))
    # Publication is only the final completion receipt, so a crash while storing
    # either immutable artifact cannot expose a half-published report.
    result = ReviewResult(
        scope=request.scope,
        status="COMPLETED" if advice is not None else "UNAVAILABLE",
        report=store.put(request.scope, advice) if advice is not None else None,
        history=await seal(store, request.scope, finished.history, agent="strategic_review_v1"),
        usage=finished.usage,
    )
    store.complete(request.scope, key, body, result.model_dump())
    return result
