"""Labeled advisory replay; live mode uses the authenticated, budgeted broker."""

import argparse
import asyncio
import json
from collections import Counter
from pathlib import Path

from civ_agent.decisions_broker import RoutingClient
from civ_agent.decisions_policy import DecisionsSettings
from civ_agent.decisions_tasks import parse_answers, request_body
from civ_agent.protocol import JsonObject


def load_cases(path):
    cases = json.loads(Path(path).read_text())["cases"]
    if not isinstance(cases, list) or not cases or len({c["id"] for c in cases}) != len(cases):
        raise ValueError("Replay requires distinct labeled cases")
    for case in cases:
        body = request_body(case["evidence"], DecisionsSettings(decisions_enabled=True))
        allowed = {q["name"]: {c["value"] for c in q["choices"]} for q in body["questions"]}
        if not case.get("provenance") or not case.get("labels"):
            raise ValueError("Replay requires provenance and labels")
        if any(name not in allowed or not set(values) <= allowed[name] for name, values in case["labels"].items()):
            raise ValueError("Replay labels must match fixed task choices")
    return cases


def report(cases, responses):
    rows, usage, fallbacks = [], Counter(), Counter()
    scored, matched = 0, 0
    for case in cases:
        raw = responses.get(case["id"])
        if raw is None:
            rows.append({"id": case["id"], "status": "missing"})
            continue
        body = request_body(case["evidence"], DecisionsSettings(decisions_enabled=True))
        # Provider envelopes contain all questions; broker replies can omit
        # refused/low-confidence questions. Preserve those as explicit fallbacks.
        answers = raw.get("answers", [])
        names = {a.get("name") for a in answers if isinstance(a, dict)}
        complete = answers + [
            {"type": "refusal", "name": q["name"]} for q in body["questions"] if q["name"] not in names
        ]
        parsed = parse_answers(body, {"answers": complete}, 0.90)
        fallback = raw.get("fallback") or parsed.get("fallback")
        if fallback:
            fallbacks[fallback] += 1
        accepted = {a["name"]: a["choice"] for a in parsed["answers"]}
        for name, values in case["labels"].items():
            scored += 1
            matched += accepted.get(name) in values
        for key, value in raw.get("usage", {}).items():
            if type(value) is int and value >= 0:
                usage[key] += value
        rows.append(
            {
                "id": case["id"],
                "status": "fallback" if fallback else "classified",
                "fallback": fallback,
                "labelMatches": {k: accepted.get(k) in v for k, v in case["labels"].items()},
            }
        )
    return {
        "scope": "Labeled memory/review triage replay; does not measure gameplay strength.",
        "cases": rows,
        "completed": len(responses),
        "missing": len(cases) - len(responses),
        "labeledAnswers": scored,
        "labelMatches": matched,
        "usage": dict(usage),
        "fallbacks": dict(fallbacks),
        "estimatedValidatedInputCostUsd": usage["input_tokens"] * 0.1 / 1_000_000,
        "responses": responses,
    }


async def live(cases, client):
    health = await client.health()
    if health.get("status") != "ready":
        raise ValueError("Authenticated compatible broker required for live replay")
    responses: JsonObject = {}
    for case in cases:
        responses[case["id"]] = await client.batch(case["evidence"])
    return report(cases, responses)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("fixtures", type=Path)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--responses", type=Path)
    mode.add_argument("--live-broker", action="store_true", help="Paid requests charged to the broker-owned allowance")
    args = parser.parse_args(argv)
    cases = load_cases(args.fixtures)
    result = (
        asyncio.run(live(cases, RoutingClient.from_environment()))
        if args.live_broker
        else report(cases, json.loads(args.responses.read_text()) if args.responses else {})
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
