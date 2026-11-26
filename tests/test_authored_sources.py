"""Authored annotations come from delivered native reads, never model claims."""

import asyncio
import copy
import hashlib
import json
from dataclasses import replace
from types import SimpleNamespace
from typing import Literal, cast

import pytest
from pydantic_ai import Agent, RunContext
from pydantic_ai.messages import (
    ModelRequest,
    ModelResponse,
    RetryFeedbackPart,
    RetryInfo,
    TextPart,
    ToolCallPart,
    ToolReturnPart,
    UserPromptPart,
)
from pydantic_ai.models.function import FunctionModel
from pydantic_ai_harness import Memory
from pydantic_ai_harness.memory import SqliteMemoryStore
from pydantic_ai_harness.step_persistence import ContinuableSnapshot, RunRecord, ToolEffectRecord

from civ_agent.memory_records import CityPlan, Strategy
from civ_agent.orchestration.authored_memory import AuthoredMetadata, authored_namespace, authored_prefix
from civ_agent.orchestration.authored_plan_service import AuthoredDocumentReply
from civ_agent.orchestration.authored_plan_tools import authored_plan_capability
from civ_agent.orchestration.authored_sources import authored_sources, project_authored_sources
from civ_agent.orchestration.contracts import GatewayCall, GatewayDependencies, GatewayJob, Scope, canonical
from civ_agent.orchestration.gateway_execution import operation_identity
from civ_agent.orchestration.store import JournalConflict, WorkflowStore
from civ_agent.orchestration.transcripts import PlayerPersistence, backend
from civ_agent.protocol import JsonObject


def strategy(assessment="Expand cautiously.", *, investment="Build a settler."):
    return json.dumps(
        {
            "type": "strategy",
            "assessment": assessment,
            "decisions": [
                {
                    "id": "capital",
                    "objective": "Establish a second city.",
                    "investment": investment,
                    "expectedObservations": ["A new city exists."],
                    "reviewAfterTurns": 5,
                    "reconsiderWhen": "A threat appears.",
                }
            ],
        }
    )


def command_call(**updates):
    return GatewayCall(
        call_id="command",
        model_step=2,
        decision={
            "operations": [
                {
                    "tool": "player_command",
                    "arguments": {
                        "command": {"kind": "FOUND_CITY", "target": "unit1"},
                        "intent": {"strategy": "strategy.md", "decision": "capital"},
                    },
                }
            ]
        },
    ).model_copy(update=updates)


def messages(
    content=None,
    *,
    file="strategy.md",
    outcome: Literal["success", "failed", "denied", "retried", "interrupted"] = "success",
):
    return [
        ModelRequest(parts=[UserPromptPart("Choose an action.")]),
        ModelResponse(parts=[ToolCallPart("read_memory", {"file": file}, "read")]),
        ModelRequest(parts=[ToolReturnPart("read_memory", content or strategy(), "read", outcome=outcome)]),
        ModelResponse(parts=[ToolCallPart("execute_commands", {"commands": []}, "command")]),
    ]


def acknowledged_write(version, content=None, *, file="strategy.md"):
    history = messages(content, file=file)
    history[1:1] = [
        ModelResponse(parts=[ToolCallPart("write_memory", {"file": file, "content": content or strategy()}, "write")]),
        ModelRequest(
            parts=[
                ToolReturnPart(
                    "write_memory", {"file": file, "version": version, "status": "created", "replayed": False}, "write"
                )
            ]
        ),
    ]
    return history


def typed_messages(
    version="1", *, city=False, outcome: Literal["success", "failed", "denied", "retried", "interrupted"] = "success"
):
    file = "city-city1.md" if city else "strategy.md"
    tool = "read_city_plan" if city else "read_strategy"
    content = (
        CityPlan(type="city_plan", city="city1", reasons={"construction": "Grow first."})
        if city
        else Strategy.model_validate_json(strategy())
    )
    reply = AuthoredDocumentReply(id=file, content=content, version=version, existed=True).model_dump(mode="json")
    return [
        ModelRequest(parts=[UserPromptPart("Choose an action.")]),
        ModelResponse(parts=[ToolCallPart(tool, {"city": "city1"} if city else {}, "read")]),
        ModelRequest(parts=[ToolReturnPart(tool, reply, "read", outcome=outcome)]),
        ModelResponse(parts=[ToolCallPart("execute_commands", {"commands": []}, "command")]),
    ]


