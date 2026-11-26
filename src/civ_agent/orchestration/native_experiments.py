"""Scoped operator aggregates from existing native stores, without opening content."""

import json
import math
from collections import Counter
from pathlib import Path

from pydantic import TypeAdapter
from sqlalchemy import func, select
from sqlalchemy.exc import SQLAlchemyError

from civ_agent import memory_schema as domain
from civ_agent.advisory_store import DELIVERY_CATEGORY, DELIVERY_VERSION
from civ_agent.container_support import ContainerError
from civ_agent.decisions_ledger import RATE_VERSION, accounts, requests, routing_requests
from civ_agent.diplomacy_decisions import BATCH_SECONDS
from civ_agent.monitoring import path_for, read_database
from civ_agent.monitoring import records as host_records
from civ_agent.operator_state import is_local, projection
from civ_agent.operator_state import published_game as published_managed_epoch
from civ_agent.orchestration.contracts import (
    AdvisoryRequest,
    AdvisoryResult,
    OpaqueId,
    Reference,
    Scope,
    canonical,
    digest,
)
from civ_agent.orchestration.job_contracts import HostUsage, JobTelemetry
from civ_agent.orchestration.managed_game_contracts import ManagedGameRequest, ManagedGameStatus
from civ_agent.orchestration.native_status import host_result, validate_epoch_publication
from civ_agent.orchestration.routing_models import RoutingLink, RoutingResult
from civ_agent.orchestration.store import artifact_scopes, artifacts, calls, epochs, ownership
from civ_agent.player_paths import PlayerPaths as ManagedPlayerPaths
from civ_agent.protocol import JsonObject
from civ_agent.telemetry import TOKEN_KEYS, classifier_metadata

_UNCERTAIN = {"broker_unavailable", "completion_unknown", "malformed_broker_output", "not_submitted"}
_REASONS = _UNCERTAIN | {
    "protected",
    "immediate",
    "routing_deadline",
    "budget_exhausted",
    "circuit_open",
    "service_failure",
    "low_confidence",
    "refusal",
    "input_unavailable",
    "native_capture_unavailable",
    "stale_native_basis",
    "stale_objectives",
    "not_active_gameplay",
    "disabled_budget",
    "unchanged_city_decision",
    "resumed_startup",
}
_ERRORS = (OSError, ValueError, KeyError, TypeError, AttributeError, SQLAlchemyError, ContainerError)
_UNAVAILABLE = {"status": "unavailable", "reason": "diagnostics_unreadable"}
_EPOCH = TypeAdapter(OpaqueId)


def _reason(value):
    return value if value in _REASONS else "unavailable" if value is not None else None


def _number(value):
    if type(value) not in {int, float} or not math.isfinite(value) or value < 0:
        raise ValueError("NATIVE_DIAGNOSTIC_NUMBER")
    return value


def _count(value):
    if type(value) is not int or value < 0:
        raise ValueError("NATIVE_DIAGNOSTIC_COUNT")
    return value


def _meta(db, key):
    value = db.scalar(select(domain.meta.c.value).where(domain.meta.c.key == key))
    return json.loads(value) if isinstance(value, str) else value


def _check_notebook(db, scope):
    if (
        _meta(db, "format") != domain.FORMAT_VERSION
        or _meta(db, "nativeGatewayOwner") != {"version": 1, "game": scope.game, "player": scope.player}
        or _meta(db, "epoch") != scope.epoch
    ):
        raise ValueError("NATIVE_DIAGNOSTIC_SCOPE")


def _advisory_field(db, field):
    value = db.scalar(
        select(func.json_extract(domain.meta.c.value, "$." + field)).where(domain.meta.c.key == "advisories")
    )
    return json.loads(value) if isinstance(value, str) else value


def _safe_city(value):
    if value is None:
        return None
    result: JsonObject = {
        key: _count(value[key])
        for key in (
            "capturedCities",
            "unavailableCities",
            "omittedCities",
            "deliveredCities",
            "deliveredQuestions",
            "gameTurn",
        )
        if value.get(key) is not None
    }
    for key in ("reason", "fallback"):
        if key in value:
            result[key] = _reason(value[key])
    if type(value.get("reused")) is bool:
        result["reused"] = value["reused"]
    return result


