import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock

from civ_agent.diplomacy_replay import collect, evaluate


def cases():
    return json.loads((Path(__file__).parent / "fixtures/diplomacy-routing-replay.json").read_text())["cases"]


def test_labeled_replay_covers_audit_cases_without_future_or_other_audience():
    fixture = cases()
    assert len({case["id"] for case in fixture}) == len(fixture)
    assert {
        "acknowledgement",
        "unchanged_report",
        "answered_question",
        "useful_new_information",
        "changed_terms",
        "urgent_request",
        "bluff",
        "ambiguous_promise",
    } <= {case["label"] for case in fixture}
    for case in fixture:
        if not case["source"].get("artifact") or case["direction"] != "incoming":
            continue
        selected = case["bundle"]["pending"][0]
        for message in case["bundle"]["recentExchanges"]:
            assert message["participants"] == selected["participants"]
            assert message["turn"] <= selected["turn"] and message["message"] != selected["message"]
    responses = {
        case["id"]: {
            "answers": [{"name": "delivery", "type": "choice", "choice": case["expected"][0], "confidence": 0.95}]
        }
        for case in fixture
    }
    scored = evaluate(fixture, responses)
    assert scored["accepted"] == len(fixture) and scored["actionableDeferred"] == 0
    # This tests the replay harness with oracle responses, not the live model.
    bad = {"answers": [{"name": "delivery", "type": "choice", "choice": "no_separate_response", "confidence": 0.99}]}
    responses["mcygkx"] = bad
    assert evaluate(fixture, responses)["actionableDeferred"] == 1
    assert evaluate(fixture, {})["missing"] == len(fixture) - 1


def test_replay_never_sends_protected_traffic_to_broker():
    async def run():
        client = AsyncMock()
        client.classify.return_value = {"choice": "respond_now", "confidence": 1}
        result = await collect(cases(), client)
        assert "mpvwks" not in result
        assert client.classify.await_count == len(cases()) - 1

    asyncio.run(run())
