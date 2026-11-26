import json
from pathlib import Path

import pytest
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import ReadableSpan
from opentelemetry.sdk.trace.export import SpanExporter, SpanExportResult
from opentelemetry.trace import SpanContext, Status, StatusCode, TraceState

from civ_agent.orchestration.tracing import SafeExporter, configure_tracing
from civ_agent.orchestration.worker_configuration import WorkerTracingConfig


class Capture(SpanExporter):
    def __init__(self, *, fail=False):
        self.spans = []
        self.fail = fail

    def export(self, spans):
        if self.fail:
            raise ConnectionError("private server address")
        self.spans.extend(spans)
        return SpanExportResult.SUCCESS


def test_export_boundary_excludes_payloads_paths_names_errors_and_resources():
    delegate = Capture()
    raw = ReadableSpan(
        name="private game name and chat message",
        context=SpanContext(trace_id=1, span_id=2, is_remote=False, trace_state=TraceState([("private", "secret")])),
        resource=Resource({"process.command_line": "--token=SECRET", "host.name": "private-host"}),
        attributes={
            "gen_ai.usage.input_tokens": 12,
            "gen_ai.input.messages": "private chat",
            "gen_ai.usage.output_tokens": "private string",
            "code.filepath": "/private/path",
        },
        status=Status(StatusCode.ERROR, "SECRET exception message"),
    )
    assert SafeExporter(delegate).export([raw]) == SpanExportResult.SUCCESS
    safe = delegate.spans[0]
    assert safe.name == "civ.workflow.step"
    assert dict(safe.attributes) == {"gen_ai.usage.input_tokens": 12}
    assert dict(safe.resource.attributes) == {"service.name": "civ-workflow"}
    assert safe.status.status_code == StatusCode.ERROR and safe.status.description is None
    assert safe.context is not None and not safe.context.trace_state
    assert not safe.events and not safe.links


def test_exporter_failure_does_not_escape_to_model_or_gameplay():
    assert SafeExporter(Capture(fail=True)).export([ReadableSpan(name="test")]) == SpanExportResult.FAILURE
    assert configure_tracing(None, None) is None


def test_context_and_concurrency_spans_export_only_numeric_evidence():
    delegate = Capture()
    raw = ReadableSpan(
        name="waiting for PRIVATE PROVIDER concurrency",
        attributes={
            "civ.context.used_tokens": 12000,
            "civ.context.window_tokens": 65536,
            "civ.context.fraction": 12000 / 65536,
            "civ.context.resolved": True,
            "waiting_count": 2,
            "max_running": 4,
            "max_queued": "PRIVATE INPUT",
            "source": "PRIVATE MODEL OR PLAYER",
            "limiter_name": "PRIVATE GAME",
        },
    )
    assert SafeExporter(delegate).export([raw]) == SpanExportResult.SUCCESS
    attributes = dict(delegate.spans[0].attributes)
    assert attributes["civ.context.resolved"] is True
    assert attributes["civ.context.used_tokens"] == 12000
    assert attributes["waiting_count"] == 2 and attributes["max_running"] == 4
    assert "max_queued" not in attributes and "source" not in attributes and "limiter_name" not in attributes
    assert "PRIVATE" not in str(delegate.spans[0].name) + str(attributes)


@pytest.mark.parametrize(
    "changed",
    [
        {"token_file": "/private/secret.token"},
        {"endpoint": "http://remote.invalid:6006/v1/traces"},
        {"endpoint": "https://phoenix:6006/v1/traces"},
        {"endpoint": "http://phoenix:6006/v1/traces?token=secret"},
        {"network": "arbitrary-game-network"},
    ],
)
def test_local_config_only_admits_fixed_collector_without_credentials(changed):
    with pytest.raises(ValueError):
        WorkerTracingConfig.model_validate({"local": True, "endpoint": "http://phoenix:6006/v1/traces", **changed})


def test_local_config_needs_no_token_reference_and_preserves_existing_remote_serialization():
    local = {"local": True, "endpoint": "http://phoenix:6006/v1/traces"}
    assert WorkerTracingConfig.model_validate(local).model_dump(mode="json") == local
    remote = {"token_file": "/private/trace.token", "endpoint": "https://collector.invalid/v1/traces"}
    assert WorkerTracingConfig.model_validate(remote).model_dump(mode="json") == remote
    with pytest.raises(ValueError):
        WorkerTracingConfig.model_validate({"endpoint": "https://collector.invalid/v1/traces"})


@pytest.mark.parametrize(
    "variable",
    [
        "OTEL_EXPORTER_OTLP_HEADERS",
        "OTEL_EXPORTER_OTLP_TRACES_HEADERS",
        "OTEL_EXPORTER_OTLP_CLIENT_KEY",
        "OTEL_EXPORTER_OTLP_TRACES_CLIENT_KEY",
        "OTEL_EXPORTER_OTLP_CLIENT_CERTIFICATE",
        "OTEL_EXPORTER_OTLP_TRACES_CLIENT_CERTIFICATE",
    ],
)
@pytest.mark.parametrize("local", [False, True], ids=["https-token", "local-no-token"])
def test_explicit_export_never_uses_ambient_extra_otlp_credentials(monkeypatch, variable, local):
    secret = "SYNTHETIC_AMBIENT_TRACE_SECRET"
    monkeypatch.setenv(variable, "x-api-key=" + secret)
    with pytest.raises(ValueError, match="ambient OTLP credentials") as raised:
        configure_tracing(
            None if local else Path("/must-not-read-private-token"),
            "http://127.0.0.1:6006/v1/traces" if local else "https://collector.invalid/v1/traces",
            local=local,
        )
    assert secret not in str(raised.value)
    assert configure_tracing(None, None) is None


def test_phoenix_classification_is_a_closed_enum_and_usage_stays_numeric():
    delegate = Capture()
    SafeExporter(delegate).export(
        [
            ReadableSpan(
                name="private model",
                attributes={
                    "openinference.span.kind": "LLM",
                    "llm.token_count.prompt": 12,
                    "llm.token_count.completion": 7,
                    "llm.token_count.total": 19,
                    "llm.input_messages.0.message.content": "PRIVATE_PROMPT",
                    "llm.token_count.prompt_details": "PRIVATE_DETAILS",
                },
            ),
            ReadableSpan(
                name="private tool",
                attributes={"openinference.span.kind": "PRIVATE_TOOL_NAME", "llm.token_count.prompt": True},
            ),
        ]
    )
    assert dict(delegate.spans[0].attributes) == {
        "openinference.span.kind": "LLM",
        "llm.token_count.prompt": 12,
        "llm.token_count.completion": 7,
        "llm.token_count.total": 19,
    }
    assert not delegate.spans[1].attributes
    assert "PRIVATE_" not in json.dumps([span.to_json() for span in delegate.spans])
