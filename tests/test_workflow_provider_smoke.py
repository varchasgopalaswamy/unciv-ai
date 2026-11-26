"""Synthetic Responses wire proves only the new native smoke protocol and limits."""

import asyncio
import json
import sys
from types import SimpleNamespace
from typing import Literal
from unittest.mock import AsyncMock, Mock

import httpx2
import pytest

from civ_agent.orchestration.contracts import ModelSelection
from civ_agent.orchestration.provider_contracts import ModelCapabilities, ReasoningCapabilities
from civ_agent.orchestration.providers import ProviderClients
from civ_agent.tls import write_private
from tests import workflow_provider_smoke as smoke


def selection(provider: Literal["openai", "openrouter"] = "openai"):
    return ModelSelection(
        provider=provider,
        model="openai/gpt-6-luna" if provider == "openrouter" else "gpt-6-luna",
        authentication="api_key",
        reasoning_effort="low",
    )


def catalog(selected):
    return ModelCapabilities(
        id=selected.model,
        context_length=200000,
        supported_parameters=["tools", "structured_outputs", "reasoning", "max_tokens"],
        reasoning=ReasoningCapabilities(supported_efforts=["none", "low", "medium"]),
        endpoints=["/api/v1/responses"],
    )


def reply(body, number, *, repeat_tool=False, wrong=False):
    first = number == 1 or repeat_tool
    arguments = (
        {"query": {"city": "harbor-city"}}
        if first
        else {"city": "harbor-city", "population": 999 if wrong else 3, "food_per_turn": 4}
    )
    return httpx2.Response(
        200,
        json={
            "id": "resp-" + str(number),
            "object": "response",
            "created_at": 0,
            "status": "completed",
            "model": body["model"],
            "output": [
                {
                    "id": "call-" + str(number),
                    "type": "function_call",
                    "call_id": "call-" + str(number),
                    "name": "inspect_fixture" if first else "submit_fixture",
                    "arguments": json.dumps(arguments),
                    "status": "completed",
                }
            ],
            "usage": {
                "input_tokens": 10,
                "output_tokens": 7,
                "total_tokens": 17,
                "input_tokens_details": {"cached_tokens": 3},
                "output_tokens_details": {"reasoning_tokens": 2},
            },
        },
    )


@pytest.mark.parametrize("provider", ["openai", "openrouter"])
def test_smoke_runs_actual_native_adapter_tool_structured_output_and_all_usage_counters(tmp_path, provider):
    async def run():
        selected = selection(provider)
        requests = []

        def respond(request):
            assert request.headers["authorization"] == "Bearer synthetic-smoke-key"
            body = json.loads(request.content)
            requests.append(body)
            assert body["max_output_tokens"] == smoke.OUTPUT_PER_REQUEST
            assert body["reasoning"]["effort"] == "low"
            assert {entry["name"] for entry in body["tools"]} == {"inspect_fixture", "submit_fixture"}
            if provider == "openrouter":
                assert body["provider"] == {"require_parameters": True}
            if len(requests) == 2:
                results = [item for item in body["input"] if item.get("type") == "function_call_output"]
                assert json.loads(results[-1]["output"]) == {"city": "harbor-city", "population": 3, "food_per_turn": 4}
            return reply(body, len(requests))

        key = tmp_path / "key"
        write_private(key, b"synthetic-smoke-key")
        clients = ProviderClients(
            openai_key_file=key if provider == "openai" else None,
            openrouter_key_file=key if provider == "openrouter" else None,
            transport=httpx2.MockTransport(respond),
        )
        try:
            report = await smoke.run_smoke(
                clients, selected, catalog=catalog(selected) if provider == "openrouter" else None
            )
        finally:
            await clients.close()
        assert report.outcome == "passed" and report.grounded and report.tool_calls == 1
        assert report.usage_complete and report.usage is not None and len(requests) == 2
        assert report.usage.model_dump() == {
            "requests": 2,
            "input_tokens": 20,
            "cached_input_tokens": 6,
            "output_tokens": 14,
            "reasoning_output_tokens": 4,
        }
        assert report.provider_output_cap_enforced and report.provider_max_output_tokens == 256
        assert report.contract_sha256 and report.sdk_inference_retries == 0
        assert report.definite_401_renewal == "none"
        raw = report.model_dump_json()
        assert "synthetic-smoke-key" not in raw and "Inspect harbor-city" not in raw
        assert "instructions" not in raw and "messages" not in raw and "query" not in raw

    asyncio.run(run())


@pytest.mark.parametrize("failure", ["quota", "extra-tool", "ungrounded"])
def test_smoke_has_no_inference_retry_fallback_or_third_model_request(tmp_path, failure):
    async def run():
        requests = []

        def respond(request):
            body = json.loads(request.content)
            requests.append(body)
            if failure == "quota":
                return httpx2.Response(
                    429,
                    json={
                        "error": {
                            "code": "insufficient_quota",
                            "type": "insufficient_quota",
                            "message": "PRIVATE_PROVIDER_ERROR synthetic-secret",
                        }
                    },
                    headers={"x-private": "synthetic-secret"},
                )
            return reply(body, len(requests), repeat_tool=failure == "extra-tool", wrong=failure == "ungrounded")

        key = tmp_path / "key"
        write_private(key, b"synthetic-secret")
        clients = ProviderClients(openai_key_file=key, transport=httpx2.MockTransport(respond))
        try:
            report = await smoke.run_smoke(clients, selection())
        finally:
            await clients.close()
        expected = {"quota": "quota", "extra-tool": "usage_limit", "ungrounded": "grounding_failed"}[failure]
        assert report.outcome == expected
        assert len(requests) == (1 if failure == "quota" else 2)
        assert report.tool_calls == (0 if failure == "quota" else 1)
        assert (
            "synthetic-secret" not in report.model_dump_json()
            and "PRIVATE_PROVIDER_ERROR" not in report.model_dump_json()
        )
        if failure == "quota":
            assert report.http_status == 429 and not report.usage_complete and report.usage is None

    asyncio.run(run())


def test_subscription_dry_run_uses_public_profile_without_reading_or_initializing_auth(tmp_path, monkeypatch, capsys):
    forbidden = Mock(side_effect=AssertionError("Dry run must not initialize authentication"))
    monkeypatch.setitem(sys.modules, "civ_agent.subscription_auth", SimpleNamespace(NativeAccount=forbidden))
    monkeypatch.setattr(
        smoke, "fetch_openrouter_catalog", AsyncMock(side_effect=AssertionError("Dry run cannot use the network"))
    )
    directory = tmp_path / "dedicated-but-not-initialized"
    assert (
        smoke.main(
            [
                "--provider",
                "openai",
                "--model",
                "gpt-6-luna",
                "--auth-dir",
                str(directory),
                "--output-dir",
                str(tmp_path / "reports"),
                "--dry-run",
            ]
        )
        == 0
    )
    forbidden.assert_not_called()
    assert not directory.exists()
    report = next((tmp_path / "reports").glob("provider-smoke-*/report.json"))
    saved = smoke.SmokeReport.model_validate_json(report.read_bytes())
    assert saved.outcome == "dry_run" and saved.usage is None
    assert not saved.provider_output_cap_enforced and saved.provider_max_output_tokens is None
    assert saved.logical_request_limit == 2 and saved.sdk_postresponse_output_limit == 512
    assert saved.definite_401_renewal == "production_bounded_once"
    assert report.stat().st_mode & 0o777 == 0o600
    assert "dedicated-but-not-initialized" not in report.read_text()
    assert "providerOutputCapEnforced" in capsys.readouterr().out
