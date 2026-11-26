"""Use native Memory tool feedback/retry budgets for recoverable author mistakes."""

import asyncio
import json
import sqlite3

import pytest
from pydantic_ai import Agent
from pydantic_ai.exceptions import UnexpectedModelBehavior
from pydantic_ai.messages import ModelRequest, ModelResponse, TextPart, ToolCallPart, ToolReturnPart
from pydantic_ai.models.function import FunctionModel

from civ_agent.orchestration.authored_memory import authored_memory_capability, decode_document
from civ_agent.orchestration.contracts import GatewayDependencies
from civ_agent.orchestration.store import JournalConflict
from tests.test_authored_memory import operation, setup


def mutation_count(path):
    with sqlite3.connect(path) as db:
        return db.execute("SELECT count(*) FROM memory_operations").fetchone()[0]


temporal_client = None


def current_client():
    assert temporal_client is not None
    return temporal_client


def temporal_model(messages, info):
    returns = [
        part
        for message in messages
        if isinstance(message, ModelRequest)
        for part in message.parts
        if isinstance(part, ToolReturnPart)
    ]
    if not returns:
        return ModelResponse(
            parts=[ToolCallPart("write_memory", {"file": "strategy.md", "content": "invalid JSON"}, "invalid")]
        )
    if returns[-1].outcome == "retried":
        return ModelResponse(
            parts=[
                ToolCallPart(
                    "write_memory",
                    {
                        "file": "strategy.md",
                        "content": json.dumps(
                            {"type": "strategy", "assessment": "Corrected through native durable Memory"}
                        ),
                    },
                    "corrected",
                )
            ]
        )
    return ModelResponse(parts=[TextPart("Saved one corrected strategy.")])


MISSING_NOTE = "# Notes\nCreated after native missing-file feedback.\n"


def missing_read_model(messages, info):
    returns = [
        part
        for message in messages
        if isinstance(message, ModelRequest)
        for part in message.parts
        if isinstance(part, ToolReturnPart)
    ]
    if not returns:
        return ModelResponse(parts=[ToolCallPart("read_memory", {"file": "MEMORY.md"}, "missing")])
    missing = [part for part in returns if part.tool_call_id == "missing"]
    assert len(missing) == 1 and missing[0].tool_name == "read_memory" and missing[0].outcome == "retried"
    assert "There is no memory file named 'MEMORY.md'" in str(missing[0].content)
    if len(returns) == 1:
        return ModelResponse(parts=[ToolCallPart("write_memory", {"content": MISSING_NOTE}, "create")])
    created = returns[1]
    assert created.tool_call_id == "create" and created.outcome == "success"
    if len(returns) == 2:
        return ModelResponse(parts=[ToolCallPart("read_memory", {"file": "MEMORY.md"}, "read-created")])
    assert len(returns) == 3 and returns[-1].tool_call_id == "read-created"
    assert returns[-1].outcome == "success" and returns[-1].content == MISSING_NOTE
    return ModelResponse(parts=[TextPart("Created and read the missing notebook.")])


def test_native_memory_missing_read_reaches_model_then_create_and_read_succeeds(tmp_path):
    async def run():
        async with setup(tmp_path) as fixture:
            assert await fixture.proxy.read(fixture.prefix + "MEMORY.md", max_chars=8000) is None
            calls = []

            def model(messages, info):
                calls.append(messages)
                if len(calls) == 2:
                    assert mutation_count(fixture.path) == 0
                return missing_read_model(messages, info)

            agent = Agent(
                FunctionModel(model),
                deps_type=GatewayDependencies,
                retries=0,
                capabilities=[authored_memory_capability(fixture.client)],
            )
            result = await agent.run(
                "Read the notebook; create it if absent.", deps=GatewayDependencies(**fixture.identity.model_dump())
            )
            assert result.output == "Created and read the missing notebook." and len(calls) == 4
            assert mutation_count(fixture.path) == 1
            saved = await fixture.proxy.read(fixture.prefix + "MEMORY.md", max_chars=8000)
            assert saved.content == MISSING_NOTE and saved.metadata["job"] == fixture.identity.job

    asyncio.run(run())


