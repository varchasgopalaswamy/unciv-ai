import copy
import json
from pathlib import Path

from civ_agent.advisory_delivery import delivery_basis, finish_delivery
from civ_agent.advisory_replay import load_cases
from civ_agent.decisions_policy import DecisionsSettings
from civ_agent.decisions_tasks import parse_answers, request_body, request_metadata
from civ_agent.experiment_status import classifier_status
from civ_agent.telemetry import JobAccounting, classifier_metadata


def result():
    case = next(
        case
        for case in load_cases(Path(__file__).parent / "fixtures/decisions-advisory-replay.json")
        if case["id"] == "mixed-independent-tasks"
    )
    body = request_body(case["evidence"], DecisionsSettings(decisions_enabled=True))
    answers = [
        {"name": name, "type": "choice", "choice": labels[0], "confidence": 0.99}
        for name, labels in case["labels"].items()
    ]
    for answer in answers:
        if answer["name"] == "retrieval_include:0":
            answer.update(
                confidence=0.2,
                probabilities=[{"value": "include", "probability": 0.7}, {"value": "omit", "probability": 0.3}],
            )
        elif answer["name"] == "retrieval_priority:0":
            answer.clear()
            answer.update(name="retrieval_priority:0", type="refusal", explanation="SECRET PROVIDER PROSE")
    return {
        **request_metadata(body, case["evidence"]),
        **parse_answers(body, {"answers": answers}, 0.90),
        "usage": {"input_tokens": 100},
        "replayEvidence": {"text": "SECRET SOURCE TEXT"},
        "providerBody": "SECRET PROVIDER BODY",
    }


def test_advisory_metadata_retains_status_and_raw_binary_confidence_without_private_text():
    original = result()
    original["rawAnswers"][0]["text"] = "SECRET MODEL TEXT"
    original["questionResults"][0]["evidence"] = "SECRET SOURCE COPY"
    report = classifier_metadata("advisory_batch", original)
    assert "SECRET" not in json.dumps(report)
    assert "replayEvidence" not in report and "providerBody" not in report
    assert report["inputHash"] == original["inputHash"] and report["promptHash"] == original["promptHash"]
    assert report["byTask"]["retrieval_include"] == {
        "questions": 1,
        "statuses": {"low_confidence": 1},
        "unreported": 0,
        "rawChoices": {"include": 1},
        "rawConfidence": {"known": 1, "median": 0.2, "min": 0.2, "max": 0.2},
    }
    assert report["byTask"]["retrieval_priority"]["statuses"] == {"refusal": 1}
    raw = next(answer for answer in report["rawAnswers"] if answer["name"] == "retrieval_include:0")
    assert raw["probabilities"] == [{"value": "include", "probability": 0.7}, {"value": "omit", "probability": 0.3}]
    assert len(report["answers"]) == 7
    assert {"city_focus_change:0", "city_construction_change:0"} <= {answer["name"] for answer in report["answers"]}


def test_advisory_statuses_survive_actual_accounting_round_trip(tmp_path):
    accounting = JobAccounting(tmp_path)
    try:
        accounting.classifier("advisory_batch", result())
        saved = accounting.classifier_reports()
        assert len(saved) == 1 and saved[0]["direction"] == "advisory_batch"
        assert saved[0]["fallback"] == "low_confidence"
        statuses = {row["name"]: row["status"] for row in saved[0]["questionResults"]}
        assert statuses["retrieval_priority:0"] == "refusal"
        assert statuses["retrieval_include:0"] == "low_confidence"
        assert statuses["preservation:0"] == "accepted"
        assert "SECRET" not in json.dumps(saved)
    finally:
        accounting.engine.dispose()