@pytest.fixture
def fixture(tmp_path):
    root = WorkflowStore(tmp_path / "workflow", player="p0")
    root.bind_epoch("game", "e1")
    identity = GatewayJob(
        scope=Scope(game="game", player="p0", epoch="e1", activation="turn1"), job="main", purpose="gameplay"
    )
    result = SimpleNamespace(
        root=root,
        identity=identity,
        memory=SqliteMemoryStore(database=tmp_path / "memory.sqlite3"),
        run_id="native-run",
        call=command_call(),
    )
    yield result
    root.close()


async def persist(fixture, history=None, *, scope=None, effect=None, agent="player_role_v1"):
    steps = backend(fixture.root, create=True)
    await steps.register_run(
        RunRecord(
            run_id=fixture.run_id,
            agent_name=agent,
            metadata={"scope": canonical((scope or fixture.identity.scope).model_dump(mode="json"))},
        )
    )
    if history is not False:
        await steps.save_snapshot(
            ContinuableSnapshot(run_id=fixture.run_id, step_index=2, messages=history or messages())
        )
    record = ToolEffectRecord(
        run_id=fixture.run_id,
        tool_call_id="command",
        tool_name="execute_commands",
        status="started",
        idempotency_key=operation_identity("main", "e1", 2, "command"),
    )
    await steps.record_tool_effect(replace(record, **effect) if effect else record)
    return steps


async def write(fixture, content, *, file="strategy.md", expected_version=None, **metadata_changes):
    metadata = AuthoredMetadata(
        game="game",
        player="p0",
        epoch="e1",
        job="main",
        purpose="gameplay",
        kind="strategy" if file == "strategy.md" else "city_plan",
        createdTurn=1,
        updatedTurn=2,
        decisionBaselines=json.dumps({"capital": {"id": "trusted-baseline", "authoredTurn": 1, "position": None}}),
    ).model_copy(update=metadata_changes)
    return await fixture.memory.write_with_metadata(
        authored_prefix(fixture.identity.scope) + file,
        content,
        expected_version=expected_version,
        metadata=metadata.strings(),
    )


def test_projection_pairs_delivered_reads_and_preserves_exact_content_hash():
    raw = strategy() + "\n"
    projected = project_authored_sources(messages(raw), "command")
    assert projected == {
        "strategy.md": {
            "version": None,
            "category": "strategy",
            "body": {
                "type": "strategy",
                "assessment": "Expand cautiously.",
                "subjects": [],
                "priority": 3,
                "expiresTurn": None,
                "reasons": [],
                "uncertainties": [],
                "reconsiderWhen": [],
                "assumptions": [],
                "sources": [],
                "decisions": [
                    {
                        "id": "capital",
                        "objective": "Establish a second city.",
                        "investment": "Build a settler.",
                        "expectedObservations": ["A new city exists."],
                        "reviewAfterTurns": 5,
                        "reconsiderWhen": "A threat appears.",
                        "subjects": [],
                    }
                ],
            },
            "source": {
                "file": "strategy.md",
                "toolCallId": "read",
                "contentSha256": hashlib.sha256(raw.encode()).hexdigest(),
            },
        }
    }


def test_same_response_sibling_reads_and_claimed_user_sources_do_not_count():
    history = [
        ModelRequest(parts=[UserPromptPart(strategy())]),
        ModelResponse(parts=[TextPart("I read strategy.md: " + strategy())]),
        ModelRequest(parts=[ToolReturnPart("read_memory", strategy(), "unrequested")]),
        ModelResponse(
            parts=[
                ToolCallPart("read_memory", {"file": "strategy.md"}, "sibling"),
                ToolCallPart("execute_commands", {"commands": []}, "command"),
            ]
        ),
        ModelRequest(parts=[ToolReturnPart("read_memory", strategy(), "sibling")]),
    ]
    assert project_authored_sources(history, "command") == {}


