"""Native tool schema normalization also applies when deriving delivered evidence."""

import asyncio

import pytest
from pydantic import ValidationError
from pydantic_ai import Agent
from pydantic_ai.messages import ModelRequest, ModelResponse, TextPart, ToolCallPart, ToolReturnPart
from pydantic_ai.models.function import FunctionModel

from civ_agent.orchestration.contracts import ReviewRequest
from civ_agent.orchestration.review import delivered_reads, read_tool
from tests.test_local_review_services import review_setup as review_setup


@pytest.mark.parametrize(
    "arguments,expected",
    [
        (None, {}),
        ({}, {}),
        ({"names": ["Writing"]}, {"names": ["Writing"]}),
        ({"arguments": {}}, {}),
        ({"arguments": {"names": ["Writing"]}}, {"names": ["Writing"]}),
    ],
)
def test_actual_sdk_read_and_evidence_use_identical_flat_wrapped_and_default_arguments(
    review_setup, arguments, expected
):
    worker, request = review_setup
    calls = []

    def respond(messages, info):
        calls.append(messages)
        return ModelResponse(
            parts=[ToolCallPart("research_details", arguments, "read1")]
            if len(calls) == 1
            else [TextPart("Observed frozen research.")]
        )

    async def run():
        agent = Agent(
            FunctionModel(respond),
            deps_type=ReviewRequest,
            tools=[read_tool("research_details", worker=worker)],
            retries=0,
        )
        result = await agent.run("Inspect frozen research.", deps=request)
        assert len(calls) == 2
        assert delivered_reads(result.all_messages()) == [{"name": "research_details", "arguments": expected}]
        returned = [
            part for message in result.all_messages() for part in message.parts if isinstance(part, ToolReturnPart)
        ]
        assert returned[0].outcome == "success"
        assert isinstance(returned[0].content, dict) and "source" in returned[0].content

    asyncio.run(run())


def test_actual_live_player_details_shape_has_no_arguments_envelope():
    call = ModelResponse(parts=[ToolCallPart("player_details", {"section": "economy"}, "read1")])
    returned = ModelRequest(parts=[ToolReturnPart("player_details", {"source": "s1", "data": {}}, "read1")])
    assert delivered_reads([call, returned]) == [{"name": "player_details", "arguments": {"section": "economy"}}]


@pytest.mark.parametrize("outcome", ["failed", "denied", "interrupted", "retried"])
def test_failed_native_return_cannot_grant_evidence_even_with_source_shaped_content(outcome):
    call = ModelResponse(parts=[ToolCallPart("research_details", {}, "read1")])
    returned = ModelRequest(
        parts=[ToolReturnPart("research_details", {"source": "s1", "data": {}}, "read1", outcome=outcome)]
    )
    assert delivered_reads([call, returned]) == []


def test_return_must_match_actual_read_name_and_call_identity():
    call = ModelResponse(parts=[ToolCallPart("research_details", {}, "read1")])
    wrong_name = ModelRequest(parts=[ToolReturnPart("player_turn", {"source": "s1", "data": {}}, "read1")])
    wrong_id = ModelRequest(parts=[ToolReturnPart("research_details", {"source": "s1", "data": {}}, "read2")])
    assert delivered_reads([call, wrong_name]) == delivered_reads([call, wrong_id]) == []


def test_success_claim_does_not_invent_missing_required_typed_arguments():
    call = ModelResponse(parts=[ToolCallPart("player_details", {}, "read1")])
    returned = ModelRequest(parts=[ToolReturnPart("player_details", {"source": "s1", "data": {}}, "read1")])
    with pytest.raises(ValidationError):
        delivered_reads([call, returned])
