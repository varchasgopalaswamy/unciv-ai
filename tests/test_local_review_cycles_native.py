"""Periodic advice freezes the old native turn and gates the next Main run."""

import asyncio
import copy
import sqlite3

import pytest
from pydantic_ai.messages import ModelResponse, ToolCallPart, ToolReturnPart
from pydantic_ai.models.function import FunctionModel

from civ_agent.monitoring import path_for, read_record
from civ_agent.operator_connection import Operator
from civ_agent.orchestration.handoff import NativeHandoffs
from civ_agent.orchestration.review_publication import NativeReviews
from civ_agent.runtime.controller import run_controller
from civ_agent.runtime.player import LocalPlayer
from civ_agent.runtime.review_cycles import ReviewCycle
from civ_agent.runtime.state import ControllerStore
from civ_agent.tls import write_private_json
from tests.test_local_controller_native import local_config, native_game


def saved_cycle(root, target):
    path = root / "coordinator/Rome/notebook/memory.sqlite3"
    with sqlite3.connect(path.as_uri() + "?mode=ro", uri=True) as database:
        row = database.execute("SELECT value FROM meta WHERE key = ?", ("nativeReviewCycle:" + str(target),)).fetchone()
    assert row is not None
    return ReviewCycle.model_validate_json(row[0])