@pytest.mark.parametrize(
    ("content", "file", "outcome"),
    [
        (strategy(), "strategy.md", "failed"),
        (strategy(), "strategy.md", "denied"),
        (strategy(), "strategy.md", "retried"),
        (strategy(), "strategy.md", "interrupted"),
        ({"content": strategy()}, "strategy.md", "success"),
        (strategy() + "\n[Truncated: exceeds max_memory_size]", "strategy.md", "success"),
        ('{"type":"strategy"}', "strategy.md", "success"),
        (strategy(), "other/strategy.md", "success"),
        (strategy(), "../strategy.md", "success"),
        (strategy(), "MEMORY.md", "success"),
        (strategy(), "contribution-strategy.md", "success"),
    ],
)
def test_failed_partial_untyped_and_foreign_reads_are_ignored(content, file, outcome):
    assert project_authored_sources(messages(content, file=file, outcome=outcome), "command") == {}


def test_retry_and_duplicate_tool_ids_are_not_successful_reads():
    history = messages()
    history[2] = ModelRequest(parts=[RetryFeedbackPart("Read failed", retry=RetryInfo("model_retry"))])
    assert project_authored_sources(history, "command") == {}
    history = messages()
    history.insert(1, ModelResponse(parts=[ToolCallPart("read_memory", {"file": "strategy.md"}, "read")]))
    assert project_authored_sources(history, "command") == {}


@pytest.mark.parametrize("file", ["strategy", " strategy.md ", " strategy "])
def test_native_filename_normalization_matches_delivered_read(file):
    assert set(project_authored_sources(messages(file=file), "command")) == {"strategy.md"}


def test_plain_read_does_not_claim_current_version_or_baseline_and_is_read_only(fixture):
    async def run():
        raw = strategy()
        steps = await persist(fixture, messages(raw))
        await write(fixture, raw)
        before = await steps.get_tool_effect(run_id=fixture.run_id, tool_call_id="command")
        source = (await authored_sources(fixture.root, fixture.identity, fixture.run_id, fixture.call, fixture.memory))[
            "strategy.md"
        ]
        assert source["version"] is None and "_decisionBaselines" not in source["body"]
        assert source["source"]["runId"] == fixture.run_id
        assert source["source"]["contentSha256"] == hashlib.sha256(raw.encode()).hexdigest()
        assert await steps.get_tool_effect(run_id=fixture.run_id, tool_call_id="command") == before
        assert await steps.list_events(run_id=fixture.run_id) == []
        with fixture.root.engine.connect() as db:
            assert db.exec_driver_sql("SELECT count(*) FROM calls").scalar_one() == 0

    asyncio.run(run())


def test_causally_prior_native_write_ack_proves_version_and_baseline(fixture):
    async def run():
        stored = await write(fixture, strategy())
        await persist(fixture, acknowledged_write(stored.version))
        source = (await authored_sources(fixture.root, fixture.identity, fixture.run_id, fixture.call, fixture.memory))[
            "strategy.md"
        ]
        assert source["version"] == stored.version
        assert source["source"]["writeToolCallId"] == "write"
        assert source["body"]["_decisionBaselines"] == {
            "capital": {"id": "trusted-baseline", "authoredTurn": 1, "position": None}
        }

    asyncio.run(run())


def test_write_and_read_siblings_do_not_prove_read_version_even_with_ack_ordered_first(fixture):
    async def run():
        stored = await write(fixture, strategy())
        history = acknowledged_write(stored.version)
        write_response, write_ack, read_response, read_result = history[1:5]
        assert isinstance(write_response, ModelResponse) and isinstance(read_response, ModelResponse)
        assert isinstance(write_ack, ModelRequest) and isinstance(read_result, ModelRequest)
        history[1:5] = [
            ModelResponse(parts=[*write_response.parts, *read_response.parts]),
            ModelRequest(parts=[*write_ack.parts, *read_result.parts]),
        ]
        await persist(fixture, history)
        source = (await authored_sources(fixture.root, fixture.identity, fixture.run_id, fixture.call, fixture.memory))[
            "strategy.md"
        ]
        assert source["version"] is None and "_decisionBaselines" not in source["body"]
        assert "writeToolCallId" not in source["source"]

    asyncio.run(run())


