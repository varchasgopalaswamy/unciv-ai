"""Read-only operator diagnostics; never construct a live memory/accounting service."""

import json
import math
import time
from collections import Counter
from statistics import median

from sqlalchemy import func, inspect, literal_column, select
from sqlalchemy.exc import SQLAlchemyError

from civ_agent import memory_schema as memory
from civ_agent.decisions_ledger import RATE_VERSION, accounts, requests
from civ_agent.decisions_policy import decisions_values
from civ_agent.diplomacy_decisions import (
    BATCH_SECONDS,
    CHOICES,
    POLICY_VERSION,
    QUESTION_CHOICES,
    two_question_answers,
)
from civ_agent.experiment_metrics import summarize_records
from civ_agent.monitoring import read_database
from civ_agent.protocol import JsonObject
from civ_agent.telemetry import calls, classifier_calls, classifier_metadata, jobs
from civ_agent.turn_archive import civilization_directory_name


def memory_status(path, *, now):
    if not path.exists():
        return {"status": "waiting_for_first_observation"}
    with read_database(path) as db:
        meta = {key: json.loads(value) for key, value in db.execute(select(memory.meta)).all()}
        if meta.get("format") != memory.FORMAT_VERSION:
            return {"status": "unavailable", "reason": "unsupported_memory_format"}
        records = [
            {"category": row["category"], "body": json.loads(row["body"])}
            for row in db.execute(
                select(memory.records)
                .select_from(memory.current_records)
                .where(memory.records.c.category.in_({"guidance", "routing", "delivery"}))
                .order_by(memory.record_order)
            ).mappings()
        ]
        exposures = [r["body"] for r in records if r["category"] == "guidance" and r["body"]["type"] == "exposure"]
        latest = exposures[-1] if exposures else None
        advice = (
            {
                "status": "available" if latest["available"] else "unavailable",
                "epoch": latest["epoch"],
                "revision": latest.get("revision"),
                "recommendationsExposed": len(latest["recommendations"]),
                "source": latest.get("source", "native"),
                "unavailableReasons": latest.get("limitations", []) if not latest["available"] else [],
            }
            if latest
            else {"status": "waiting_for_first_observation"}
        )
        drafts = [r["body"] for r in records if r["category"] == "delivery"]
        outstanding = [d for d in drafts if d["status"] in {"classifying", "queued", "sending", "omitted"}]
        waiting = [d for d in outstanding if d["status"] != "omitted"]
        states = meta.get("routing:events", {})
        incoming = [s for s in states.values() if not s.get("attempted") and not s.get("completed")]
        ready = sum(
            s.get("choice") == "respond_now" or s.get("choice") == "batch" and s["deadline"] <= now for s in incoming
        )
        return {
            "status": "ready",
            "epoch": meta.get("epoch"),
            "paused": meta.get("paused", False),
            "advice": advice,
            "routing": {
                "incomingReady": ready,
                "incomingUnclassified": sum(not s.get("choice") for s in incoming),
                "incomingUnclassifiedOverdue": sum(
                    not s.get("choice") and s.get("receivedAt", now) + BATCH_SECONDS <= now for s in incoming
                ),
                "outstanding": dict(Counter(d["status"] for d in outstanding)),
                "oldestWaitSeconds": max((max(0, now - d["createdAt"]) for d in waiting), default=0),
                "overdueDeliveries": sum(d["deadline"] <= now for d in waiting),
                "deliveries": [
                    {"deliveryId": d["deliveryId"], "status": d["status"], "deadline": d["deadline"]}
                    for d in outstanding
                ],
            },
            "metrics": summarize_records(records, states),
            "additionalDecisions": meta.get("advisories", {}).get("accounting"),
            "cityAdvice": meta.get("advisories", {}).get("cityDelivery"),
            "fullStrategicReviews": {
                "completed": db.scalar(
                    select(func.count()).select_from(memory.reviews).where(memory.reviews.c.status == "COMPLETED")
                )
                if inspect(db).has_table("reviews")
                else 0
            },
        }


