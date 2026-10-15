"""Corpus provenance/label integrity checks, not measurements of classifier accuracy."""

import hashlib
import json
import subprocess
import sys
from collections import Counter, defaultdict
from pathlib import Path

import pytest

from civ_agent.conversation_context import audience

ROOT = Path(__file__).resolve().parents[1]
CORPUS = ROOT / "tests/fixtures/diplomacy-routing-calibration.json"
LEGACY = ROOT / "tests/fixtures/diplomacy-routing-replay.json"


def corpus():
    return json.loads(CORPUS.read_text())


def by_id():
    return {case["id"]: case for case in corpus()["cases"]}


def test_generated_corpus_is_reproducible_without_working_directory_or_network(tmp_path):
    # The builder reads the checked-in evidence, not live game databases. Run
    # outside the repo so accidental cwd-relative evidence reads cannot pass.
    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts/build_routing_corpus.py"), "--check"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def test_scenario_groups_and_real_threads_never_cross_reserved_split():
    data = corpus()
    cases = data["cases"]
    assert data["version"] == 2
    assert 150 <= len(cases) <= 200
    assert len({case["id"] for case in cases}) == len(cases)
    groups = defaultdict(list)
    for case in cases:
        groups[case["group"]].append(case)
    for rows in groups.values():
        assert len({row["split"] for row in rows}) == 1
        assert rows[0]["split"] in {"calibration", "holdout"}
    synthetic = [rows for rows in groups.values() if rows[0]["source"]["synthetic"]]
    assert len(synthetic) >= 35
    assert all(len(rows) == 4 for rows in synthetic)
    outgoing = [rows for rows in synthetic if rows[0]["direction"] == "outgoing"]
    assert len(outgoing) >= 10
    assert Counter(rows[0]["split"] for rows in outgoing)["holdout"] >= 5
    # Directions/unsafe error types remain represented in each reserved split.
    for direction in ("incoming", "outgoing"):
        for split in ("calibration", "holdout"):
            represented = {
                choice
                for case in cases
                if case["direction"] == direction and case["split"] == split and not case["protected"]
                for choice in case["expected"]
            }
            assert (
                represented == {"batch", "respond_now", "no_separate_response"}
                if direction == "incoming"
                else represented == {"batch", "send_now", "omit"}
            )


def test_label_provenance_discloses_synthetic_grouping_and_no_human_review():
    data = corpus()
    assert data["provenance"]["humanReviewed"] is False
    assert data["provenance"]["labelAuthor"] == "assistant/manual scheduling judgment"
    assert data["provenance"]["legacyFixtureSha256"] == hashlib.sha256(LEGACY.read_bytes()).hexdigest()
    assert "not independent" in " ".join(data["provenance"]["limitations"])
    for case in data["cases"]:
        source = case["source"]
        assert source["author"] == "assistant" and source["humanReviewed"] is False
        assert source["kind"] == ("synthetic_context_variation" if source["synthetic"] else "historical_excerpt")
        assert source["transformation"]
        assert case["rationale"]
        if source["synthetic"]:
            assert source["scenario"] == case["group"]
        else:
            assert source["artifact"] and source["message"] and source["schedulerTimingKnown"] is False


def test_real_examples_preserve_source_messages_and_only_earlier_exact_audience_history():
    source_cases = {case["id"]: case for case in json.loads(LEGACY.read_text())["cases"]}
    real = [case for case in corpus()["cases"] if not case["source"]["synthetic"]]
    assert len(real) == 7
    for case in real:
        legacy = source_cases[case["source"]["message"]]
        bundle = case["bundle"]
        assert bundle["pending"] == legacy["bundle"]["pending"]
        assert bundle["recentExchanges"][0]["messages"] == legacy["bundle"]["recentExchanges"][-6:]
        assert bundle["timing"]["oldestPendingAgeSeconds"] is None
        assert bundle["timing"]["remainingBatchSeconds"] is None
        selected = bundle["pending"][0]
        for prior in bundle["recentExchanges"][0]["messages"]:
            assert audience(prior, bundle["player"]) == audience(selected, bundle["player"])
            assert prior["turn"] <= selected["turn"]
            assert prior["message"] != selected["message"]
        # The original broad urgency labels are not accepted as timing evidence.
        assert case["expected"] == (["respond_now"] if case["protected"] else ["batch"])


def test_every_bundle_has_production_thread_wrappers_and_pending_exclusion():
    for case in corpus()["cases"]:
        bundle = case["bundle"]
        assert bundle["schemaVersion"] == 2
        assert bundle["player"] and bundle["direction"] == case["direction"]
        assert bundle["timing"]["maxBatchSeconds"] == 30
        assert bundle["historyCoverage"] == "bounded_exact_audience_excerpt"
        thread = (bundle["audience"][0], tuple(bundle["audience"][1]))
        pending_ids = {message["message"] for message in bundle["pending"]}
        for message in bundle["pending"]:
            assert audience(message, bundle["player"]) == thread
        for wrapper in bundle["recentExchanges"]:
            assert audience(wrapper, bundle["player"]) == thread
            assert isinstance(wrapper["hasEarlierMessages"], bool)
            assert isinstance(wrapper["omittedForBudget"], int)
            assert len(wrapper["messages"]) <= 6
            for message in wrapper["messages"]:
                assert audience(message, bundle["player"]) == thread
                assert message["message"] not in pending_ids
        if case["direction"] == "outgoing":
            assert not bundle["pending"]
            assert bundle["proposedMessage"]["author"] == bundle["player"]
        else:
            assert bundle["pending"]
        assert len(json.dumps(bundle).encode()) < 64_000