@pytest.mark.parametrize("lost_handoff_ack", [False, True], ids=["periodic", "resume-pending-cycle"])
def test_periodic_review_freezes_source_before_handoff_then_gates_next_main(tmp_path, monkeypatch, lost_handoff_ack):
    root = tmp_path / "game"
    with native_game(root) as (url, ca, token):
        config = local_config()
        config.agents.review_interval = 1
        config.limits.max_turns = 3
        config.limits.max_seconds = 180
        manifest = {
            "id": "localreview",
            "runtime": "pydantic-local-v1",
            "launch": "initial",
            "native": {"leaseId": "local-review-lease"},
        }
        write_private_json(root / "resolved.json", config.model_dump(mode="json"))
        write_private_json(root / "container.json", manifest)
        operator = Operator(url, token, ca)

        async def run():
            review_started, release_review, barrier, main_started = (asyncio.Event() for _ in range(4))
            captured, order, model_requests, periodic_requests = [], [], [], []
            players = []
            original_capture = NativeReviews.capture
            original_execute = NativeHandoffs.execute
            original_review = LocalPlayer.review
            original_role = LocalPlayer.run_role
            original_phase = ControllerStore.phase
            from civ_agent.orchestration import scripted_model

            original_model = scripted_model.scripted_review_model
            injected = False

            async def capture(service, source_turn, target_turn, **kwargs):
                cycle = await original_capture(service, source_turn, target_turn, **kwargs)
                if (source_turn, target_turn) == (1, 2):
                    assert cycle is not None and cycle.inputs is not None
                    inputs = service.root.store.get(cycle.scope, cycle.inputs)
                    if not captured:
                        native = (await asyncio.to_thread(operator.call, "status"))["status"]
                        assert native["turn"] == 1 and native["handoff"] == cycle.source_handoff
                        assert not cycle.committed and cycle.request is None
                        assert inputs["observation"]["turn"] == inputs["reads"]["player_turn"]["turn"] == 1
                        captured.append((cycle.model_copy(deep=True), copy.deepcopy(inputs)))
                        order.append("captured-before-handoff")
                    else:
                        assert cycle.inputs == captured[0][0].inputs and inputs == captured[0][1]
                return cycle

            async def execute(service, effect):
                nonlocal injected
                periodic = effect.request.ticket.turn == 1
                if periodic:
                    assert captured and not saved_cycle(root, 2).committed
                    order.append("handoff-execute")
                receipt = await original_execute(service, effect)
                if periodic and lost_handoff_ack and not injected:
                    injected = True
                    raise RuntimeError("INJECTED_AFTER_PERIODIC_HANDOFF_COMMIT")
                return receipt

            async def review(player, cycle):
                if (cycle.source_turn, cycle.target_turn) != (1, 2):
                    return await original_review(player, cycle)
                assert cycle.committed and captured
                native = (await asyncio.to_thread(operator.call, "status"))["status"]
                assert native["handoff"] > captured[0][0].source_handoff
                assert player.root.store.get(cycle.scope, cycle.inputs) == captured[0][1]
                players.append(player)
                order.append("review-after-handoff")
                review_started.set()
                await release_review.wait()
                await original_review(player, cycle)
                assert (await player.root.reviews.for_target(2)).status == "READY"
                order.append("review-ready")

            async def role(player, purpose, context):
                if purpose == "gameplay" and player.ticket.turn == 2:
                    assert release_review.is_set() and "review-ready" in order
                    assert (await player.root.reviews.for_target(2)).status == "READY"
                    order.append("next-main")
                    main_started.set()
                return await original_role(player, purpose, context)

            async def phase(store, value, **kwargs):
                await original_phase(store, value, **kwargs)
                attempt = store.current.active_attempt
                if value == "WAITING_REVIEW" and attempt is not None and attempt.turn == 2:
                    assert not main_started.is_set()
                    order.append("next-turn-barrier")
                    barrier.set()

            def model(request):
                native_model = original_model(request)
                if (request.source_turn, request.target_turn) != (1, 2):
                    return native_model
                periodic_requests.append(request)
                callback = native_model.function
                assert callback is not None

                def correctable(messages, info):
                    response = callback(messages, info)
                    assert isinstance(response, ModelResponse)
                    model_requests.append(copy.deepcopy(messages))
                    if len(model_requests) == 1:
                        report = response.parts[0]
                        assert isinstance(report, ToolCallPart)
                        body = report.args_as_dict()
                        body["review"]["materialAssumptions"][0].update(status="inferred", sources=["invented-source"])
                        return ModelResponse(parts=[ToolCallPart("submit_report", body, "invalid-source")])
                    assert any(
                        isinstance(part, ToolReturnPart)
                        and part.tool_name == "submit_report"
                        and part.outcome == "retried"
                        for message in messages
                        for part in message.parts
                    )
                    return response

                return FunctionModel(correctable, model_name="scripted-v1")

            monkeypatch.setattr(NativeReviews, "capture", capture)
            monkeypatch.setattr(NativeHandoffs, "execute", execute)
            monkeypatch.setattr(LocalPlayer, "review", review)
            monkeypatch.setattr(LocalPlayer, "run_role", role)
            monkeypatch.setattr(ControllerStore, "phase", phase)
            monkeypatch.setattr(scripted_model, "scripted_review_model", model)

            if lost_handoff_ack:
                failed = await run_controller(root, resume=False, host_url=url, host_ca=ca)
                assert (failed.mode, failed.reason) == ("FAILED", "RuntimeError")
                pending = saved_cycle(root, 2)
                assert not pending.committed and pending.status == "PENDING" and pending.request is None
                assert not review_started.is_set() and not periodic_requests
                manifest["launch"] = "explicit-resume"
                write_private_json(root / "container.json", manifest)
            task = asyncio.create_task(run_controller(root, resume=lost_handoff_ack, host_url=url, host_ca=ca))
            try:
                await asyncio.wait_for(asyncio.gather(review_started.wait(), barrier.wait()), 90)
                assert saved_cycle(root, 2).status == "PENDING"
                assert not main_started.is_set() and not model_requests
                assert not task.done()
                release_review.set()
                final = await asyncio.wait_for(task, 90)
            finally:
                release_review.set()
                if not task.done():
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
            assert final.mode in {"STOPPED", "FINISHED"}, final.reason
            assert main_started.is_set() and len(periodic_requests) == 1 and len(model_requests) == 2
            assert (
                order.index("captured-before-handoff")
                < order.index("handoff-execute")
                < order.index("review-after-handoff")
            )
            assert order.index("next-turn-barrier") < order.index("review-ready") < order.index("next-main")
            current = saved_cycle(root, 2)
            assert current.status == "READY" and current.committed
            assert current.inputs == captured[0][0].inputs and current.source_handoff == captured[0][0].source_handoff
            request = periodic_requests[0]
            runs = await players[0].steps.list_runs(
                conversation_id=request.scope.identity("conversation", "strategic_review_v1")
            )
            assert len(runs) == 1
            assert runs[0].metadata["source_turn"] == "1" and runs[0].metadata["target_turn"] == "2"
            assert runs[0].metadata["review_cycle_id"] == current.id
            events = await players[0].steps.list_events(run_id=runs[0].run_id)
            assert sum(event.kind == "model_request_started" for event in events) == 2
            assert sum(event.kind == "model_request_completed" for event in events) == 2
            # These are native API counters from real Python MCP calls, not model-request counters.
            for _ in range(20):
                monitored = read_record(path_for(root), "state", "result")
                calls = monitored.get("hostCalls", {}) if monitored else {}
                if calls.get("origin:agent", {}).get("calls", 0) and calls.get("origin:coordinator", {}).get(
                    "calls", 0
                ):
                    break
                await asyncio.sleep(0.1)
            assert calls["origin:agent"]["calls"] > 0 and calls["origin:coordinator"]["calls"] > 0

        asyncio.run(run())
