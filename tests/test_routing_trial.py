import asyncio
import json
from unittest.mock import AsyncMock

import pytest

from civ_agent.decisions_ledger import NANODOLLARS_PER_INPUT_TOKEN, estimate_tokens
from civ_agent.diplomacy_decisions import POLICY_VERSION, request_body
from civ_agent.protocol import JsonObject
from scripts.trial_diplomacy_routing import TrialStore, trial


def fixture(path, *, protected=False):
    value = {
        "version": 2,
        "cases": [
            {
                "id": "case-1",
                "group": "thread-1",
                "split": "calibration",
                "label": "ordinary",
                "source": {"synthetic": True},
                "direction": "incoming",
                "protected": protected,
                "expected": ["batch"],
                "bundle": {
                    "player": "Rome",
                    "direction": "incoming",
                    "pending": [{"author": "Greece", "text": "The road is complete."}],
                    "recentExchanges": [],
                },
            }
        ],
    }
    path.write_text(json.dumps(value))
    return value


def response(tokens=50):
    return {
        "answers": [{"name": "delivery", "type": "choice", "choice": "batch", "confidence": 0.98}],
        "usage": {"input_tokens": tokens},
        "latencyMs": 12,
    }


def test_trial_reuses_exact_frozen_responses_without_new_calls_or_copying_key(tmp_path):
    path = tmp_path / "fixture.json"
    fixture(path)
    client = AsyncMock()
    client.evaluate.return_value = response()
    root = tmp_path / "isolated"
    first = asyncio.run(trial(root, tmp_path / "not-read.key", path, client=client))
    assert first["accounting"]["calls"] == 2
    assert first["policies"]["legacy"]["evaluation"]["accepted"] == 1
    assert first["policies"]["candidate"]["evaluation"]["accepted"] == 1
    assert first["accounting"]["spentNanodollars"] == 100 * NANODOLLARS_PER_INPUT_TOKEN
    assert first["comparisonThreshold"] == 0.90
    client.reset_mock()
    again = asyncio.run(trial(root, tmp_path / "not-read.key", path, client=client))
    assert again == first
    client.evaluate.assert_not_awaited()
    assert json.loads((root / "fixture.json").read_text()) == json.loads(path.read_text())
    assert not any("key" in file.name for file in root.iterdir())


def test_trial_call_cap_and_budget_apply_before_dispatch(tmp_path):
    path = tmp_path / "fixture.json"
    fixture(path)
    client = AsyncMock()
    client.evaluate.return_value = response()
    capped = asyncio.run(trial(tmp_path / "capped", None, path, max_calls=1, client=client))
    assert client.evaluate.await_count == 1
    assert capped["accounting"]["calls"] == 1
    assert capped["policies"]["candidate"]["evaluation"]["missing"] == 1
    client.reset_mock()
    empty = asyncio.run(trial(tmp_path / "empty", None, path, budget_usd=0, client=client))
    assert empty["accounting"]["calls"] == 0
    client.evaluate.assert_not_awaited()


def test_unknown_completion_is_durable_and_never_dispatched_again(tmp_path):
    path = tmp_path / "fixture.json"
    value = fixture(path)
    client = AsyncMock()
    client.evaluate.side_effect = RuntimeError("simulated crash")
    root = tmp_path / "interrupted"
    with pytest.raises(RuntimeError, match="simulated crash"):
        asyncio.run(trial(root, None, path, policies=("legacy",), client=client))
    pending = json.loads((root / "trial.json").read_text())
    saved = next(iter(pending["requests"].values()))
    assert saved["response"] is None
    body = request_body("incoming", value["cases"][0]["bundle"], policy_version=POLICY_VERSION)
    assert saved["reservedNanodollars"] == estimate_tokens(body) * NANODOLLARS_PER_INPUT_TOKEN
    client.reset_mock()
    result = asyncio.run(trial(root, None, path, policies=("legacy",), client=client))
    client.evaluate.assert_not_awaited()
    assert result["accounting"]["unknownCompletions"] == 1
    assert result["policies"]["legacy"]["evaluation"]["fallbacks"] == {"completion_unknown": 1}