def test_malformed_advisory_metadata_drops_untyped_or_arbitrary_text_fields_without_crashing():
    supplied = {
        "questionNames": ["preservation:0", "preservation:²", {"text": "SECRET"}],
        "rawAnswers": [
            {
                "name": "preservation:0",
                "type": "choice",
                "choice": "consider",
                "confidence": 0.7,
                "probabilities": [{"value": [], "probability": 1}],
            },
            {"name": "city_focus:0", "type": "choice", "choice": "SECRET FREE TEXT", "confidence": 1},
            {"name": "preservation:0", "type": "refusal", "message": "SECRET refusal"},
        ],
        "questionResults": [
            {"name": "preservation:0", "status": []},
            {"name": "preservation:0", "status": "missing", "why": "SECRET"},
        ],
    }
    report = classifier_metadata("advisory_batch", supplied)
    assert report["questionNames"] == ["preservation:0"]
    assert report["questionResults"] == [{"name": "preservation:0", "status": "missing"}]
    assert "probabilities" not in report["rawAnswers"][0]
    assert "SECRET" not in json.dumps(report)
    assert report["byTask"]["preservation"]["statuses"] == {"missing": 1}


def test_missing_diagnostics_are_unreported_not_invented_refusals():
    report = classifier_metadata(
        "advisory_batch", {"questionNames": ["early_review"], "answers": [], "fallback": "timeout"}
    )
    assert report["questionResults"] == []
    assert report["byTask"]["early_review"]["unreported"] == 1
    assert report["byTask"]["early_review"]["statuses"] == {}


def test_delivery_telemetry_records_only_typed_exposure_and_never_duplicates_cost(tmp_path):
    case = next(
        case
        for case in load_cases(Path(__file__).parent / "fixtures/decisions-advisory-replay.json")
        if case["id"] == "preserve-unresolved"
    )
    body = request_body(case["evidence"], DecisionsSettings(decisions_enabled=True))
    response = {
        **request_metadata(body, case["evidence"]),
        **parse_answers(
            body,
            {"answers": [{"name": "preservation:0", "type": "choice", "choice": "consider", "confidence": 1}]},
            0.9,
        ),
    }
    rows = finish_delivery(
        delivery_basis(case["evidence"], response),
        {"advisories": {"preservationCandidates": case["evidence"]["preservation"]}},
    )
    rows[0]["source"]["text"] = "SECRET SOURCE"
    rows[0]["cityBasis"] = {"revision": "SECRET REVISION"}
    original = {"deliveryResults": rows, "usage": {"input_tokens": 100}, "accounting": {"spentNanodollars": 1000}}
    report = classifier_metadata("advisory_delivery", original)
    assert report["usage"] is None and report["accounting"] is None
    assert report["deliveryResults"] == [
        {
            "requestId": response["requestId"],
            "name": "preservation:0",
            "choice": "consider",
            "status": "delivered",
            "exposure": "notice",
        }
    ]
    assert report["byTask"]["preservation"] == {"questions": 1, "statuses": {"delivered": 1}}
    assert "SECRET" not in json.dumps(report)
    accounting = JobAccounting(tmp_path)
    try:
        accounting.classifier("advisory_delivery", original)
        saved = accounting.classifier_reports()[0]
        assert saved["usage"] is None and saved["deliveryResults"] == report["deliveryResults"]
    finally:
        accounting.engine.dispose()


def reconciliation_observations(*, initial_usage=True):
    receipt = result()
    receipt.update(usage={"input_tokens": 100}, latencyMs=17, brokerReconciliation=True)
    initial = copy.deepcopy(receipt)
    initial.pop("brokerReconciliation")
    initial.update(
        fallback="malformed_broker_output" if initial_usage else "broker_unavailable", answers=[], rawAnswers=[]
    )
    if not initial_usage:
        initial.pop("usage")
        initial.pop("latencyMs")
    return initial, receipt


def test_receipt_diagnostics_keep_original_provider_usage_and_typed_reconciliation_marker():
    _, receipt = reconciliation_observations()
    metadata = classifier_metadata("advisory_batch", receipt)
    assert metadata["brokerReconciliation"] is True
    assert metadata["usage"] == {"input_tokens": 100} and metadata["latencyMs"] == 17
    assert metadata["rawAnswers"] and metadata["questionResults"]
    receipt["brokerReconciliation"] = "PRIVATE INVALID FLAG"
    assert "brokerReconciliation" not in classifier_metadata("advisory_batch", receipt)


