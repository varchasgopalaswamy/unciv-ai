"""Strategy prompts compare alternatives without prescribing war or promise keeping."""

import copy

import pytest

from civ_agent.player_prompts import DIPLOMACY_GUIDANCE, STRATEGY_GUIDANCE, VICTORY_HORIZON_GUIDANCE
from civ_agent.protocol import JsonObject
from civ_agent.startup_context import startup_prompt
from civ_agent.strategic_review import ADVISOR_INSTRUCTIONS, advice_prompt
from civ_agent.strategy_guide import strategy_reference
from tests.test_victory_snapshot import position


@pytest.fixture
def review_inputs() -> JsonObject:
    return {
        "catalogue": [],
        "turn": 10,
        "civilization": "Rome",
        "observation": {"turn": 10, "cities": [], "map": {"tiles": []}},
        "reads": {
            "player_turn": {"turn": 10, "civilization": "Rome"},
            "victory_progress": position(10),
            "chat_read": {"civilization": "Rome", "channels": ["Rome", "France"], "context": {"turn": 10}},
        },
        "actions": {"entries": []},
    }


@pytest.mark.parametrize("purpose", ["gameplay", "communication", "review"])
@pytest.mark.parametrize("strategy_guide", [False, True])
def test_guidance_reaches_each_prompt_independently_of_optional_reference(review_inputs, purpose, strategy_guide):
    before = copy.deepcopy(review_inputs)
    if purpose == "review":
        prompt = advice_prompt(review_inputs, strategy_guide=strategy_guide)
    else:
        prepared = {"coreTools": [], "readInstructions": {}, "reads": review_inputs["reads"]}
        prompt = startup_prompt(prepared, purpose, "Choose your next steps", strategy_guide=strategy_guide)
    assert prompt.instructions.count(DIPLOMACY_GUIDANCE) == 1
    assert prompt.instructions.count(STRATEGY_GUIDANCE) == int(purpose != "communication")
    assert prompt.instructions.count(VICTORY_HORIZON_GUIDANCE) == 1
    assert prompt.user.count(strategy_reference()) == int(strategy_guide)
    assert review_inputs == before


def test_peace_guidance_preserves_agency_and_compares_incremental_betrayal_costs():
    for principle in (
        "bluff or break a social promise",
        "no automatic penalties or action restrictions",
        "you need not withdraw",
        "Private promises are not automatically public",
        "compare acting now, waiting for it to expire and keeping it",
    ):
        assert principle in DIPLOMACY_GUIDANCE


def test_review_compares_limited_intervention_with_relative_development():
    for principle in (
        "finishing before rivals",
        "catching up, disrupting a rival or finding allies",
        "neither peace nor war is always best",
    ):
        assert principle in STRATEGY_GUIDANCE
    for principle in (
        "over the same number of turns",
        "or one if only one is realistic",
        "gains to uninvolved rivals",
        "say when to seek peace",
    ):
        assert principle in ADVISOR_INSTRUCTIONS


def test_victory_horizon_focuses_information_on_current_decisions():
    for principle in (
        "choices that matter over the next several turns",
        "could change today's decision",
        "A missing requirement does not mean a rival has started its final project",
    ):
        assert principle in VICTORY_HORIZON_GUIDANCE


def test_optional_reference_matches_peace_and_relative_finish_guidance():
    reference = " ".join(strategy_reference().split())
    for principle in (
        "These are suggestions, not instructions, rules, or a checklist",
        "Before accepting or renewing peace, compare who gains more",
        "Separate the costs of war from the additional costs of breaking the promise",
        "Private terms are not automatically public",
        "withdrawal is optional",
        "Improving our output does not establish that we can finish before rivals",
        "neither peace nor war is automatically best",
    ):
        assert principle in reference
