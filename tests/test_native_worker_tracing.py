"""Optional worker-only telemetry exports metadata through the actual SDK path."""

import hashlib

import pytest
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from pydantic import ValidationError

from civ_agent.game_config import GameConfig, Runtime
from civ_agent.orchestration.worker_configuration import WorkerProviderConfig, WorkerTracingConfig


def test_absent_tracing_preserves_exact_default_serialization_and_does_not_resolve_credentials():
    for value, expected in [
        (Runtime(), "6e0b7c7ce4417f4feb22699829880a6516d7885cd1c4faf2bec82ee656b906b9"),
        (GameConfig(), "0a4aa73bd92a9eb73011a3adcd53b5fb4139fda9ca5ea825eda7e581483c9249"),
        (
            WorkerProviderConfig(scripted=True),
            "98447fd6788be133762d7e9552bdd170fdca19e06a46796003a861a917371aea",
        ),
    ]:
        assert hashlib.sha256(value.model_dump_json().encode()).hexdigest() == expected


@pytest.mark.parametrize(
    "changed",
    [
        {"token_file": "relative.token"},
        {"endpoint": "http://collector.invalid/v1/traces"},
        {"endpoint": "https://user:secret@collector.invalid/v1/traces"},
        {"endpoint": "https://collector.invalid/v1/traces?token=secret"},
        {"endpoint": "https://collector.invalid/v1/traces#secret"},
        {"token": "PRIVATE_RAW_TOKEN"},
    ],
)
def test_tracing_configuration_requires_explicit_safe_export_references(changed):
    with pytest.raises(ValidationError) as raised:
        WorkerTracingConfig.model_validate(
            {"token_file": "/not-mounted/tracing.token", "endpoint": "https://collector.invalid/v1/traces", **changed}
        )
    assert "PRIVATE_RAW_TOKEN" not in str(raised.value)


class Capture(InMemorySpanExporter):
    def __init__(self, *, fail=False):
        super().__init__()
        self.closed = False
        self.fail = fail

    def export(self, spans):
        if self.fail:
            raise ConnectionError("PRIVATE_COLLECTOR_FAILURE")
        return super().export(spans)

    def shutdown(self):
        self.closed = True
        super().shutdown()
