"""Write synthetic SDK histories through the upstream store in domain fixtures."""

from pydantic_ai_harness.step_persistence import ContinuableSnapshot, RunRecord, ToolEffectRecord

from civ_agent.orchestration.contracts import canonical
from civ_agent.orchestration.transcripts import backend, pointer


async def save_history(store, scope, messages, *, agent="strategic_review_v1", conversation="review"):
    transcript = pointer(scope.identity("fixture-history", agent), messages)
    steps = backend(store, create=True)
    await steps.register_run(
        RunRecord(
            run_id=transcript.run_id,
            agent_name=agent,
            metadata={"scope": canonical(scope.model_dump(mode="json"))},
            conversation_id=scope.identity("conversation", conversation),
        )
    )
    await steps.save_snapshot(
        ContinuableSnapshot(run_id=transcript.run_id, step_index=0, messages=messages, agent_name=agent)
    )
    return transcript


async def save_wait(store, request, identifier):
    steps = backend(store, create=True)
    await steps.register_run(
        RunRecord(
            run_id=request.run_id,
            agent_name="player_role_v1",
            metadata={"scope": canonical(request.scope.model_dump(mode="json"))},
        )
    )
    await steps.record_tool_effect(
        ToolEffectRecord(
            run_id=request.run_id,
            tool_call_id=request.call_id,
            tool_name="economy_wait",
            status="started",
            idempotency_key=identifier,
        )
    )
