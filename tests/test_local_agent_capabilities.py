"""Actual SDK requests share public prefixes, metadata and bounded model capacity."""

import asyncio
import json
import logging
import warnings
from types import SimpleNamespace

import pytest
from pydantic import BaseModel
from pydantic_ai import Agent, ConcurrencyLimiter
from pydantic_ai.exceptions import ModelContextWindowExceededError
from pydantic_ai.messages import ModelRequest, ModelResponse, TextPart, ToolCallPart
from pydantic_ai.models.function import DeltaToolCall, FunctionModel
from pydantic_ai_harness import Planning
from pydantic_ai_harness.planning import InMemoryPlanStore, PlanItem
from pydantic_ai_harness.step_persistence import SqliteStepStore
from pydantic_ai_harness.warn_on_cache_busts import CacheBustWarning, WarnOnCacheBusts

from civ_agent.orchestration.contracts import GatewayReceipt, ModelSelection, PrepareRole, canonical
from civ_agent.runtime.agents import LocalAgentRunner, LocalAgentServices, configure_cache_warning_logging
from civ_agent.runtime.capabilities import agent_capabilities, run_metadata
from civ_agent.runtime.model_policy import ModelPolicySettings
from civ_agent.runtime.preload import COMMON_INSTRUCTIONS, InstructionPreload, build_preload, model_cache_settings
from tests.archive_fixtures import session
from tests.domain_fixtures import run_async
from tests.test_local_agents import call, request
from tests.test_native_player_stores import production_stores


class Report(BaseModel):
    summary: str


def with_stream(model):
    async def stream(messages, info):
        response = model(messages, info)
        for index, part in enumerate(response.parts):
            if isinstance(part, ToolCallPart):
                yield {
                    index: DeltaToolCall(
                        name=part.tool_name, json_args=json.dumps(part.args_as_dict()), tool_call_id=part.tool_call_id
                    )
                }
            elif isinstance(part, TextPart):
                yield part.content

    return FunctionModel(model, stream_function=stream)


def report_deps():
    return SimpleNamespace(
        scope=request().scope,
        selection=ModelSelection(model="test"),
        provider_contract=None,
        source_turn=4,
        target_turn=5,
        review_cycle_id="review-cycle",
    )


def test_preload_and_cache_keys_follow_public_prefix_and_model_only():
    keys = []
    for purpose in ("gameplay", "economy", "military", "communication"):
        preload = build_preload(purpose, strategy_guide="PUBLIC STRATEGY", civilization_guide="PUBLIC CIV GUIDE")
        assert preload.instructions.startswith(COMMON_INSTRUCTIONS)
        assert preload.instructions.count(COMMON_INSTRUCTIONS) == 1
        assert preload.instructions.index("PUBLIC STRATEGY") < preload.instructions.index("PUBLIC CIV GUIDE")
        keys.append(model_cache_settings(ModelSelection(model="test"), preload).get("openai_prompt_cache_key"))
        assert "PUBLIC CIV GUIDE" not in str(preload.metadata())
    assert len(set(keys)) == 1
    assert keys[0] != model_cache_settings(ModelSelection(model="other"), preload).get("openai_prompt_cache_key")
    changed = InstructionPreload(preload.instructions, "A different actual public prefix")
    assert keys[0] != model_cache_settings(ModelSelection(model="test"), changed).get("openai_prompt_cache_key")
    routing = model_cache_settings(ModelSelection(provider="openrouter", model="other"), preload)
    assert routing["extra_body"] == {"provider": {"require_parameters": True}}


def test_extra_metadata_cannot_replace_the_run_owner_or_selected_model():
    deps = request()
    labels = run_metadata(
        deps,
        agent_name="player_role_v1",
        extra={
            "scope": "wrong",
            "purpose": "other",
            "agent_name": "other",
            "model": "wrong",
            "native_job_id": "native-job",
        },
    )
    assert labels["scope"] == canonical(deps.scope.model_dump(mode="json"))
    assert labels["purpose"] == "gameplay" and labels["agent_name"] == "player_role_v1"
    assert labels["model"] == deps.selection.model and labels["provider"] == deps.selection.provider
    assert labels["reasoningEffort"] == deps.selection.reasoning_effort
    assert labels["native_job_id"] == "native-job"


