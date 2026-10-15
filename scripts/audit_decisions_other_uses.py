"""Read-only audit of nonrouting Decisions results and retained delivery evidence.

Only SQLite database/WAL files are read from the auth directory; credential files
are never opened. Private stable copies keep SQLite from creating shared-memory
files beside a source database. No provider calls or game writes are performed.
"""

import argparse
import collections
import copy
import hashlib
import json
import math
import sqlite3
import tempfile
from contextlib import closing, contextmanager
from pathlib import Path
from statistics import median

from civ_agent.advisory_store import active
from civ_agent.decisions_ledger import fingerprint
from civ_agent.protocol import JsonObject
from civ_agent.telemetry import classifier_metadata
from civ_agent.tls import private_directory, write_private, write_private_json

TASKS = ("retrieval", "preservation", "beliefs", "early_review", "city_focus", "city_construction")
QUESTION_TYPES = ("retrieval", "retrieval_include", "retrieval_priority", *TASKS[1:])


def decoded(value):
    try:
        result = json.loads(value)
        return result if isinstance(result, dict) else {}
    except (ValueError, TypeError):
        return {}


def task(answer):
    prefix = answer.get("name", "").split(":", 1)[0]
    return "retrieval" if prefix in {"retrieval_include", "retrieval_priority"} else prefix


def confidence(values):
    known = [v for v in values if type(v) in (float, int) and math.isfinite(v)]
    return {
        "known": len(known),
        "median": median(known) if known else None,
        "min": min(known) if known else None,
        "max": max(known) if known else None,
        "bins": {
            "<0.50": sum(v < 0.5 for v in known),
            "0.50-<0.70": sum(0.5 <= v < 0.7 for v in known),
            "0.70-<0.80": sum(0.7 <= v < 0.8 for v in known),
            "0.80-<0.90": sum(0.8 <= v < 0.9 for v in known),
            ">=0.90": sum(v >= 0.9 for v in known),
        },
    }


def signature(path):
    if not path.exists():
        return None
    stat = path.stat()
    return stat.st_ino, stat.st_size, stat.st_mtime_ns


def stable_copy(source, destination):
    """Reject a copy if a writer changed the source DB or WAL during capture."""
    paths = [source, source.with_name(source.name + "-wal")]
    for _ in range(3):
        before = [signature(path) for path in paths]
        payloads = [path.read_bytes() if mark is not None else None for path, mark in zip(paths, before, strict=True)]
        if before != [signature(path) for path in paths]:
            continue
        meta = {"artifact": str(source), "files": {}}
        for path, payload, mark in zip(paths, payloads, before, strict=True):
            if payload is None:
                continue
            assert mark is not None
            target = destination if path == source else destination.with_name(destination.name + "-wal")
            write_private(target, payload)
            meta["files"]["database" if path == source else "wal"] = {
                "sha256": hashlib.sha256(payload).hexdigest(),
                "bytes": len(payload),
                "mtimeNs": mark[2],
            }
        meta["snapshotFingerprint"] = fingerprint(meta["files"])
        return meta
    raise ValueError("Database changed during read-only capture; retry the audit")


@contextmanager
def snapshot(source, *, preserve=None):
    with tempfile.TemporaryDirectory(prefix="decisions-readonly-") as temporary:
        target = Path(temporary) / "snapshot.sqlite3"
        if preserve is not None:
            preserved = [preserve.with_name(preserve.name + suffix) for suffix in ("", "-wal", "-shm")]
            if any(path.exists() or path.is_symlink() for path in preserved):
                raise ValueError("Preserved snapshot already exists; use a fresh audit output directory")
        meta = stable_copy(source, target)
        if preserve is not None:
            # Preserve the exact captured files without ever opening that path
            # in SQLite. Reads always use a fresh directory, so unrelated WAL
            # or SHM files from an earlier audit cannot affect the snapshot.
            for suffix in ("", "-wal"):
                captured = target.with_name(target.name + suffix)
                if captured.exists():
                    write_private(preserve.with_name(preserve.name + suffix), captured.read_bytes())
        with closing(sqlite3.connect(target.resolve().as_uri() + "?mode=ro", uri=True)) as db:
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA query_only=ON")
            db.execute("BEGIN")
            yield db, meta


