"""Corrupt frozen review inputs fail before any model or gameplay side effects."""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from civ_agent.review_runtime import ReviewFatalError, ReviewRuntime


@pytest.mark.parametrize(
    "inputs,settings,cause",
    [
        pytest.param("{", "{}", json.JSONDecodeError, id="corrupt-position-json"),
        pytest.param(
            json.dumps({"observation": {"turn": 6}, "reads": {"player_turn": {"turn": 6}}}),
            "{",
            json.JSONDecodeError,
            id="corrupt-settings-json",
        ),
        pytest.param(
            json.dumps({"observation": {}, "reads": {"player_turn": {"turn": 6}}}),
            "{}",
            KeyError,
            id="missing-observation-turn",
        ),
        pytest.param(
            json.dumps({"observation": {"turn": 6}, "reads": {"player_turn": {"turn": 5}}}),
            "{}",
            ValueError,
            id="mismatched-frozen-positions",
        ),
    ],
)
def test_invalid_frozen_input_is_fatal_before_model_start(inputs, settings, cause):
    process_factory = Mock(side_effect=AssertionError("Invalid input must not start a model"))
    # Deliberately omit chat, gateway and accounting: rejecting invalid inputs
    # must not depend on any connected player service or allocate a model job.
    service = SimpleNamespace(memory=None, process_factory=process_factory)
    runtime = ReviewRuntime(service)
    with pytest.raises(ReviewFatalError, match="^REVIEW_INPUT_FAILED$") as failure:
        asyncio.run(runtime.compute({"inputs": inputs, "settings": settings}, "invalid-input-review"))
    assert isinstance(failure.value.__cause__, cause)
    process_factory.assert_not_called()
