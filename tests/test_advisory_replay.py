import json
from pathlib import Path

import pytest

from civ_agent.advisory_replay import load_cases, main, report
from civ_agent.decisions_policy import DecisionsSettings
from civ_agent.decisions_tasks import parse_answers, request_body, request_metadata

FIXTURES = Path(__file__).parent / "fixtures/decisions-advisory-replay.json"


def captured(case, *, tokens=100):
    body = request_body(case["evidence"], DecisionsSettings(decisions_enabled=True))
    provider = {
        "answers": [
            {"name": name, "type": "choice", "choice": values[0], "confidence": 0.99}
            for name, values in case["labels"].items()
        ]
    }
    return {
        **request_metadata(body, case["evidence"]),
        **parse_answers(body, provider, 0.90),
        "usage": {"input_tokens": tokens},
    }


def test_replay_requires_real_answers_and_reports_current_labeled_scope(capsys):
    cases = load_cases(FIXTURES)
    assert len(cases) == 19
    missing = report(cases, {})
    assert missing["missing"] == 19 and missing["completed"] == missing["labeledAnswers"] == 0
    responses = {case["id"]: captured(case) for case in cases}
    result = report(cases, responses)
    assert result["labelMatches"] == result["labeledAnswers"] == 42
    assert result["rawLabelMatches"] == result["rawLabeledAnswers"] == 42
    assert result["usage"]["input_tokens"] == 1900
    assert result["estimatedValidatedInputCostUsd"] == pytest.approx(0.00019)
    assert "gameplay strength" in result["scope"]
    assert result["delivery"]["casesWithObservedEvidence"] == 0
    main([str(FIXTURES)])
    assert json.loads(capsys.readouterr().out)["missing"] == 19


def test_historical_captures_cannot_be_reinterpreted_as_current_policy():
    with pytest.raises(ValueError, match="current versioned"):
        load_cases(FIXTURES.with_name("decisions-advisory-replay-v2.json"))
    historical = json.loads(FIXTURES.with_name("decisions-advisory-responses-2026-10-14.json").read_text())
    case = load_cases(FIXTURES)[0]
    result = report([case], {case["id"]: historical["attributed-question"]})
    assert result["identityMismatches"] == 1
    assert result["rawLabeledAnswers"] == result["labeledAnswers"] == 0
    assert result["labelMatches"] == result["rawLabelMatches"] == 0
