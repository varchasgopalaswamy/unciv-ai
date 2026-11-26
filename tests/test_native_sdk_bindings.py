"""Readers use saved SDK identities and frozen review bindings, never guessed dialogue IDs."""

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from pydantic_ai.messages import ModelRequest, ModelResponse, TextPart, ToolCallPart, ToolReturnPart
from pydantic_ai_harness.step_persistence import StepEvent

from civ_agent.orchestration.contracts import ModelSelection, Reference, ReviewRequest, digest
from civ_agent.orchestration.job_contracts import NativeJob, NativeReviewBinding, host_job_id
from civ_agent.orchestration.native_watch import task_metadata
from civ_agent.runtime.review_cycles import ReviewCycle
from civ_agent.runtime_profile import profile
from civ_agent.tool_failures import failure_report
from civ_agent.watch_state import WatchReader
from tests.native_watch_fixtures import fixture, history, memory, persistent_files, task


def test_all_readers_find_recorded_opaque_conversations_with_legacy_scope_metadata(tmp_path):
    fixture(tmp_path)
    request = task(tmp_path)
    messages = [
        ModelResponse(
            [TextPart("Opaque conversation is visible"), ToolCallPart("player_turn", {}, "read")], run_id="opaque"
        ),
        ModelRequest([ToolReturnPart("player_turn", {"error": "STALE_REVISION"}, "read")], run_id="opaque"),
    ]

    async def record():
        store = await history(
            tmp_path, request, run_id="opaque", conversation_id="actual-random-dialogue", messages=messages
        )
        stamp = datetime(2026, 11, 5, 12, tzinfo=UTC)
        await store.append_event(
            StepEvent(
                run_id="opaque",
                agent_name="player_role_v1",
                kind="model_request_completed",
                step_index=0,
                timestamp=stamp + timedelta(milliseconds=125),
            )
        )

    asyncio.run(record())
    before = persistent_files(tmp_path)
    view = WatchReader(tmp_path).read()
    assert not view.errors and any(item.text == "Opaque conversation is visible" for item in view.activity)
    measured = profile([tmp_path])
    assert not measured["unavailable"] and measured["llm"]["metrics"]["sdk.model_request.duration_ms"]["meanMs"] == 125
    failures = failure_report([tmp_path])
    assert not failures["unavailable"] and failures["failures"][0]["code"] == "STALE_REVISION"
    assert persistent_files(tmp_path) == before


def review(tmp_path):
    fixture(tmp_path)
    role = task(tmp_path)
    cycle_scope = role.scope.model_copy(update={"activation": digest("review-cycle")})
    request = ReviewRequest(
        scope=cycle_scope.model_copy(update={"activation": "new-review-attempt"}),
        inputs=Reference(sha256=digest("inputs")),
        selection=ModelSelection(model="exact-review-model", reasoning_effort="high"),
        source_turn=2,
        target_turn=3,
        review_cycle_id=cycle_scope.activation,
    )
    cycle = ReviewCycle(
        scope=cycle_scope, source_turn=2, target_turn=3, kind="periodic", inputs=request.inputs, request=request
    )
    state = memory(tmp_path)
    with state.transaction() as db:
        state._set(db, "nativeReviewCycle:3", cycle.model_dump(mode="json"))
    state.engine.dispose()
    return request, cycle