def memory_projection(path, scope, *, now, paused):
    if not path.exists():
        return {"status": "waiting_for_first_observation"}
    with read_database(path) as db:
        _check_notebook(db, scope)
        states = _meta(db, "routing:events") or {}
        if not isinstance(states, dict):
            raise ValueError("NATIVE_DIAGNOSTIC_ROUTING")
        # Only aggregate columns leave SQLite. Recommendations, command arguments,
        # draft text and authored document bodies are never loaded by this reader.
        guidance = list(
            db.execute(
                select(
                    func.json_extract(domain.records.c.body, "$.type").label("kind"),
                    func.json_extract(domain.records.c.body, "$.epoch").label("epoch"),
                    func.json_extract(domain.records.c.body, "$.revision").label("revision"),
                    func.json_extract(domain.records.c.body, "$.available").label("available"),
                    func.json_array_length(domain.records.c.body, "$.recommendations").label("recommendations"),
                    func.json_array_length(domain.records.c.body, "$.matchedRecommendations").label("matches"),
                )
                .select_from(domain.current_records)
                .where(domain.records.c.category == "guidance")
                .order_by(domain.record_order)
            ).mappings()
        )
        fields = ("deliveryId", "status", "deadline", "createdAt", "override", "queueDelaySeconds")
        drafts = list(
            db.execute(
                select(
                    *(func.json_extract(domain.records.c.body, "$." + key).label(key) for key in fields),
                    func.json_extract(domain.records.c.body, "$.decision.choice").label("choice"),
                    func.json_extract(domain.records.c.body, "$.decision.fallback").label("fallback"),
                    func.json_extract(domain.records.c.body, "$.payload.epoch").label("epoch"),
                )
                .select_from(domain.current_records)
                .where(domain.records.c.category == "delivery")
                .order_by(domain.record_order)
            ).mappings()
        )
        routing_count = db.scalar(
            select(func.count()).select_from(domain.current_records).where(domain.records.c.category == "routing")
        )
        review_count = db.scalar(
            select(func.count()).select_from(domain.reviews).where(domain.reviews.c.status == "COMPLETED")
        )
        accounting = classifier_metadata("advisory_batch", {"accounting": _advisory_field(db, "accounting")})[
            "accounting"
        ]
        city = _safe_city(_advisory_field(db, "cityDelivery"))
        routing_held = _meta(db, "nativeRoutingHeld:" + scope.epoch)
    metrics = Counter()
    advice = {"status": "waiting_for_first_observation"}
    for item in guidance:
        if item["epoch"] != scope.epoch:
            continue
        if item["kind"] == "exposure":
            if item["available"] not in (0, 1):
                raise ValueError("NATIVE_DIAGNOSTIC_ADVICE")
            exposed = _count(item["recommendations"])
            metrics.update(exposure=1, recommendations_exposed=exposed, advice_unavailable=int(not item["available"]))
            advice = {
                "status": "available" if item["available"] else "unavailable",
                "epoch": scope.epoch,
                "revision": item["revision"],
                "recommendationsExposed": exposed,
                "source": "native",
                "unavailableReasons": [] if item["available"] else ["native_advice_unavailable"],
            }
        elif item["kind"] == "action_match":
            metrics.update(action_match=1, actions_matching_advice=int(_count(item["matches"]) > 0))
        else:
            raise ValueError("NATIVE_DIAGNOSTIC_GUIDANCE")
    incoming_ready = unclassified = overdue = timing_unknown = 0
    for state in states.values():
        choice = state.get("choice")
        if choice is not None:
            if choice not in {"respond_now", "batch", "no_separate_response"}:
                raise ValueError("NATIVE_DIAGNOSTIC_ROUTING_CHOICE")
            metrics["incoming_" + choice] += 1
        if state.get("attempted") or state.get("completed"):
            continue
        incoming_ready += (
            choice == "respond_now"
            or choice == "batch"
            and state.get("deadline") is not None
            and _number(state["deadline"]) <= now
        )
        unclassified += choice is None
        if choice is None:
            if state.get("receivedAt") is None:
                timing_unknown += 1
            else:
                overdue += _number(state["receivedAt"]) + BATCH_SECONDS <= now
    outstanding, waiting, delays = [], [], []
    for item in drafts:
        if _EPOCH.validate_python(item["epoch"]) != scope.epoch:
            continue
        status = item["status"]
        if status not in {"classifying", "queued", "sending", "omitted", "sent", "cancelled", "failed"}:
            raise ValueError("NATIVE_DIAGNOSTIC_DELIVERY")
        _number(item["deadline"])
        _number(item["createdAt"])
        metrics["delivery_" + status] += 1
        if item["override"] in {"immediate", "cancel"}:
            metrics["override_" + item["override"]] += 1
        if item["queueDelaySeconds"] is not None:
            delays.append(_number(item["queueDelaySeconds"]))
        metrics["omissions"] += item["choice"] == "omit"
        if item["fallback"] is not None:
            metrics["fallback_" + (_reason(item["fallback"]) or "unavailable")] += 1
        if status in {"classifying", "queued", "sending", "omitted"}:
            outstanding.append(item)
            if status != "omitted":
                waiting.append(item)
    metrics["incoming_routing_decisions"] += _count(routing_count)
    return {
        "status": "ready",
        "epoch": scope.epoch,
        "paused": paused,
        "advice": advice,
        "routing": {
            "held": routing_held,
            "incomingReady": incoming_ready,
            "incomingUnclassified": unclassified,
            "incomingUnclassifiedOverdue": overdue,
            "incomingTimingUnknown": timing_unknown,
            "outstanding": dict(Counter(item["status"] for item in outstanding)),
            "oldestWaitSeconds": max((max(0, now - item["createdAt"]) for item in waiting), default=0),
            "overdueDeliveries": sum(item["deadline"] <= now for item in waiting),
            "deliveries": [
                {key: item[key] for key in ("deliveryId", "status", "deadline")} for item in outstanding[:1000]
            ],
            "deliveriesTruncated": len(outstanding) > 1000,
        },
        "metrics": {
            "reachableOutcomes": dict(metrics),
            "queueDelaySeconds": delays[:1000],
            "queueDelaySamples": len(delays),
            "interpretation": "Action matches do not establish advice causality. Classifier billing is accounted separately.",
        },
        "additionalDecisions": accounting,
        "cityAdvice": city,
        "fullStrategicReviews": {"completed": _count(review_count)},
    }


