"""Native operator diagnostics preserve provenance and never open private content."""

import json
import sqlite3
from contextlib import closing
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Literal
from unittest.mock import Mock

import pytest
from sqlalchemy import select

from civ_agent import cli, memory_schema
from civ_agent.civilization_state import CivilizationState
from civ_agent.decisions_ledger import RATE_VERSION, DecisionsLedger, requests, routing_requests
from civ_agent.decisions_policy import DecisionsSettings
from civ_agent.diplomacy_decisions import POLICY_VERSION
from civ_agent.experiment_status import experiment_status
from civ_agent.game_config import GameConfig
from civ_agent.memory_store import RecordStore
from civ_agent.monitoring import MonitorStore, path_for
from civ_agent.orchestration import native_experiments
from civ_agent.orchestration.contracts import AdvisoryRequest, AdvisoryResult, Reference, Scope, digest
from civ_agent.orchestration.job_contracts import JobTelemetry
from civ_agent.orchestration.managed_game_contracts import ManagedGameRequest, ManagedGameStatus, ManagedSeat
from civ_agent.orchestration.managed_host import GameAuthority
from civ_agent.orchestration.managed_player import NativePlayerSession, NativePlayerSettings
from civ_agent.orchestration.routing_models import RoutingLink, RoutingResult, RoutingRun
from civ_agent.orchestration.store import WorkflowStore, artifacts, calls
from civ_agent.player_paths import PlayerPaths as ManagedPlayerPaths
from civ_agent.tls import write_private_json
from tests.test_native_status import host_publication


@pytest.fixture
def native_state(tmp_path, monkeypatch):
    config = GameConfig(agents={"native_advice_enabled": True, "decisions_enabled": True}).resolved()
    identifier, epoch = "1234567890", "epoch"
    manifest = {"format": 1, "id": identifier, "auth_root": str(tmp_path / "auth")}
    request = ManagedGameRequest(
        authority=GameAuthority(game=identifier, epoch=epoch, lease_id="lease", limits=config.limits),
        seats=[
            ManagedSeat(
                civilization=player.civilization,
                session=NativePlayerSession(
                    game=identifier,
                    player=f"p{index}",
                    settings=NativePlayerSettings.model_validate(config.settings(player)),
                ),
            )
            for index, player in enumerate(config.agent_players)
        ],
    )
    started = datetime(2026, 11, 5, tzinfo=UTC)
    status = ManagedGameStatus(
        game=identifier,
        epoch=epoch,
        phase="PAUSED",
        mode="PAUSED",
        reason="OPERATOR",
        quiesced=True,
        initial_started_at=started,
        deadline=started + timedelta(seconds=config.limits.max_seconds),
        initial_turn=0,
        turn=2,
        handoff=4,
        model_invocations=14,
        active_civilization=config.agent_players[0].civilization,
        active_controller="AGENT",
        active_turn_id=None,
        rollovers=0,
    )
    write_private_json(tmp_path / "native-runtime/game.json", request.model_dump(mode="json"))
    write_private_json(tmp_path / "native-runtime/status.json", status.model_dump(mode="json"))
    write_private_json(tmp_path / "resolved.json", config.model_dump(mode="json"))
    write_private_json(tmp_path / "container.json", manifest)
    player = config.agent_players[0]
    paths = ManagedPlayerPaths(tmp_path, 0, player.civilization)
    paths.prepare()
    scope = Scope(game=identifier, player="p0", epoch=epoch, activation="diagnostics")
    state = CivilizationState(paths.coordinator / "notebook", player.civilization)
    state.bind(epoch)
    state.start_job("running", "gameplay", {})
    with state.transaction() as db:
        state._set(db, "nativeGatewayOwner", {"version": 1, "game": identifier, "player": "p0"})
        state._set(db, "routing:events", {"1": {"receivedAt": 100}, "2": {"choice": "respond_now"}})
        state._set(db, "nativeRoutingHeld:" + epoch, True)
        state._set(
            db,
            "advisories",
            {
                "cityDelivery": {"deliveredCities": 1, "deliveredQuestions": 2},
                "reviewDelivery": [
                    {
                        "requestId": digest("advisory"),
                        "name": "early_review",
                        "choice": "review",
                        "status": "delivered",
                        "exposure": "scheduler",
                    }
                ],
                "privateEvidence": "PRIVATE EVIDENCE MUST NOT BE READ",
            },
        )
        records = RecordStore(db)
        records.write(
            "exposure",
            "guidance",
            {
                "type": "exposure",
                "epoch": epoch,
                "revision": "revision",
                "available": False,
                "recommendations": [{"private": "PRIVATE RECOMMENDATION"}],
                "limitations": ["PRIVATE REASON"],
            },
        )
        for disposition in ("queued", "omitted", "sent"):
            records.write(
                disposition,
                "delivery",
                {
                    "deliveryId": disposition,
                    "status": disposition,
                    "deadline": 130,
                    "createdAt": 100,
                    "payload": {"text": "PRIVATE DRAFT", "epoch": epoch},
                },
            )
        db.execute(
            memory_schema.jobs.update().where(memory_schema.jobs.c.id == "running").values(snapshot="PRIVATE SNAPSHOT")
        )
    store = WorkflowStore(paths.store, player="p0")
    store.bind_epoch(identifier, epoch)
    host = host_publication(tmp_path, epoch=epoch, finished=False)
    health = Mock(return_value={"status": "ready", "upstreamStatus": "unavailable"})
    monkeypatch.setattr("civ_agent.container_supervisor.routing_broker_health", health)
    yield SimpleNamespace(
        root=tmp_path,
        config=config,
        manifest=manifest,
        state=state,
        store=store,
        paths=paths,
        scope=scope,
        player=player,
        host=host,
        health=health,
    )
    state.engine.dispose()
    store.close()