def test_protected_cases_never_reserve_or_call_and_missing_usage_retains_budget(tmp_path):
    path = tmp_path / "fixture.json"
    fixture(path, protected=True)
    client = AsyncMock()
    protected = asyncio.run(trial(tmp_path / "protected", None, path, client=client))
    client.evaluate.assert_not_awaited()
    assert protected["accounting"]["calls"] == 0
    assert protected["policies"]["legacy"]["evaluation"]["protectedBypasses"] == 1
    fixture(path)
    client.evaluate.return_value = {"answers": [], "fallback": "timeout", "usage": {}}
    missing = asyncio.run(trial(tmp_path / "unknown-usage", None, path, policies=("legacy",), client=client))
    assert missing["accounting"]["reservedNanodollars"] > 0
    assert missing["accounting"]["spentNanodollars"] == 0


def test_trial_pins_configuration_and_prevents_parallel_processes(tmp_path):
    path = tmp_path / "fixture.json"
    value = fixture(path)
    root = tmp_path / "pinned"
    store = TrialStore(root, value, max_calls=1, budget_usd=0.1, policies=[POLICY_VERSION])
    try:
        with pytest.raises(ValueError, match="running process"):
            TrialStore(root, value, max_calls=1, budget_usd=0.1, policies=[POLICY_VERSION])
    finally:
        store.close()
    for values in (dict(max_calls=2, budget_usd=0.1), dict(max_calls=1, budget_usd=0.2)):
        with pytest.raises(ValueError, match="immutable"):
            TrialStore(root, value, policies=[POLICY_VERSION], **values)
    changed = {**value, "notes": "changed fixture"}
    with pytest.raises(ValueError, match="immutable"):
        TrialStore(root, changed, max_calls=1, budget_usd=0.1, policies=[POLICY_VERSION])


def two_direction_fixture(path):
    value = fixture(path)
    original = value["cases"][0]
    value["cases"].append(
        {
            **original,
            "id": "case-outgoing",
            "direction": "outgoing",
            "bundle": {
                "player": "Rome",
                "direction": "outgoing",
                "pending": [],
                "recentExchanges": [],
                "proposedMessage": {"author": "Rome", "text": "A useful report"},
            },
        }
    )
    path.write_text(json.dumps(value))
    return value


def two_question_response():
    return {
        "answers": [
            {"name": "response_needed", "type": "choice", "choice": "needed", "confidence": 0.98},
            {"name": "response_timing", "type": "choice", "choice": "can_wait", "confidence": 0.97},
        ],
        "usage": {"input_tokens": 60},
        "latencyMs": 15,
    }


def test_two_question_trial_reuses_only_exact_outgoing_with_separate_cost_and_provenance(tmp_path):
    path = tmp_path / "fixture.json"
    two_direction_fixture(path)
    prior_client = AsyncMock()
    prior_client.evaluate.side_effect = [
        response(),
        {"answers": [], "fallback": "timeout", "usage": {}, "latencyMs": 5000},
    ]
    prior = tmp_path / "prior"
    asyncio.run(trial(prior, None, path, policies=("candidate",), client=prior_client))
    frozen = prior / "fixture.json"
    before = {file.name: file.read_bytes() for file in prior.iterdir() if file.is_file()}
    client = AsyncMock()
    client.evaluate.return_value = two_question_response()
    current = tmp_path / "two-question"
    result = asyncio.run(
        trial(
            current,
            None,
            frozen,
            policies=("two-question",),
            client=client,
            reuse_trial=prior,
            max_calls=200,
            budget_usd=0.05,
        )
    )
    assert client.evaluate.await_count == 1
    assert len(client.evaluate.call_args.args[0]["questions"]) == 2
    assert result["accounting"]["calls"] == 1 and result["accounting"]["reusedResponses"] == 1
    assert result["accounting"]["reusedFallbacks"] == {"timeout": 1}
    assert result["accounting"]["spentNanodollars"] == 60 * NANODOLLARS_PER_INPUT_TOKEN
    assert (current / "fixture.json").read_bytes() == frozen.read_bytes()
    journal = json.loads((current / "trial.json").read_text())
    assert len(journal["requests"]) == len(journal["reused"]) == 1
    reused = next(iter(journal["reused"].values()))
    assert reused["provenance"]["sourcePolicyVersion"] == "diplomacy-routing-v2"
    assert reused["response"]["fallback"] == "timeout"
    assert {file.name: file.read_bytes() for file in prior.iterdir() if file.is_file()} == before
    client.reset_mock()
    assert (
        asyncio.run(
            trial(
                current,
                None,
                frozen,
                policies=("two-question",),
                client=client,
                reuse_trial=prior,
                max_calls=200,
                budget_usd=0.05,
            )
        )
        == result
    )
    client.evaluate.assert_not_awaited()