def _artifact(db, scope, reference):
    body = db.scalar(
        select(artifacts.c.body)
        .join(artifact_scopes, artifact_scopes.c.sha256 == artifacts.c.sha256)
        .where(artifacts.c.sha256 == reference.sha256, artifact_scopes.c.scope == digest(scope.model_dump()))
    )
    if body is None:
        raise ValueError("NATIVE_DIAGNOSTIC_ARTIFACT_MISSING")
    value = json.loads(body)
    if digest(value) != reference.sha256:
        raise ValueError("NATIVE_DIAGNOSTIC_ARTIFACT_CORRUPT")
    return value


def _receipt(db, scope, key):
    row = (
        db.execute(select(calls.c.scope, calls.c.result).where(calls.c.id == scope.identity("call", key)))
        .mappings()
        .one_or_none()
    )
    if row is None:
        return None
    if row["scope"] != canonical(scope.model_dump()):
        raise ValueError("NATIVE_DIAGNOSTIC_RECEIPT_SCOPE")
    return _artifact(db, scope, Reference(sha256=row["result"])) if row["result"] is not None else None


def _delivery_records(db, scope, review_delivery):
    rows = db.execute(
        select(domain.records.c.key, domain.records.c.version, domain.records.c.body)
        .select_from(domain.record_versions(domain.reachable))
        .where(domain.records.c.category == DELIVERY_CATEGORY)
        .order_by(domain.record_order)
    ).mappings()
    deliveries = []
    for row in rows:
        event = json.loads(row["body"])
        if (
            set(event) != {"version", "scope", "source", "policyVersion", "atMs", "deliveryResults"}
            or type(event["version"]) is not int
            or event["version"] != DELIVERY_VERSION
            or event["source"] not in {"role", "review"}
        ):
            raise ValueError("NATIVE_DIAGNOSTIC_DELIVERY_EVENT")
        observed = Scope.model_validate(event["scope"])
        if (observed.game, observed.player) != (scope.game, scope.player) or row[
            "key"
        ] != "advisory-delivery:" + observed.activation:
            raise ValueError("NATIVE_DIAGNOSTIC_DELIVERY_SCOPE")
        metadata = classifier_metadata("advisory_delivery", event)
        if (
            metadata.get("policyVersion") != event["policyVersion"]
            or metadata["deliveryResults"] != event["deliveryResults"]
        ):
            raise ValueError("NATIVE_DIAGNOSTIC_DELIVERY_METADATA")
        timestamp = _count(event["atMs"])
        for identifier in dict.fromkeys(item["requestId"] for item in metadata["deliveryResults"]):
            deliveries.append(
                {
                    "direction": "advisory_delivery",
                    "atMs": timestamp,
                    "eventId": digest([row["key"], row["version"], identifier]),
                    "originEpoch": observed.epoch,
                    "source": event["source"],
                    **classifier_metadata(
                        "advisory_delivery",
                        {
                            "policyVersion": event["policyVersion"],
                            "deliveryResults": [
                                item for item in metadata["deliveryResults"] if item["requestId"] == identifier
                            ],
                        },
                    ),
                }
            )
    selected = classifier_metadata("advisory_delivery", {"deliveryResults": review_delivery})["deliveryResults"]
    selected.sort(key=lambda item: (item["requestId"] or "", item["name"], item["status"]))
    complete = True
    if selected:
        turn = _advisory_field(db, "reviewDeliveryTurn")
        identity = digest(["advisory-delivery", "review", turn])
        recorded = db.scalar(
            select(func.json_extract(domain.records.c.body, "$.deliveryResults"))
            .select_from(domain.current_records)
            .where(
                domain.records.c.key == "advisory-delivery:" + identity, domain.records.c.category == DELIVERY_CATEGORY
            )
        )
        complete = recorded is not None and json.loads(recorded) == selected
        if not complete:
            for identifier in dict.fromkeys(item["requestId"] for item in selected):
                deliveries.append(
                    {
                        "direction": "advisory_delivery",
                        "atMs": None,
                        **classifier_metadata(
                            "advisory_delivery",
                            {"deliveryResults": [item for item in selected if item["requestId"] == identifier]},
                        ),
                    }
                )
    return deliveries, complete


