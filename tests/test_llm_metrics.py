"""Runner timing stays content-free, durable and independent of billed usage."""

import asyncio
import copy
import json
import sqlite3
import tomllib
from unittest.mock import AsyncMock

import aiohttp
import pytest
from google.protobuf.json_format import MessageToDict
from opentelemetry.proto.collector.metrics.v1.metrics_service_pb2 import ExportMetricsServiceRequest
from opentelemetry.proto.metrics.v1.metrics_pb2 import (
    AGGREGATION_TEMPORALITY_CUMULATIVE,
    AGGREGATION_TEMPORALITY_DELTA,
)

from civ_agent.llm_metrics import LlmMetricsCollector, timing_series
from civ_agent.runtime_profile import histogram_summary, profile
from civ_agent.telemetry import JobAccounting


def metric_message(*, count=2, total=300, end=3_000_000_000, temporality=2):
    message = ExportMetricsServiceRequest()
    resource = message.resource_metrics.add()
    resource.resource.attributes.add(key="PRIVATE_RESOURCE").value.string_value = "PRIVATE_PROMPT"
    metric = resource.scope_metrics.add().metrics.add(name="codex.responses_api_inference_time.duration_ms")
    metric.histogram.aggregation_temporality = (
        AGGREGATION_TEMPORALITY_DELTA if temporality == 1 else AGGREGATION_TEMPORALITY_CUMULATIVE
    )
    point = metric.histogram.data_points.add(
        count=count, sum=total, start_time_unix_nano=1_000_000_000, time_unix_nano=end
    )
    point.explicit_bounds.extend([100, 500])
    point.bucket_counts.extend([1, count - 1, 0])
    point.attributes.add(key="success").value.bool_value = True
    point.attributes.add(key="error").value.string_value = "PRIVATE_TOKEN"
    return message


def test_only_selected_numeric_timing_and_safe_status_attributes_survive():
    message = metric_message()
    ignored = message.resource_metrics[0].scope_metrics[0].metrics.add(name="codex.user_prompt")
    ignored.histogram.CopyFrom(message.resource_metrics[0].scope_metrics[0].metrics[0].histogram)
    rows = timing_series(message, "process-one")
    assert len(rows) == 1
    assert rows[0]["sumMs"] == 300 and rows[0]["count"] == 2
    assert rows[0]["attributes"] == {"success": True}
    assert "PRIVATE" not in json.dumps(rows)
    changed = metric_message()
    changed.resource_metrics[0].scope_metrics[0].metrics[0].histogram.data_points[0].attributes.add(
        key="tool"
    ).value.string_value = "PRIVATE_TOOL"
    assert timing_series(changed, "process-one")[0]["id"] != rows[0]["id"]
    assert histogram_summary(rows) == {
        "count": 2,
        "sumMs": 300,
        "meanMs": 150,
        "p50UpperBoundMs": 100,
        "p95UpperBoundMs": 500,
    }


@pytest.mark.parametrize("problem", ["nan", "buckets", "bounds", "flags", "missing_sum"])
def test_malformed_or_unrecorded_histograms_are_not_reported_as_timings(problem):
    message = metric_message()
    point = message.resource_metrics[0].scope_metrics[0].metrics[0].histogram.data_points[0]
    if problem == "nan":
        point.sum = float("nan")
    elif problem == "buckets":
        point.bucket_counts[0] = 100
    elif problem == "bounds":
        point.explicit_bounds[0] = 1000
    elif problem == "flags":
        point.flags = 1
    else:
        point.ClearField("sum")
    assert timing_series(message, "process-one") == []


