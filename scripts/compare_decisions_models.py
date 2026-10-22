"""Compare fixed project Decisions prompts without changing runtime settings.

The labeled fixtures contain provisional assistant judgments. Production replays
measure agreement with recorded Luna answers, not correctness. Each completed
request is checkpointed; rerunning the exact command reuses captured responses.
"""

import argparse
import asyncio
import json
import math
import time
from collections import Counter, defaultdict
from datetime import UTC, datetime
from pathlib import Path
from statistics import median

import aiohttp

from civ_agent.advisory_answers import parse_answers, typed_answer
from civ_agent.advisory_replay import load_cases
from civ_agent.decisions_backends import BACKENDS
from civ_agent.decisions_broker import read_decisions_key
from civ_agent.decisions_ledger import estimate_tokens, fingerprint
from civ_agent.decisions_policy import DecisionsSettings
from civ_agent.decisions_tasks import TASK_VERSION
from civ_agent.decisions_tasks import request_body as advisory_body
from civ_agent.diplomacy_decisions import POLICY_VERSION
from civ_agent.diplomacy_decisions import request_body as routing_body
from civ_agent.diplomacy_replay import evaluate, load_fixture
from civ_agent.protocol import JsonObject
from civ_agent.tls import private_directory, write_private_json
from scripts.audit_decisions_other_uses import snapshot

MODEL = BACKENDS["luna"].model


def local_request(body) -> JsonObject:
    """Change the wire format only; preserve input text and every description."""
    return {
        "model": "Winnow-12B",
        "state": body["input"],
        "questions": {
            q["name"]: {
                "type": "choice",
                "instructions": q["instructions"],
                "criteria": {c["value"]: c["description"] for c in q["choices"]},
            }
            for q in body["questions"]
        },
        "winnow": {"diagnostics": True},
    }


def local_answers(response) -> JsonObject:
    """Adapt typed Winnow answers; never substitute max probability for confidence."""
    if not isinstance(response, dict) or not isinstance(response.get("answers"), dict):
        return {"answers": []}
    answers = []
    for name, value in response["answers"].items():
        if not isinstance(value, dict):
            answers.append({"name": name})
            continue
        answer = {"name": name, **{k: value[k] for k in ("type", "choice", "confidence") if k in value}}
        if isinstance(value.get("probabilities"), dict):
            answer["probabilities"] = [{"value": k, "probability": p} for k, p in value["probabilities"].items()]
        answers.append(answer)
    return {"answers": answers}


def fixture_cases() -> tuple[list[JsonObject], JsonObject]:
    advisory = load_cases("tests/fixtures/decisions-advisory-replay.json")
    routing = load_fixture("tests/fixtures/diplomacy-routing-calibration.json")["cases"]
    settings = DecisionsSettings(decisions_enabled=True, decisions_backend="luna")
    cases = [
        {"id": "advisory/" + c["id"], "kind": "advisory", "case": c, "body": advisory_body(c["evidence"], settings)}
        for c in advisory
    ]
    cases += [
        {
            "id": "routing/" + c["id"],
            "kind": "routing",
            "case": c,
            "body": routing_body(c["direction"], c["bundle"], backend="luna"),
        }
        for c in routing
        if not c.get("protected")
    ]
    return cases, {"protectedRoutingCasesSkipped": sum(bool(c.get("protected")) for c in routing)}


def production_cases(path, limit) -> tuple[list[JsonObject], JsonObject]:
    """Read a stable copy; verify exact current input and prompt hashes."""
    candidates, counts = [], Counter()
    settings = DecisionsSettings(decisions_enabled=True, decisions_backend="luna")
    with snapshot(path) as (db, provenance):
        for saved in db.execute(
            "SELECT result,input_tokens FROM requests WHERE result IS NOT NULL ORDER BY created_ms DESC LIMIT 20000"
        ):
            result = json.loads(saved["result"])
            counts["inspected"] += 1
            if not result.get("rawAnswers") or result.get("policyVersion") != TASK_VERSION:
                continue
            try:
                body = advisory_body(result["replayEvidence"], settings)
            except (KeyError, ValueError):
                counts["reconstructionRejected"] += 1
                continue
            if (
                result.get("model") != MODEL
                or result.get("promptHash") != fingerprint(body["questions"])
                or result.get("inputHash") != fingerprint(json.loads(body["input"]))
                or result.get("questionNames") != [q["name"] for q in body["questions"]]
            ):
                counts["identityRejected"] += 1
                continue
            counts["eligible"] += 1
            candidates.append(
                {
                    "id": "production/" + fingerprint(body),
                    "kind": "production",
                    "body": body,
                    "reference": result,
                    "recordedInputTokens": saved["input_tokens"],
                }
            )
    # Stratify by task and size; retain both size extremes in each task family.
    buckets = defaultdict(list)
    for row in candidates:
        task = tuple(sorted({q["name"].split(":")[0] for q in row["body"]["questions"]}))
        buckets[task].append(row)
    queues = []
    for rows in buckets.values():
        rows.sort(key=lambda c: c["recordedInputTokens"] or 0)
        queues.append(
            [rows[i] for pair in zip(range(len(rows)), reversed(range(len(rows))), strict=True) for i in pair]
        )
    selected, seen = [], set()
    while queues and len(selected) < limit:
        remaining = []
        for queue in queues:
            while queue and queue[0]["id"] in seen:
                queue.pop(0)
            if queue and len(selected) < limit:
                row = queue.pop(0)
                selected.append(row)
                seen.add(row["id"])
            if queue:
                remaining.append(queue)
        queues = remaining
    return selected, {"counts": dict(counts), "source": provenance, "selected": len(selected)}