def test_detached_review_cycle_and_prior_attempt_are_included_with_exact_native_bindings(tmp_path):
    request, cycle = review(tmp_path)

    async def record():
        for activation in (request.scope.activation, "previous-review-attempt"):
            prior = request.model_copy(update={"scope": request.scope.model_copy(update={"activation": activation})})
            native = NativeJob(
                scope=prior.scope,
                job=activation,
                purpose="review",
                review=NativeReviewBinding(cycle_id=cycle.id, source_turn=2, target_turn=3),
            )
            await history(
                tmp_path,
                prior,
                text="Detached review " + activation,
                run_id=activation,
                conversation_id="opaque-" + activation,
                metadata={
                    "agent_name": "strategic_review_v1",
                    "purpose": "review",
                    "source_turn": "2",
                    "target_turn": "3",
                    "review_cycle_id": cycle.id,
                    "native_job_id": host_job_id(native),
                    "model": "exact-review-model",
                    "provider": "openai",
                    "reasoningEffort": "high",
                },
            )

    asyncio.run(record())
    from civ_agent.player_paths import PlayerPaths

    paths = PlayerPaths(tmp_path, 0, "Rome")
    tasks, _ = task_metadata(paths.coordinator / "notebook/memory.sqlite3", request.scope)
    assert tasks[request.scope.activation].host_job == host_job_id(
        NativeJob(
            scope=request.scope,
            job=request.scope.activation,
            purpose="review",
            review=NativeReviewBinding(cycle_id=cycle.id, source_turn=2, target_turn=3),
        )
    )
    measured = profile([tmp_path])
    assert not measured["unavailable"]
    assert measured["llm"]["observedBoundaries"]["model_request:started"] == 2
    view = WatchReader(tmp_path).read()
    assert not view.errors
    from civ_agent.orchestration.native_watch import sdk_lines

    lines = sdk_lines(paths, request.scope, tasks)
    assert sum(item.label == "Assistant" and item.purpose == "review" for item in lines) == 2
    failures = failure_report([tmp_path])
    assert not failures["unavailable"]


@pytest.mark.parametrize(
    "field,value",
    [
        ("native_job_id", "native:wrong"),
        ("agent_name", "peace_main_v1"),
        ("purpose", "communication"),
        ("job", "other"),
    ],
)
def test_canonical_sdk_labels_cannot_rebind_a_role_run(tmp_path, field, value):
    fixture(tmp_path)
    request = task(tmp_path)
    asyncio.run(history(tmp_path, request, metadata={field: value}))
    assert profile([tmp_path])["unavailable"]
    assert failure_report([tmp_path])["unavailable"]
    assert WatchReader(tmp_path).read().errors


def test_consultation_phases_with_shared_legacy_scope_keep_their_recorded_agents(tmp_path):
    from civ_agent.orchestration.contracts import PeacePhase, PeaceRequest
    from civ_agent.orchestration.native_watch import operator_tasks, sdk_lines
    from civ_agent.player_paths import PlayerPaths

    fixture(tmp_path)
    role = task(tmp_path)
    request = PeaceRequest(
        scope=role.scope.model_copy(update={"activation": "peace-base"}),
        basis=digest("basis"),
        inputs=Reference(sha256=digest("inputs")),
        selection=role.selection,
    )
    state = memory(tmp_path)
    with state.transaction() as db:
        state._set(db, "nativePeace:base", {"request": request.model_dump(mode="json")})
    state.engine.dispose()
    jobs = []

    async def record():
        for purpose, agent in (("military", "peace_military_v1"), ("gameplay", "peace_main_v1")):
            phase = PeacePhase(request=request, phase=purpose)
            native = NativeJob(scope=phase.scope, job=phase.scope.activation, purpose="consultation")
            jobs.append({"id": host_job_id(native), "purpose": "consultation", "gameTurn": 2})
            await history(
                tmp_path,
                request,
                text=purpose + " consultation",
                run_id=purpose,
                agent_name=agent,
                conversation_id="recorded-" + purpose,
                metadata={"purpose": purpose, "native_job_id": host_job_id(native), "agent_name": agent},
            )

    asyncio.run(record())
    paths = PlayerPaths(tmp_path, 0, "Rome")
    tasks = operator_tasks(paths.coordinator / "notebook/memory.sqlite3", role.scope, jobs)
    lines = sdk_lines(paths, role.scope, tasks)
    assert {line.text for line in lines if line.label == "Assistant"} == {
        "military consultation",
        "gameplay consultation",
    }