def routing_classifier_summary(reports) -> JsonObject:
    """Classifications are paid decisions, distinct from advisory/bypass work."""
    classified = [
        report
        for report in reports
        if report.get("direction") in CHOICES
        and report.get("fallback") not in {"protected", "immediate", "routing_deadline"}
    ]
    summary = _routing_sample(classified)
    summary["excluded"] = {
        "advisoryBatch": sum(report.get("direction") == "advisory_batch" for report in reports),
        "protectedOrDeadlineBypass": sum(
            report.get("direction") in CHOICES
            and report.get("fallback") in {"protected", "immediate", "routing_deadline"}
            for report in reports
        ),
        "otherDirections": sum(report.get("direction") not in {*CHOICES, "advisory_batch"} for report in reports),
    }
    summary["byDirection"] = {
        direction: _routing_sample([report for report in classified if report["direction"] == direction])
        for direction in CHOICES
    }
    return summary


def _routing_sample(reports) -> JsonObject:
    raw = Counter()
    effective = Counter()
    for report in reports:
        choices = CHOICES[report["direction"]]
        for key, counts in (("rawChoice", raw), ("choice", effective)):
            if isinstance(report.get(key), str) and report[key] in choices:
                counts[report[key]] += 1
    return {
        "calls": len(reports),
        "currentPolicyVersion": POLICY_VERSION,
        "policyMismatch": sum(report.get("policyVersion") != POLICY_VERSION for report in reports),
        "rawDispositions": dict(raw),
        "rawDispositionUnknown": len(reports) - sum(raw.values()),
        "effectiveDispositions": dict(effective),
        "effectiveDispositionUnknown": len(reports) - sum(effective.values()),
        "genuineOverrides": sum(report.get("overridden") is True for report in reports),
        "overrideUnknown": sum(type(report.get("overridden")) is not bool for report in reports),
        "lowConfidenceImmediate": sum(
            report.get("lowConfidence") is True
            and report.get("overridden") is False
            and report.get("rawChoice") == report.get("choice") == next(iter(CHOICES[report["direction"]]))
            for report in reports
        ),
        "lowConfidence": sum(report.get("lowConfidence") is True for report in reports),
        "lowConfidenceUnknown": sum(type(report.get("lowConfidence")) is not bool for report in reports),
        "confidence": _confidence_summary([report.get("confidence") for report in reports]),
        "rawChoiceConfidence": _confidence_summary([report.get("rawChoiceConfidence") for report in reports]),
        "confidenceByPolicy": {
            policy: _confidence_summary(
                [report.get("confidence") for report in reports if (report.get("policyVersion") or "unknown") == policy]
            )
            for policy in sorted({report.get("policyVersion") or "unknown" for report in reports})
        },
        "questionBasis": dict(Counter(report["questionBasis"] for report in reports if report.get("questionBasis"))),
        "perQuestion": _question_summaries(reports),
    }


def _confidence_summary(values):
    known = [value for value in values if type(value) in (int, float) and math.isfinite(value) and 0 <= value <= 1]
    return {
        "known": len(known),
        "unknown": len(values) - len(known),
        "mean": sum(known) / len(known) if known else None,
        "median": median(known) if known else None,
        "min": min(known) if known else None,
        "max": max(known) if known else None,
        "bins": {
            "<0.50": sum(value < 0.5 for value in known),
            "0.50-<0.70": sum(0.5 <= value < 0.7 for value in known),
            "0.70-<0.80": sum(0.7 <= value < 0.8 for value in known),
            "0.80-<0.90": sum(0.8 <= value < 0.9 for value in known),
            ">=0.90": sum(value >= 0.9 for value in known),
        },
    }


def _question_summaries(reports):
    questions = {name: [] for choices in QUESTION_CHOICES.values() for name in choices}
    for report in reports:
        if report.get("direction") not in QUESTION_CHOICES or report.get("policyVersion") != POLICY_VERSION:
            continue
        for answer in two_question_answers(report.get("rawAnswers"), report["direction"]) or []:
            questions[answer["name"]].append(answer)
    return {
        name: {
            "calls": len(answers),
            "choices": dict(Counter(answer["choice"] for answer in answers if answer.get("type") == "choice")),
            "refusals": sum(answer.get("type") == "refusal" for answer in answers),
            "confidence": _confidence_summary([answer.get("confidence") for answer in answers]),
        }
        for name, answers in questions.items()
    }