def test_same_millisecond_reconciliation_is_one_request_with_one_provider_usage_and_latency(tmp_path, monkeypatch):
    initial, receipt = reconciliation_observations()
    accounting = JobAccounting(tmp_path)
    try:
        monkeypatch.setattr("civ_agent.telemetry.time.time", lambda: 1)
        accounting.classifier("advisory_batch", initial)
        accounting.classifier("advisory_batch", receipt)
        unrelated = {**receipt, "requestId": "b" * 64, "usage": {"input_tokens": 300}, "latencyMs": 31}
        unrelated.pop("brokerReconciliation")
        monkeypatch.setattr("civ_agent.telemetry.time.time", lambda: 0.5)
        accounting.classifier("advisory_batch", unrelated)
        saved = accounting.classifier_reports()
        assert len(saved) == 3 and saved[1]["brokerReconciliation"] is True
        assert saved[0]["usage"] == saved[1]["usage"] == {"input_tokens": 100}
        status = classifier_status(accounting.path)
        assert status["calls"] == 2 and status["usage"] == {"input_tokens": 400}
        assert status["averageLatencyMs"] == 24
        assert status["latest"]["fallback"] == receipt["fallback"] == "low_confidence"
        assert status["latest"]["rawAnswers"] == saved[1]["rawAnswers"]
        observations = status["advisoryObservations"]
        assert observations["events"] == 3 and observations["requests"] == 2 and observations["reconciliations"] == 1
        assert observations["fallbacks"]["malformed_broker_output"] == 1
        assert observations["latestReconciliation"]["questionResults"] == saved[1]["questionResults"]
    finally:
        accounting.engine.dispose()


def test_usage_known_only_after_receipt_is_counted_once_and_later_empty_lookup_cannot_erase_it(tmp_path, monkeypatch):
    initial, receipt = reconciliation_observations(initial_usage=False)
    accounting = JobAccounting(tmp_path)
    try:
        monkeypatch.setattr("civ_agent.telemetry.time.time", lambda: 1)
        accounting.classifier("advisory_batch", initial)
        accounting.classifier("advisory_batch", receipt)
        empty = {**receipt, "usage": {}, "latencyMs": None, "fallback": "completion_unknown"}
        accounting.classifier("advisory_batch", empty)
        status = classifier_status(accounting.path)
        assert status["calls"] == 1 and status["usage"] == {"input_tokens": 100}
        assert status["averageLatencyMs"] == 17 and status["latest"]["fallback"] == "low_confidence"
        assert status["advisoryObservations"]["events"] == 3
        assert status["advisoryObservations"]["reconciliations"] == 2
    finally:
        accounting.engine.dispose()


def test_missing_request_ids_and_routing_calls_remain_independent_observations(tmp_path):
    accounting = JobAccounting(tmp_path)
    try:
        for identifier in (None, "INVALID", None):
            accounting.classifier("advisory_batch", {"requestId": identifier, "usage": {"input_tokens": 10}})
        for _ in range(2):
            accounting.classifier("incoming", {"requestId": "a" * 64, "choice": "batch", "usage": {"input_tokens": 20}})
        status = classifier_status(accounting.path)
        assert status["calls"] == 5 and status["usage"] == {"input_tokens": 70}
        assert status["routing"]["calls"] == 2 and status["advisoryObservations"]["requests"] == 3
    finally:
        accounting.engine.dispose()


def test_conflicting_known_request_identity_is_exposed_without_splicing_its_usage(tmp_path):
    initial, receipt = reconciliation_observations()
    initial.update(promptHash="c" * 64, usage={"input_tokens": 900})
    receipt.update(usage={}, latencyMs=None)
    accounting = JobAccounting(tmp_path)
    try:
        accounting.classifier("advisory_batch", initial)
        accounting.classifier("advisory_batch", receipt)
        status = classifier_status(accounting.path)
        assert status["calls"] == 1 and status["usage"] == {} and status["averageLatencyMs"] is None
        assert status["advisoryObservations"]["identityConflicts"] == 1
        assert status["latest"]["observationIdentityConflict"] is True
    finally:
        accounting.engine.dispose()
