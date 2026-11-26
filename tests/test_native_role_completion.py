"""Native choices fence new role completion without changing frozen jobs."""

import asyncio
import copy
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from civ_agent.economy_roles import FinishRole, TurnRoles
from civ_agent.tool_schemas import EconomyFinishRole
from tests.domain_fixtures import run_async


def native_view(pending=(), *, defer=True):
    return {
        "phase": "ACTIVE",
        "epoch": "e1",
        "turn": 4,
        "revision": "r1",
        "canEndTurn": not pending,
        "pending": [{"kind": kind, "supported": True} for kind in pending],
        "turnControls": {"canDeferChoices": defer},
        "cities": [{"handle": "city1", "puppet": False, "constructionQueue": []}],
    }


def coordinator(view, *, strict=True):
    service = SimpleNamespace(_save=AsyncMock(), records={})
    record = {"epoch": "e1", "gameTurn": 4, "activationId": "turn1"}
    roles = TurnRoles(service, record)
    jobs = {}
    for purpose in ("gameplay", "economy", "military"):
        job = SimpleNamespace(
            purpose=purpose,
            epoch="e1",
            memory={"gameTurn": 4, **({"nativeRoleContract": 1} if strict else {})},
            observation=native_view(),
            connection=SimpleNamespace(tool=AsyncMock(return_value=view)),
            pending_calls={},
            gameplay_ended=asyncio.Event(),
        )
        jobs[purpose] = job
        roles.register(job)
    return roles, jobs


def unchanged_state(roles, jobs):
    return copy.deepcopy(
        {
            "state": roles.state(),
            "record": roles.record,
            "revision": roles.economy_revision,
            "messages": roles.messages,
            "events": {role: job.role_finished.is_set() for role, job in jobs.items()},
        }
    )


@pytest.mark.parametrize(
    "kind",
    [
        "Research",
        "Policy",
        "FreeGreatPerson",
        "DiplomaticVote",
        "PopupAlerts",
        "TradeRequests",
        "DiplomaticVotingResults",
        "FutureNativeChoice",
    ],
)
@run_async
async def test_main_cannot_finish_its_fresh_native_choices(kind):
    view = native_view([kind])
    view["pending"][0]["futureNativeDetail"] = {"target": "native1"}
    roles, jobs = coordinator(view)
    before = unchanged_state(roles, jobs)
    result = await roles.tool(jobs["gameplay"], "finish_role", {})
    assert result["error"] == "NATIVE_ROLE_DECISIONS_REQUIRED" and result["recoverable"]
    assert not result["applied"]
    assert result["roleCompletion"]["pendingChoices"] == view["pending"]
    assert unchanged_state(roles, jobs) == before
    roles.service._save.assert_not_awaited()
    jobs["gameplay"].connection.tool.assert_awaited_once_with("player_turn", {})


@run_async
async def test_main_can_finish_while_economy_owns_required_construction():
    roles, jobs = coordinator(native_view(["Construction"]))
    result = await roles.tool(jobs["gameplay"], "finish_role", {})
    assert result["roleFinished"]
    assert roles.ready["gameplay"] and not roles.ready["economy"]


@pytest.mark.parametrize("returned", [False, True])
@run_async
async def test_main_must_decide_overridden_or_returned_construction(returned):
    view = native_view(["Construction"])
    roles, jobs = coordinator(view)
    if returned:
        roles.record["handoffReview"] = {"pending": view["pending"], "role": "gameplay"}
    else:
        roles.overrides["city1"] = "Main owns this city's decision."
    before = unchanged_state(roles, jobs)
    assert (await roles.tool(jobs["gameplay"], "finish_role", {}))["error"] == "NATIVE_ROLE_DECISIONS_REQUIRED"
    assert unchanged_state(roles, jobs) == before


@run_async
async def test_economy_cannot_acknowledge_unresolved_owned_construction():
    roles, jobs = coordinator(native_view(["Construction"]))
    before = unchanged_state(roles, jobs)
    result = await roles.tool(jobs["economy"], "finish_role", {})
    assert result["error"] == "NATIVE_ROLE_DECISIONS_REQUIRED"
    assert result["roleCompletion"]["constructionCities"] == [{"city": "city1", "owner": "economy"}]
    assert unchanged_state(roles, jobs) == before
    assert roles.economy_completions == 0


@run_async
async def test_economy_does_not_complete_main_owned_choices_or_invent_required_work():
    roles, jobs = coordinator(native_view(["Research", "Construction"]))
    roles.overrides["city1"] = "Main will decide construction."
    assert (await roles.tool(jobs["economy"], "finish_role", {}))["roleFinished"]
    roles, jobs = coordinator(native_view())
    assert (await roles.tool(jobs["economy"], "finish_role", {}))["roleFinished"]


@pytest.mark.parametrize("reason", ["", "Wait for the next opportunity."])
@run_async
async def test_main_can_explicitly_defer_only_when_native_allows_it(reason):
    roles, jobs = coordinator(native_view(["Research"], defer=True))
    assert (await roles.tool(jobs["gameplay"], "finish_role", {"deferChoices": True, "deferReason": reason}))[
        "roleFinished"
    ]
    assert roles.deferral == {"deferChoices": True, "deferReason": reason}
    roles, jobs = coordinator(native_view(["PopupAlerts"], defer=False))
    before = unchanged_state(roles, jobs)
    result = await roles.tool(jobs["gameplay"], "finish_role", {"deferChoices": True, "deferReason": reason})
    assert result["error"] == "NATIVE_ROLE_DEFERRAL_NOT_PERMITTED"
    assert unchanged_state(roles, jobs) == before