@run_async
async def test_role_and_report_requests_have_canonical_persisted_metadata_and_context_events(tmp_path):
    events, requests = [], []
    store = SqliteStepStore(database=tmp_path / "steps.sqlite3")

    async def context(deps, event):
        events.append((deps, event))

    async def invoke(*_):
        return GatewayReceipt(result={"roleFinished": True})

    def model(messages, info):
        assert COMMON_INSTRUCTIONS in info.instructions
        requests.append(info)
        name = info.output_tools[0].name
        if name == "finish_role":
            assert [tool.name for tool in info.function_tools] == ["player_turn", "unit_details"]
        else:
            assert info.function_tools == []
        return call(info, name, {"summary": "Done."}, "finish")

    services = LocalAgentServices(
        invoke=invoke,
        step_store=store,
        on_context_usage=context,
        run_metadata={"native_job_id": "native-review-job"},
    )
    runner = LocalAgentRunner(services)
    role = request()
    role_result = await runner.run_role(
        role,
        startup={"instructions": "ROLE MISSION", "user": "PRIVATE CURRENT POSITION"},
        catalogue=[
            {"name": "unit_details", "inputSchema": {}},
            {"name": "finish_role", "inputSchema": {}},
            {"name": "player_turn", "inputSchema": {}},
        ],
        model=with_stream(model),
    )
    review = report_deps()
    report = await runner.run_report(
        review,
        startup={"instructions": "REVIEW MISSION", "user": "PRIVATE FROZEN POSITION"},
        model=with_stream(model),
        output_type=Report,
        name="strategic_review_v1",
    )
    assert len(events) == 2 and events[0][0] is role and events[1][0] is review
    assert all(event.used_tokens > 0 and event.window_tokens == 65536 and not event.resolved for _, event in events)
    assert requests[0].model_settings.get("openai_prompt_cache_key") == requests[1].model_settings.get(
        "openai_prompt_cache_key"
    )
    for result, deps, name in ((role_result, role, "player_role_v1"), (report, review, "strategic_review_v1")):
        saved = await store.get_run(run_id=result.run_id)
        assert saved is not None
        assert saved.metadata == run_metadata(deps, agent_name=name, extra=services.run_metadata)
        assert saved.metadata["scope"] == canonical(deps.scope.model_dump(mode="json"))
        assert saved.metadata["native_job_id"] == "native-review-job"
        assert saved.metadata["model"] == deps.selection.model
        assert saved.metadata["provider"] == deps.selection.provider
        assert saved.metadata["reasoningEffort"] == deps.selection.reasoning_effort
        assert "PRIVATE" not in str(saved.metadata)
    saved_report = await store.get_run(run_id=report.run_id)
    assert saved_report is not None
    assert saved_report.metadata["source_turn"] == "4"
    assert saved_report.metadata["target_turn"] == "5"
    assert saved_report.metadata["review_cycle_id"] == "review-cycle"


@run_async
async def test_shared_request_limiter_allows_other_role_while_a_tool_waits(tmp_path):
    limiter = ConcurrencyLimiter(1)
    waiting, release = asyncio.Event(), asyncio.Event()
    role_calls = []

    async def invoke(_request, _identifier, _step, decision):
        if decision["kind"] != "finish":
            waiting.set()
            await release.wait()
        return GatewayReceipt(result={"roleFinished": True})

    def role_model(_messages, info):
        role_calls.append(True)
        return (
            call(info, "player_turn", {}, "read")
            if len(role_calls) == 1
            else call(info, "finish_role", {"summary": "Done."}, "finish")
        )

    def report_model(_messages, info):
        return call(info, "submit_report", {"summary": "Report finished during the tool wait."}, "report")

    runner = LocalAgentRunner(
        LocalAgentServices(
            invoke=invoke,
            step_store=SqliteStepStore(database=tmp_path / "steps.sqlite3"),
            model_limiter=limiter,
        )
    )
    role_task = asyncio.create_task(
        runner.run_role(
            request(),
            startup={"instructions": "Review.", "user": "Role."},
            catalogue=[{"name": "finish_role", "inputSchema": {}}, {"name": "player_turn", "inputSchema": {}}],
            model=FunctionModel(role_model),
        )
    )
    try:
        await asyncio.wait_for(waiting.wait(), 2)
        report = await asyncio.wait_for(
            runner.run_report(
                report_deps(),
                startup={"instructions": "Review.", "user": "Report."},
                model=FunctionModel(report_model),
                output_type=Report,
                name="strategic_review_v1",
            ),
            2,
        )
        assert report.report.summary == "Report finished during the tool wait." and not role_task.done()
    finally:
        release.set()
        await asyncio.wait_for(role_task, 2)
    assert limiter.running_count == limiter.waiting_count == 0


@run_async
async def test_cancelled_queued_role_does_not_leak_shared_model_capacity(tmp_path):
    limiter = ConcurrencyLimiter(1)
    started, release = asyncio.Event(), asyncio.Event()
    provider_calls = []

    async def model(_messages, info):
        provider_calls.append(True)
        if len(provider_calls) == 1:
            started.set()
            await release.wait()
        return call(info, "submit_report", {"summary": "Done."}, "report")

    async def invoke(*_):
        raise AssertionError("A report or cancelled queued role cannot execute a native effect")

    runner = LocalAgentRunner(
        LocalAgentServices(
            invoke=invoke,
            step_store=SqliteStepStore(database=tmp_path / "steps.sqlite3"),
            model_limiter=limiter,
        )
    )

    async def report():
        return await runner.run_report(
            report_deps(),
            startup={"instructions": "Review.", "user": "Report."},
            model=FunctionModel(model),
            output_type=Report,
            name="strategic_review_v1",
        )

    first = asyncio.create_task(report())
    queued = None
    try:
        await asyncio.wait_for(started.wait(), 2)
        queued = asyncio.create_task(
            runner.run_role(
                request(),
                startup={"instructions": "Review.", "user": "Role."},
                catalogue=[{"name": "finish_role", "inputSchema": {}}],
                model=FunctionModel(model),
            )
        )

        async def queued_request():
            while limiter.waiting_count != 1:
                await asyncio.sleep(0)

        await asyncio.wait_for(queued_request(), 2)
        queued.cancel()
        with pytest.raises(asyncio.CancelledError):
            await queued
        assert len(provider_calls) == 1 and limiter.waiting_count == 0
    finally:
        if queued is not None and not queued.done():
            queued.cancel()
            await asyncio.gather(queued, return_exceptions=True)
        release.set()
        await asyncio.wait_for(first, 2)
    assert (await asyncio.wait_for(report(), 2)).report.summary == "Done."
    assert len(provider_calls) == 2 and limiter.running_count == limiter.waiting_count == 0