def routing(
    fixture, *, activation="routing", settled=True, direction: Literal["incoming", "outgoing"] = "outgoing", usage=9
):
    scope = fixture.scope.model_copy(update={"activation": activation})
    request = RoutingRun(
        scope=scope,
        submission=fixture.store.put(scope, {"private": "PRIVATE INPUT"}),
        policy=fixture.store.put(scope, {"private": "PRIVATE POLICY"}),
        request_id=digest(activation),
        operation_id=activation,
        direction=direction,
        timeout_seconds=30,
    )
    link = RoutingLink(scope=scope, id=scope.identity("workflow", "routing"), kind="routing", request=request)
    with fixture.state.transaction() as db:
        fixture.state._set(db, "nativeRoutingLink:" + link.id, link.model_dump(mode="json"))
    if settled:
        outcome = {
            "requestId": request.request_id,
            "direction": direction,
            "policyVersion": POLICY_VERSION,
            "choice": "send_now" if direction == "outgoing" else "respond_now",
            "fallback": "service_failure",
            "latencyMs": 14,
            "providerError": {"kind": "authentication", "httpStatus": 401},
            "usage": {"input_tokens": usage},
        }
        result = RoutingResult(scope=scope, request_id=request.request_id, outcome=fixture.store.put(scope, outcome))
        fixture.store.reserve(scope, "routing-result", {"id": request.request_id})
        fixture.store.complete(scope, "routing-result", {"id": request.request_id}, result.model_dump(mode="json"))
    return request


def advisory(fixture):
    scope = fixture.scope.model_copy(update={"activation": "advisory"})
    request = AdvisoryRequest(
        scope=scope,
        evidence=fixture.store.put(scope, {"private": "PRIVATE INPUT"}),
        policy=fixture.store.put(scope, {"private": "PRIVATE POLICY"}),
        request_id=digest("advisory"),
        timeout_seconds=30,
    )
    outcome = {
        "requestId": request.request_id,
        "latencyMs": 16,
        "usage": {"input_tokens": 7},
        "questionNames": ["early_review"],
        "answers": [],
        "rawAnswers": [],
        "questionResults": [{"name": "early_review", "status": "low_confidence"}],
        "brokerReconciliation": True,
    }
    result = AdvisoryResult(scope=scope, request_id=request.request_id, outcome=fixture.store.put(scope, outcome))
    fixture.store.reserve(scope, "advisory-result", {"id": request.request_id})
    fixture.store.complete(scope, "advisory-result", {"id": request.request_id}, result.model_dump(mode="json"))
    with fixture.state.transaction() as db:
        fixture.state._set(db, "nativeAdvisory:" + scope.activation, request.model_dump(mode="json"))