@run_async
async def test_explicit_main_deferral_also_permits_economy_completion():
    roles, jobs = coordinator(native_view(["Construction"]))
    assert (await roles.tool(jobs["gameplay"], "finish_role", {"deferChoices": True}))["roleFinished"]
    assert (await roles.tool(jobs["economy"], "finish_role", {}))["roleFinished"]
    assert roles.deferral == {"deferChoices": True}


@run_async
async def test_economy_can_explicitly_transfer_its_native_choices_to_main():
    view = native_view(["Construction"])
    roles, jobs = coordinator(view)
    roles.ready["gameplay"] = True
    jobs["gameplay"].role_finished.set()
    result = await roles.tool(jobs["economy"], "finish_role", {"escalateChoices": "Main must decide whether to wait."})
    assert result["roleFinished"] and result["nativeChoicesEscalated"]
    assert roles.ready["economy"] and not roles.ready["gameplay"]
    assert roles.economy_completions == 1
    assert not jobs["gameplay"].role_finished.is_set()
    assert roles.record["handoffReview"] == {"pending": view["pending"], "role": "gameplay"}
    assert "Main must decide whether to wait" in roles.messages["gameplay"]
    assert (await roles.tool(jobs["gameplay"], "finish_role", {}))["error"] == "NATIVE_ROLE_DECISIONS_REQUIRED"
    assert (await roles.tool(jobs["gameplay"], "finish_role", {"deferChoices": True}))["roleFinished"]


@pytest.mark.parametrize(
    "role,pending,arguments",
    [
        ("gameplay", ["Construction"], {"escalateChoices": "Transfer it."}),
        ("military", ["Construction"], {"escalateChoices": "Transfer it."}),
        ("economy", [], {"escalateChoices": "Transfer it."}),
        ("economy", ["Construction"], {"escalateChoices": "   "}),
        ("economy", ["Construction"], {"escalateChoices": "Transfer it.", "deferChoices": True}),
        ("economy", ["Construction"], {"escalateChoices": "Transfer it.", "deferReason": "Wait."}),
        ("economy", ["Construction"], {"escalateChoices": "Transfer it.", "initiativeDecisions": [{}]}),
    ],
)
@run_async
async def test_invalid_escalation_cannot_change_readiness_or_review(role, pending, arguments):
    roles, jobs = coordinator(native_view(pending))
    before = unchanged_state(roles, jobs)
    assert (await roles.tool(jobs[role], "finish_role", arguments))["error"] == "NATIVE_ROLE_ESCALATION_INVALID"
    assert unchanged_state(roles, jobs) == before


@run_async
async def test_failed_economy_review_does_not_transfer_choices_or_mutate_scope():
    view = native_view(["Construction"])
    roles, jobs = coordinator(view)
    roles.economy_schedule = SimpleNamespace(complete=Mock(return_value=False))
    roles.economy_observation = copy.deepcopy(view)
    roles.service.io = SimpleNamespace(call=AsyncMock(side_effect=lambda fn, *args, **kwargs: fn(*args, **kwargs)))
    roles.synchronize_economy = AsyncMock()
    before = unchanged_state(roles, jobs)
    result = await roles.tool(jobs["economy"], "finish_role", {"escalateChoices": "Main must decide."})
    assert result["error"] == "ECONOMY_OBSERVATION_CHANGED"
    assert unchanged_state(roles, jobs) == before
    roles.synchronize_economy.assert_not_awaited()
    roles.service._save.assert_not_awaited()


@pytest.mark.parametrize(
    "change", [{"phase": "PAUSED"}, {"epoch": "old"}, {"turn": 3}, {"pending": [{"kind": "Research"}]}]
)
@run_async
async def test_missing_or_stale_native_facts_cannot_complete(change):
    roles, jobs = coordinator({**native_view(["Research"]), **change})
    before = unchanged_state(roles, jobs)
    assert (await roles.tool(jobs["gameplay"], "finish_role", {}))["error"] == "NATIVE_ROLE_REFRESH_REQUIRED"
    assert unchanged_state(roles, jobs) == before


@run_async
async def test_native_endable_view_may_omit_empty_pending_list():
    view = native_view()
    del view["pending"]
    roles, jobs = coordinator(view)
    assert (await roles.tool(jobs["gameplay"], "finish_role", {}))["roleFinished"]


@pytest.mark.parametrize("role", ["gameplay", "economy"])
@run_async
async def test_blocked_native_view_with_no_choice_facts_requires_refresh(role):
    view = native_view()
    view["canEndTurn"] = False
    roles, jobs = coordinator(view)
    before = unchanged_state(roles, jobs)
    assert (await roles.tool(jobs[role], "finish_role", {}))["error"] == "NATIVE_ROLE_REFRESH_REQUIRED"
    assert unchanged_state(roles, jobs) == before


@run_async
async def test_frozen_job_without_marker_keeps_its_original_finish_contract():
    roles, jobs = coordinator(native_view(["Research"]), strict=False)
    assert (await roles.tool(jobs["gameplay"], "finish_role", {}))["roleFinished"]
    jobs["gameplay"].connection.tool.assert_not_awaited()


def test_escalation_is_bounded_semantic_input():
    assert EconomyFinishRole(escalateChoices="Main should decide.").escalateChoices == "Main should decide."
    assert "escalateChoices" in EconomyFinishRole.model_json_schema()["properties"]
    assert "escalateChoices" not in FinishRole.model_json_schema()["properties"]
