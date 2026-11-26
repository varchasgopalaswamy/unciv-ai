"""Accepted native observations refresh role guards after another actor's peace."""

import copy
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from civ_agent.economy_roles import TurnRoles
from tests.domain_fixtures import run_async
from tests.test_diplomacy_contracts import opened


@pytest.mark.parametrize("still_at_war", [False, True])
@run_async
async def test_fresh_native_diplomacy_updates_initial_goal_guard(tmp_path, still_at_war):
    async with opened(tmp_path, "gameplay") as (gateway, main, host):
        service = SimpleNamespace(memory=gateway.memory, io=gateway.io, records={}, _save=AsyncMock())
        roles = TurnRoles(service, {"epoch": "g1", "gameTurn": 0, "activationId": main.id})
        gateway.role_coordinator = roles
        roles.register(main)
        host.view["diplomacy"] = {
            "knownCivilizations": [{"civilizationId": "Greece", "atWar": True, "defeated": False}],
            "cityStates": [],
        }
        await roles.synchronize_war(await host.tool("player_turn", {}))
        blocked = await gateway.call(main, "finish_role", {}, "before-observation")
        assert blocked["error"] == "INITIAL_WAR_GOALS_REQUIRED"
        assert blocked["opponents"] == ["Greece"]
        host.view["diplomacy"]["knownCivilizations"][0]["atWar"] = still_at_war
        host.revision = "fresh-diplomacy"

        delivered = await gateway.call(main, "player_turn", {}, "fresh-diplomacy")
        assert "error" not in delivered
        assert main.observation is not None
        assert main.observation["diplomacy"]["knownCivilizations"][0]["atWar"] is still_at_war
        assert any(episode["active"] for episode in roles.war_state["episodes"].values()) is still_at_war
        finished = await gateway.call(main, "finish_role", {}, "after-observation")
        if still_at_war:
            assert finished["error"] == "INITIAL_WAR_GOALS_REQUIRED"
            assert finished["opponents"] == ["Greece"]
        else:
            assert finished["roleFinished"] is True
            episode = next(iter(roles.war_state["episodes"].values()))
            assert episode["version"] == 0 and not episode["goals"]
        assert not host.executed  # Observing peace never enacts a game command.


@run_async
async def test_replayed_old_peace_read_does_not_roll_back_new_war_state(tmp_path):
    async with opened(tmp_path, "gameplay") as (gateway, main, host):
        service = SimpleNamespace(memory=gateway.memory, io=gateway.io, records={}, _save=AsyncMock())
        roles = TurnRoles(service, {"epoch": "g1", "gameTurn": 0, "activationId": main.id})
        gateway.role_coordinator = roles
        roles.register(main)
        host.view["diplomacy"] = {
            "knownCivilizations": [{"civilizationId": "Greece", "atWar": False, "defeated": False}],
            "cityStates": [],
        }
        peace = await gateway.call(main, "player_turn", {}, "peace-read")
        host.view["diplomacy"]["knownCivilizations"][0]["atWar"] = True
        host.revision = "new-war"
        await gateway.call(main, "player_turn", {}, "new-war-read")
        current = copy.deepcopy(roles.war_state)
        assert any(episode["active"] for episode in current["episodes"].values())

        assert await gateway.call(main, "player_turn", {}, "peace-read") == peace
        assert roles.war_state == current
        blocked = await gateway.call(main, "finish_role", {}, "new-war-finish")
        assert blocked["error"] == "INITIAL_WAR_GOALS_REQUIRED"
        assert not host.executed
