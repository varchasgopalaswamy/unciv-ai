"""Labeled, scheduling-only evaluation. Network use requires --live-broker."""

import argparse
import asyncio
import json
from collections import Counter
from pathlib import Path

from civ_agent.decisions_broker import RoutingClient
from civ_agent.diplomacy_decisions import CHOICES, fallback, parse_answer
from civ_agent.protocol import JsonObject


def evaluate(cases, responses):
    rows: list[JsonObject] = []
    for case in cases:
        response = responses.get(case["id"])
        if case.get("protected"):
            decision = fallback(case["direction"], "protected")
        elif response is None:
            rows.append({"id": case["id"], "label": case["label"], "evaluated": False})
            continue
        else:
            decision = parse_answer(case["direction"], response)
        immediate = next(iter(CHOICES[case["direction"]]))
        rows.append(
            {
                "id": case["id"],
                "label": case["label"],
                "evaluated": True,
                "decision": decision,
                "accepted": decision["choice"] in case["expected"],
                "actionableDeferred": case["expected"] == [immediate] and decision["choice"] != immediate,
                "usage": (response or {}).get("usage", {}),
            }
        )
    scored = [row for row in rows if row["evaluated"]]
    return {
        "cases": rows,
        "evaluated": len(scored),
        "missing": len(rows) - len(scored),
        "accepted": sum(row["accepted"] for row in scored),
        "actionableDeferred": sum(row["actionableDeferred"] for row in scored),
        "dispositions": dict(Counter(row["decision"]["choice"] for row in scored)),
        "limitations": "Small labeled replay, not a benchmark effect estimate. Labels do not adjudicate promises or truth.",
    }


async def collect(cases, client):
    responses = {}
    for case in cases:
        if case.get("protected"):
            continue
        result = await client.classify(case["direction"], case["bundle"])
        responses[case["id"]] = {
            "answers": [
                {
                    "name": "delivery",
                    "type": "choice",
                    "choice": result["choice"],
                    "confidence": result.get("confidence"),
                }
            ],
            "usage": result.get("usage", {}),
            "routingResult": result,
        }
    return responses


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("fixtures", type=Path)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--responses", type=Path, help="Replay captured Decisions response envelopes by case id; no network"
    )
    mode.add_argument(
        "--live-broker", action="store_true", help="Explicitly make paid requests through the configured trusted broker"
    )
    args = parser.parse_args()
    fixture = json.loads(args.fixtures.read_text())
    if fixture.get("version") != 1:
        parser.error("Unsupported fixture version")
    responses = json.loads(args.responses.read_text()) if args.responses else {}
    if args.live_broker:
        responses = asyncio.run(collect(fixture["cases"], RoutingClient.from_environment()))
    print(json.dumps({"evaluation": evaluate(fixture["cases"], responses), "responses": responses}, indent=2))


if __name__ == "__main__":
    main()