@pytest.mark.parametrize("mistake", ["invalid_json", "invalid_schema", "append_json", "role_policy"])
def test_native_memory_corrects_author_error_once_with_agent_retries_zero(tmp_path, mistake):
    async def run():
        async with setup(tmp_path, purpose="economy" if mistake == "role_policy" else "gameplay") as fixture:
            original = json.dumps({"type": "strategy", "assessment": "Existing choice"})
            replacement = json.dumps({"type": "strategy", "assessment": "Corrected choice"})
            city = json.dumps({"type": "city_plan", "city": "city2", "reasons": {"construction": "Retain Granary"}})
            if mistake == "append_json":
                await fixture.proxy.write(
                    fixture.prefix + "strategy.md", original, expected_version=None, operation=operation("seed")
                )
            # Initialize the public native schema before measuring mutations.
            await fixture.service.store.list_paths(fixture.prefix, limit=10)
            before, calls = mutation_count(fixture.path), []

            def model(messages, info):
                calls.append(messages)
                attempt = len(calls)
                if attempt == 1:
                    content = (
                        replacement
                        if mistake == "append_json"
                        else "PRIVATE_RAW_INPUT is not JSON"
                        if mistake == "invalid_json"
                        else '{"type":"strategy","assessment":42,"PRIVATE_RAW_INPUT":"secret"}'
                        if mistake == "invalid_schema"
                        else original
                    )
                    return ModelResponse(
                        parts=[ToolCallPart("write_memory", {"file": "strategy.md", "content": content}, "bad")]
                    )
                if attempt == 2:
                    retry = [
                        part
                        for message in messages
                        if isinstance(message, ModelRequest)
                        for part in message.parts
                        if isinstance(part, ToolReturnPart) and part.outcome == "retried"
                    ]
                    assert len(retry) == 1 and retry[0].tool_name == "write_memory"
                    feedback = str(retry[0].content)
                    assert "PRIVATE_RAW_INPUT" not in feedback
                    assert "role" in feedback if mistake == "role_policy" else "old_text" in feedback
                    arguments = (
                        {"file": "city-city2.md", "content": city}
                        if mistake == "role_policy"
                        else {"file": "strategy.md", "content": replacement}
                    )
                    if mistake == "append_json":
                        arguments["old_text"] = original
                    return ModelResponse(parts=[ToolCallPart("write_memory", arguments, "corrected")])
                return ModelResponse(parts=[TextPart("Role continues after saving the corrected document.")])

            agent = Agent(
                FunctionModel(model),
                deps_type=GatewayDependencies,
                retries=0,
                capabilities=[authored_memory_capability(fixture.client)],
            )
            result = await agent.run(
                "Save the authored document.", deps=GatewayDependencies(**fixture.identity.model_dump())
            )
            assert result.output.startswith("Role continues") and len(calls) == 3
            assert mutation_count(fixture.path) == before + 1
            path = fixture.prefix + ("city-city2.md" if mistake == "role_policy" else "strategy.md")
            saved = await fixture.proxy.read(path, max_chars=8000)
            assert json.loads(saved.content) == json.loads(city if mistake == "role_policy" else replacement)
            assert saved.metadata["purpose"] == fixture.identity.purpose
            assert saved.metadata["job"] == fixture.identity.job and saved.metadata["epoch"] == "g1"
            assert decode_document(path, saved)["category"] == ("city_plan" if mistake == "role_policy" else "strategy")

    asyncio.run(run())