def lower_bound(answers):
    counts = {}
    for answer in answers:
        name = answer.get("name", "")
        prefix, _, index = name.partition(":")
        if prefix in QUESTION_TYPES:
            counts[prefix] = max(counts.get(prefix, 0), int(index) + 1 if index.isdigit() else 1)
    return max(1, sum(counts.values()))


def request_summary(rows):
    accepted = [answer for row in rows for answer in row["answers"]]
    exact = [row for row in rows if row.get("questionNames")]
    observed = [
        {
            "name": name,
            "status": {item["name"]: item["status"] for item in row.get("questionResults", [])}.get(name, "unreported"),
        }
        for row in exact
        for name in row["questionNames"]
    ]
    raw = [answer for row in rows for answer in row.get("rawAnswers", []) if answer["type"] == "choice"]
    return {
        "requests": len(rows),
        "fallbacks": dict(collections.Counter(row["fallback"] or "none" for row in rows)),
        "withFallback": sum(bool(row["fallback"]) for row in rows),
        "zeroAcceptedAnswers": sum(not row["answers"] for row in rows),
        "partialFallbackRequests": sum(bool(row["fallback"] and row["answers"]) for row in rows),
        "acceptedAnswers": len(accepted),
        "acceptedAnswerCountHistogram": dict(collections.Counter(str(len(row["answers"])) for row in rows)),
        "minimumSubmittedQuestions": sum(
            len(row["questionNames"]) if row.get("questionNames") else lower_bound(row["answers"]) for row in rows
        ),
        "requestsWithExactQuestionNames": len(exact),
        "requestsWithoutExactQuestionNames": len(rows) - len(exact),
        "exactSubmittedQuestions": len(observed),
        "knownRequestsWithAllAcceptedAnswers": sum(
            {answer["name"] for answer in row["answers"]} == set(row["questionNames"]) for row in exact
        ),
        "knownRequestsWithPartialAnswers": sum(
            0 < len({answer["name"] for answer in row["answers"]}) < len(row["questionNames"]) for row in exact
        ),
        "questionDiagnostics": {
            "denominator": "Retained exact submitted questionNames only; missing status is unreported, never refusal.",
            "statuses": dict(collections.Counter(item["status"] for item in observed)),
            "byTask": {
                name: {
                    "submittedQuestions": sum(task(item) == name for item in observed),
                    "statuses": dict(collections.Counter(item["status"] for item in observed if task(item) == name)),
                    "retainedRawChoices": dict(collections.Counter(a["choice"] for a in raw if task(a) == name)),
                    "retainedRawConfidence": confidence([a.get("confidence") for a in raw if task(a) == name]),
                }
                for name in TASKS
            },
            "byQuestionType": {
                name: {
                    "submittedQuestions": sum(item["name"].split(":", 1)[0] == name for item in observed),
                    "statuses": dict(
                        collections.Counter(
                            item["status"] for item in observed if item["name"].split(":", 1)[0] == name
                        )
                    ),
                    "retainedRawConfidence": confidence(
                        [a.get("confidence") for a in raw if a["name"].split(":", 1)[0] == name]
                    ),
                }
                for name in QUESTION_TYPES
            },
        },
        "acceptedByTask": {
            name: {
                "answers": sum(task(a) == name for a in accepted),
                "choices": dict(collections.Counter(a["choice"] for a in accepted if task(a) == name)),
                "acceptedConfidence": confidence([a.get("confidence") for a in accepted if task(a) == name]),
            }
            for name in TASKS
        },
        "acceptedByQuestionType": {
            name: {
                "answers": sum(a.get("name", "").split(":", 1)[0] == name for a in accepted),
                "acceptedConfidence": confidence(
                    [a.get("confidence") for a in accepted if a.get("name", "").split(":", 1)[0] == name]
                ),
            }
            for name in QUESTION_TYPES
        },
    }


def size_band(tokens):
    if type(tokens) is not int:
        return "unknown"
    for limit, name in ((1000, "<1000"), (2000, "1000-1999"), (5000, "2000-4999"), (10000, "5000-9999")):
        if tokens < limit:
            return name
    return ">=10000"


