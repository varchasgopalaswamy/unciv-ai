import json
from pathlib import Path

import pytest

from civ_agent.advisory_replay import load_cases, main, report

FIXTURES = Path(__file__).parent / "fixtures/decisions-advisory-replay.json"


def test_replay_requires_real_answers_and_reports_labeled_scope(capsys):
    cases = load_cases(FIXTURES)
    assert len(cases) == 7
    missing = report(cases, {})
    assert missing["missing"] == 7 and missing["completed"] == missing["labeledAnswers"] == 0
    responses = {
        c["id"]: {
            "answers": [
                {"name": name, "type": "choice", "choice": values[0], "confidence": 0.99}
                for name, values in c["labels"].items()
            ],
            "usage": {"input_tokens": 100},
        }
        for c in cases
    }
    result = report(cases, responses)
    assert result["labelMatches"] == result["labeledAnswers"] == 9
    assert result["usage"]["input_tokens"] == 700
    assert result["estimatedValidatedInputCostUsd"] == 0.00007
    assert "gameplay strength" in result["scope"]
    assert all("gameplay" not in c["evidence"] for c in cases)
    main([str(FIXTURES)])
    assert json.loads(capsys.readouterr().out)["missing"] == 7


def test_captured_live_triage_answers_replay_without_gameplay_questions():
    responses = json.loads(FIXTURES.with_name("decisions-advisory-responses-2026-10-14.json").read_text())
    result = report(load_cases(FIXTURES), responses)
    assert result["completed"] == 7 and result["missing"] == 0
    assert result["labelMatches"] == 5 and result["labeledAnswers"] == 9
    assert result["fallbacks"] == {"low_confidence": 4}
    assert result["usage"]["input_tokens"] == 2674
    assert result["estimatedValidatedInputCostUsd"] == pytest.approx(0.0002674)
