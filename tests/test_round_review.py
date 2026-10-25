"""Shared reviews drain conversations, finish together, and honor stop requests."""

from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from civ_agent.round_review import review_round


@pytest.mark.parametrize("resume", [False, True])
def test_early_review_shares_barrier_resets_anchor_and_suppresses_duplicates(monkeypatch, resume):
    status = {
        "epoch": "g1",
        "serverInstance": "host",
        "handoff": 18,
        "turn": 15,
        "activeCivilization": "Rome",
        "phase": "PAUSED",
    }
    calls, fenced = [], [False]
    requested = None

    def call(name, operation, **fields):
        nonlocal requested
        calls.append((name, operation, fields))
        if operation == "review_triage":
            assert name == "Rome" and not fenced[0]
            assert fields == {"epoch": "g1", "gameTurn": 15, "scheduledSoon": False, "nextReviewTurn": 20}
            if resume:
                requested = "stop"
            return {"earlyReview": True}
        if fields["stage"] == "start":
            assert fenced[0]
            assert fields.get("earlyReview", False) == (name == "Rome")
        return {"civilization": name, "epoch": "g1", "gameTurn": 15, "ready": True, "pendingReview": None}

    agents = {
        name: SimpleNamespace(call=lambda op, name=name, **fields: call(name, op, **fields))
        for name in ("Rome", "Greece")
    }
    scheduler = SimpleNamespace(
        review_intervals={"Rome": 20, "Greece": 4},
        decisions_players={"Rome", "Greece"},
        agents=agents,
        state={"reviewedBoundary": 14, "reviewAnchors": {"Rome": 0, "Greece": 0}},
        round_open=True,
        interrupted=False,
        lease="lease",
        store=SimpleNamespace(requested=lambda: requested),
        operator=SimpleNamespace(call=lambda *args, **kwargs: {"status": status}),
        turns=SimpleNamespace(
            call_agent=lambda agent, op, **fields: agent.call(op, **fields),
            quiesce=lambda: fenced.__setitem__(0, True),
            release=lambda: fenced.__setitem__(0, False),
        ),
        collect_finalizations=Mock(),
        check_status=Mock(),
        check_agent_health=Mock(),
        budget_reason=lambda _: None,
        note=Mock(),
        save=Mock(),
        output=SimpleNamespace(line=Mock()),
    )
    monkeypatch.setattr("civ_agent.round_review.time.sleep", lambda _: None)
    if resume:
        assert not review_round(scheduler, status)
        assert scheduler.state["reviewPhase"]["earlyCivilizations"] == ["Rome"]
        requested = None
        scheduler.round_open = False
        calls.clear()
    assert review_round(scheduler, status)
    assert scheduler.state["reviewAnchors"] == {"Rome": 14, "Greece": 14}
    assert [name for name, op, fields in calls if op == "review_phase" and fields["stage"] == "start"] == [
        "Greece",
        "Rome",
    ]
    calls.clear()
    assert review_round(scheduler, status)
    assert calls == []  # The completed boundary neither reclassifies nor repeats reviews.


def test_early_triage_is_skipped_when_periodic_review_is_next_round():
    calls = []
    agent = SimpleNamespace(call=lambda op, **fields: calls.append((op, fields)))
    scheduler = SimpleNamespace(
        agents={"Rome": agent},
        review_intervals={"Rome": 5},
        decisions_players={"Rome"},
        state={"reviewedBoundary": 14, "reviewAnchors": {"Rome": 10}},
        store=SimpleNamespace(requested=lambda: None),
        interrupted=False,
        round_open=True,
        save=Mock(),
    )
    assert review_round(scheduler, {"epoch": "g1", "turn": 15})
    assert calls == []


@pytest.mark.parametrize("stop_kind", ["stop", "pause", "signal"])
@pytest.mark.parametrize("after", [0, 1, 3])
@pytest.mark.parametrize("accepted", [False, True])
def test_shutdown_stops_triage_fanout_and_retains_accepted_reviews(stop_kind, after, accepted):
    names = ["Rome", "Greece", "France"]
    calls = []

    def call(operation, **fields):
        assert operation == "review_triage"
        calls.append(fields)
        if len(calls) == after and stop_kind == "signal":
            scheduler.interrupted = True
        return {"earlyReview": accepted}

    scheduler = SimpleNamespace(
        agents={name: SimpleNamespace(call=call) for name in names},
        review_intervals=dict.fromkeys(names, 20),
        decisions_players=set(names),
        state={"reviewedBoundary": 14, "reviewAnchors": dict.fromkeys(names, 0)},
        store=SimpleNamespace(requested=lambda: stop_kind if stop_kind != "signal" and len(calls) >= after else None),
        interrupted=stop_kind == "signal" and after == 0,
        turns=SimpleNamespace(call_agent=lambda agent, op, **fields: agent.call(op, **fields)),
        round_open=True,
        note=Mock(),
        save=Mock(),
    )
    assert not review_round(scheduler, {"epoch": "g1", "turn": 15})
    assert len(calls) == after
    assert scheduler.state["reviewedBoundary"] == 14
    scheduler.note.assert_called_once_with(
        "stopped" if stop_kind == "stop" else "paused",
        "Operator requested " + ("pause" if stop_kind == "signal" else stop_kind) + ".",
    )
    if accepted and after:
        assert scheduler.state["reviewPhase"]["earlyCivilizations"] == names[:after]
    else:
        assert "reviewPhase" not in scheduler.state