def ledger_inventory(path, output):
    with snapshot(path, preserve=output / "source-ledger.sqlite3") as (db, provenance):
        accounts = {
            row["principal"]: dict(row)
            for row in db.execute("SELECT principal,game,player,ceiling,settings FROM accounts")
        }
        rows = []
        for saved in db.execute(
            "SELECT principal,id,reserved,spent,input_tokens,result,rate_version,created_ms FROM requests ORDER BY principal,id"
        ):
            result = decoded(saved["result"])
            diagnostics = classifier_metadata("advisory_batch", result)
            account = accounts[saved["principal"]]
            settings = decoded(account["settings"])
            rows.append(
                {
                    "principal": saved["principal"],
                    "requestId": saved["id"],
                    "game": account["game"],
                    "player": account["player"],
                    "createdMs": saved["created_ms"],
                    "fallback": result.get("fallback"),
                    "completed": saved["result"] is not None,
                    "answers": [
                        {k: a[k] for k in ("name", "type", "choice", "confidence") if k in a}
                        for a in result.get("answers", [])
                    ],
                    "inputTokens": saved["input_tokens"],
                    "spentNanodollars": saved["spent"],
                    "reservedNanodollars": saved["reserved"],
                    "policyVersion": result.get("policyVersion"),
                    "threshold": settings.get("decisions_min_confidence"),
                    "budgetUsd": settings.get("additional_decisions_budget_usd"),
                    "rawAnswersRetained": isinstance(result.get("rawAnswers"), list),
                    "rawAnswers": diagnostics["rawAnswers"],
                    "questionNames": diagnostics["questionNames"]
                    if isinstance(result.get("questionNames"), list)
                    and len(diagnostics["questionNames"]) == len(result["questionNames"])
                    else None,
                    "questionResults": diagnostics["questionResults"],
                    "replayEvidenceRetained": isinstance(result.get("replayEvidence"), dict),
                    "originalRequestRetained": False,
                }
            )
    return rows, accounts, provenance


def optional_evidence(db, saved):
    counts = collections.Counter()
    for key, reference in saved.get("records", {}).items():
        record = reference if "category" in reference else None
        if record is None:
            stored = db.execute(
                "SELECT category,body FROM records WHERE key=? AND version=?", (key, reference.get("version"))
            ).fetchone()
            if stored is None or stored["category"] not in {"belief", "note", "strategy"}:
                continue
            body = decoded(stored["body"])
            for removed in reference.get("removed", []):
                body.pop(removed, None)
            body.update(reference.get("changes", {}))
            record = {"category": stored["category"], "body": body}
        category = record.get("category")
        if category not in {"belief", "note", "strategy"}:
            continue
        counts[category + "Records"] += 1
        if active(record, saved.get("gameTurn")):
            counts[category + "Active"] += 1
            if category in {"belief", "note"} and key not in saved.get("contextIds", []):
                counts["optionalOutsideSelectedContext"] += 1
    return dict(counts)