def test_native_version_prevents_aba_content_from_borrowing_new_baseline(fixture):
    async def run():
        original = await write(fixture, strategy())
        await persist(fixture, acknowledged_write(original.version))
        changed = await write(fixture, strategy("Different state."), expected_version=original.version)
        restored = await write(
            fixture,
            strategy(),
            expected_version=changed.version,
            decisionBaselines='{"capital":{"id":"new-baseline","authoredTurn":2,"position":null}}',
        )
        assert original.version != restored.version
        source = (await authored_sources(fixture.root, fixture.identity, fixture.run_id, fixture.call, fixture.memory))[
            "strategy.md"
        ]
        assert source["body"]["assessment"] == "Expand cautiously."
        assert source["version"] is None and "_decisionBaselines" not in source["body"]

    asyncio.run(run())


@pytest.mark.parametrize("change", ["prose", "decision", "deleted"])
def test_stale_read_preserves_delivered_document_without_claiming_current_version(fixture, change):
    async def run():
        raw = strategy()
        await persist(fixture, messages(raw))
        if change != "deleted":
            await write(
                fixture,
                strategy(
                    "Later assessment.", investment="Build a settler." if change == "prose" else "Build a granary."
                ),
            )
        source = (await authored_sources(fixture.root, fixture.identity, fixture.run_id, fixture.call, fixture.memory))[
            "strategy.md"
        ]
        assert source["version"] is None
        assert source["body"]["assessment"] == "Expand cautiously."
        assert source["body"]["decisions"][0]["investment"] == "Build a settler."
        assert "_decisionBaselines" not in source["body"]

    asyncio.run(run())


def test_prose_change_does_not_attach_an_unselected_current_baseline(fixture):
    async def run():
        await persist(fixture)
        await write(fixture, strategy("Later assessment."))
        call = command_call(decision={"operations": []})
        source = (await authored_sources(fixture.root, fixture.identity, fixture.run_id, call, fixture.memory))[
            "strategy.md"
        ]
        assert source["version"] is None and "_decisionBaselines" not in source["body"]

    asyncio.run(run())


def test_city_plan_uses_delivered_typed_content_and_exact_city_filename(fixture):
    async def run():
        content = json.dumps({"type": "city_plan", "city": "city1", "reasons": {"construction": "Grow first."}})
        stored = await write(fixture, content, file="city-city1.md")
        await persist(fixture, acknowledged_write(stored.version, content, file="city-city1.md"))
        sources = await authored_sources(fixture.root, fixture.identity, fixture.run_id, fixture.call, fixture.memory)
        assert sources["city-city1.md"]["version"] == stored.version
        assert sources["city-city1.md"]["category"] == "city_plan"
        assert sources["city-city1.md"]["body"]["city"] == "city1"
        assert "_decisionBaselines" not in sources["city-city1.md"]["body"]
        assert project_authored_sources(messages(content, file="city-city2.md"), "command") == {}

    asyncio.run(run())


@pytest.mark.parametrize("field,value", [("player", "p1"), ("epoch", "e2"), ("kind", "note")])
def test_current_file_owner_and_kind_are_integrity_boundaries(fixture, field, value):
    async def run():
        stored = await write(fixture, strategy(), **{field: value})
        await persist(fixture, acknowledged_write(stored.version))
        with pytest.raises(JournalConflict, match="METADATA_CONFLICT"):
            await authored_sources(fixture.root, fixture.identity, fixture.run_id, fixture.call, fixture.memory)

    asyncio.run(run())


