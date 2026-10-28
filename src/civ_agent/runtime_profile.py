"""Read recorded latency measurements without contacting games or providers."""

import json
import math
import sqlite3
import time
from collections import Counter, defaultdict
from contextlib import closing
from datetime import datetime
from pathlib import Path

from civ_agent.experiment_status import _advisory_request_reports
from civ_agent.protocol import JsonObject


def sample_summary(values: list[float]) -> JsonObject:
    ordered = sorted(values)
    return {
        "count": len(ordered),
        "sumMs": sum(ordered),
        "meanMs": sum(ordered) / len(ordered) if ordered else None,
        "p50Ms": ordered[math.ceil(len(ordered) * 0.5) - 1] if ordered else None,
        "p95Ms": ordered[math.ceil(len(ordered) * 0.95) - 1] if ordered else None,
    }


def histogram_summary(rows: list[JsonObject]) -> JsonObject:
    count = sum(row["count"] for row in rows)
    total = sum(row["sumMs"] for row in rows)
    buckets: Counter = Counter()
    recorded = 0
    for row in rows:
        if not row["buckets"]:
            continue
        recorded += row["count"]
        for bound, number in zip([*row["boundsMs"], math.inf], row["buckets"], strict=True):
            buckets[bound] += number

    def percentile(fraction):
        if not count or recorded != count:
            return None
        cumulative = 0
        for bound, number in sorted(buckets.items()):
            cumulative += number
            if cumulative >= math.ceil(count * fraction):
                return bound if math.isfinite(bound) else None
        return None

    return {
        "count": count,
        "sumMs": total,
        "meanMs": total / count if count else None,
        "p50UpperBoundMs": percentile(0.5),
        "p95UpperBoundMs": percentile(0.95),
    }


def game_directories(roots: list[Path]) -> list[Path]:
    games = []
    for root in roots:
        root = root.expanduser().resolve()
        manifest = root / "batch.json"
        candidates = (
            [root / "games" / row["id"] for row in json.loads(manifest.read_text())["runs"]]
            if manifest.exists()
            else [root]
        )
        for game in candidates:
            if not (game / "agent-services").is_dir():
                raise ValueError(f"No local player services at {game}")
            if game not in games:
                games.append(game)
    return games


def _restart_time(root: Path) -> int:
    record = json.loads((root / "container.json").read_text())
    if "launchStartedAtMs" in record:
        return record["launchStartedAtMs"]
    status = json.loads((root / "container-status.json").read_text())
    if status.get("status") == "running":
        return round(status["updated_at"] * 1000)
    # Older supervisors did not save launch-start time. Directory modification
    # records creation of the launch's log files, before gameplay begins.
    return round((root / "launches" / record["launch"]).stat().st_mtime * 1000)


def _host_samples(service: Path, since: int, until: int):
    for path in sorted(service.glob("diagnostics.jsonl*")):
        if path.is_symlink() or not path.is_file():
            continue
        with path.open() as stream:
            for line in stream:
                try:
                    row = json.loads(line)
                    if row.get("event") not in {"host_call_completed", "host_call_failed", "host_call_cancelled"}:
                        continue
                    at = round(datetime.fromisoformat(row["time"]).timestamp() * 1000)
                    elapsed = row.get("elapsedMs")
                    if (
                        not since <= at <= until
                        or type(elapsed) not in (int, float)
                        or not math.isfinite(elapsed)
                        or elapsed < 0
                        or row.get("communicationOp") == "read"
                    ):
                        continue
                    yield float(elapsed)
                except (ValueError, KeyError, TypeError):
                    # A running logger may have an incomplete final line.
                    continue


