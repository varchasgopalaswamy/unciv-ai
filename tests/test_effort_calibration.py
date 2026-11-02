"""Keep the optional live study's provenance and comparison controls honest."""

import copy
import json

import pytest

from civ_agent.effort_decisions import EFFORT_LEVELS
from tests.effort_calibration import MODELS, digest, schedule, visible_prefix, wire_context


def test_visible_prefix_excludes_target_future_reasoning_and_metadata(tmp_path):
    path = tmp_path / "source.jsonl"
    rows = [
        {"type": "session_meta", "payload": {"base_instructions": {"text": "base"}}},
        {
            "type": "response_item",
            "payload": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "known"}]},
        },
        {"type": "response_item", "payload": {"type": "reasoning", "text": "secret reasoning"}},
        {
            "type": "response_item",
            "payload": {
                "type": "function_call_output",
                "id": "old",
                "name": "decision_receipts",
                "output": "known receipt",
                "internal_chat_message_metadata_passthrough": {"private": "metadata"},
            },
        },
        {
            "type": "response_item",
            "payload": {
                "type": "message",
                "id": "target",
                "role": "assistant",
                "content": [{"type": "output_text", "text": "target answer"}],
            },
        },
        {
            "type": "response_item",
            "payload": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "future fact"}]},
        },
    ]
    path.write_text("\n".join(json.dumps(row) for row in rows))
    base, items = visible_prefix(path, "target")
    assert base == "base" and len(items) == 2
    assert items[1] == {"type": "function_call_output", "name": "decision_receipts", "output": "known receipt"}
    serialized = json.dumps(items)
    assert all(text not in serialized for text in ("reasoning", "metadata", "target answer", "future fact"))
    with pytest.raises(ValueError, match="unavailable"):
        visible_prefix(path, "absent")


def test_compacted_prefix_cannot_be_silently_reconstructed(tmp_path):
    path = tmp_path / "source.jsonl"
    path.write_text(json.dumps({"type": "compacted", "payload": {}}))
    with pytest.raises(ValueError, match="Compacted"):
        visible_prefix(path, "target")


def test_schedule_has_every_model_effort_context_and_repetition_once():
    rows = schedule(["a", "b", "c", "d", "e", "f"])
    assert rows == schedule(["a", "b", "c", "d", "e", "f"])
    expected = {(c, m, e, r) for c in "abcdef" for m in MODELS for e in EFFORT_LEVELS for r in (1, 2)}
    assert {(r["case"], r["model"], r["effort"], r["repetition"]) for r in rows} == expected
    assert len(rows) == len({r["id"] for r in rows}) == 120


@pytest.mark.parametrize("effort", EFFORT_LEVELS)
def test_wire_replay_preserves_context_and_checks_effort(effort):
    schema = {"type": "object"}
    case = {"input": [{"type": "message", "role": "user", "content": []}], "instructions": "fixed"}
    case["inputSha256"] = digest(case["input"])
    body = {
        "model": MODELS[0],
        "reasoning": {"effort": effort},
        "tools": [],
        "tool_choice": "none",
        "text": {"format": {"schema": schema}},
    }
    original = copy.deepcopy(case)
    report = wire_context(body, case, MODELS[0], effort, schema)
    assert body["input"] == case["input"] and body["input"] is not case["input"]
    assert body["instructions"] == "fixed" and case == original
    assert report["reasoning"]["effort"] == effort and report["inputSha256"] == case["inputSha256"]
    body["tools"] = [{"type": "function"}]
    with pytest.raises(ValueError, match="tools"):
        wire_context(body, case, MODELS[0], effort, schema)
    body["tools"] = []
    case["input"].append({"future": "leak"})
    with pytest.raises(ValueError, match="Frozen"):
        wire_context(body, case, MODELS[0], effort, schema)