def test_model_cannot_supply_trusted_baselines_and_corrupt_trusted_metadata_is_rejected(fixture):
    async def run():
        content = json.loads(strategy())
        content["_decisionBaselines"] = {"capital": {"id": "forged"}}
        assert project_authored_sources(messages(json.dumps(content)), "command") == {}
        stored = await write(
            fixture, strategy(), decisionBaselines='{"capital":{"id":"bad","authoredTurn":true,"position":null}}'
        )
        await persist(fixture, acknowledged_write(stored.version))
        with pytest.raises(JournalConflict, match="BASELINE_CONFLICT"):
            await authored_sources(fixture.root, fixture.identity, fixture.run_id, fixture.call, fixture.memory)

    asyncio.run(run())


@pytest.mark.parametrize("boundary", ["scope", "agent", "effect", "currentcall"])
def test_foreign_run_and_unbound_current_call_are_rejected_before_memory_reads(fixture, boundary):
    async def run():
        scope = fixture.identity.scope.model_copy(update={"player": "p1"}) if boundary == "scope" else None
        history = messages()
        if boundary == "currentcall":
            history[-1] = ModelResponse(parts=[ToolCallPart("other_tool", {}, "command")])
        await persist(fixture, history, scope=scope, agent="other_agent" if boundary == "agent" else "player_role_v1")
        call = command_call(model_step=3) if boundary == "effect" else fixture.call

        async def forbidden(*args, **kwargs):
            pytest.fail("Invalid native boundary must not read authored storage")

        with pytest.raises(JournalConflict):
            await authored_sources(
                fixture.root, fixture.identity, fixture.run_id, call, SimpleNamespace(read=forbidden)
            )

    asyncio.run(run())


@pytest.mark.parametrize("effect", [{"status": "failed"}, {"tool_name": "read_memory"}, {"idempotency_key": None}])
def test_current_native_effect_must_be_admitted_and_have_exact_function_identity(fixture, effect):
    async def run():
        await persist(fixture, effect=effect)
        with pytest.raises(JournalConflict, match="EFFECT_CONFLICT"):
            await authored_sources(fixture.root, fixture.identity, fixture.run_id, fixture.call, fixture.memory)

    asyncio.run(run())


def test_epoch_is_rechecked_after_native_read(fixture):
    async def run():
        stored = await write(fixture, strategy())
        await persist(fixture, acknowledged_write(stored.version))

        async def advance_epoch(*args, **kwargs):
            current = await fixture.memory.read(*args, **kwargs)
            fixture.root.bind_epoch("game", "e2", previous="e1")
            return current

        with pytest.raises(JournalConflict, match="EPOCH_CONFLICT"):
            await authored_sources(
                fixture.root, fixture.identity, fixture.run_id, fixture.call, SimpleNamespace(read=advance_epoch)
            )

    asyncio.run(run())


def test_unavailable_file_is_nonblocking_and_missing_snapshot_has_no_source(fixture):
    async def run():
        await persist(fixture, acknowledged_write("unavailable-version"))

        async def unavailable(*args, **kwargs):
            raise TimeoutError("temporary store outage")

        source = await authored_sources(
            fixture.root, fixture.identity, fixture.run_id, fixture.call, SimpleNamespace(read=unavailable)
        )
        assert source["strategy.md"]["version"] is None
        assert "_decisionBaselines" not in source["strategy.md"]["body"]
        fixture.run_id = "no-snapshot"
        await persist(fixture, False)
        assert (
            await authored_sources(fixture.root, fixture.identity, fixture.run_id, fixture.call, fixture.memory) == {}
        )

    asyncio.run(run())