def memory_inventory(root, accounts):
    identities = {(a["game"], a["player"]): principal for principal, a in accounts.items()}
    jobs, attempts, provenance = [], [], []
    for path in sorted(root.glob("*/games/*/agent-services/*/civilization/memory.sqlite3")):
        game_root = path.parents[3]
        container = (
            decoded((game_root / "container.json").read_text()) if (game_root / "container.json").exists() else {}
        )
        player, game = path.parent.parent.name, container.get("id")
        principal = identities.get((game, player))
        with snapshot(path) as (db, meta):
            provenance.append(meta)
            advisory_meta = db.execute("SELECT value FROM meta WHERE key='advisories'").fetchone()
            advisory_meta = decoded(advisory_meta[0]) if advisory_meta else {}
            for identifier, result in advisory_meta.get("attempts", {}).items():
                attempts.append(
                    {
                        "principal": principal,
                        "game": game,
                        "player": player,
                        "requestId": identifier,
                        "fallback": result.get("fallback"),
                        "answers": [
                            {k: a[k] for k in ("name", "type", "choice", "confidence") if k in a}
                            for a in result.get("answers", [])
                        ],
                    }
                )
            for row in db.execute("SELECT rowid,id,purpose,game_turn,snapshot FROM jobs ORDER BY rowid"):
                saved = decoded(row["snapshot"])
                notices = saved.get("advisories", {})
                if not notices:
                    continue
                status = notices.get("cityAdviceStatus", {})
                city = notices.get("cityRecommendations", [])
                delivered = {
                    "retrieval": len(saved.get("advisoryRanking", {})),
                    "preservation": len(notices.get("preservationCandidates", [])),
                    "beliefs": len(notices.get("beliefNotices", [])),
                    "city_focus": sum("focus" in item for item in city),
                    "city_construction": sum("construction" in item for item in city),
                }
                jobs.append(
                    {
                        "artifact": str(path),
                        "principal": principal,
                        "game": game,
                        "player": player,
                        "job": row["id"],
                        "jobRowId": row["rowid"],
                        "purpose": row["purpose"],
                        "gameTurn": row["game_turn"] if row["game_turn"] is not None else saved.get("gameTurn"),
                        "requestId": notices.get("requestId"),
                        "cityAdviceStatus": copy.deepcopy(status),
                        "delivered": delivered,
                        "optionalMemoryEvidence": optional_evidence(db, saved),
                    }
                )
    return jobs, attempts, provenance


