"""Read-only audit and schema preparation for the completed four-context screen.

The live request budget has been consumed. No model or engine is contacted.
"""

import argparse
import asyncio
import hashlib
import json
import re
from pathlib import Path
from unittest.mock import AsyncMock

from civ_agent.decision_contracts import DecisionContract
from civ_agent.player_prompts import job_instructions
from civ_agent.proposal_gateway import READ_TOOLS
from civ_agent.protocol import JsonObject
from civ_agent.startup_context import startup_instructions
from civ_agent.strict_schema import encoded
from civ_agent.tls import private_directory, write_private_json
from tests.test_economy_optimization import managed


async def prepare(contexts, root):
    contracts, prompts = {}, {}
    for role in ("gameplay", "economy", "military", "communication"):
        source = contexts[role]
        rows = [json.loads(line) for line in Path(source["history"]).read_text().splitlines()]
        old = next(e["data"]["text"] for e in rows if e["kind"] == "developer_message")
        user = next(e["data"]["text"] for e in rows if e["kind"] == "user_message")
        descriptions = {}
        for match in re.finditer(r"\{", old):
            try:
                candidate, _ = json.JSONDecoder().raw_decode(old[match.start() :])
            except ValueError:
                continue
            if isinstance(candidate, dict) and "readInstructions" in candidate:
                descriptions = candidate["readInstructions"]
                break
        async with managed(root / ("contracts-" + role)) as m:
            m.host.list_tools = AsyncMock(
                return_value=[
                    {"name": name, "description": descriptions.get(name, name)} for name in sorted(READ_TOOLS)
                ]
            )
            job = (
                m.main
                if role == "gameplay"
                else m.economy
                if role == "economy"
                else await m.gateway.open(role, role, "g1", m.host, {}, 0)
            )
            job.catalogue = None
            catalogue = await m.gateway.tools(job)
        for variant in ("verbose", "compact"):
            contract = DecisionContract(role, catalogue, compact=variant == "compact")
            contracts[role, variant] = contract
            write_private_json(root / f"{role}-{variant}-schema.json", contract.schema)
        instructions = (
            job_instructions(role)
            + "\n\n"
            + startup_instructions(
                {
                    "purpose": role,
                    "overviewView": "main" if role == "gameplay" else role,
                    "readInstructions": descriptions,
                    "economyCoordination": True,
                }
            )
        )
        prompts[role] = {"instructions": instructions, "user": user}
        write_private_json(
            root / (role + "-context.json"),
            {
                **source,
                **prompts[role],
                "historySha256": hashlib.sha256(Path(source["history"]).read_bytes()).hexdigest(),
            },
        )
    return contracts, prompts


def assess(decision, context):
    """Bounded semantic screening, not a legality or gameplay-quality verdict."""
    issues = []
    for operation in decision.get("operations", []):
        arguments = operation["arguments"]
        for key in ("target", "city", "unit", "proposalId", "counterofferTo", "messageId", "requestId"):
            value = arguments.get(key)
            if isinstance(value, str) and value not in context:
                issues.append(
                    {
                        "category": "reference_not_in_saved_context",
                        "tool": operation["tool"],
                        "field": key,
                        "value": value,
                    }
                )
    if decision["kind"] == "finish" and decision.get("arguments", {}).get("decisions"):
        issues.append(
            {
                "category": "military_dispositions_need_delivered_review",
                "note": "This one-response screen cannot acquire a fresh review.",
            }
        )
    return issues


def audit(root):
    """Use content-free per-response usage records, including internal continuations.

    App-server turns are not provider request boundaries. Preserve the original
    captures and emit a separate correction; never overwrite the measured data.
    """
    rows = json.loads((root / "results.json").read_text())
    sessions: dict[str | None, JsonObject] = {}
    for path in (root / "codex" / "sessions").rglob("*.jsonl"):
        responses, rejected_tools = {}, []
        thread = None
        for line in path.read_text().splitlines():
            event = json.loads(line)
            payload = event.get("payload", {})
            if event.get("type") == "session_meta":
                thread = payload.get("id")
            elif event.get("type") == "token_usage_record":
                thread = payload["thread_id"]
                responses[payload["response_id"]] = payload["usage"]
            elif event.get("type") == "response_item" and payload.get("type") == "custom_tool_call":
                # Only tool names, never generated code or reasoning.
                rejected_tools.append(payload.get("name"))
        sessions[thread] = {"path": str(path), "responses": responses, "rejectedTools": rejected_tools}
    corrected: list[JsonObject] = []
    for row in rows:
        capture = sessions.get(row.get("threadId"), {"responses": {}, "rejectedTools": []})
        reports = list(capture["responses"].values())
        usage = {
            key: sum(report.get(key, 0) for report in reports)
            for key in ("input_tokens", "cached_input_tokens", "output_tokens", "reasoning_output_tokens")
        }
        usage["uncached_input_tokens"] = usage["input_tokens"] - usage["cached_input_tokens"]
        unknown = int(row.get("status") == "requested" and not reports)
        corrected.append(
            {
                **{
                    key: row[key]
                    for key in (
                        "model",
                        "role",
                        "variant",
                        "schema",
                        "instructionCharacters",
                        "observationCharacters",
                        "valid",
                    )
                },
                "completedProviderResponses": len(reports),
                "unreportedRequests": unknown,
                "providerRequestsLowerBound": len(reports) + unknown,
                "automaticContinuationsObserved": max(0, len(reports) - 1),
                "rejectedTools": capture["rejectedTools"],
                "usage": usage,
                "usageComplete": not unknown,
                "evidence": capture.get("path"),
            }
        )
    total = {key: sum(row["usage"][key] for row in corrected) for key in corrected[0]["usage"]}
    requests = sum(row["providerRequestsLowerBound"] for row in corrected)
    return {
        "turnStarts": len(rows),
        "providerRequestsLowerBound": requests,
        "requestLimit": 16,
        "requestLimitSatisfied": False if requests > 16 else None,
        "usage": total,
        "usageComplete": all(row["usageComplete"] for row in corrected),
        "rows": corrected,
        "notes": [
            "The original runner counted turn/start calls. Four disabled exec calls caused internal continuations and exceeded the request cap.",
            "No execution tool ran and no decisions were dispatched. Original per-turn last-response usage undercounted these continuations.",
            "Counts are lower bounds: failed requests can lack token records. No further live calls are authorized by this screen.",
            "Only usage counters and rejected tool names are extracted. Raw reasoning, generated code and credentials are excluded.",
        ],
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--audit", type=Path, help="Read the completed screen and its local usage records")
    parser.add_argument("--contexts", type=Path, help="Prepare schemas from retained contexts without calling models")
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    root = private_directory(args.output)
    if args.audit:
        result = audit(args.audit)
        write_private_json(root / "usage-audit.json", result)
        print(
            encoded(
                {key: result[key] for key in ("turnStarts", "providerRequestsLowerBound", "usage", "usageComplete")}
            )
        )
    elif args.contexts:
        asyncio.run(prepare(json.loads(args.contexts.read_text()), root))
    else:
        parser.error("Choose --audit or --contexts. This completed screen cannot launch further model calls.")


if __name__ == "__main__":
    main()