async def request(session, endpoint, body, timeout, key=None) -> JsonObject:
    started = time.monotonic()
    headers = {"Authorization": "Bearer " + key} if key else {}
    result = {}
    try:
        async with session.post(
            endpoint, json=body, headers=headers, timeout=aiohttp.ClientTimeout(total=timeout)
        ) as reply:
            result["httpStatus"] = reply.status
            raw = await reply.json()
            result["response"] = raw
            if reply.status >= 400:
                result["error"] = "http_error"
    except TimeoutError:
        result["error"] = "timeout"
    except (aiohttp.ClientError, OSError, ValueError) as error:
        result["error"] = type(error).__name__
    return {**result, "latencyMs": round((time.monotonic() - started) * 1000, 3)}


def envelope(capture, provider, deadline=None) -> JsonObject:
    raw = capture.get("response", {})
    result = local_answers(raw) if provider == "local" else {"answers": raw.get("answers", [])}
    if capture.get("error") or (deadline is not None and capture["latencyMs"] > deadline * 1000):
        result = {"answers": [], "fallback": capture.get("error") or "timeout"}
    return {**result, "policyVersion": POLICY_VERSION, "latencyMs": capture["latencyMs"], "usage": raw.get("usage", {})}


def concentration_envelope(capture, provider, body) -> JsonObject:
    """Derive a common diagnostic concentration score; keep originals untouched.

    This is not a probability of correctness. For binary questions the score is
    abs(p0 - p1); for K choices it is (K * max(p) - 1) / (K - 1).
    """
    result = envelope(capture, provider)
    questions = {q["name"]: q for q in body["questions"]}
    answers = []
    for answer in result["answers"]:
        question = questions.get(answer.get("name"))
        raw: JsonObject | None = typed_answer(question, answer) if question else None
        if raw is not None and raw.get("probabilities"):
            probabilities = [p["probability"] for p in raw["probabilities"]]
            count = len(probabilities)
            answer = {**answer, "confidence": round((count * max(probabilities) - 1) / (count - 1), 12)}
        answers.append(answer)
    return {**result, "answers": answers}


def score_questions(cases, captures, provider, deadline=None, *, concentration=False):
    rows = []
    for item in cases:
        if item["kind"] != "advisory":
            continue
        response = (
            concentration_envelope(captures[item["id"]][provider], provider, item["body"])
            if concentration
            else envelope(captures[item["id"]][provider], provider, deadline)
        )
        parsed = parse_answers(item["body"], response, 0.90)
        raw = {a["name"]: a for a in parsed["rawAnswers"]}
        accepted = {a["name"] for a in parsed["answers"]}
        for name, labels in item["case"]["labels"].items():
            answer = raw.get(name, {})
            rows.append(
                {
                    "id": item["id"],
                    "name": name,
                    "task": name.split(":")[0],
                    "split": item["case"].get("split"),
                    "expected": labels,
                    "choice": answer.get("choice"),
                    "confidence": answer.get("confidence"),
                    "correct": answer.get("choice") in labels,
                    "accepted": name in accepted,
                }
            )
    return rows


def question_summary(rows):
    valid = [r for r in rows if r["choice"] is not None]
    accepted = [r for r in rows if r["accepted"]]
    return {
        "questions": len(rows),
        "valid": len(valid),
        "correct": sum(r["correct"] for r in valid),
        "accuracy": sum(r["correct"] for r in valid) / len(valid) if valid else None,
        "accepted": len(accepted),
        "acceptedErrors": sum(not r["correct"] for r in accepted),
        "acceptedAccuracy": sum(r["correct"] for r in accepted) / len(accepted) if accepted else None,
    }


