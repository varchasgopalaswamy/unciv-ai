"""Verify single-payload model input against durable calls, without exporting reasoning."""

import argparse
import contextlib
import hashlib
import json
import sqlite3
from collections import Counter
from pathlib import Path

from civ_agent.mcp_results import canonical_tool_result
from civ_agent.proposal_gateway import tool_result
from civ_agent.runner_history import Chunks
from civ_agent.tls import write_private_json
from tests.verify_tool_discovery_live import verify as verify_discovery


def encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def comparison_key(value):
    """JavaScript prints integral JSON numbers without .0; booleans stay distinct."""

    def normalize(item):
        if isinstance(item, float) and item.is_integer():
            return int(item)
        if isinstance(item, list):
            return [normalize(child) for child in item]
        if isinstance(item, dict):
            return {key: normalize(child) for key, child in item.items()}
        return item

    return encoded(normalize(value))


def envelopes(value):
    """Find printed MCP envelopes inside batches; never descend into their payloads."""
    if isinstance(value, dict):
        if isinstance(value.get("structuredContent"), dict) and isinstance(value.get("content"), list):
            yield value
        else:
            for item in value.values():
                yield from envelopes(item)
    elif isinstance(value, list):
        for item in value:
            yield from envelopes(item)


def plain_payloads(value, recorded):
    """Recognize directly printed structuredContent without descending into it."""
    if isinstance(value, dict):
        if isinstance(value.get("structuredContent"), dict) and isinstance(value.get("content"), list):
            return
        if comparison_key(value) in recorded:
            yield value
        else:
            for item in value.values():
                yield from plain_payloads(item, recorded)
    elif isinstance(value, list):
        for item in value:
            yield from plain_payloads(item, recorded)


def result_metrics(path: Path, audited_results):
    recorded = Counter(comparison_key(result) for result in audited_results)
    observed = Counter()
    wrapped = Counter()
    metrics = dict(
        printedStructuredResults=0,
        printedPayloadResults=0,
        duplicateTextBlocks=0,
        errorResults=0,
        additionalContentBlocks=0,
        observedEnvelopeChars=0,
        observedResultChars=0,
        equivalentLegacyChars=0,
        equivalentSinglePayloadChars=0,
        truncatedOutputs=0,
    )
    for line in path.read_text().splitlines():
        event = json.loads(line)
        if event.get("type") != "response_item":
            continue
        item = event.get("payload", {})
        if item.get("type") not in {"custom_tool_call_output", "function_call_output"}:
            continue
        output = item.get("output", [])
        blocks = [{"text": output}] if isinstance(output, str) else output
        for block in blocks:
            text = block.get("text", "")
            if text.startswith("Script completed\n"):
                continue
            metrics["truncatedOutputs"] += "truncated output" in text
            try:
                value = json.loads(text)
            except ValueError:
                continue
            for result in envelopes(value):
                canonical = canonical_tool_result(result)
                payload = result["structuredContent"]
                observed[comparison_key(payload)] += 1
                wrapped[comparison_key(payload)] += 1
                metrics["printedStructuredResults"] += 1
                metrics["duplicateTextBlocks"] += len(result["content"]) - len(canonical["content"])
                metrics["errorResults"] += result.get("isError") is True
                metrics["additionalContentBlocks"] += len(canonical["content"])
                metrics["observedEnvelopeChars"] += len(encoded(result))
                metrics["observedResultChars"] += len(encoded(result))
                legacy = tool_result(payload)
                metrics["equivalentLegacyChars"] += len(encoded(legacy))
                metrics["equivalentSinglePayloadChars"] += len(encoded(canonical_tool_result(legacy)))
            for payload in plain_payloads(value, recorded):
                observed[comparison_key(payload)] += 1
                metrics["printedStructuredResults"] += 1
                metrics["printedPayloadResults"] += 1
                metrics["errorResults"] += "error" in payload
                metrics["observedResultChars"] += len(encoded(payload))
                legacy = tool_result(payload)
                metrics["equivalentLegacyChars"] += len(encoded(legacy))
                metrics["equivalentSinglePayloadChars"] += len(encoded(canonical_tool_result(legacy)))
    metrics["auditedResults"] = sum(recorded.values())
    metrics["matchedPrintedResults"] = sum((observed & recorded).values())
    metrics["unmatchedPrintedResults"] = sum((observed - recorded).values())
    metrics["auditedResultsNotPrintedAsEnvelopes"] = sum((recorded - wrapped).values())
    metrics["auditedResultsNotPrinted"] = sum((recorded - observed).values())
    return metrics


def verify(root: Path, sessions: Path):
    # Also verify task completion, Luna/medium, and service/host/provider accounting.
    usage = verify_discovery(root, sessions)
    record = json.loads((root / "discovery-run.json").read_text())
    paths = list(sessions.rglob(f"*{record['threadId']}.jsonl"))
    assert len(paths) == 1
    history = root / "agent-services/Rome/history" / hashlib.sha256(record["activationId"].encode()).hexdigest()
    chunks = Chunks()
    audited = []
    for line in (history / "events.jsonl").read_text().splitlines():
        event = chunks.expand(json.loads(line))
        if event is not None and event["kind"] == "tool_result":
            result = event["data"].get("result")
            if isinstance(result, dict) and isinstance(result.get("structuredContent"), dict):
                audited.append(result["structuredContent"])
    # Open read-only: constructing CivilizationState would mark old jobs interrupted.
    with contextlib.closing(
        sqlite3.connect(f"file:{root / 'agent-services/Rome/civilization/memory.sqlite3'}?mode=ro", uri=True)
    ) as db:
        results = [
            json.loads(row[0])
            for row in db.execute(
                "SELECT result FROM calls WHERE job=? AND result IS NOT NULL", (record["activationId"],)
            )
        ]
    # Boundary rejections can leave a call's result NULL; their complete error
    # responses are still present in the MCP audit history.
    durable = Counter(encoded(value) for value in results)
    audit = Counter(encoded(value) for value in audited)
    assert not durable - audit, "Durable call results are missing from MCP audit history"
    assert all("error" in json.loads(value) for value in audit - durable), "Unexpected audit-only result"
    metrics = result_metrics(paths[0], audited)
    metrics["durableCallResults"] = sum(durable.values())
    metrics["auditOnlyErrorResults"] = sum((audit - durable).values())
    assert metrics["printedStructuredResults"] > 0, "No model-facing structured results were captured"
    assert metrics["duplicateTextBlocks"] == 0, "Duplicate payloads reached the model"
    assert metrics["unmatchedPrintedResults"] == 0, "Printed payloads differ from durable MCP audit records"
    # Record oversized/truncated batches explicitly. They limit this live check
    # to complete printed envelopes; reducing observation size is separate work.
    report = {**usage, "responses": metrics}
    write_private_json(root / "response-metrics.json", report)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("state_dir", type=Path)
    parser.add_argument("--sessions", type=Path, default=Path.home() / ".codex/sessions")
    args = parser.parse_args()
    print(json.dumps(verify(args.state_dir, args.sessions), indent=2))


if __name__ == "__main__":
    main()
