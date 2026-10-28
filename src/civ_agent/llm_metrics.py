"""Local, content-free collection of the model runner's OTLP timing histograms."""

import hashlib
import json
import math
import secrets
import uuid
from collections.abc import Awaitable, Callable

from aiohttp import web
from google.protobuf.json_format import ParseDict, ParseError
from google.protobuf.message import DecodeError
from opentelemetry.proto.collector.metrics.v1.metrics_service_pb2 import ExportMetricsServiceRequest

from civ_agent.protocol import JsonObject

# Keep transport, provider inference and whole-turn durations separate. In
# particular, an HTTP request duration does not include its entire SSE stream.
METRICS = frozenset(
    {
        "codex.api_request.duration_ms",
        "codex.websocket.request.duration_ms",
        "codex.responses_api_overhead.duration_ms",
        "codex.responses_api_inference_time.duration_ms",
        "codex.responses_api_engine_iapi_ttft.duration_ms",
        "codex.responses_api_engine_service_ttft.duration_ms",
        "codex.turn.e2e_duration_ms",
        "codex.turn.ttft.duration_ms",
        "codex.turn.ttfm.duration_ms",
        "codex.tool.call.duration_ms",
        "codex.mcp.call.duration_ms",
    }
)


def timing_series(message: ExportMetricsServiceRequest, instance: str) -> list[JsonObject]:
    """Discard resource metadata, arbitrary attributes, logs and all content."""
    result = []
    for resource in message.resource_metrics:
        for scope in resource.scope_metrics:
            for metric in scope.metrics:
                if metric.name not in METRICS or metric.WhichOneof("data") != "histogram":
                    continue
                histogram = metric.histogram
                if histogram.aggregation_temporality not in (1, 2):
                    continue
                for point in histogram.data_points:
                    if point.flags & 1 or not point.count or not point.HasField("sum"):
                        continue
                    bounds, buckets = list(point.explicit_bounds), list(point.bucket_counts)
                    if (
                        not math.isfinite(point.sum)
                        or point.sum < 0
                        or len(bounds) > 128
                        or any(not math.isfinite(value) or value < 0 for value in bounds)
                        or any(a >= b for a, b in zip(bounds, bounds[1:], strict=False))
                        or buckets
                        and (len(buckets) != len(bounds) + 1 or sum(buckets) != point.count)
                        or bounds
                        and not buckets
                        or not point.time_unix_nano
                        or point.start_time_unix_nano > point.time_unix_nano
                    ):
                        continue
                    attributes = {}
                    for attribute in point.attributes:
                        key, value = attribute.key, attribute.value
                        kind = value.WhichOneof("value")
                        if key == "success" and kind == "bool_value":
                            attributes[key] = value.bool_value
                        elif key == "status" and kind == "int_value" and 100 <= value.int_value <= 599:
                            attributes[key] = value.int_value
                        elif key == "status" and kind == "string_value" and value.string_value.isdecimal():
                            status = int(value.string_value[:4])
                            if 100 <= status <= 599:
                                attributes[key] = status
                    # Distinct tool/model/status series must not collapse when
                    # their arbitrary labels are discarded from stored data.
                    labels = hashlib.sha256(
                        resource.resource.SerializeToString(deterministic=True)
                        + scope.scope.SerializeToString(deterministic=True)
                        + b"".join(sorted(value.SerializeToString(deterministic=True) for value in point.attributes))
                    ).hexdigest()
                    identity = [instance, metric.name, labels, point.start_time_unix_nano]
                    if histogram.aggregation_temporality == 1:
                        identity.append(point.time_unix_nano)
                    sample: JsonObject = {
                        "id": hashlib.sha256(json.dumps(identity).encode()).hexdigest(),
                        "metric": metric.name,
                        "temporality": "delta" if histogram.aggregation_temporality == 1 else "cumulative",
                        "startNs": point.start_time_unix_nano,
                        "endNs": point.time_unix_nano,
                        "count": point.count,
                        "sumMs": point.sum,
                        "boundsMs": bounds,
                        "buckets": buckets,
                        "attributes": attributes,
                    }
                    for field in ("min", "max"):
                        value = getattr(point, field)
                        if point.HasField(field) and math.isfinite(value) and value >= 0:
                            sample[field + "Ms"] = value
                    result.append(sample)
    return result


class LlmMetricsCollector:
    """One loopback collector per model process; no remote telemetry exporter."""

    def __init__(self, notification: Callable[[str, JsonObject], Awaitable[None]]):
        self.notification = notification
        self.instance = uuid.uuid4().hex
        self.route = "/" + secrets.token_urlsafe(24) + "/v1/metrics"
        self.runner: web.AppRunner | None = None
        self.endpoint: str | None = None

    async def start(self) -> None:
        app = web.Application(client_max_size=2 * 1024 * 1024)
        app.router.add_post(self.route, self.receive)
        self.runner = web.AppRunner(app, access_log=None)
        await self.runner.setup()
        try:
            await web.TCPSite(self.runner, "127.0.0.1", 0).start()
            address = self.runner.addresses[0]
            assert isinstance(address, tuple)
            self.endpoint = f"http://127.0.0.1:{address[1]}{self.route}"
        except BaseException:
            await self.close()
            raise

    def arguments(self) -> list[str]:
        assert self.endpoint is not None
        exporter = "{otlp-http={endpoint=" + json.dumps(self.endpoint) + ',protocol="json"}}'
        return ["-c", "otel.metrics_exporter=" + exporter]

    async def receive(self, request: web.Request) -> web.Response:
        message = ExportMetricsServiceRequest()
        try:
            if request.content_type == "application/json":
                ParseDict(await request.json(), message)
            elif request.content_type == "application/x-protobuf":
                message.ParseFromString(await request.read())
            else:
                raise web.HTTPUnsupportedMediaType()
        except (ValueError, TypeError, ParseError, DecodeError):
            raise web.HTTPBadRequest() from None
        series = timing_series(message, self.instance)
        if series:
            # Independent of the socket notification queue: the runner can flush
            # final metrics after its app-server socket has already closed.
            await self.notification("service/llmMetrics", {"series": series})
        return web.json_response({})

    async def close(self) -> None:
        if self.runner is not None:
            await self.runner.cleanup()
            self.runner = None


async def record_llm_metrics(service, record, params) -> None:
    settings = record.get("effectiveSettings", record.get("settings", {}))
    metadata = {
        "purpose": record.get("purpose", "gameplay"),
        **{key: settings.get(key) for key in ("provider", "model", "reasoningEffort")},
    }
    await service.io.call(service.accounting.record_llm_metrics, record["activationId"], metadata, params["series"])