def classifier_projection(notebook, store, scope):
    if not notebook.exists():
        return [], [], False
    with read_database(notebook) as db:
        _check_notebook(db, scope)
        rows = list(
            db.execute(
                select(domain.meta.c.key, domain.meta.c.value)
                .where(
                    domain.meta.c.key.like("nativeRoutingLink:%") | domain.meta.c.key.like("nativeAdvisory:%"),
                    func.json_extract(domain.meta.c.value, "$.scope.game") == scope.game,
                    func.json_extract(domain.meta.c.value, "$.scope.player") == scope.player,
                    func.json_extract(domain.meta.c.value, "$.scope.epoch") == scope.epoch,
                )
                .order_by(domain.meta.c.key)
            )
        )
        review_delivery = _advisory_field(db, "reviewDelivery") or []
        deliveries, delivery_complete = _delivery_records(db, scope, review_delivery)
    admitted = []
    for key, value in rows:
        if key.startswith("nativeRoutingLink:"):
            link = RoutingLink.model_validate_json(value)
            if key != "nativeRoutingLink:" + link.id:
                raise ValueError("NATIVE_DIAGNOSTIC_LINK")
            if link.kind != "routing":
                continue
            if link.request is None:
                raise ValueError("NATIVE_DIAGNOSTIC_LINK_REQUEST")
            request, direction, result_type, receipt_key = (
                link.request,
                link.request.direction,
                RoutingResult,
                "routing-result",
            )
        else:
            request = AdvisoryRequest.model_validate_json(value)
            if key != "nativeAdvisory:" + request.scope.activation:
                raise ValueError("NATIVE_DIAGNOSTIC_LINK")
            direction, result_type, receipt_key = "advisory_batch", AdvisoryResult, "advisory-result"
        admitted.append((request, direction, result_type, receipt_key))
    reports = {}
    if admitted:
        with read_database(store) as db:
            owner = db.execute(select(ownership)).mappings().one()
            if (
                owner["player"] != scope.player
                or owner["version"] != 1
                or db.scalar(select(epochs.c.epoch).where(epochs.c.game == scope.game)) != scope.epoch
            ):
                raise ValueError("NATIVE_DIAGNOSTIC_WORKFLOW_SCOPE")
            for request, direction, result_type, key in admitted:
                saved = _receipt(db, request.scope, key)
                outcome = {"requestId": request.request_id, "fallback": "completion_unknown"}
                if saved is not None:
                    result = result_type.model_validate(saved)
                    if (result.scope, result.request_id) != (request.scope, request.request_id):
                        raise ValueError("NATIVE_DIAGNOSTIC_RECEIPT_BINDING")
                    outcome = _artifact(db, result.scope, result.outcome)
                    if (
                        outcome.get("requestId") != request.request_id
                        or direction != "advisory_batch"
                        and outcome.get("direction") != direction
                    ):
                        raise ValueError("NATIVE_DIAGNOSTIC_OUTCOME_BINDING")
                report = {"direction": direction, "atMs": None, **classifier_metadata(direction, outcome)}
                if report.get("fallback") is not None:
                    report["fallback"] = _reason(report["fallback"])
                report["status"] = (
                    "uncertain" if saved is None or outcome.get("fallback") in _UNCERTAIN else "completed"
                )
                prior = reports.get(request.request_id)
                if prior is not None and prior != report:
                    raise ValueError("NATIVE_DIAGNOSTIC_REQUEST_CONFLICT")
                reports[request.request_id] = report
    return list(reports.values()), deliveries, delivery_complete