def audit(benchmarks, ledger, output):
    output = private_directory(output)
    ledger_rows, accounts, ledger_source = ledger_inventory(ledger, output)
    jobs, attempts, sources = memory_inventory(Path(benchmarks), accounts)
    ledger_map = {(r["principal"], r["requestId"]): r for r in ledger_rows}
    attempt_map = {(r["principal"], r["requestId"]): r for r in attempts}
    groups = collections.defaultdict(list)
    for job in jobs:
        if job["requestId"]:
            groups[(job["principal"], job["requestId"])].append(job)
    deliveries: list[JsonObject] = []
    for key, rows in groups.items():
        ledger_row = ledger_map.get(key)
        if ledger_row is None:
            continue
        accepted = collections.Counter(task(a) for a in ledger_row["answers"])
        delivered = {
            name: max(row["delivered"].get(name, 0) for row in rows) for name in TASKS if name != "early_review"
        }
        deliveries.append(
            {
                "principal": key[0],
                "requestId": key[1],
                "ledgerFallback": ledger_row["fallback"],
                "serviceFallback": attempt_map.get(key, {}).get("fallback"),
                "acceptedByTask": dict(accepted),
                "maxRetainedDeliveryByTask": delivered,
                "jobsWithRetainedView": len(rows),
            }
        )
    transitions = collections.Counter(
        (r["fallback"] or "none", attempt_map[(r["principal"], r["requestId"])]["fallback"] or "none")
        for r in ledger_rows
        if (r["principal"], r["requestId"]) in attempt_map
    )
    partial = [row for row in deliveries if row["ledgerFallback"] and sum(row["acceptedByTask"].values())]
    per_size = {
        band: request_summary([r for r in ledger_rows if size_band(r["inputTokens"]) == band])
        for band in sorted({size_band(r["inputTokens"]) for r in ledger_rows})
    }
    capture = collections.Counter((j["purpose"], j["cityAdviceStatus"].get("reason", "unspecified")) for j in jobs)
    summary = {
        "sourceLedger": ledger_source,
        "accounts": len(accounts),
        "accountsWithRequests": len({row["principal"] for row in ledger_rows}),
        "accountGamesWithRequests": len({row["game"] for row in ledger_rows}),
        "ledger": request_summary(ledger_rows),
        "byReportedInputTokens": per_size,
        "memory": {
            "databases": len(sources),
            "databasesWithAdvisoryViews": len({job["artifact"] for job in jobs}),
            "gamesWithAdvisoryViews": len({job["game"] for job in jobs}),
            "jobsWithAdvisoryViews": len(jobs),
            "retainedAttempts": len(attempts),
            "attemptFallbacks": dict(collections.Counter(r["fallback"] or "none" for r in attempts)),
            "ledgerToServiceFallback": [
                {"ledger": a, "service": b, "requests": count} for (a, b), count in sorted(transitions.items())
            ],
            "cityCaptureByPurposeAndReason": [
                {"purpose": a, "reason": b, "jobs": count} for (a, b), count in sorted(capture.items())
            ],
            "capturedCitySlots": sum(j["cityAdviceStatus"].get("capturedCities", 0) for j in jobs),
            "retainedDeliveredSlotsAcrossJobs": {
                name: sum(j["delivered"].get(name, 0) for j in jobs) for name in TASKS if name != "early_review"
            },
            "jobsWithOptionalMemoryCandidates": sum(
                any(j["optionalMemoryEvidence"].get(name, 0) for name in ("beliefActive", "noteActive")) for j in jobs
            ),
            "jobsWithOptionalCandidatesOutsideContext": sum(
                j["optionalMemoryEvidence"].get("optionalOutsideSelectedContext", 0) > 0 for j in jobs
            ),
            "optionalEvidenceAcrossRetainedJobs": {
                name: sum(job["optionalMemoryEvidence"].get(name, 0) for job in jobs)
                for name in (
                    "noteRecords",
                    "noteActive",
                    "beliefRecords",
                    "beliefActive",
                    "optionalOutsideSelectedContext",
                )
            },
        },
        "partialFallbackDelivery": {
            "requestsWithRetainedView": len(partial),
            "acceptedByTask": dict(
                sum((collections.Counter(r["acceptedByTask"]) for r in partial), collections.Counter())
            ),
            "maxRetainedDeliveryByTask": {
                name: sum(r["maxRetainedDeliveryByTask"].get(name, 0) for r in partial)
                for name in TASKS
                if name != "early_review"
            },
        },
        "limitations": [
            "Older ledger rows retain accepted answers only; their rejected choices, raw confidence and probability vectors cannot be reconstructed. Current v3 rows can retain typed rawAnswers, questionResults, exact questionNames and replayEvidence; the auditor reports retention per row and never invents missing answers. Accepted confidence distributions remain truncated by policy thresholds.",
            "Exact submitted question counts and task-specific status rates use only retained questionNames. Other rows contribute a lower bound from accepted indices and the nonempty-request contract to minimumSubmittedQuestions. Missing diagnostics are unreported, not refusals.",
            "Reported input-token size is observed request size; accepted-answer count is not submitted batch question count.",
            "Retained job views show what was made available, not whether the player model read or followed it. Reused views can repeat the same request. Per-request maxima avoid counting repeated jobs as separate accepted answer slots.",
            "Missing retained delivery evidence is not proof that a once-delivered answer was discarded; snapshots can mutate and unreachable branches can be absent.",
            "Historical native city alternatives and frozen advisory evidence were not retained. Current replayEvidence retention is reported, but this count-only auditor does not adjudicate labels, native equivalence, calibration quality or gameplay benefit.",
            "For v3 retrieval, include and priority are separate question types but delivered rankings count memory candidates; those denominators must not be subtracted from each other.",
        ],
    }
    for name, value in (
        ("ledger-requests", ledger_rows),
        ("memory-jobs", jobs),
        ("memory-attempts", attempts),
        ("request-delivery", deliveries),
        ("source-snapshots", sources),
        ("summary", summary),
    ):
        write_private_json(output / (name + ".json"), value)
    return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--benchmarks-root", type=Path, default=Path("benchmarks"))
    parser.add_argument(
        "--ledger", type=Path, default=Path("/home/vgopalas/.local/state/unciv-host/auth/decisions.sqlite3")
    )
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    summary = audit(args.benchmarks_root, args.ledger, args.output_dir)
    # Counts only; conversation text and credentials never enter stdout.
    print(
        json.dumps(
            {
                "accounts": summary["accounts"],
                "ledgerRequests": summary["ledger"]["requests"],
                "acceptedByTask": {k: v["answers"] for k, v in summary["ledger"]["acceptedByTask"].items()},
                "jobsWithAdvisoryViews": summary["memory"]["jobsWithAdvisoryViews"],
                "capturedCitySlots": summary["memory"]["capturedCitySlots"],
                "partialFallbackDelivery": summary["partialFallbackDelivery"],
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