@pytest.mark.parametrize("sibling", [False, True])
@pytest.mark.parametrize("prior_write", [False, True])
def test_real_native_memory_and_step_persistence_expose_current_tool_frontier(fixture, sibling, prior_write):
    async def run():
        raw = strategy()
        if not prior_write:
            await write(fixture, raw)
        steps = backend(fixture.root, create=True)
        captures, requests = [], []

        def respond(history, info):
            requests.append(copy.deepcopy(history))
            if prior_write and len(requests) == 1:
                return ModelResponse(
                    parts=[ToolCallPart("write_memory", {"file": "strategy.md", "content": raw}, "write")]
                )
            if len(requests) == (2 if prior_write else 1):
                parts = [ToolCallPart("read_memory", {"file": "strategy.md"}, "read")]
                if sibling:
                    parts.append(ToolCallPart("execute_commands", {}, "command"))
                return ModelResponse(parts=parts)
            if len(requests) == (3 if prior_write else 2) and not sibling:
                return ModelResponse(parts=[ToolCallPart("execute_commands", {}, "command")])
            return ModelResponse(parts=[TextPart("done")])

        agent = Agent(
            FunctionModel(respond),
            name="player_role_v1",
            deps_type=GatewayDependencies,
            capabilities=[
                Memory(
                    store=fixture.memory,
                    agent_name="notes",
                    namespace=authored_namespace(fixture.identity.scope),
                    inject_memory=False,
                    write_metadata=AuthoredMetadata(
                        game="game",
                        player="p0",
                        epoch="e1",
                        kind="strategy",
                        job="main",
                        purpose="gameplay",
                        decisionBaselines='{"capital":{"id":"actual-ack-baseline","authoredTurn":1,"position":null}}',
                    ).strings(),
                ),
                PlayerPersistence(store=steps, capture_frontier=True),
            ],
        )

        @agent.tool(sequential=True)
        async def execute_commands(ctx: RunContext[GatewayDependencies]) -> str:
            assert ctx.run_id is not None
            captures.append(
                await authored_sources(
                    fixture.root,
                    fixture.identity,
                    ctx.run_id,
                    command_call(call_id=ctx.tool_call_id, model_step=ctx.deps.model_step_offset + ctx.run_step),
                    fixture.memory,
                )
            )
            return "Action accepted."

        result = await agent.run(
            "Read and choose.",
            deps=GatewayDependencies(**fixture.identity.model_dump(), model_step_offset=7),
            run_id=fixture.run_id,
        )
        assert result.output == "done"
        if sibling:
            assert captures == [{}]
        else:
            source = captures[0]["strategy.md"]
            assert source["source"]["toolCallId"] == "read"
            current = await fixture.memory.read(authored_prefix(fixture.identity.scope) + "strategy.md", max_chars=8000)
            assert source["version"] == (current.version if prior_write else None)
            assert ("_decisionBaselines" in source["body"]) == prior_write
        effect = await steps.get_tool_effect(run_id=fixture.run_id, tool_call_id="command")
        assert effect is not None and effect.status == "completed" and effect.idempotency_key

    asyncio.run(run())


@pytest.mark.parametrize("city", [False, True])
def test_typed_read_proves_observed_id_content_and_version_without_model_bookkeeping(fixture, city):
    async def run():
        file = "city-city1.md" if city else "strategy.md"
        history = typed_messages(city=city)
        part = history[2].parts[0]
        assert isinstance(part, ToolReturnPart)
        assert isinstance(part.content, dict)
        reply = cast(JsonObject, part.content)
        raw = json.dumps(reply["content"], indent=2)  # Whitespace is not part of the typed wire contract.
        stored = await write(fixture, raw, file=file)
        assert reply["version"] == stored.version
        await persist(fixture, history)
        source = (await authored_sources(fixture.root, fixture.identity, fixture.run_id, fixture.call, fixture.memory))[
            file
        ]
        assert source["version"] == stored.version
        assert source["source"]["readVersion"] == stored.version
        assert source["source"]["toolCallId"] == "read"
        assert source["body"] | {} == reply["content"] | (
            {"_decisionBaselines": {"capital": {"id": "trusted-baseline", "authoredTurn": 1, "position": None}}}
            if not city
            else {}
        )
        assert "writeToolCallId" not in source["source"]

    asyncio.run(run())


@pytest.mark.parametrize(
    "failure", ["failed", "denied", "retried", "interrupted", "wrong-id", "wrong-kind", "no-version", "duplicate"]
)
def test_typed_reads_do_not_credit_failed_or_mismatched_proof(failure):
    history = typed_messages(
        outcome=failure if failure in {"failed", "denied", "retried", "interrupted"} else "success"
    )
    part = history[2].parts[0]
    assert isinstance(part, ToolReturnPart)
    assert isinstance(part.content, dict)
    reply = cast(JsonObject, part.content)
    if failure == "wrong-id":
        reply["id"] = "city-city1.md"
    elif failure == "wrong-kind":
        reply["content"] = {"type": "city_plan", "city": "city1", "reasons": {"construction": "Other document"}}
    elif failure == "no-version":
        reply["version"] = None
    elif failure == "duplicate":
        history.insert(1, copy.deepcopy(history[1]))
    assert project_authored_sources(history, "command") == {}