@pytest.mark.parametrize("anchor,interval,boundary,expected", [(None, 5, 17, 20), (13, 8, 17, 21), (19, 10, 23, 29)])
def test_triage_receives_scheduler_next_review_in_completed_turns(anchor, interval, boundary, expected):
    calls = []
    agent = SimpleNamespace(call=lambda op, **fields: calls.append((op, fields)) or {"earlyReview": False})
    scheduler = SimpleNamespace(
        agents={"Rome": agent},
        review_intervals={"Rome": interval},
        decisions_players={"Rome"},
        state={"reviewedBoundary": boundary - 1, "reviewAnchors": {} if anchor is None else {"Rome": anchor}},
        store=SimpleNamespace(requested=lambda: None),
        interrupted=False,
        turns=SimpleNamespace(call_agent=lambda agent, op, **fields: agent.call(op, **fields)),
        round_open=True,
        save=Mock(),
    )
    assert review_round(scheduler, {"epoch": "g1", "turn": boundary})
    assert calls == [
        (
            "review_triage",
            {"epoch": "g1", "gameTurn": boundary, "scheduledSoon": False, "nextReviewTurn": expected},
        )
    ]


@pytest.mark.parametrize("boundary", [1, 4, 10, 11, 16])
@pytest.mark.parametrize("startup", [False, True])
def test_early_triage_skips_opening_turns_and_startup_then_resumes_at_fresh_boundaries(boundary, startup):
    calls = []
    status = {"epoch": "g1", "turn": boundary}

    def call(operation, **fields):
        if operation == "review_triage":
            calls.append(fields)
            return {"earlyReview": False}
        return {
            "civilization": "Rome",
            "epoch": status["epoch"],
            "gameTurn": status["turn"],
            "ready": True,
            "pendingReview": None,
        }

    agent = SimpleNamespace(call=call)
    scheduler = SimpleNamespace(
        agents={"Rome": agent},
        review_intervals={"Rome": 40},
        decisions_players={"Rome"},
        state={"reviewedBoundary": boundary - 1, "reviewAnchors": {"Rome": 0}},
        store=SimpleNamespace(requested=lambda: None),
        interrupted=False,
        turns=SimpleNamespace(call_agent=lambda agent, op, **fields: agent.call(op, **fields), release=Mock()),
        round_open=not startup,
        save=Mock(),
    )
    assert review_round(scheduler, status)
    assert len(calls) == int(not startup and boundary >= 11)
    assert scheduler.round_open
    if startup and boundary >= 11:
        status["turn"] += 1
        assert review_round(scheduler, status)
        assert len(calls) == 1
        assert calls[0]["gameTurn"] == boundary + 1