def _advisory_delivery_summary(reports):
    rows = [row for report in reports for row in report.get("deliveryResults", [])]
    prefixes = {row["name"].split(":", 1)[0] for row in rows}
    return {
        "events": len(reports),
        "questionObservations": len(rows),
        "latest": max(reports, key=lambda report: report["atMs"]) if reports else None,
        "byTask": {
            prefix: {
                "questionObservations": sum(row["name"].split(":", 1)[0] == prefix for row in rows),
                "statuses": dict(Counter(row["status"] for row in rows if row["name"].split(":", 1)[0] == prefix)),
            }
            for prefix in sorted(prefixes)
        },
    }


def _advisory_request_reports(reports):
    """Fold observations of the same immutable advisory request, not routing calls."""
    groups = {}
    for index, report in enumerate(reports):
        identifier = report.get("requestId")
        valid = (
            isinstance(identifier, str) and len(identifier) == 64 and all(c in "0123456789abcdef" for c in identifier)
        )
        key = ("advisory", identifier) if report.get("direction") == "advisory_batch" and valid else ("event", index)
        groups.setdefault(key, []).append((index, report))
    unresolved = {"broker_unavailable", "malformed_broker_output", "completion_unknown"}
    folded = []
    for observations in groups.values():
        observations.sort(key=lambda item: (item[1]["atMs"], item[0]))
        if observations[0][1].get("direction") != "advisory_batch":
            folded.append(observations[0][1])
            continue
        resolved = [item for item in observations if item[1].get("fallback") not in unresolved]
        selected = dict((resolved or observations)[-1][1])
        compatible = [item for item in observations if _same_advisory_identity(selected, item[1])]
        if len(compatible) != len(observations):
            selected["observationIdentityConflict"] = True
        # A receipt can reveal previously unknown provider usage. It never adds
        # another charge, and a later empty receipt cannot erase known usage.
        for field in ("usage", "latencyMs"):
            known = [item for item in compatible if item[1].get(field) is not None and item[1].get(field) != {}]
            preferred = [item for item in known if item[1].get("fallback") not in unresolved]
            if known:
                selected[field] = (preferred or known)[-1][1][field]
        folded.append(selected)
    return folded


def _same_advisory_identity(first, second):
    return all(
        first.get(key) in (None, []) or second.get(key) in (None, []) or first[key] == second[key]
        for key in ("model", "policyVersion", "inputHash", "promptHash", "questionNames")
    )


def classifier_status(path) -> JsonObject:
    if not path.exists():
        return {"status": "waiting_for_first_classification", "calls": 0}
    with read_database(path) as db:
        if not inspect(db).has_table("classifier_calls"):
            return {"status": "waiting_for_first_classification", "calls": 0}
        reports = [
            {
                "direction": report.get("direction"),
                "atMs": report["atMs"],
                **classifier_metadata(report.get("direction"), report),
            }
            for value in db.scalars(select(classifier_calls.c.payload).order_by(literal_column("rowid")))
            for report in [json.loads(value)]
        ]
        deliveries = [report for report in reports if report.get("direction") == "advisory_delivery"]
        reports = [report for report in reports if report.get("direction") != "advisory_delivery"]
        advisory_observations = [report for report in reports if report.get("direction") == "advisory_batch"]
        reconciliations = [report for report in advisory_observations if report.get("brokerReconciliation") is True]
        reports = _advisory_request_reports(reports)
        latest = max(reports, key=lambda r: r["atMs"]) if reports else None
        latency = [r["latencyMs"] for r in reports if isinstance(r.get("latencyMs"), (int, float))]
        usage = Counter()
        for report in reports:
            for key, value in (report.get("usage") or {}).items():
                if type(value) is int and value >= 0:
                    usage[key] += value
        advice_latency = db.scalar(
            select(calls.c.elapsed)
            .where(
                calls.c["keys"].in_(
                    [json.dumps([prefix + name]) for prefix in ("tool:", "startup:") for name in ("native_advice",)]
                )
            )
            .order_by(calls.c.started.desc())
            .limit(1)
        )
        review_jobs = (
            [json.loads(p) for p in db.scalars(select(jobs.c.payload)) if json.loads(p).get("purpose") == "review"]
            if inspect(db).has_table("jobs")
            else []
        )
        return {
            "status": "degraded"
            if latest and latest.get("fallback")
            else "active"
            if latest
            else "waiting_for_first_classification",
            "calls": len(reports),
            "fallbacks": dict(Counter(r["fallback"] for r in reports if r.get("fallback"))),
            "routing": routing_classifier_summary(reports),
            "advisoryDelivery": _advisory_delivery_summary(deliveries),
            "advisoryObservations": {
                "events": len(advisory_observations),
                "requests": sum(report.get("direction") == "advisory_batch" for report in reports),
                "reconciliations": len(reconciliations),
                "identityConflicts": sum(report.get("observationIdentityConflict") is True for report in reports),
                "fallbacks": dict(Counter(r["fallback"] for r in advisory_observations if r.get("fallback"))),
                "latestReconciliation": reconciliations[-1] if reconciliations else None,
            },
            "latest": latest,
            "averageLatencyMs": sum(latency) / len(latency) if latency else None,
            "usage": dict(usage),
            "lastAdviceCallLatencyMs": advice_latency,
            "fullReviewUsage": {
                "jobs": len(review_jobs),
                "usage": dict(
                    Counter(
                        {
                            key: sum(r.get("usage", {}).get(key, 0) for r in review_jobs)
                            for key in (
                                "input_tokens",
                                "output_tokens",
                                "cached_input_tokens",
                                "reasoning_output_tokens",
                            )
                        }
                    )
                ),
            },
        }