def _review_jobs(root, scope, civilization):
    result, _ = host_result(root, epoch=scope.epoch)
    if result is None:
        return None
    with read_database(path_for(root)) as db:
        rows = (
            db.execute(
                select(host_records.c.payload).where(
                    host_records.c.collection == "jobs",
                    host_records.c.epoch == scope.epoch,
                    host_records.c.civilization == civilization,
                    func.json_extract(host_records.c.payload, "$.job.purpose") == "review",
                )
            )
            .scalars()
            .all()
        )
    jobs = []
    for raw in rows:
        value = json.loads(raw)
        job = value["job"]
        if (
            value["schemaVersion"] != 1
            or value["matchId"] != result.matchId
            or job["epoch"] != scope.epoch
            or job["civilization"] != civilization
        ):
            raise ValueError("NATIVE_DIAGNOSTIC_HOST_JOB_SCOPE")
        usage = None
        if job.get("usage") is not None:
            if not all(key in job["usage"] for key in TOKEN_KEYS):
                raise ValueError("NATIVE_DIAGNOSTIC_HOST_JOB_USAGE")
            usage = HostUsage.model_validate(job["usage"]).model_dump(mode="json")
        telemetry = (
            JobTelemetry.model_validate(job["telemetry"]).model_dump(mode="json")
            if job.get("telemetry") is not None
            else {}
        )
        jobs.append({"usage": usage, "telemetry": telemetry})
    return jobs


def _spending(path, *, game, index, civilization):
    if not path.exists():
        return {"status": "unavailable", "reason": "broker_accounting_missing"}
    principal = f"{game}-{index}"
    with read_database(path) as db:
        account = db.execute(select(accounts).where(accounts.c.principal == principal)).mappings().one_or_none()
        if account is None or (account["game"], account["player"]) != (game, civilization):
            raise ValueError("NATIVE_DIAGNOSTIC_BROKER_SCOPE")
        rows = db.execute(select(requests).where(requests.c.principal == principal)).mappings().all()
        routing = db.execute(select(routing_requests).where(routing_requests.c.principal == principal)).mappings().all()
    return {
        "rateVersion": RATE_VERSION,
        "limitKind": "local_estimated_spending_ceiling",
        "ceilingNanodollars": account["ceiling"],
        "spentNanodollars": sum(_count(row["spent"]) for row in rows),
        "reservedNanodollars": sum(_count(row["reserved"]) for row in rows),
        "inputTokens": sum(_count(row["input_tokens"]) for row in rows if row["input_tokens"] is not None),
        "requests": len(rows),
        "unknownUsage": sum(row["input_tokens"] is None for row in rows),
        "routingAccounting": {
            "requests": len(routing),
            "spentNanodollars": sum(_count(row["spent"]) for row in routing),
            "inputTokens": sum(_count(row["input_tokens"]) for row in routing if row["input_tokens"] is not None),
            "unknownUsage": sum(row["input_tokens"] is None for row in routing),
        },
    }