def test_typed_read_sibling_and_save_ack_without_read_grant_no_source():
    history = typed_messages()
    read_call, read_reply = history[1:3]
    command = history[3]
    assert isinstance(read_call, ModelResponse) and isinstance(command, ModelResponse)
    history[1:] = [ModelResponse(parts=[*read_call.parts, *command.parts]), read_reply]
    assert project_authored_sources(history, "command") == {}
    history = typed_messages()
    history[1] = ModelResponse(parts=[ToolCallPart("save_strategy", {"strategy": json.loads(strategy())}, "save")])
    history[2] = ModelRequest(
        parts=[ToolReturnPart("save_strategy", {"id": "strategy.md", "version": "1", "applied": True}, "save")]
    )
    assert project_authored_sources(history, "command") == {}


def test_stale_typed_read_retains_observed_version_without_borrowing_current_baselines(fixture):
    async def run():
        first = await write(fixture, strategy())
        await persist(fixture, typed_messages(first.version))
        await write(
            fixture,
            strategy(),
            expected_version=first.version,
            decisionBaselines='{"capital":{"id":"new-baseline","authoredTurn":2,"position":null}}',
        )
        source = (await authored_sources(fixture.root, fixture.identity, fixture.run_id, fixture.call, fixture.memory))[
            "strategy.md"
        ]
        assert source["version"] is None and source["source"]["readVersion"] == first.version
        assert "_decisionBaselines" not in source["body"]

    asyncio.run(run())


def test_real_typed_read_and_native_action_frontier_supply_cited_strategy(tmp_path):
    from tests.test_authored_memory import operation, setup

    async def run():
        async with setup(tmp_path / "gateway") as fixture:
            workflow = WorkflowStore(tmp_path / "workflow", player="p0")
            workflow.bind_epoch("game", "g1")
            try:
                await fixture.proxy.write(
                    fixture.prefix + "strategy.md", strategy(), expected_version=None, operation=operation("seed")
                )
                steps = backend(workflow, create=True)
                calls, captured = [], []

                def respond(history, info):
                    calls.append(history)
                    if len(calls) == 1:
                        return ModelResponse(parts=[ToolCallPart("read_strategy", {}, "read")])
                    if len(calls) == 2:
                        return ModelResponse(parts=[ToolCallPart("execute_commands", {}, "command")])
                    return ModelResponse(parts=[TextPart("Action accepted.")])

                agent = Agent(
                    FunctionModel(respond),
                    name="player_role_v1",
                    deps_type=GatewayDependencies,
                    capabilities=[
                        authored_plan_capability(fixture.client, enabled=lambda ctx: True),
                        PlayerPersistence(store=steps, capture_frontier=True),
                    ],
                )

                @agent.tool(sequential=True)
                async def execute_commands(ctx: RunContext[GatewayDependencies]) -> str:
                    assert ctx.run_id is not None
                    captured.append(
                        await authored_sources(
                            workflow,
                            fixture.identity,
                            ctx.run_id,
                            command_call(call_id=ctx.tool_call_id, model_step=ctx.run_step),
                            fixture.service.store,
                        )
                    )
                    return "Action accepted."

                result = await agent.run(
                    "Read intent and choose.",
                    deps=GatewayDependencies(**fixture.identity.model_dump()),
                    run_id="typed-native-source-run",
                )
                assert result.output == "Action accepted."
                source = captured[0]["strategy.md"]
                assert source["version"] == "1" and source["source"]["toolName"] == "read_strategy"
                assert source["body"]["_decisionBaselines"]["capital"]["id"]
            finally:
                workflow.close()

    asyncio.run(run())