def experiment_status(root, config, manifest, *, now=None):
    now = time.time() if now is None else now
    result: JsonObject = {"settings": manifest.get("experiments", {}), "players": []}
    if config is None:
        return result
    result["settings"] = {
        **result["settings"],
        "native_advice_enabled": config.agents.native_advice_enabled,
        "decisions_enabled": config.agents.decisions_enabled,
        **decisions_values(config.agents),
    }
    if config.agents.decisions_enabled:
        from pathlib import Path

        from civ_agent.container_supervisor import routing_broker_health

        auth_root = manifest.get("auth_root")
        result["broker"] = (
            routing_broker_health(Path(auth_root))
            if auth_root
            else {"status": "unavailable", "reason": "auth_directory_missing"}
        )
        result["preflight"] = manifest.get("routingPreflight")
    if not (config.agents.native_advice_enabled or config.agents.decisions_enabled):
        return result
    for index, player in enumerate(config.agent_players):
        directory = root / "agent-services" / civilization_directory_name(player.civilization)
        row = {"player": player.name, "civilization": player.civilization}
        if config.agents.decisions_enabled and manifest.get("auth_root") and manifest.get("id"):
            from pathlib import Path

            ledger = Path(manifest["auth_root"]) / "decisions.sqlite3"
            if ledger.exists():
                try:
                    with read_database(ledger) as db:
                        principal = f"{manifest['id']}-{index}"
                        account = (
                            db.execute(select(accounts).where(accounts.c.principal == principal)).mappings().first()
                        )
                        entries = db.execute(select(requests).where(requests.c.principal == principal)).mappings().all()
                        row["additionalSpending"] = {
                            "rateVersion": RATE_VERSION,
                            "limitKind": "local_estimated_spending_ceiling",
                            "ceilingNanodollars": account["ceiling"] if account else None,
                            "spentNanodollars": sum(r["spent"] for r in entries),
                            "reservedNanodollars": sum(r["reserved"] for r in entries),
                            "inputTokens": sum(r["input_tokens"] or 0 for r in entries),
                            "requests": len(entries),
                        }
                except (OSError, ValueError, KeyError, SQLAlchemyError):
                    row["additionalSpending"] = {"status": "unavailable"}
        for name, read, path in (
            ("memory", lambda p: memory_status(p, now=now), directory / "civilization/memory.sqlite3"),
            ("classifier", classifier_status, directory / "telemetry/accounting.sqlite3"),
        ):
            try:
                row[name] = read(path)
            except (OSError, ValueError, KeyError, TypeError, AttributeError, SQLAlchemyError):
                row[name] = {"status": "unavailable", "reason": "diagnostics_unreadable"}
        result["players"].append(row)
    return result
