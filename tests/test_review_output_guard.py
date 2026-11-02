"""Structured report limits detect runaway output without parsing model reasoning."""

import pytest

from civ_agent.review_output_guard import ReviewOutputGuard


def event(method, *, turn="turn-1", **params):
    return method, {"threadId": "thread-1", "turnId": turn, **params}


def start(guard, *, phase="final_answer", turn="turn-1"):
    return guard.observe(
        *event("item/started", turn=turn, item={"id": "report", "type": "agentMessage", "phase": phase})
    )


def delta(guard, text, *, turn="turn-1"):
    return guard.observe(*event("item/agentMessage/delta", turn=turn, itemId="report", delta=text))


def test_whitespace_run_outside_strings_is_bounded_across_fragments():
    guard = ReviewOutputGuard("thread-1", max_whitespace=32)
    start(guard)
    assert delta(guard, '{"assessment":"unfinished"') is None
    for _ in range(4):
        assert delta(guard, " \n\t" * 2) is None
    assert delta(guard, " " * 9) == "REVIEW_OUTPUT_STALLED"


def test_json_string_whitespace_and_escaped_quotes_are_not_stalls():
    guard = ReviewOutputGuard("thread-1", max_whitespace=8)
    start(guard)
    assert delta(guard, '{"assessment":"a \\"') is None
    assert delta(guard, " " * 100) is None
    assert delta(guard, '\\" end"}') is None
    assert delta(guard, " " * 9) == "REVIEW_OUTPUT_STALLED"


@pytest.mark.parametrize("phase", ["final_answer", "commentary", "analysis", None])
def test_report_byte_limit_applies_to_streamed_or_completed_message(phase):
    guard = ReviewOutputGuard("thread-1", max_bytes=32)
    start(guard, phase=phase)
    assert delta(guard, '"' + "é" * 15) is None
    assert delta(guard, "é") == "REVIEW_OUTPUT_LIMIT"
    guard = ReviewOutputGuard("thread-1", max_bytes=32)
    assert (
        guard.observe(
            *event("item/completed", item={"id": "report", "type": "agentMessage", "phase": phase, "text": "é" * 17})
        )
        == "REVIEW_OUTPUT_LIMIT"
    )


def test_reasoning_and_other_turns_are_ignored():
    guard = ReviewOutputGuard("thread-1", max_bytes=32, max_whitespace=8)
    guard.bind_turn("turn-1")
    start(guard, turn="old-turn")
    assert delta(guard, " " * 1000, turn="old-turn") is None
    assert guard.observe(*event("item/reasoning/textDelta", delta=" " * 1000)) is None
    assert (
        guard.observe(
            "item/agentMessage/delta",
            {"threadId": "other", "turnId": "turn-1", "itemId": "report", "delta": " " * 1000},
        )
        is None
    )


def test_completed_message_replaces_stream_for_size_checks_without_double_counting():
    guard = ReviewOutputGuard("thread-1", max_bytes=32)
    start(guard)
    assert delta(guard, '{"answer":"fine"}') is None
    assert (
        guard.observe(
            *event(
                "item/completed",
                item={"id": "report", "type": "agentMessage", "phase": "final_answer", "text": '{"answer":"fine"}'},
            )
        )
        is None
    )


def test_early_events_are_bound_to_the_acknowledged_turn():
    guard = ReviewOutputGuard("thread-1", max_bytes=32)
    start(guard, turn="old-turn")
    assert delta(guard, '"' + "x" * 20, turn="old-turn") is None
    start(guard)
    assert delta(guard, '"' + "x" * 20) is None
    guard.bind_turn("turn-1")
    assert delta(guard, "x" * 12) == "REVIEW_OUTPUT_LIMIT"


@pytest.mark.parametrize("phase", ["final_answer", "commentary", "analysis", None])
@pytest.mark.parametrize("pattern", ['"}', '\\"}', "abcde"])
def test_repetitive_output_stops_across_arbitrary_fragments(phase, pattern):
    guard = ReviewOutputGuard("thread-1")
    start(guard, phase=phase)
    text = '{"decision":{"kind":"inspect"}}' + pattern * 3000
    failure = None
    for index in range(0, len(text), 37):
        failure = delta(guard, text[index : index + 37])
        if failure:
            break
    assert failure == "REVIEW_OUTPUT_STALLED"
    assert index < 5000


def test_varied_long_message_is_not_a_repetition_stall():
    guard = ReviewOutputGuard("thread-1")
    start(guard)
    text = str(list(range(3000)))
    for index in range(0, len(text), 37):
        assert delta(guard, text[index : index + 37]) is None