@run_async
async def test_saved_planning_is_reinjected_after_real_context_overflow(tmp_path):
    store = InMemoryPlanStore()
    await store.set_items([PlanItem(content="PRESERVE THIS CURRENT SAVED PLAN")])
    readings, sent = [], []

    async def context(_deps, event):
        readings.append(event)

    capabilities = agent_capabilities(
        store=SqliteStepStore(database=tmp_path / "steps.sqlite3"),
        metadata={},
        settings=ModelPolicySettings(),
        cache_warnings=WarnOnCacheBusts(),
        context_window=1000000,
        on_context_usage=context,
        role_capabilities=[Planning(store=store)],
    )

    def model(messages, _info):
        sent.append(list(messages))
        if len(sent) <= 4:
            return ModelResponse(parts=[ToolCallPart("read", {}, "read" + str(len(sent)))])
        if len(sent) == 5:
            raise ModelContextWindowExceededError("synthetic", "Full")
        assert len(messages) < len(sent[-2])
        assert "PRESERVE THIS CURRENT SAVED PLAN" in str(messages)
        assert "Critical role mission" in str(messages)
        assert any(isinstance(message, ModelRequest) for message in messages)
        return ModelResponse(parts=[TextPart("Complete.")])

    agent = Agent(with_stream(model), deps_type=type(request()), capabilities=capabilities)

    @agent.tool_plain
    async def read():
        return "x" * 16000

    result = await agent.run("Current required choice", deps=request(), instructions="Critical role mission")
    assert result.output == "Complete." and len(sent) == 6 and result.usage.requests == 5
    assert len(readings) == 6 and readings[-1].used_tokens > 0 and readings[-1].resolved
    assert len(await store.get_items()) == 1


@run_async
async def test_native_coordinator_freezes_guides_as_instruction_blocks_before_private_user_facts(tmp_path, monkeypatch):
    async with production_stores(tmp_path) as fixture:
        configured = session("p0")
        configured = configured.model_copy(
            update={
                "settings": configured.settings.model_copy(update={"strategy_guide": True, "civilization_guide": True})
            }
        )
        fixture.root.game_state["managedSession"] = configured.model_dump(mode="json")
        monkeypatch.setattr(
            "civ_agent.orchestration.coordinator.player_strategy_reference", lambda *_: "PUBLIC STRATEGY GUIDE"
        )
        monkeypatch.setattr(
            "civ_agent.orchestration.coordinator.civilization_reference", lambda *_: "PUBLIC OWN CIV GUIDE"
        )
        prepared = await fixture.root.prepare_role(
            PrepareRole(ticket=fixture.ticket, purpose="gameplay", pass_number=0)
        )
        assert prepared is not None
        startup = fixture.store.get(prepared.scope, prepared.startup)
        assert startup["instructions"].startswith(COMMON_INSTRUCTIONS)
        assert startup["instructions"].count("PUBLIC STRATEGY GUIDE") == 1
        assert startup["instructions"].count("PUBLIC OWN CIV GUIDE") == 1
        assert startup["instructions"].index("PUBLIC STRATEGY GUIDE") < startup["instructions"].index(
            "PUBLIC OWN CIV GUIDE"
        )
        assert "PUBLIC" not in startup["user"]
        assert "optionalReferences" not in json.loads(startup["user"])
        assert json.loads(startup["user"])["reads"]["player_turn"]
        assert startup["preload"]["preload_version"] == "1"


def test_cache_warning_logs_safe_fields_for_every_occurrence(caplog):
    caplog.set_level(logging.WARNING, logger="py.warnings")
    try:
        with warnings.catch_warnings():
            configure_cache_warning_logging()
            for _ in range(2):
                warnings.warn(
                    CacheBustWarning(
                        "PRIVATE PROMPT OR MODEL ERROR",
                        reason="unknown",
                        established_tokens=12000,
                        cache_read_tokens=2000,
                        missed_tokens=10000,
                    ),
                    stacklevel=2,
                )
        records = [record for record in caplog.records if record.name == "py.warnings"]
        assert len(records) == 2
        assert all(record.levelno == logging.WARNING for record in records)
        assert all("12000" in record.message and "2000" in record.message for record in records)
        assert "PRIVATE" not in " ".join(record.message for record in records)
    finally:
        logging.captureWarnings(False)
