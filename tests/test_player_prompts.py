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
        "observation": {"cities": [], "map": {"tiles": []}},
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
        "breaking social promises are available strategies",
        "not action restrictions or automatic breach penalties",
        "withdrawal is optional",
        "Interpret their stated terms and conditions",
        "native treaties and declarations of friendship have separate engine effects",
        "Before accepting or renewing peace, compare who gains more",
        "compare acting now with waiting for its stated expiry or keeping peace",
        "Separate the costs of war itself from additional costs of breaking the promise",
        "who could learn of it, how, and what different response is plausible",
        "do not assume automatic public knowledge or a coalition",
        "surprise, lost military opportunities and rival progress while waiting",
    ):
        assert principle in DIPLOMACY_GUIDANCE


def test_review_compares_limited_intervention_with_relative_development():
    for principle in (
        "whether it can finish before rivals, not just improve our output",
        "compare catching up with timely disruption or a coalition",
        "Limited military action can support any victory",
        "Use reconnaissance to resolve important gaps",
        "do not assume either peace or war is best",
    ):
        assert principle in STRATEGY_GUIDANCE
    for principle in (
        "Use one plan when only one is credible",
        "comparable short-, mid- and long-term horizons",
        "Military costs include recruitment, upgrades, positioning and travel",
        "taking a useful city, pillaging or forcing rival military spending",
        "conditions for seeking peace",
        "rather than judging every military option as a full domination campaign",
        "benefits to uninvolved rivals",
        "earliest feasible intervention with rival progress and the opportunity window",
    ):
        assert principle in ADVISOR_INSTRUCTIONS


def test_victory_horizon_keeps_locked_endgame_projects_out_of_early_diplomacy_and_memory():
    for principle in (
        "Separate a broad victory direction from its terminal projects",
        "background rules, not current objectives or routine intelligence requirements",
        "direct enabling prerequisite is available or expected within the current planning horizon",
        "General output or technology and policy counts do not make a terminal project actionable",
        "an unstarted terminal project is not evidence of danger",
        "do not request or volunteer updates about its absence",
        "turn it into a diplomatic initiative",
        "put those reports into agreements",
        "create reminders or memory records for them",
        "only according to its stated checkpoint or trigger",
        "do not repeat no-change reports between checkpoints",
        "renew an irrelevant reporting clause by default",
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
