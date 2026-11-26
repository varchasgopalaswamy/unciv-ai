"""Classifier reporting derives scoped receipts without copying accounting state."""

import copy
import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from sqlalchemy import func, select

from civ_agent.civilization_state import CivilizationState
from civ_agent.orchestration.contracts import Reference, Scope, digest
from civ_agent.orchestration.routing_contracts import RoutingDirection
from civ_agent.orchestration.routing_models import RoutingLink, RoutingResult, RoutingRun
from civ_agent.orchestration.routing_reports import routing_reports
from civ_agent.orchestration.store import JournalConflict, WorkflowStore, artifacts, calls, generation_attempts, outbox
from civ_agent.telemetry import classifier_metadata
from tests.test_routing_telemetry import answer


@pytest.fixture
def runtime(tmp_path):
    memory = CivilizationState(tmp_path / "domain", "Rome")
    memory.bind("e1")
    store = WorkflowStore(tmp_path / "workflow", player="p0")
    store.bind_epoch("game", "e1")

    def owner(scope):
        if (scope.game, scope.player) != ("game", "p0"):
            raise JournalConflict("COORDINATOR_OWNER_CONFLICT")

    root = SimpleNamespace(memory=memory, store=store, check_owner=Mock(side_effect=owner))
    scope = Scope(game="game", player="p0", epoch="e1", activation="communication")
    try:
        yield root, scope
    finally:
        memory.engine.dispose()
        store.close()


def request(scope, *, direction: RoutingDirection = "incoming", request_id=None):
    return RoutingRun(
        scope=scope,
        submission=Reference(sha256=digest("submission")),
        policy=Reference(sha256=digest("policy")),
        request_id=request_id or digest([scope.model_dump(), direction]),
        operation_id="routing-operation",
        direction=direction,
        timeout_seconds=5,
    )


def record_link(root, run):
    link = RoutingLink(scope=run.scope, id=run.scope.identity("workflow", "routing"), kind="routing", request=run)
    with root.memory.transaction() as db:
        root.memory._set(db, "nativeRoutingLink:" + link.id, link.model_dump(mode="json"))
    return link


def finish(root, run, outcome, *, recorded_scope=None, recorded_request_id=None):
    outcome = {"requestId": run.request_id, "direction": run.direction, **outcome}
    result = RoutingResult(
        scope=recorded_scope or run.scope,
        request_id=recorded_request_id or run.request_id,
        outcome=root.store.put(run.scope, outcome),
    )
    root.store.reserve(run.scope, "routing-result", {"requestId": run.request_id})
    root.store.complete(run.scope, "routing-result", {"requestId": run.request_id}, result.model_dump(mode="json"))
    return outcome


def counts(root):
    with root.store.engine.connect() as db:
        return [
            db.scalar(select(func.count()).select_from(table))
            for table in (calls, artifacts, generation_attempts, outbox)
        ]


@pytest.mark.parametrize("direction,choice", [("incoming", "batch"), ("outgoing", "omit")])
def test_reports_match_existing_allowlist_and_repeated_reads_do_not_write_accounting(
    runtime, tmp_path, direction, choice
):
    root, scope = runtime
    run = request(scope.model_copy(update={"activation": "classified"}), direction=direction)
    record_link(root, run)
    result = answer(direction, choice, 0.99)
    result.update(
        model="gpt-6-luna",
        inputHash="a" * 64,
        promptHash="b" * 64,
        contextSchemaVersion=2,
        latencyMs=12.5,
        usage={
            "input_tokens": 125,
            "output_tokens": 0,
            "input_tokens_details": {"cached_tokens": 7, "text": "PRIVATE TEXT"},
            "providerText": "PRIVATE TEXT",
        },
        providerError={"kind": "authentication", "httpStatus": 401, "body": "PRIVATE TEXT"},
        input="PRIVATE TEXT",
        response={"text": "PRIVATE TEXT"},
    )
    result["rawAnswers"][0]["reason"] = "PRIVATE TEXT"
    outcome = finish(root, run, result)
    expected = {"direction": direction, "status": "completed", **classifier_metadata(direction, outcome)}
    before = counts(root)
    assert routing_reports(root, scope) == [expected]
    assert routing_reports(root, scope) == [expected]
    assert counts(root) == before
    assert not list(tmp_path.rglob("accounting.sqlite3"))
    assert "PRIVATE TEXT" not in json.dumps(expected)
    assert expected["usage"] == {
        "input_tokens": 125,
        "output_tokens": 0,
        "input_tokens_details": {"cached_tokens": 7},
    }
    assert expected["requestId"] == run.request_id and expected["questionBasis"] in {
        "response_timing",
        "delivery_needed",
    }


