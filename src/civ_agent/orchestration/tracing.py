"""Allowlist telemetry at the export boundary, including names, resources and errors."""

import os
from collections.abc import Sequence
from pathlib import Path
from urllib.parse import urlsplit

import requests
from openinference.instrumentation.pydantic_ai import OpenInferenceSpanProcessor
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.metrics import NoOpMeterProvider
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import ReadableSpan, TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, SpanExporter, SpanExportResult
from opentelemetry.trace import SpanContext, Status
from pydantic_ai import Agent
from pydantic_ai.models.instrumented import InstrumentationSettings

from civ_agent.tls import regular_file

COUNTERS = frozenset(
    {
        "gen_ai.usage.input_tokens",
        "gen_ai.usage.output_tokens",
        "gen_ai.usage.cache_read.input_tokens",
        "gen_ai.aggregated_usage.input_tokens",
        "gen_ai.aggregated_usage.output_tokens",
        "gen_ai.aggregated_usage.requests",
        "gen_ai.aggregated_usage.tool_calls",
        "llm.token_count.prompt",
        "llm.token_count.completion",
        "llm.token_count.total",
        "civ.context.used_tokens",
        "civ.context.window_tokens",
        "civ.context.fraction",
        "waiting_count",
        "max_running",
        "max_queued",
    }
)
SPAN_KINDS = frozenset({"AGENT", "CHAIN", "LLM", "TOOL", "UNKNOWN"})
KIND_ATTRIBUTE = "openinference.span.kind"


def private_context(context: SpanContext | None) -> SpanContext | None:
    if context is None:
        return None
    # Vendor trace-state values can contain arbitrary upstream text.
    return SpanContext(
        trace_id=context.trace_id, span_id=context.span_id, is_remote=context.is_remote, trace_flags=context.trace_flags
    )


class SafeExporter(SpanExporter):
    """Even unexpected library attributes cannot export private content."""

    def __init__(self, delegate: SpanExporter):
        self.delegate = delegate

    def export(self, spans: Sequence[ReadableSpan]) -> SpanExportResult:
        safe = [
            ReadableSpan(
                name="civ.workflow.step",
                context=private_context(span.context),
                parent=private_context(span.parent),
                resource=Resource({"service.name": "civ-workflow"}),
                attributes={
                    key: value
                    for key, value in (span.attributes or {}).items()
                    if (key in COUNTERS and type(value) in {int, float})
                    or (key == KIND_ATTRIBUTE and type(value) is str and value in SPAN_KINDS)
                    or (key == "civ.context.resolved" and type(value) is bool)
                },
                status=Status(span.status.status_code),
                kind=span.kind,
                start_time=span.start_time,
                end_time=span.end_time,
                # Events, links, status descriptions, scope metadata and span names
                # can all carry tool input, source paths or arbitrary authored text.
            )
            for span in spans
        ]
        try:
            return self.delegate.export(safe)
        except Exception:
            return SpanExportResult.FAILURE

    def shutdown(self):
        try:
            self.delegate.shutdown()
        except Exception:
            pass

    def force_flush(self, timeout_millis: int = 30000) -> bool:
        try:
            return self.delegate.force_flush(timeout_millis)
        except Exception:
            return False


def configure_tracing(token_file: Path | None, endpoint: str | None, *, local: bool = False) -> TracerProvider | None:
    """Export sanitized spans to an explicit local collector or authenticated HTTPS endpoint."""
    if token_file is None and not local:
        return None
    if endpoint is None:
        raise ValueError("Specify the OTLP traces endpoint")
    url = urlsplit(endpoint)
    if not url.hostname or url.username or url.password or url.query or url.fragment:
        raise ValueError("Invalid OTLP endpoint")
    # The public OTLP exporter merges headers and resolves mTLS identity from env
    # even with an explicit session. Never read/log those implicit credentials.
    if any(
        name in os.environ
        for name in (
            "OTEL_EXPORTER_OTLP_HEADERS",
            "OTEL_EXPORTER_OTLP_TRACES_HEADERS",
            "OTEL_EXPORTER_OTLP_CLIENT_KEY",
            "OTEL_EXPORTER_OTLP_TRACES_CLIENT_KEY",
            "OTEL_EXPORTER_OTLP_CLIENT_CERTIFICATE",
            "OTEL_EXPORTER_OTLP_TRACES_CLIENT_CERTIFICATE",
        )
    ):
        raise ValueError("Tracing does not accept ambient OTLP credentials")
    headers = {}
    if local:
        if (
            token_file is not None
            or url.scheme != "http"
            or url.hostname not in {"phoenix", "localhost", "127.0.0.1", "::1"}
        ):
            raise ValueError("Invalid local OTLP endpoint")
    else:
        if url.scheme != "https" or token_file is None:
            raise ValueError("Invalid authenticated OTLP endpoint")
        regular_file(token_file, private=True)
        token = token_file.read_text().strip()
        if not token or any(ord(character) < 33 or ord(character) > 126 for character in token):
            raise ValueError("Invalid Logfire token")
        headers = {"Authorization": token}
    session = requests.Session()
    session.trust_env = False
    provider = TracerProvider(resource=Resource({"service.name": "civ-workflow"}))
    provider.add_span_processor(OpenInferenceSpanProcessor())
    provider.add_span_processor(
        BatchSpanProcessor(
            SafeExporter(
                OTLPSpanExporter(
                    endpoint=endpoint,
                    headers=headers,
                    timeout=5,
                    session=session,
                )
            )
        )
    )
    Agent.instrument_all(
        InstrumentationSettings(
            tracer_provider=provider,
            meter_provider=NoOpMeterProvider(),
            include_content=False,
            include_binary_content=False,
            include_model_request_parameters=False,
        )
    )
    return provider