def latency_summary(captures):
    values = sorted(c["latencyMs"] for c in captures)
    return {
        "calls": len(values),
        "medianMs": median(values) if values else None,
        "p95Ms": values[math.ceil(0.95 * len(values)) - 1] if values else None,
        "maxMs": max(values) if values else None,
        "overFiveSeconds": sum(v > 5000 for v in values),
        "errors": dict(Counter(c["error"] for c in captures if c.get("error"))),
        "inputTokens": sum(c.get("response", {}).get("usage", {}).get("input_tokens", 0) for c in captures),
        "cacheHits": sum(bool(c.get("response", {}).get("winnow", {}).get("cache_hit")) for c in captures),
    }


def results(cases, captures) -> JsonObject:
    result: JsonObject = {}
    routing = [c for c in cases if c["kind"] == "routing"]
    for provider in ("local", "luna"):
        rows = score_questions(cases, captures, provider)
        strict = score_questions(cases, captures, provider, 5)
        routing_responses = {c["case"]["id"]: envelope(captures[c["id"]][provider], provider) for c in routing}
        strict_routing = {c["case"]["id"]: envelope(captures[c["id"]][provider], provider, 5) for c in routing}
        concentration_rows = score_questions(cases, captures, provider, concentration=True)
        concentration_responses = {
            c["case"]["id"]: concentration_envelope(captures[c["id"]][provider], provider, c["body"]) for c in routing
        }
        result[provider] = {
            "commonConcentration": {
                "metric": "(K * max(conditional probabilities) - 1) / (K - 1); diagnostic score, not accuracy",
                "threshold": 0.9,
                "advisory": question_summary(concentration_rows),
                "routing": evaluate([c["case"] for c in routing], concentration_responses, threshold=0.9),
            },
            "advisory": {
                "all": question_summary(rows),
                "byTask": {
                    task: question_summary([r for r in rows if r["task"] == task])
                    for task in sorted({r["task"] for r in rows})
                },
                "bySplit": {
                    split: question_summary([r for r in rows if r["split"] == split])
                    for split in sorted({r["split"] for r in rows})
                },
                "withinFiveSeconds": question_summary(strict),
                "errors": [r for r in rows if not r["correct"]],
                "confidenceSweep": {
                    str(threshold): {
                        split: question_summary(
                            [
                                {**r, "accepted": r["confidence"] is not None and r["confidence"] >= threshold}
                                for r in rows
                                if r["split"] == split
                            ]
                        )
                        for split in sorted({r["split"] for r in rows})
                    }
                    for threshold in (0, 0.25, 0.5, 0.7, 0.8, 0.9, 0.95, 0.99)
                },
            },
            "routing": evaluate([c["case"] for c in routing], routing_responses, threshold=0.9),
            "routingWithoutThreshold": evaluate([c["case"] for c in routing], routing_responses, threshold=0),
            "routingWithinFiveSeconds": evaluate([c["case"] for c in routing], strict_routing, threshold=0.9),
            "routingConfidenceSweep": {
                str(threshold): {
                    split: {
                        key: scored[key]
                        for key in (
                            "evaluated",
                            "accepted",
                            "harmfulDeferred",
                            "harmfulOmitted",
                            "unnecessaryImmediate",
                            "dispositions",
                        )
                    }
                    for split, scored in evaluate([c["case"] for c in routing], routing_responses, threshold=threshold)[
                        "bySplit"
                    ].items()
                }
                for threshold in (0, 0.25, 0.5, 0.7, 0.8, 0.9, 0.95, 0.99)
            },
            "latency": {
                kind: latency_summary([captures[c["id"]][provider] for c in cases if c["kind"] == kind])
                for kind in ("advisory", "routing", "production")
            },
        }
    agreement: list[JsonObject] = []
    for c in cases:
        if c["kind"] != "production":
            continue
        local = envelope(captures[c["id"]]["local"], "local")
        reference = {a["name"]: a for a in c["reference"]["rawAnswers"]}
        answers = {a["name"]: a for a in local["answers"]}
        for q in c["body"]["questions"]:
            a, b = typed_answer(q, answers.get(q["name"])), typed_answer(q, reference.get(q["name"]))
            if a and b and a.get("type") == b.get("type") == "choice":
                agreement.append(
                    {
                        "id": c["id"],
                        "name": q["name"],
                        "local": a["choice"],
                        "luna": b["choice"],
                        "localConfidence": a["confidence"],
                        "lunaConfidence": b["confidence"],
                        "agree": a["choice"] == b["choice"],
                    }
                )
    result["productionAgreement"] = {
        "questions": len(agreement),
        "agreements": sum(r["agree"] for r in agreement),
        "byTask": {
            task: {"questions": len(selected), "agreements": sum(r["agree"] for r in selected)}
            for task in sorted({r["name"].split(":")[0] for r in agreement})
            if (selected := [r for r in agreement if r["name"].split(":")[0] == task])
        },
        "disagreements": [r for r in agreement if not r["agree"]],
    }
    result["invalidQuestionSets"] = [
        {"id": c["id"], "questions": [q["name"] for q in c["body"]["questions"] if not 2 <= len(q["choices"]) <= 64]}
        for c in cases
        if any(not 2 <= len(q["choices"]) <= 64 for q in c["body"]["questions"])
    ]
    return result


