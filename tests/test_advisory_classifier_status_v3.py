"""Exposure events do not become paid classifier calls or change classifier health."""

import json

from civ_agent.experiment_status import classifier_status
from civ_agent.telemetry import JobAccounting


def delivery():
    return {
        "requestId": "a" * 64,
        "policyVersion": "player-advisories-v3",
        "usage": {"input_tokens": 9000},
        "latencyMs": 9000,
        "deliveryResults": [
            {
                "requestId": "a" * 64,
                "name": "preservation:0",
                "choice": "consider",
                "status": "delivered",
                "exposure": "notice",
                "source": {"text": "PRIVATE SOURCE MUST STAY OUT OF STATUS"},
            }
        ],
    }


def test_delivery_after_uncertain_batch_preserves_call_count_failure_usage_and_latency(tmp_path, monkeypatch):
    accounting = JobAccounting(tmp_path)
    try:
        monkeypatch.setattr("civ_agent.telemetry.time.time", lambda: 1)
        accounting.classifier(
            "advisory_batch",
            {
                "requestId": "a" * 64,
                "policyVersion": "player-advisories-v3",
                "model": "gpt-6-luna",
                "fallback": "low_confidence",
                "usage": {"input_tokens": 100},
                "latencyMs": 17,
            },
        )
        monkeypatch.setattr("civ_agent.telemetry.time.time", lambda: 2)
        accounting.classifier("advisory_delivery", delivery())
        report = classifier_status(accounting.path)
        assert report["status"] == "degraded" and report["calls"] == 1
        assert report["fallbacks"] == {"low_confidence": 1}
        assert report["latest"]["direction"] == "advisory_batch" and report["latest"]["atMs"] == 1000
        assert report["usage"] == {"input_tokens": 100} and report["averageLatencyMs"] == 17
        assert report["routing"]["calls"] == 0
        assert report["routing"]["excluded"] == {
            "advisoryBatch": 1,
            "protectedOrDeadlineBypass": 0,
            "otherDirections": 0,
        }
        exposed = report["advisoryDelivery"]
        assert exposed["events"] == exposed["questionObservations"] == 1
        assert exposed["byTask"] == {"preservation": {"questionObservations": 1, "statuses": {"delivered": 1}}}
        assert exposed["latest"]["atMs"] == 2000
        assert exposed["latest"]["usage"] is None and exposed["latest"]["accounting"] is None
        assert "PRIVATE SOURCE" not in json.dumps(report)
    finally:
        accounting.engine.dispose()


def test_delivery_without_classification_does_not_claim_activity_or_paid_usage(tmp_path):
    accounting = JobAccounting(tmp_path)
    try:
        accounting.classifier("advisory_delivery", delivery())
        report = classifier_status(accounting.path)
        assert report["status"] == "waiting_for_first_classification"
        assert report["calls"] == 0 and report["latest"] is None
        assert report["fallbacks"] == {} and report["usage"] == {} and report["averageLatencyMs"] is None
        assert report["routing"]["calls"] == 0
        assert report["routing"]["excluded"]["otherDirections"] == 0
        assert report["advisoryDelivery"]["events"] == 1
    finally:
        accounting.engine.dispose()
