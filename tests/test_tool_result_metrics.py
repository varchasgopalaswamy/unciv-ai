import json

from civ_agent.mcp_results import canonical_tool_result
from civ_agent.proposal_gateway import tool_result
from tests.verify_tool_results_live import comparison_key, result_metrics


def test_counts_printed_payloads_and_compares_audits_without_exporting_contents(tmp_path):
    payload = {"message": "PRIVATE_GAME_MESSAGE", "gold": 500}
    error = {"error": "DETAIL_REQUIRED"}
    legacy = tool_result(payload)
    normalized = canonical_tool_result(legacy)
    events = [
        {"type": "response_item", "payload": {"type": "reasoning", "text": "PRIVATE_REASONING_SENTINEL"}},
        {
            "type": "response_item",
            "payload": {
                "type": "custom_tool_call_output",
                "output": [
                    {"text": "Script completed\nOutput:"},
                    {"text": json.dumps({"old": legacy, "new": normalized})},
                ],
            },
        },
        {
            "type": "response_item",
            "payload": {
                "type": "function_call_output",
                "output": json.dumps(canonical_tool_result(tool_result(error))),
            },
        },
    ]
    path = tmp_path / "trace.jsonl"
    path.write_text("\n".join(json.dumps(event) for event in events))
    metrics = result_metrics(path, [payload, payload, error, {"notPrinted": True}])
    assert metrics["printedStructuredResults"] == metrics["matchedPrintedResults"] == 3
    assert metrics["duplicateTextBlocks"] == metrics["errorResults"] == 1
    assert metrics["unmatchedPrintedResults"] == 0
    assert metrics["auditedResultsNotPrintedAsEnvelopes"] == 1
    assert metrics["equivalentLegacyChars"] > metrics["observedEnvelopeChars"] > metrics["equivalentSinglePayloadChars"]
    assert "PRIVATE_REASONING_SENTINEL" not in json.dumps(metrics)
    assert "PRIVATE_GAME_MESSAGE" not in json.dumps(metrics)


def test_reports_unmatched_results_truncation_and_distinct_content(tmp_path):
    result = canonical_tool_result(tool_result({"unmatched": True}))
    result["content"] = [{"type": "text", "text": "Distinct warning"}]
    path = tmp_path / "trace.jsonl"
    path.write_text(
        json.dumps(
            {
                "type": "response_item",
                "payload": {
                    "type": "custom_tool_call_output",
                    "output": [{"text": json.dumps(result)}, {"text": "Warning: truncated output"}],
                },
            }
        )
    )
    metrics = result_metrics(path, [])
    assert metrics["unmatchedPrintedResults"] == metrics["additionalContentBlocks"] == metrics["truncatedOutputs"] == 1
    assert metrics["duplicateTextBlocks"] == 0


def test_matches_javascript_numbers_and_direct_payload_prints_without_hiding_changes(tmp_path):
    payload = {"influence": 0.0, "available": True}
    printed = {"influence": 0, "available": True}
    changed = {"influence": 0, "available": 1}
    wrapped = canonical_tool_result(tool_result(printed))
    wrong = canonical_tool_result(tool_result(changed))
    path = tmp_path / "trace.jsonl"
    path.write_text(
        json.dumps(
            {
                "type": "response_item",
                "payload": {
                    "type": "custom_tool_call_output",
                    "output": [{"text": json.dumps([wrapped, {"direct": printed}, wrong])}],
                },
            }
        )
    )
    metrics = result_metrics(path, [payload, payload])
    assert comparison_key(payload) == comparison_key(printed)
    assert comparison_key(payload) != comparison_key(changed)
    assert metrics["printedStructuredResults"] == 3
    assert metrics["printedPayloadResults"] == 1
    assert metrics["matchedPrintedResults"] == 2
    assert metrics["unmatchedPrintedResults"] == 1
    assert metrics["auditedResultsNotPrinted"] == 0
    assert metrics["auditedResultsNotPrintedAsEnvelopes"] == 1
    assert metrics["observedResultChars"] > metrics["observedEnvelopeChars"]