@pytest.mark.parametrize("stop", [None, "pause", "stop", "budget"])
@pytest.mark.parametrize("unavailable", [False, True])
def test_all_reports_finish_before_release_and_last_completion_still_checks_limits(monkeypatch, stop, unavailable):
    status = {
        "epoch": "g1",
        "serverInstance": "host",
        "handoff": 18,
        "turn": 6,
        "activeCivilization": "Rome",
        "phase": "PAUSED",
    }
    names = ["Rome", "Greece", "France"]
    calls, started, finished = [], set(), set()
    holds, polls = dict.fromkeys(names, 0), dict.fromkeys(names, 0)
    fenced = False
    requested = None
    budget = None

    def call(name, operation, **fields):
        nonlocal requested, budget
        assert operation == "review_phase"
        stage = fields["stage"]
        calls.append((name, stage))
        ready, pending = True, None
        if stage == "hold":
            holds[name] += 1
            ready = name != "Greece" or holds[name] >= 3  # An existing conversation drains first.
        elif stage == "start":
            assert fenced and holds["Greece"] >= 3
            started.add(name)
        elif stage == "status":
            assert started == set(names) and fenced
            polls[name] += 1
            if name == "Greece" and polls[name] == 1:
                # Report committed, but final accounting/permit release still runs.
                ready = False
            else:
                finished.add(name)
            if finished == set(names):
                if stop == "budget":
                    budget = "Wall-time budget exhausted."
                else:
                    requested = stop
        elif stage == "release":
            assert finished == set(names) and not requested and not budget
        return {
            "civilization": name,
            "epoch": "g1",
            "gameTurn": 6,
            "ready": ready,
            "pendingReview": pending,
            **(
                {"reviewOutcome": {"status": "UNAVAILABLE", "attempts": 2}}
                if unavailable and name == "France" and stage == "status"
                else {}
            ),
        }

    agents = {name: SimpleNamespace(call=lambda op, name=name, **fields: call(name, op, **fields)) for name in names}

    def quiesce():
        nonlocal fenced
        assert holds["Greece"] >= 3
        fenced = True

    def release():
        nonlocal fenced
        assert finished == set(names)
        fenced = False

    scheduler = SimpleNamespace(
        review_intervals=dict.fromkeys(names, 5),
        agents=agents,
        state={"reviewedBoundary": 5},
        round_open=True,
        interrupted=False,
        lease="lease",
        store=SimpleNamespace(requested=lambda: requested),
        operator=SimpleNamespace(call=lambda *args, **kwargs: {"status": status}),
        turns=SimpleNamespace(
            call_agent=lambda agent, op, **fields: agent.call(op, **fields), quiesce=quiesce, release=release
        ),
        collect_finalizations=Mock(),
        check_status=Mock(),
        check_agent_health=Mock(),
        budget_reason=lambda current: budget,
        note=Mock(),
        save=Mock(),
        output=SimpleNamespace(line=Mock()),
    )
    monkeypatch.setattr("civ_agent.round_review.time.sleep", lambda seconds: None)
    assert review_round(scheduler, status) is (stop is None)
    scheduler.collect_finalizations.assert_called_once_with(wait=True)
    if stop:
        assert fenced and scheduler.state["reviewedBoundary"] == 5
        assert not any(stage == "release" for _, stage in calls)
        assert (
            scheduler.note.call_args.args[0]
            == {"pause": "paused", "stop": "stopped", "budget": "budget_exhausted"}[stop]
        )
    else:
        assert not fenced and scheduler.state == {
            "reviewedBoundary": 6,
            "reviewAnchors": {name: 5 for name in names},
            **({"unavailableReviews": [{"civilization": "France", "turn": 5, "attempts": 2}]} if unavailable else {}),
        }
        assert [name for name, stage in calls if stage == "release"] == names
        # Reconnecting at the completed boundary releases holds without another review.
        scheduler.round_open = False
        calls.clear()
        assert review_round(scheduler, status)
        assert calls == [(name, "release") for name in names]


def test_war_review_resets_each_civilizations_interval_clock(monkeypatch):
    names = ["Rome", "Greece"]
    status = {
        "epoch": "g1",
        "serverInstance": "host",
        "handoff": 20,
        "turn": 10,
        "activeCivilization": "Rome",
        "phase": "PAUSED",
        "warDeclarationTurn": 9,
    }
    calls = []

    def call(name, operation, **fields):
        calls.append((name, fields["stage"], fields.get("forceReview", False)))
        return {
            "civilization": name,
            "epoch": "g1",
            "gameTurn": status["turn"],
            "ready": True,
            "pendingReview": None,
        }

    agents = {name: SimpleNamespace(call=lambda op, name=name, **fields: call(name, op, **fields)) for name in names}
    scheduler = SimpleNamespace(
        review_intervals=dict.fromkeys(names, 10),
        agents=agents,
        state={"reviewedBoundary": 9, "reviewAnchors": dict.fromkeys(names, 0)},
        round_open=True,
        interrupted=False,
        lease="lease",
        store=SimpleNamespace(requested=lambda: None),
        operator=SimpleNamespace(call=lambda *args, **kwargs: {"status": status}),
        turns=SimpleNamespace(call_agent=lambda agent, op, **fields: agent.call(op, **fields), quiesce=Mock()),
        collect_finalizations=Mock(),
        check_status=Mock(),
        check_agent_health=Mock(),
        budget_reason=lambda current: None,
        note=Mock(),
        save=Mock(),
        output=SimpleNamespace(line=Mock()),
    )
    scheduler.turns.release = lambda: setattr(scheduler, "round_open", True)
    monkeypatch.setattr("civ_agent.round_review.time.sleep", lambda seconds: None)

    assert review_round(scheduler, status)
    assert scheduler.state == {
        "reviewedBoundary": 10,
        "reviewAnchors": {name: 9 for name in names},
        "reviewedWarTurn": 9,
    }
    assert {(name, forced) for name, stage, forced in calls if stage == "start"} == {
        ("Rome", True),
        ("Greece", True),
    }

    # The old modulo boundary at round 10 no longer fires. Ten completed
    # rounds after the declaration, round 19 is the next interval review.
    calls.clear()
    status["turn"] = 11
    assert review_round(scheduler, status)
    assert not [row for row in calls if row[1] == "start"]
    status["turn"] = 20
    assert review_round(scheduler, status)
    assert {(name, forced) for name, stage, forced in calls if stage == "start"} == {
        ("Rome", True),
        ("Greece", True),
    }
