"""The live screen must count real attempts and require accepted role completion."""

import asyncio
from collections import Counter
from types import SimpleNamespace

from civ_agent.provider_gate import RequestBudget
from tests.live_effort_turns import PairedBudget, schedule, terminal_state


def test_paired_schedule_is_balanced_deterministic_and_has_separate_hosts():
    rows = schedule(["worker", "routine"], repetitions=2)
    assert rows == schedule(["worker", "routine"], repetitions=2)
    assert len(rows) == len({r["id"] for r in rows}) == len({r["port"] for r in rows}) == 16
    assert set(Counter((r["case"], r["variant"], r["model"]) for r in rows).values()) == {2}


def test_provider_budget_counts_continuations_retries_and_uncertain_reservations(tmp_path):
    suite = RequestBudget(tmp_path / "suite.sqlite", 3)
    first = PairedBudget(suite, RequestBudget(tmp_path / "first.sqlite", 2))
    assert first.reserve("first", "generation")
    assert first.reserve("retry", "generation", retry_scope="failed-first")
    assert not first.reserve("extra", "generation")
    assert first.trial.used() == suite.used() == 2
    # A fresh process does not refund an attempted request without a receipt.
    second = PairedBudget(RequestBudget(tmp_path / "suite.sqlite", 3), RequestBudget(tmp_path / "second.sqlite", 2))
    assert second.reserve("continuation", "generation")
    assert not second.reserve("over-global", "generation")
    assert suite.used() == 3 and second.trial.used() == 1


def test_finishing_text_or_a_wait_is_not_accepted_completion():
    job = SimpleNamespace(
        role_finished=asyncio.Event(), role_wait_pending=asyncio.Event(), negotiation_pending=asyncio.Event()
    )
    assert terminal_state(job) is None
    job.role_wait_pending.set()
    assert terminal_state(job) == "unfinished_peer_wait"
    job.role_wait_pending.clear()
    job.negotiation_pending.set()
    assert terminal_state(job) == "unfinished_peer_wait"
    job.negotiation_pending.clear()
    job.role_finished.set()
    assert terminal_state(job) == "completed"