async def run(args):
    root = private_directory(args.output)
    manifest = root / "manifest.json"
    saved = None
    if manifest.exists():
        saved = json.loads(manifest.read_text())
        cases, selection = saved["cases"], saved["selection"]
    else:
        cases, selection = fixture_cases()
        if args.production_ledger and args.production_samples:
            production, selection["production"] = production_cases(args.production_ledger, args.production_samples)
            cases += production
    ceiling = sum(estimate_tokens(c["body"]) for c in cases if c["kind"] != "production") * 1e-7
    if ceiling > args.max_luna_usd:
        raise ValueError(f"Conservative input estimate ${ceiling:.4f} exceeds configured ${args.max_luna_usd:.4f}")
    config = {
        "casesHash": fingerprint(cases),
        "localEndpoint": args.local_endpoint,
        "lunaModel": MODEL,
        "timeoutSeconds": args.timeout,
        "maxLunaUsd": args.max_luna_usd,
    }
    if saved is not None and saved["configuration"] != config:
        raise ValueError("Existing run configuration differs; choose a fresh output directory")
    if not manifest.exists():
        write_private_json(
            manifest,
            {
                "createdUtc": datetime.now(UTC).isoformat(),
                "configuration": config,
                "selection": selection,
                "cases": cases,
            },
        )
    captures_path = root / "captures.json"
    captures = json.loads(captures_path.read_text()) if captures_path.exists() else {}
    key = read_decisions_key(args.key_file)
    print(json.dumps({"cases": len(cases), "lunaConservativeCeilingUsd": ceiling, "selection": selection}), flush=True)
    async with aiohttp.ClientSession() as session:
        for index, case in enumerate(cases):
            identifier = case["id"]
            row = captures.setdefault(identifier, {})
            providers = [p for p in ("local", "luna") if p not in row]
            if case["kind"] == "production" and "luna" in providers:
                row["luna"] = {
                    "response": {
                        "model": MODEL,
                        "answers": case["reference"]["rawAnswers"],
                        "usage": case["reference"].get("usage", {}),
                    },
                    "latencyMs": case["reference"]["latencyMs"],
                    "recorded": True,
                }
                providers.remove("luna")
            replies = await asyncio.gather(
                *(
                    request(session, args.local_endpoint, local_request(case["body"]), args.timeout)
                    if p == "local"
                    else request(session, "https://api.openai.com/v1/decisions", case["body"], args.timeout, key)
                    for p in providers
                )
            )
            row.update(zip(providers, replies, strict=True))
            write_private_json(captures_path, captures)
            if index % 10 == 0 or index + 1 == len(cases):
                print(
                    json.dumps(
                        {
                            "completed": index + 1,
                            "total": len(cases),
                            "case": identifier,
                            "providers": {p: {"ms": c["latencyMs"], "error": c.get("error")} for p, c in row.items()},
                        }
                    ),
                    flush=True,
                )
    report = results(cases, captures)
    report["selection"] = selection
    report["configuration"] = config
    report["lunaConservativeCeilingUsd"] = ceiling
    report["lunaMeasuredInputCostUsd"] = (
        sum(
            c["luna"].get("response", {}).get("usage", {}).get("input_tokens", 0)
            for c in captures.values()
            if not c["luna"].get("recorded")
        )
        * 1e-7
    )
    write_private_json(root / "results.json", report)
    print(
        json.dumps(
            {"results": str(root / "results.json"), "lunaMeasuredInputCostUsd": report["lunaMeasuredInputCostUsd"]}
        ),
        flush=True,
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--key-file", type=Path, required=True)
    parser.add_argument("--local-endpoint", default="http://127.0.0.1:8091/v1/systemone")
    parser.add_argument("--timeout", type=float, default=30)
    parser.add_argument("--max-luna-usd", type=float, default=0.15)
    parser.add_argument("--production-ledger", type=Path)
    parser.add_argument("--production-samples", type=int, default=24)
    args = parser.parse_args()
    if (
        not math.isfinite(args.timeout)
        or args.timeout <= 0
        or not math.isfinite(args.max_luna_usd)
        or args.max_luna_usd < 0
        or args.production_samples < 0
    ):
        parser.error("Timeout must be positive and finite; budget and sample count must be nonnegative")
    asyncio.run(run(args))


if __name__ == "__main__":
    main()