@pytest.mark.parametrize("missing_read", [False, True])
def test_native_memory_retry_allowance_is_one_and_does_not_raise_global_agent_budget(tmp_path, missing_read):
    async def run():
        async with setup(tmp_path) as fixture:
            calls = []

            def model(messages, info):
                calls.append(messages)
                return ModelResponse(
                    parts=[
                        ToolCallPart(
                            "read_memory" if missing_read else "write_memory",
                            {"file": "MEMORY.md"}
                            if missing_read
                            else {"file": "strategy.md", "content": "invalid JSON"},
                            "bad" + str(len(calls)),
                        )
                    ]
                )

            agent = Agent(
                FunctionModel(model),
                deps_type=GatewayDependencies,
                retries=0,
                capabilities=[authored_memory_capability(fixture.client)],
            )
            with pytest.raises(UnexpectedModelBehavior, match="retries"):
                await agent.run(
                    "Save an invalid strategy twice.", deps=GatewayDependencies(**fixture.identity.model_dump())
                )
            assert len(calls) == 2 and mutation_count(fixture.path) == 0
            assert await fixture.proxy.read(fixture.prefix + "strategy.md", max_chars=8000) is None

    asyncio.run(run())


def test_epoch_failure_stays_fatal_instead_of_becoming_a_model_correction(tmp_path):
    async def run():
        async with setup(tmp_path) as fixture:
            calls = []

            def model(messages, info):
                calls.append(messages)
                return ModelResponse(parts=[ToolCallPart("write_memory", {"content": "Ordinary notes"}, "note")])

            with fixture.domain.memory.transaction() as db:
                fixture.domain.memory._set(db, "epoch", "g2")
            agent = Agent(
                FunctionModel(model),
                deps_type=GatewayDependencies,
                retries=0,
                capabilities=[authored_memory_capability(fixture.client)],
            )
            with pytest.raises(JournalConflict, match="EPOCH"):
                await agent.run(
                    "An expired role cannot author notes.", deps=GatewayDependencies(**fixture.identity.model_dump())
                )
            assert len(calls) == 1

    asyncio.run(run())


def test_main_native_notebook_uses_plain_markdown_append_unique_edit_and_clearing(tmp_path):
    async def run():
        async with setup(tmp_path) as fixture:
            calls = [
                {"content": "# Notes\nUnverified report."},
                {"content": "A second observation remains unverified."},
                {"old_text": "Unverified report.", "content": "A player claimed this; no evidence yet."},
                {"file": "strategy.md", "content": json.dumps({"type": "strategy", "assessment": "Consider growth"})},
                {"file": "strategy.md"},
            ]
            seen = []

            def model(messages, info):
                seen.append(messages)
                index = len(seen) - 1
                if index < len(calls):
                    return ModelResponse(
                        parts=[
                            ToolCallPart(
                                "delete_memory" if index == 4 else "write_memory", calls[index], "call" + str(index)
                            )
                        ]
                    )
                return ModelResponse(parts=[TextPart("Notes updated and strategy retired.")])

            agent = Agent(
                FunctionModel(model),
                deps_type=GatewayDependencies,
                retries=0,
                capabilities=[authored_memory_capability(fixture.client)],
            )
            await agent.run(
                "Maintain the notebook with native edits.", deps=GatewayDependencies(**fixture.identity.model_dump())
            )
            saved = await fixture.proxy.read(fixture.prefix + "MEMORY.md", max_chars=8000)
            assert "A player claimed this" in saved.content and "A second observation" in saved.content
            projected = decode_document(fixture.prefix + "MEMORY.md", saved)
            assert projected["category"] == "note" and projected["body"]["text"] == saved.content
            assert await fixture.proxy.read(fixture.prefix + "strategy.md", max_chars=8000) is None
            clear = Agent(
                FunctionModel(
                    lambda messages, info: (
                        ModelResponse(
                            parts=[ToolCallPart("write_memory", {"old_text": saved.content, "content": ""}, "clear")]
                        )
                        if len(messages) == 1
                        else ModelResponse(parts=[TextPart("Cleared")])
                    )
                ),
                deps_type=GatewayDependencies,
                retries=0,
                capabilities=[authored_memory_capability(fixture.client)],
            )
            await clear.run("Clear the native notebook.", deps=GatewayDependencies(**fixture.identity.model_dump()))
            empty = await fixture.proxy.read(fixture.prefix + "MEMORY.md", max_chars=8000)
            assert empty.content == "" and decode_document(fixture.prefix + "MEMORY.md", empty)["category"] == "note"

    asyncio.run(run())