@pytest.mark.parametrize("change", ["fixture_bytes", "model", "request", "pending", "prompt"])
def test_reuse_refuses_changed_or_uncompleted_cache_before_any_paid_call(tmp_path, monkeypatch, change):
    import scripts.trial_diplomacy_routing as module
    from civ_agent.diplomacy_decisions import TWO_QUESTION_POLICY_VERSION

    path = tmp_path / "fixture.json"
    two_direction_fixture(path)
    original = AsyncMock()
    original.evaluate.return_value = response()
    prior = tmp_path / "prior"
    asyncio.run(trial(prior, None, path, policies=("candidate",), client=original))
    frozen = prior / "fixture.json"
    journal = json.loads((prior / "trial.json").read_text())
    row = next(row for row in journal["requests"].values() if row["caseId"] == "case-outgoing")
    if change == "fixture_bytes":
        path.write_bytes(frozen.read_bytes() + b"\n")
    else:
        path = frozen
    if change == "model":
        journal["configuration"]["model"] = "another-model"
    elif change == "request":
        row["request"]["input"] += " "
    elif change == "pending":
        row["response"] = None
    elif change == "prompt":
        implementation = module.request_body

        def altered(direction, bundle, *, policy_version):
            body: JsonObject = implementation(direction, bundle, policy_version=policy_version)
            if direction == "outgoing" and policy_version == TWO_QUESTION_POLICY_VERSION:
                body["questions"][0]["instructions"] += " changed"
            return body

        monkeypatch.setattr(module, "request_body", altered)
    (prior / "trial.json").write_text(json.dumps(journal))
    client = AsyncMock()
    with pytest.raises(ValueError, match="fixture|model|request|changed"):
        asyncio.run(
            trial(tmp_path / "rejected", None, path, policies=("two-question",), client=client, reuse_trial=prior)
        )
    client.evaluate.assert_not_awaited()
    assert not (tmp_path / "rejected").exists()


def test_reuse_preserves_prior_refusal_without_new_outgoing_call(tmp_path):
    path = tmp_path / "fixture.json"
    two_direction_fixture(path)
    prior_client = AsyncMock()
    refusal = {"answers": [{"name": "delivery", "type": "refusal"}], "usage": {"input_tokens": 25}}
    prior_client.evaluate.side_effect = [response(), refusal]
    prior = tmp_path / "prior"
    asyncio.run(trial(prior, None, path, policies=("candidate",), client=prior_client))
    client = AsyncMock()
    client.evaluate.return_value = two_question_response()
    result = asyncio.run(
        trial(
            tmp_path / "current",
            None,
            prior / "fixture.json",
            policies=("two-question",),
            client=client,
            reuse_trial=prior,
        )
    )
    assert client.evaluate.await_count == 1
    assert result["accounting"]["reusedResponses"] == 1
    report = result["policies"]["two-question"]["evaluation"]
    assert report["byDirection"]["outgoing"]["fallbacks"] == {"refusal": 1}