def test_native_diagnostics_preserve_aggregates_without_private_content_or_service_construction(
    native_state, monkeypatch, capsys
):
    fixture = native_state
    routing(fixture)
    advisory(fixture)
    for filename in ("authored-memory.sqlite3", "plans.sqlite3"):
        (fixture.paths.coordinator / filename).write_bytes(b"PRIVATE AUTHORED STORE")
    (fixture.paths.store / "agent-steps.sqlite3").write_bytes(b"PRIVATE SDK HISTORY")
    original_loads = json.loads

    def safe_loads(value, *args, **kwargs):
        assert "PRIVATE" not in value, "Diagnostics loaded content rather than selected metadata"
        return original_loads(value, *args, **kwargs)

    monkeypatch.setattr(native_experiments.json, "loads", safe_loads)
    report = experiment_status(fixture.root, fixture.config, fixture.manifest, now=140)
    row = report["players"][0]
    assert row["memory"]["paused"] is True and row["memory"]["routing"]["held"] is True
    assert row["memory"]["advice"]["recommendationsExposed"] == 1
    assert row["memory"]["advice"]["unavailableReasons"] == ["native_advice_unavailable"]
    assert row["memory"]["routing"]["outstanding"] == {"queued": 1, "omitted": 1}
    assert row["memory"]["routing"]["oldestWaitSeconds"] == 40
    assert row["memory"]["routing"]["overdueDeliveries"] == 1
    assert row["memory"]["routing"]["incomingUnclassifiedOverdue"] == 1
    classifier = row["classifier"]
    assert classifier["calls"] == 2 and classifier["usage"] == {"input_tokens": 16}
    assert classifier["averageLatencyMs"] == 15 and classifier["routing"]["calls"] == 1
    assert classifier["latest"] is None and classifier["chronologyComplete"] is False
    assert classifier["lastAdviceCallLatencyMs"] is None
    assert classifier["advisoryObservations"]["reconciliations"] == 1
    assert classifier["advisoryDelivery"]["events"] == 1 and classifier["advisoryDelivery"]["complete"] is False
    assert classifier["advisoryDelivery"]["coverage"] == "partial_native_delivery_records"
    assert "PRIVATE" not in json.dumps(report)
    with fixture.state.transaction() as db:
        assert db.scalar(select(memory_schema.jobs.c.status).where(memory_schema.jobs.c.id == "running")) == "RUNNING"
    assert cli.main(["game", "status", "--state-dir", str(fixture.root)]) == 0
    assert json.loads(capsys.readouterr().out)["experiments"]["players"][0]["classifier"]["calls"] == 2


def test_unsettled_native_classifier_retains_unknown_usage_and_chronology(native_state):
    routing(native_state, settled=False)
    row = experiment_status(native_state.root, native_state.config, native_state.manifest, now=140)["players"][0]
    assert row["classifier"]["calls"] == 1 and row["classifier"]["unknownUsage"] == 1
    assert row["classifier"]["usage"] == {} and row["classifier"]["latest"] is None
    assert row["classifier"]["fallbacks"] == {"completion_unknown": 1}


