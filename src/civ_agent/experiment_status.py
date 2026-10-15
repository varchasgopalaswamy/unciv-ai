"""Read-only operator diagnostics; never construct a live memory/accounting service."""

import json
import time
from collections import Counter

from sqlalchemy import func, inspect, select
from sqlalchemy.exc import SQLAlchemyError

from civ_agent import memory_schema as memory
from civ_agent.decisions_ledger import RATE_VERSION, accounts, requests
from civ_agent.decisions_policy import decisions_values
from civ_agent.diplomacy_decisions import BATCH_SECONDS
from civ_agent.experiment_metrics import summarize_records
from civ_agent.monitoring import read_database
from civ_agent.protocol import JsonObject
from civ_agent.telemetry import calls, classifier_calls, jobs
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


def classifier_status(path):
    if not path.exists():
        return {"status": "waiting_for_first_classification", "calls": 0}
    with read_database(path) as db:
        if not inspect(db).has_table("classifier_calls"):
            return {"status": "waiting_for_first_classification", "calls": 0}
        reports = [json.loads(value) for value in db.scalars(select(classifier_calls.c.payload))]
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
