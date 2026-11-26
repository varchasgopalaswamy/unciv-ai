"""The same canonical tool data survives every supported native provider path."""

import asyncio
import json

import httpx2
import pytest
from pydantic_ai.messages import (
    ModelMessage,
    ModelMessagesTypeAdapter,
    ModelRequest,
    ModelResponse,
    ToolCallPart,
    ToolReturnPart,
    UserPromptPart,
)
from pydantic_ai.models import ModelRequestParameters

from civ_agent.orchestration.contracts import ModelSelection
from civ_agent.orchestration.providers import ProviderClients, model_settings
from civ_agent.tls import write_private
from tests.test_google_cloud_provider import cloud_clients
from tests.test_google_cloud_provider import response as google_response
from tests.test_google_cloud_provider import selection as google_selection
from tests.test_provider_capabilities import catalog


class _Subscription:
    async def fetch(self, **_):
        return {"accessToken": "synthetic-access-token", "chatgptAccountId": "synthetic-account"}


def _response(body):
    response = {
        "id": "resp-synthetic",
        "object": "response",
        "created_at": 0,
        "status": "completed",
        "model": body["model"],
        "output": [
            {
                "id": "msg-synthetic",
                "type": "message",
                "role": "assistant",
                "status": "completed",
                "content": [{"type": "output_text", "text": "Ready", "annotations": [], "logprobs": []}],
            }
        ],
        "usage": {"input_tokens": 10, "output_tokens": 1, "total_tokens": 11},
    }
    if not body.get("stream"):
        return httpx2.Response(200, json=response)
    events = [
        {
            "type": "response.created",
            "sequence_number": 0,
            "response": {**response, "status": "in_progress", "output": []},
        },
        {
            "type": "response.output_text.delta",
            "sequence_number": 1,
            "item_id": "msg-synthetic",
            "output_index": 0,
            "content_index": 0,
            "delta": "Ready",
            "logprobs": [],
        },
        {"type": "response.completed", "sequence_number": 2, "response": response},
    ]
    return httpx2.Response(
        200,
        content="".join("data: " + json.dumps(event) + "\n\n" for event in events),
        headers={"content-type": "text/event-stream"},
    )


@pytest.mark.parametrize("provider", ["google-cloud", "openai-api", "openai-subscription", "openrouter"])
@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize(
    "payload",
    [
        {"$ref": "#/$defs/MoveCommand", "$defs": {"MoveCommand": {"type": "object"}}},
        {"nested": [{"schema": {"$ref": "#/$defs/MoveCommand"}}], "unicode": "Déplacer"},
        [{"$ref": "ordinary-data-reference"}, "tail"],
        {"output": {"error": "ordinary data"}, "turn": 1},
        'Literal text: {"$ref": "not a JSON object"}',
        None,
    ],
)
def test_provider_transport_preserves_tool_data_and_canonical_history(tmp_path, monkeypatch, provider, stream, payload):
    async def run():
        returned = ToolReturnPart("lookup", payload, "call-lookup", metadata={"receipt": "retained"})
        messages: list[ModelMessage] = [
            ModelRequest(parts=[UserPromptPart("Read tool result")]),
            ModelResponse(parts=[ToolCallPart("lookup", {}, "call-lookup")]),
            ModelRequest(parts=[returned]),
        ]
        original = ModelMessagesTypeAdapter.dump_json(messages)
        expected_text = returned.model_response_str(wrap_if_error=False)
        calls = []

        def respond(request):
            body = json.loads(request.content)
            calls.append(body)
            if provider == "google-cloud":
                wire_result = next(
                    part["functionResponse"]["response"]
                    for content in body["contents"]
                    for part in content["parts"]
                    if "functionResponse" in part
                )
                # References must be text; objects with no reference stay structured.
                if '"$ref"' in json.dumps(returned.model_response_object(wrap_if_error=False)) and not isinstance(
                    payload, str
                ):
                    assert wire_result == {"return_value": expected_text}
                else:
                    assert wire_result == returned.model_response_object(wrap_if_error=False)
                answer = google_response("gemini-3.8-flash", {"text": "Ready"})
                if stream:
                    return httpx2.Response(
                        200, content=b"data: " + answer.content + b"\n\n", headers={"content-type": "text/event-stream"}
                    )
                return answer
            outputs = [part["output"] for part in body["input"] if part.get("type") == "function_call_output"]
            assert outputs == [expected_text]
            assert "service_tier" not in body
            assert body["reasoning"]["effort"] == "medium"
            return _response(body)

        if provider == "google-cloud":
            clients, _ = cloud_clients(tmp_path, monkeypatch, respond)
            chosen = google_selection()
        else:
            path = tmp_path / "provider.key"
            write_private(path, b"synthetic-test-key")
            clients = ProviderClients(
                broker=_Subscription(),
                openai_key_file=path,
                openrouter_key_file=path,
                transport=httpx2.MockTransport(respond),
            )
            chosen = ModelSelection(
                provider="openrouter" if provider == "openrouter" else "openai",
                authentication="subscription" if provider == "openai-subscription" else "api_key",
                model="openai/gpt-6-luna" if provider == "openrouter" else "gpt-6-luna",
            )
        try:
            contract = await clients.prepare(
                chosen, catalog=catalog(chosen.model) if provider == "openrouter" else None
            )
            assert not calls
            model = await clients.model(chosen, contract=contract)
            if stream:
                async with model.request_stream(messages, model_settings(chosen), ModelRequestParameters()) as streamed:
                    async for _ in streamed:
                        pass
                    assert streamed.get().text == "Ready"
            else:
                assert (await model.request(messages, model_settings(chosen), ModelRequestParameters())).text == "Ready"
            assert len(calls) == 1
            assert ModelMessagesTypeAdapter.dump_json(messages) == original
        finally:
            await clients.close()

    asyncio.run(run())