def test_native_diagnostics_exclude_historical_delivery_intents_from_current_queue_metrics(native_state):
    fixture = native_state
    with fixture.state.transaction() as db:
        for status in ("classifying", "queued", "sending", "omitted", "sent", "cancelled", "failed"):
            RecordStore(db).write(
                "old-" + status,
                "delivery",
                {
                    "deliveryId": "old-" + status,
                    "status": status,
                    "deadline": 0,
                    "createdAt": 0,
                    "override": "immediate",
                    "queueDelaySeconds": 1000,
                    "payload": {"epoch": "previous-epoch", "text": "PRIVATE OLD INTENT"},
                },
            )
    row = experiment_status(fixture.root, fixture.config, fixture.manifest, now=140)["players"][0]["memory"]
    assert row["status"] == "ready"
    assert row["routing"]["outstanding"] == {"queued": 1, "omitted": 1}
    assert row["routing"]["oldestWaitSeconds"] == 40 and row["routing"]["overdueDeliveries"] == 1
    assert {item["deliveryId"] for item in row["routing"]["deliveries"]} == {"queued", "omitted"}
    outcomes = row["metrics"]["reachableOutcomes"]
    assert {key: value for key, value in outcomes.items() if key.startswith("delivery_")} == {
        "delivery_queued": 1,
        "delivery_omitted": 1,
        "delivery_sent": 1,
    }
    assert not outcomes.get("override_immediate") and row["metrics"]["queueDelaySamples"] == 0
    assert "PRIVATE" not in json.dumps(row)
    with fixture.state.transaction() as db:
        assert len(RecordStore(db).all(categories={"delivery"})) == 10


@pytest.mark.parametrize("field", ["player", "epoch"])
def test_native_classifier_excludes_sibling_and_stale_scope_links(native_state, field):
    fixture = native_state
    routing(fixture)
    foreign = fixture.scope.model_copy(update={field: "foreign", "activation": "excluded"})
    request = RoutingRun(
        scope=foreign,
        submission=Reference(sha256=digest("missing-private-input")),
        policy=Reference(sha256=digest("missing-private-policy")),
        request_id=digest("excluded"),
        operation_id="excluded",
        direction="outgoing",
        timeout_seconds=30,
    )
    link = RoutingLink(scope=foreign, id=foreign.identity("workflow", "routing"), kind="routing", request=request)
    with fixture.state.transaction() as db:
        fixture.state._set(db, "nativeRoutingLink:" + link.id, link.model_dump(mode="json"))
    report = experiment_status(fixture.root, fixture.config, fixture.manifest, now=140)
    assert report["players"][0]["classifier"]["calls"] == 1


@pytest.mark.parametrize("broken", ["owner", "epoch", "routing", "delivery", "receipt", "artifact", "workflow_owner"])
def test_native_diagnostics_refuse_invalid_scope_and_records(native_state, broken):
    fixture = native_state
    request = routing(fixture)
    with fixture.state.transaction() as db:
        if broken == "owner":
            fixture.state._set(
                db, "nativeGatewayOwner", {"version": 1, "game": fixture.scope.game, "player": "foreign"}
            )
        elif broken == "epoch":
            fixture.state._set(db, "epoch", "foreign")
        elif broken == "routing":
            fixture.state._set(db, "routing:events", ["invalid"])
        elif broken == "delivery":
            RecordStore(db).write("bad", "delivery", {"status": "queued"})
    with fixture.store.engine.begin() as db:
        if broken == "receipt":
            db.execute(
                calls.update().where(calls.c.id == request.scope.identity("call", "routing-result")).values(scope="{}")
            )
        elif broken == "artifact":
            db.execute(artifacts.update().values(body='{"private":"PRIVATE CORRUPT ARTIFACT"}'))
        elif broken == "workflow_owner":
            db.exec_driver_sql("UPDATE ownership SET player='foreign'")
    row = experiment_status(fixture.root, fixture.config, fixture.manifest, now=140)["players"][0]
    key = "classifier" if broken in {"receipt", "artifact", "workflow_owner"} else "memory"
    assert row[key] == {"status": "unavailable", "reason": "diagnostics_unreadable"}
    if broken in {"owner", "epoch"}:
        assert row["classifier"] == {"status": "unavailable", "reason": "diagnostics_unreadable"}
    assert "PRIVATE" not in json.dumps(row)