def test_unfinished_links_remain_uncertain_without_fabricated_usage_and_completed_identity_wins(runtime, monkeypatch):
    root, scope = runtime
    unfinished = request(scope.model_copy(update={"activation": "unfinished"}))
    record_link(root, unfinished)
    get = Mock(wraps=root.store.get)
    monkeypatch.setattr(root.store, "get", get)
    report = routing_reports(root, scope)[0]
    assert report["direction"] == "incoming" and report["requestId"] == unfinished.request_id
    assert report["status"] == "uncertain" and report["fallback"] == "completion_unknown"
    assert report["usage"] is None and report["latencyMs"] is None and report["model"] is None
    get.assert_not_called()  # An unfinished link needs no artifact scan or inferred result.

    settled = request(scope.model_copy(update={"activation": "settled"}), request_id=unfinished.request_id)
    record_link(root, settled)
    finish(root, settled, answer("incoming", "batch", 0.99))
    before = counts(root)
    derived = routing_reports(root, scope)
    assert len(derived) == 1 and derived[0]["status"] == "completed"
    assert derived[0]["requestId"] == unfinished.request_id and derived[0]["usage"] is None
    assert counts(root) == before


def test_scoped_derivation_never_reads_other_game_player_epoch_or_delivery_artifacts(runtime, monkeypatch):
    root, scope = runtime
    included = request(scope.model_copy(update={"activation": "own"}))
    record_link(root, included)
    finish(root, included, answer("incoming", "batch", 0.99))
    for change in ({"game": "other-game"}, {"player": "p1"}, {"epoch": "e0"}):
        record_link(root, request(scope.model_copy(update=change)))
    delivery = RoutingLink(scope=scope, id=digest("unrelated-delivery"), kind="outgoing-delivery")
    with root.memory.transaction() as db:
        root.memory._set(db, "nativeRoutingLink:" + delivery.id, delivery.model_dump(mode="json"))
    receipt, get = Mock(wraps=root.store.receipt), Mock(wraps=root.store.get)
    monkeypatch.setattr(root.store, "receipt", receipt)
    monkeypatch.setattr(root.store, "get", get)
    assert len(routing_reports(root, scope)) == 1
    receipt.assert_called_once_with(included.scope, "routing-result")
    assert get.call_count == 2 and all(call.args[0] == included.scope for call in get.call_args_list)


@pytest.mark.parametrize("changed", [{"player": "p1"}, {"game": "other"}, {"epoch": "e0"}])
def test_caller_scope_is_checked_before_receipts_or_artifacts_are_read(runtime, monkeypatch, changed):
    root, scope = runtime
    receipt = Mock(wraps=root.store.receipt)
    monkeypatch.setattr(root.store, "receipt", receipt)
    with pytest.raises(JournalConflict):
        routing_reports(root, scope.model_copy(update=changed))
    receipt.assert_not_called()


@pytest.mark.parametrize("damage", ["request_scope", "workflow_id", "missing_request"])
def test_changed_link_binding_fails_before_any_receipt_read(runtime, monkeypatch, damage):
    root, scope = runtime
    link = record_link(root, request(scope))
    damaged = copy.deepcopy(link.model_dump(mode="json"))
    if damage == "request_scope":
        damaged["request"]["scope"]["activation"] = "other"
    elif damage == "workflow_id":
        damaged["id"] = "f" * 64
    else:
        damaged.pop("request")
    with root.memory.transaction() as db:
        root.memory._set(db, "nativeRoutingLink:" + link.id, damaged)
    receipt = Mock(wraps=root.store.receipt)
    monkeypatch.setattr(root.store, "receipt", receipt)
    with pytest.raises(JournalConflict, match="LINK_CONFLICT"):
        routing_reports(root, scope)
    receipt.assert_not_called()


@pytest.mark.parametrize("damage", ["scope", "request", "outcome_request", "outcome_direction"])
def test_foreign_or_changed_receipt_cannot_contribute_reported_usage(runtime, damage):
    root, scope = runtime
    run = request(scope)
    record_link(root, run)
    outcome = answer("incoming", "batch", 0.99)
    options = {}
    if damage == "scope":
        options["recorded_scope"] = scope.model_copy(update={"player": "p1"})
    elif damage == "request":
        options["recorded_request_id"] = "f" * 64
    elif damage == "outcome_request":
        outcome["requestId"] = "f" * 64
    else:
        outcome["direction"] = "outgoing"
    finish(root, run, outcome, **options)
    with pytest.raises(JournalConflict, match="REPORT_(?:RECEIPT|OUTCOME)_CONFLICT"):
        routing_reports(root, scope)


def test_terminal_unknown_completion_remains_uncertain_with_only_observed_usage(runtime):
    root, scope = runtime
    run = request(scope)
    record_link(root, run)
    finish(root, run, {"choice": "respond_now", "fallback": "completion_unknown"})
    report = routing_reports(root, scope)[0]
    assert report["status"] == "uncertain" and report["usage"] is None
    assert report["fallback"] == "completion_unknown" and report["requestId"] == run.request_id


@pytest.mark.parametrize("malformed", [{"text": "PRIVATE TEXT"}, ["PRIVATE TEXT"], True])
def test_malformed_diagnostics_are_redacted_without_bypassing_the_existing_allowlist(runtime, malformed):
    root, scope = runtime
    run = request(scope)
    record_link(root, run)
    finish(root, run, {**answer("incoming", "respond_now", 0.99), "fallback": malformed})
    report = routing_reports(root, scope)[0]
    assert report["status"] == "uncertain" and report["fallback"] is None
    assert "PRIVATE TEXT" not in json.dumps(report)