def native_experiment_players(root, config, manifest, *, now):
    from civ_agent.experiment_status import classifier_summary

    output = []
    request_path = root / "native-runtime/game.json"
    pointer_path = root / "native-runtime/current-epoch.json"
    if not any(
        path.exists() or path.is_symlink()
        for path in (request_path, pointer_path, root / "native-runtime/controller.json")
    ):
        return [
            {
                "player": player.name,
                "civilization": player.civilization,
                "memory": {"status": "waiting_for_native_configuration"},
                "classifier": {"status": "waiting_for_native_configuration", "calls": 0},
            }
            for player in config.agent_players
        ]
    try:
        request = published_managed_epoch(root)
        if manifest.get("id", request.authority.game) != request.authority.game:
            raise ValueError("NATIVE_DIAGNOSTIC_GAME_SCOPE")
        status_path = root / "native-runtime/status.json"
        status = (
            projection(root).status
            if is_local(root)
            else ManagedGameStatus.model_validate_json(status_path.read_bytes())
            if status_path.exists()
            else None
        )
        if status is not None and (status.game, status.epoch) != (request.authority.game, request.authority.epoch):
            raise ValueError("NATIVE_DIAGNOSTIC_STATUS_SCOPE")
        if isinstance(request, ManagedGameRequest) and isinstance(status, ManagedGameStatus):
            validate_epoch_publication(request, status)
    except _ERRORS:
        return [
            {
                "player": player.name,
                "civilization": player.civilization,
                "memory": dict(_UNAVAILABLE),
                "classifier": dict(_UNAVAILABLE),
            }
            for player in config.agent_players
        ]
    for index, player in enumerate(config.agent_players):
        row: JsonObject = {"player": player.name, "civilization": player.civilization}
        paths = ManagedPlayerPaths(root, index, player.civilization)
        scope = Scope(
            game=request.authority.game, player=paths.player, epoch=request.authority.epoch, activation="diagnostics"
        )
        if not any(
            seat.civilization == player.civilization and seat.session.player == scope.player for seat in request.seats
        ):
            row.update(memory=dict(_UNAVAILABLE), classifier=dict(_UNAVAILABLE))
            output.append(row)
            continue
        try:
            row["memory"] = memory_projection(
                paths.coordinator / "notebook/memory.sqlite3",
                scope,
                now=now,
                paused=status.mode != "RUNNING" if status is not None else None,
            )
        except _ERRORS:
            row["memory"] = dict(_UNAVAILABLE)
        try:
            reports, deliveries, delivery_complete = classifier_projection(
                paths.coordinator / "notebook/memory.sqlite3", paths.store / "workflow.sqlite3", scope
            )
            reviews = _review_jobs(root, scope, player.civilization)
            summary = classifier_summary([*reports, *deliveries], review_jobs=reviews or ())
            summary["advisoryDelivery"].update(
                status="available" if delivery_complete else "partial",
                coverage="reachable_native_delivery_records"
                if delivery_complete
                else "partial_native_delivery_records",
                complete=delivery_complete,
            )
            summary["unavailable"] = [
                "classifier_completion_timestamps",
                "native_advice_tool_latency",
            ]
            if not delivery_complete:
                summary["unavailable"].append("complete_advisory_delivery_history")
            if reviews is None:
                summary["fullReviewUsage"] = {"status": "unavailable", "reason": "host_accounting_missing"}
            row["classifier"] = summary
        except _ERRORS:
            row["classifier"] = dict(_UNAVAILABLE)
        if config.agents.decisions_enabled and manifest.get("auth_root"):
            try:
                row["additionalSpending"] = _spending(
                    Path(manifest["auth_root"]) / "decisions.sqlite3",
                    game=scope.game,
                    index=index,
                    civilization=player.civilization,
                )
            except _ERRORS:
                row["additionalSpending"] = dict(_UNAVAILABLE)
        output.append(row)
    return output
