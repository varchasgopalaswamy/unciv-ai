import json

from tests.verify_tool_discovery_live import discovery_metrics, request_usage


def test_discovery_metrics_separate_names_definitions_and_per_request_usage(tmp_path):
    events = []

    def add(kind, **payload):
        events.append({"type": kind, "payload": payload})

    def lookup(id, output):
        add("response_item", type="custom_tool_call", call_id=id, input="text(ALL_TOOLS.filter(t => t.name));")
        add(
            "response_item",
            type="custom_tool_call_output",
            call_id=id,
            output=[{"text": "Script completed\nOutput:\n"}, {"text": output}],
        )

    index = json.dumps(["mcp__civ__player_turn", "mcp__civ__unit_details"])
    definition = json.dumps([{"name": "mcp__civ__unit_details", "description": "Full signature and guidance"}])
    usage = {"input_tokens": 100, "cached_input_tokens": 80, "output_tokens": 10, "reasoning_output_tokens": 3}
    lookup("index", index)
    add("token_usage_record", response_id="one", usage=usage)
    # Cumulative notifications and duplicate provider records must not inflate spending.
    add("event_msg", type="token_count", info={"total_token_usage": usage})
    add("token_usage_record", response_id="one", usage=usage)
    lookup("definition", definition)
    add("token_usage_record", response_id="two", usage=usage)
    lookup("repeat", definition)
    add("token_usage_record", response_id="three", usage=usage)
    add("response_item", type="reasoning", content="PRIVATE_REASONING_SENTINEL")
    path = tmp_path / "trace.jsonl"
    path.write_text("\n".join(json.dumps(event) for event in events))
    metrics = discovery_metrics(path)
    assert metrics["indexChars"] == len(index)
    assert metrics["definitionChars"] == 2 * len(definition)
    assert metrics["modelRequests"] == metrics["discoveryRequests"] == 3
    assert metrics["usage"]["input_tokens"] == 300
    assert metrics["usage"]["uncached_input_tokens"] == 60
    assert metrics["firstRequestUsage"]["input_tokens"] == 100
    assert metrics["laterRequestUsage"]["input_tokens"] == 200
    assert metrics["firstRequestUsage"]["cache_write_input_tokens"] is None
    assert metrics["repeatedDefinitions"] == ["mcp__civ__unit_details"]
    assert [row["request"] for row in metrics["lookups"]] == [1, 2, 3]
    assert "PRIVATE_REASONING_SENTINEL" not in json.dumps(metrics)


def test_cache_statistics_distinguish_opening_reuse_and_reported_writes(tmp_path):
    events = [
        {
            "type": "token_usage_record",
            "payload": {
                "response_id": name,
                "usage": {
                    "input_tokens": total,
                    "cached_input_tokens": cached,
                    "cache_write_input_tokens": writes,
                    "output_tokens": 10,
                    "reasoning_output_tokens": 3,
                },
            },
        }
        for name, total, cached, writes in (("first", 1000, 400, 600), ("next", 1200, 1000, 200))
    ]
    path = tmp_path / "trace.jsonl"
    path.write_text("\n".join(map(json.dumps, events)))
    metrics = discovery_metrics(path)
    assert metrics["firstRequestUsage"]["uncached_input_tokens"] == 600
    assert metrics["laterRequestUsage"]["uncached_input_tokens"] == 200
    assert metrics["firstRequestUsage"]["cache_write_input_tokens"] == 600
    assert metrics["usage"]["cache_write_input_tokens"] == 800


def test_no_followup_requests_means_zero_cache_writes_not_missing_measurements():
    assert request_usage([])["cache_write_input_tokens"] == 0


def test_plain_text_tool_abort_retains_usage_without_block_parsing(tmp_path):
    output = "aborted by user after 0.4s"
    usage = {"input_tokens": 100, "cached_input_tokens": 80, "output_tokens": 10, "reasoning_output_tokens": 3}
    events = [
        {
            "type": "response_item",
            "payload": {"type": "custom_tool_call", "call_id": "lookup", "input": "text(ALL_TOOLS)"},
        },
        {
            "type": "response_item",
            "payload": {"type": "custom_tool_call_output", "call_id": "lookup", "output": output},
        },
        {"type": "token_usage_record", "payload": {"response_id": "one", "usage": usage}},
    ]
    path = tmp_path / "trace.jsonl"
    path.write_text("\n".join(map(json.dumps, events)))
    metrics = discovery_metrics(path)
    assert metrics["usage"]["input_tokens"] == 100
    assert metrics["modelRequests"] == 1
    assert metrics["lookups"][0]["otherChars"] == len(output)
    assert metrics["indexChars"] == metrics["definitionChars"] == 0