def test_exports_are_idempotent_monotonic_and_survive_service_restart(tmp_path):
    store = JobAccounting(tmp_path)
    context = {"purpose": "economy", "model": "test-model", "provider": "openai"}
    first = timing_series(metric_message(), "one")
    updated = timing_series(metric_message(count=3, total=600, end=4_000_000_000), "one")
    assert first[0]["id"] == updated[0]["id"]
    for rows in (first, updated, first, updated):
        store.record_llm_metrics("job", context, rows)
    restarted = JobAccounting(tmp_path)
    with sqlite3.connect(restarted.path) as db:
        rows = [json.loads(row[0]) for row in db.execute("SELECT payload FROM llm_metrics")]
    assert len(rows) == 1 and rows[0]["count"] == 3 and rows[0]["sumMs"] == 600
    assert rows[0]["purpose"] == "economy"
    assert restarted.pending() == []  # Profiling never creates native billing reports.
    delta1 = timing_series(metric_message(temporality=1), "one")
    delta2 = timing_series(metric_message(temporality=1, end=4_000_000_000), "one")
    assert delta1[0]["id"] != delta2[0]["id"]


def test_local_collector_accepts_standard_otlp_json_and_binary_only():
    async def run():
        notification = AsyncMock()
        collector = LlmMetricsCollector(notification)
        await collector.start()
        assert collector.endpoint is not None and collector.endpoint.startswith("http://127.0.0.1:")
        configured = tomllib.loads(collector.arguments()[1])["otel"]["metrics_exporter"]
        assert configured["otlp-http"]["endpoint"] == collector.endpoint
        try:
            message = metric_message()
            async with aiohttp.ClientSession() as client:
                async with client.post(collector.endpoint, json=MessageToDict(message)) as response:
                    assert response.status == 200
                async with client.post(
                    collector.endpoint,
                    data=message.SerializeToString(),
                    headers={"Content-Type": "application/x-protobuf"},
                ) as response:
                    assert response.status == 200
                async with client.post(collector.endpoint, data="PRIVATE_PROMPT") as response:
                    assert response.status == 415
                async with client.post(collector.endpoint, json={"resourceMetrics": "invalid"}) as response:
                    assert response.status == 400
            assert notification.await_count == 2
            for call in notification.await_args_list:
                assert call.args[0] == "service/llmMetrics"
                assert "PRIVATE" not in json.dumps(call.args[1])
        finally:
            await collector.close()

    asyncio.run(run())


def test_profile_reports_missing_llm_timing_and_excludes_idle_waits(tmp_path):
    root = tmp_path / "game"
    service = root / "agent-services/Rome"
    service.mkdir(parents=True)
    (root / "container.json").write_text(json.dumps({"launchStartedAtMs": 2000}))
    (root / "container-status.json").write_text(json.dumps({"status": "running", "updated_at": 2}))
    store = JobAccounting(service / "telemetry")
    events = [
        {"time": "1970-01-01T00:00:03+00:00", "event": "host_call_completed", "elapsedMs": 30},
        {
            "time": "1970-01-01T00:00:03+00:00",
            "event": "host_call_completed",
            "communicationOp": "read",
            "elapsedMs": 25000,
        },
        {"time": "1970-01-01T00:00:01+00:00", "event": "host_call_completed", "elapsedMs": 500},
    ]
    log = service / "diagnostics.jsonl"
    log.write_text("\n".join(json.dumps(event) for event in events) + "\n{unfinished")
    before = store.path.read_bytes()
    result = profile([root, root], since_restart=True)
    assert result["hostRoundTrips"]["sumMs"] == 30
    assert len(result["windows"]) == 1
    assert not result["llm"]["available"] and result["llm"]["metrics"] == {}
    assert store.path.read_bytes() == before
    samples = timing_series(metric_message(), "one")
    # This cumulative series began before the selected window and cannot be sliced.
    store.record_llm_metrics("job", {"model": "test-model", "purpose": "review"}, samples)
    assert not profile([root], since_restart=True)["llm"]["available"]
    current = copy.deepcopy(samples)
    current[0].update(id="new-process", startNs=2_000_000_000)
    store.record_llm_metrics("job", {"model": "test-model", "purpose": "review"}, current)
    report = profile([root], since_restart=True)
    metric = report["llm"]["metrics"]["codex.responses_api_inference_time.duration_ms"]
    assert metric["count"] == 2 and metric["sumMs"] == 300
    assert report["llm"]["byModelAndPurpose"][0]["purpose"] == "review"
