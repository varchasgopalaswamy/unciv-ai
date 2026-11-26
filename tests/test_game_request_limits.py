"""Ordinary configuration binds the existing native SDK generation ceiling."""

import pytest
from pydantic import ValidationError

from civ_agent.game_config import AgentDefaults, GameAgentSettings, GameConfig


@pytest.mark.parametrize("limit", [0, 101, -1, True, "12", 12.0])
def test_game_request_limit_rejects_invalid_native_scope_ceilings(limit):
    with pytest.raises(ValidationError):
        GameConfig.model_validate({"agents": {"request_limit": limit}})


@pytest.mark.parametrize("limit", [1, 12, 24, 100])
def test_public_agents_model_constructor_preserves_settings_and_subclass_budget(limit):
    settings = AgentDefaults(model="gpt-6-luna", reasoning_effort="low", review_attempts=1)
    direct = GameConfig(agents=settings)
    assert direct.agents.request_limit == 24
    assert direct.agents.model_dump(exclude={"request_limit"}) == settings.model_dump()
    assert direct == GameConfig.model_validate({"agents": settings.model_dump()})
    configured = GameAgentSettings(**settings.model_dump(), request_limit=limit)
    assert GameConfig(agents=configured).agents.request_limit == limit