def test_native_review_usage_comes_from_scoped_host_jobs_with_completeness(native_state):
    fixture = native_state
    telemetry = JobTelemetry(
        sequence=1,
        phase="COMPLETED",
        startedAtMs=100,
        endedAtMs=200,
        elapsedMs=100,
        usageReported=True,
        usageComplete=True,
        model="selected-model",
        provider="openai",
        modelRequests=2,
    )
    usage = {"input_tokens": 20, "cached_input_tokens": 6, "output_tokens": 14, "reasoning_output_tokens": 4}
    store = MonitorStore(path_for(fixture.root))
    store.put(
        "jobs",
        "native:review",
        {
            "schemaVersion": 1,
            "matchId": fixture.host["matchId"],
            "jobId": "native:review",
            "job": {
                "civilization": fixture.player.civilization,
                "epoch": fixture.scope.epoch,
                "purpose": "review",
                "usage": usage,
                "telemetry": telemetry.model_dump(mode="json"),
            },
        },
        epoch=fixture.scope.epoch,
        civilization=fixture.player.civilization,
    )
    store.engine.dispose()
    summary = experiment_status(fixture.root, fixture.config, fixture.manifest, now=140)["players"][0]["classifier"]
    assert summary["fullReviewUsage"] == {"jobs": 1, "complete": True, "usage": usage}


def test_native_broker_spending_uses_matching_account_and_keeps_routing_separate(native_state):
    fixture = native_state
    ledger = DecisionsLedger(fixture.root / "auth")
    principal = fixture.scope.game + "-0"
    ledger.register(
        principal, fixture.scope.game, fixture.player.civilization, DecisionsSettings(decisions_enabled=True)
    )
    with ledger.engine.begin() as db:
        db.execute(
            requests.insert().values(
                principal=principal,
                id=digest("advisory"),
                fingerprint=digest("body"),
                reserved=0,
                spent=900,
                input_tokens=9,
                result="{}",
                rate_version=RATE_VERSION,
                created_ms=123,
            )
        )
        db.execute(
            routing_requests.insert().values(
                principal=principal,
                id=digest("routing"),
                operation_id="routing",
                fingerprint=digest("routing-body"),
                result="{}",
                input_tokens=7,
                spent=700,
                rate_version=RATE_VERSION,
                created_ms=124,
            )
        )
    ledger.engine.dispose()
    spending = experiment_status(fixture.root, fixture.config, fixture.manifest, now=140)["players"][0][
        "additionalSpending"
    ]
    assert spending["spentNanodollars"] == 900 and spending["inputTokens"] == 9
    assert spending["routingAccounting"]["spentNanodollars"] == 700


def test_native_diagnostics_missing_configuration_and_old_paths_are_not_fallbacks(tmp_path, monkeypatch):
    config = GameConfig(agents={"native_advice_enabled": True}).resolved()
    old = tmp_path / "agent-services/Rome/civilization/memory.sqlite3"
    old.parent.mkdir(parents=True)
    old.write_bytes(b"PRIVATE LEGACY STORE")
    report = experiment_status(tmp_path, config, {}, now=140)
    assert all(row["memory"]["status"] == "waiting_for_native_configuration" for row in report["players"])
    assert not (tmp_path / "coordinator").exists() and not (tmp_path / "player-stores").exists()


def test_native_diagnostic_read_lock_is_explicit_without_mutating_jobs(native_state):
    fixture = native_state
    with closing(sqlite3.connect(fixture.state.path)) as writer:
        writer.execute("PRAGMA journal_mode=DELETE")
        writer.execute("BEGIN EXCLUSIVE")
        try:
            row = experiment_status(fixture.root, fixture.config, fixture.manifest, now=140)["players"][0]
            assert row["memory"] == {"status": "unavailable", "reason": "diagnostics_unreadable"}
            assert row["classifier"] == {"status": "unavailable", "reason": "diagnostics_unreadable"}
        finally:
            writer.rollback()