def profile(roots: list[Path], *, since_restart: bool = False) -> JsonObject:
    until = int(time.time() * 1000)
    host, decisions, metrics, windows = [], [], [], []
    jobs = Counter()
    decision_models: dict[str, list[float]] = defaultdict(list)
    decision_fallbacks = Counter()
    for root in game_directories(roots):
        since = _restart_time(root) if since_restart else 0
        windows.append({"game": str(root), "sinceMs": since or None, "untilMs": until})
        for service in sorted((root / "agent-services").iterdir()):
            database = service / "telemetry/accounting.sqlite3"
            if not database.is_file():
                continue
            host.extend(_host_samples(service, since, until))
            with closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True, timeout=5)) as db:
                tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                if "llm_metrics" in tables:
                    metrics.extend(
                        row
                        for (payload,) in db.execute("SELECT payload FROM llm_metrics")
                        for row in [json.loads(payload)]
                        # Histograms cannot be sliced at an arbitrary timestamp.
                        if row["startNs"] >= since * 1_000_000 and row["endNs"] <= until * 1_000_000
                    )
                if "jobs" in tables:
                    for (payload,) in db.execute("SELECT payload FROM jobs"):
                        job = json.loads(payload)
                        timing = job["telemetry"]
                        if (
                            timing["phase"] == "COMPLETED"
                            and timing["startedAtMs"] >= since
                            and (timing.get("endedAtMs") or until + 1) <= until
                        ):
                            jobs["completed"] += 1
                            jobs["elapsedMs"] += timing["elapsedMs"]
                            jobs["modelRequests"] += timing.get("modelRequests", 0)
                if "classifier_calls" in tables:
                    reports = [
                        json.loads(payload)
                        for (payload,) in db.execute("SELECT payload FROM classifier_calls ORDER BY rowid")
                    ]
                    first = {}
                    for row in reports:
                        if row.get("direction") == "advisory_batch" and row.get("requestId"):
                            first.setdefault(row["requestId"], row["atMs"])
                    for row in _advisory_request_reports(reports):
                        at = first.get(row.get("requestId"), row["atMs"])
                        latency = row.get("latencyMs")
                        if (
                            row.get("direction") != "advisory_delivery"
                            and type(at) is int
                            and since <= at <= until
                            and type(latency) in (int, float)
                            and math.isfinite(latency)
                            and latency >= 0
                        ):
                            if row.get("fallback"):
                                decision_fallbacks[row["fallback"]] += 1
                            if row.get("fallback") == "circuit_open":
                                continue  # No model request was submitted.
                            decisions.append(float(latency))
                            decision_models[row.get("model") or "unknown"].append(float(latency))
    grouped: dict[str, list[JsonObject]] = defaultdict(list)
    by_model: dict[tuple[str, str, str], list[JsonObject]] = defaultdict(list)
    for row in metrics:
        grouped[row["metric"]].append(row)
        by_model[(row.get("model") or "unknown", row.get("purpose") or "unknown", row["metric"])].append(row)
    return {
        "windows": windows,
        "hostRoundTrips": sample_summary(host),
        "decisions": {
            **sample_summary(decisions),
            "byModel": {model: sample_summary(values) for model, values in sorted(decision_models.items())},
            "fallbacks": dict(decision_fallbacks),
        },
        "llm": {
            "available": bool(metrics),
            "metrics": {name: histogram_summary(rows) for name, rows in sorted(grouped.items())},
            "byModelAndPurpose": [
                {"model": model, "purpose": purpose, "metric": metric, **histogram_summary(rows)}
                for (model, purpose, metric), rows in sorted(by_model.items())
            ],
        },
        "modelJobs": dict(jobs),
        "notes": [
            "Cumulative call durations overlap across concurrent jobs and games; they are not a wall-time partition.",
            "Host round trips include transport and service overhead, excluding idle communication subscriptions.",
            "API transport timing is not full streaming-response latency. Provider inference is shown only when emitted.",
            "LLM percentiles are histogram bucket upper bounds. Missing metrics remain unavailable, not zero.",
            "Host diagnostics have bounded retention; historical host totals can be incomplete.",
        ],
    }