def test_protected_cases_have_immediate_labels_and_explicit_bypass_provenance():
    protected = [case for case in corpus()["cases"] if case["protected"]]
    assert {case["bypassReason"] for case in protected} >= {
        "native_action",
        "active_binding_negotiation",
        "human_audience",
        "explicit_immediate_override",
    }
    for case in protected:
        assert case["expected"] == ["respond_now" if case["direction"] == "incoming" else "send_now"]
    assert all(case["bypassReason"] is None for case in corpus()["cases"] if not case["protected"])


def test_relative_time_duplicate_labels_do_not_cross_game_turns():
    cases = by_id()
    for case in cases.values():
        if not case["source"]["synthetic"] or case["protected"]:
            continue
        skip = "no_separate_response" if case["direction"] == "incoming" else "omit"
        if case["expected"] != [skip]:
            continue
        history = [message for wrapper in case["bundle"]["recentExchanges"] for message in wrapper["messages"]]
        # These authored duplicate/answered cases intentionally refer to the
        # same current turn. Relative dates are not silently shifted by one turn.
        assert all(message["turn"] == case["bundle"]["currentTurn"] for message in history)
    for identifier in ("incoming_status/time_advanced", "outgoing_report/time_advanced"):
        case = cases[identifier]
        bundle = case["bundle"]
        old = bundle["recentExchanges"][0]["messages"][0]
        new = bundle["pending"][0] if case["direction"] == "incoming" else bundle["proposedMessage"]
        assert old["text"] == new["text"] and old["turn"] < bundle["currentTurn"]
        assert case["expected"] == ["batch"]


@pytest.mark.parametrize("direction", ["incoming", "outgoing"])
def test_identical_deadline_content_changes_label_with_remaining_budget(direction):
    cases = by_id()
    long_delay = cases[f"{direction}_remaining_budget/deadline_before_batch"]
    short_delay = cases[f"{direction}_remaining_budget/batch_before_deadline"]
    field = "pending" if direction == "incoming" else "proposedMessage"
    assert (
        long_delay["bundle"][field] == short_delay["bundle"][field]
        if direction == "outgoing"
        else long_delay["bundle"][field][0]["text"] == short_delay["bundle"][field][0]["text"]
    )
    assert long_delay["bundle"]["timing"]["remainingBatchSeconds"] == 28
    assert short_delay["bundle"]["timing"]["remainingBatchSeconds"] == 5
    assert long_delay["expected"] == ["respond_now" if direction == "incoming" else "send_now"]
    assert short_delay["expected"] == ["batch"]
    expired = cases[f"{direction}_remaining_budget/no_remaining_delay"]
    assert expired["bundle"]["timing"]["remainingBatchSeconds"] == 0
    assert expired["expected"] == long_delay["expected"]


def test_urgent_words_alone_are_not_a_label_shortcut():
    cases = by_id()
    for suffix in ("quoted_deadline", "negated_deadline", "past_deadline"):
        case = cases["incoming_urgency_quotation/" + suffix]
        assert "ten" in case["bundle"]["pending"][0]["text"]
        assert case["expected"] == ["batch"]
    assert cases["incoming_remaining_budget/longer_window"]["expected"] == ["batch"]
    urgent = cases["incoming_urgency_quotation/imminent_without_number"]
    assert "seconds" not in urgent["bundle"]["pending"][0]["text"]
    assert urgent["expected"] == ["respond_now"]


@pytest.mark.parametrize(
    ("identifier", "expected"),
    [
        ("incoming_routine_question/new", ["batch"]),
        ("incoming_changed_trade/future", ["batch"]),
        ("incoming_changed_trade/imminent", ["respond_now"]),
        ("incoming_read_vs_answer/merely_observed", ["batch"]),
        ("incoming_read_vs_answer/own_unrelated_reply", ["batch"]),
        ("incoming_read_vs_answer/answered", ["no_separate_response"]),
        ("incoming_history_truncation/visible_question", ["batch"]),
        ("incoming_multiple_pending/mixed_urgent", ["respond_now"]),
        ("incoming_multiple_pending/mixed_question", ["batch"]),
        ("incoming_social_breach/accusation", ["batch"]),
        ("outgoing_bluff/new_claim", ["batch"]),
        ("outgoing_queued_status/queued_same", ["omit"]),
        ("outgoing_queued_status/classifying_same", ["batch"]),
        ("outgoing_queued_status/omitted_same", ["batch"]),
        ("outgoing_contextual_duplicate/absent_context", ["batch"]),
        ("outgoing_promise/revised", ["batch"]),
        ("outgoing_deadline_wording/polite_soon", ["batch"]),
        ("outgoing_deadline_wording/specific_window", ["send_now"]),
        ("outgoing_prompt_injection/suppress_instruction", ["batch"]),
    ],
)
def test_safety_critical_annotation_distinctions(identifier, expected):
    # These are explicit checks of the intended labels, not oracle responses
    # passed to the model and not evidence that the model gets them right.
    assert by_id()[identifier]["expected"] == expected
