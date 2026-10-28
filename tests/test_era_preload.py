"""Early preloads develop science without priming a premature space race."""

import copy

import pytest

from civ_agent.observations import overview
from civ_agent.startup_context import startup_prompt, startup_text
from civ_agent.strategic_review import advice_prompt, frozen_read
from civ_agent.strategy_guide import (
    EARLY_GAME_GUIDANCE,
    ERA_FILES,
    LATE_GAME_GUIDANCE,
    ROLE_SECTIONS,
    current_constraints,
    era_brief,
    era_guidance,
    modern_era_reached,
    player_strategy_reference,
    strategy_reference,
)
from tests.test_tactical_review import reviewed
from tests.test_victory_snapshot import position


@pytest.mark.parametrize("modern", [False, True])
@pytest.mark.parametrize("purpose", ["gameplay", "economy", "military", "communication", "interactive", "review"])
@pytest.mark.parametrize("guide", [False, True])
def test_preloads_follow_known_major_era_for_all_roles(modern, purpose, guide):
    progress = {
        "current": "Renaissance era",
        "highestKnownMajor": "Modern era" if modern else "Industrial era",
        "modernOrLater": modern,
    }
    # A rival's entry is sufficient even while our own civilization is earlier.
    reads = {
        "player_turn": {"turn": 10, "eraProgress": progress},
        "chat_read": {"civilization": "Rome", "context": {"eraProgress": progress}},
        "victory_progress": position(10),
    }
    if purpose == "communication":
        del reads["player_turn"]
    original = copy.deepcopy(reads)
    prepared = {"coreTools": [], "readInstructions": {}, "reads": reads}
    if purpose == "review":
        inputs = {
            "catalogue": [],
            "civilization": "Rome",
            "turn": 10,
            "observation": {"turn": 10},
            "reads": reads,
            "actions": {"entries": []},
        }
        prompt = advice_prompt(inputs, strategy_guide=guide)
    elif purpose == "interactive":
        prompt = startup_text(prepared, strategy_guide=guide)
    else:
        prompt = startup_prompt(prepared, purpose, "Choose next steps", strategy_guide=guide)
    text = prompt if isinstance(prompt, str) else prompt.user
    assert (EARLY_GAME_GUIDANCE in text) is not modern
    assert (LATE_GAME_GUIDANCE in text) is modern
    assert ("Apollo" in text) is (modern and guide)
    assert ("spaceship" in text) is (modern and guide)
    if guide:
        assert player_strategy_reference(reads, "gameplay" if purpose == "interactive" else purpose) in text
    assert reads == original


@pytest.mark.parametrize("era", ERA_FILES)
@pytest.mark.parametrize("purpose", ROLE_SECTIONS)
def test_each_role_gets_its_own_era_and_relevant_sections(era, purpose):
    reads = {"player_turn": {"eraProgress": {"current": era, "highestKnownMajor": era, "modernOrLater": False}}}
    reference = player_strategy_reference(reads, purpose)
    assert f"## {era}: guidance for {purpose}" in reference
    for heading in ROLE_SECTIONS["gameplay"]:
        assert (f"### {heading}\n" in reference) is (heading in ROLE_SECTIONS[purpose])
    assert all(f"## {other}: guidance" not in reference for other in ERA_FILES if other != era)
    assert len(era_brief(era, purpose).split()) <= 280


def test_advanced_rival_adds_warning_without_replacing_our_development_brief():
    reads = {
        "player_turn": {
            "eraProgress": {"current": "Medieval era", "highestKnownMajor": "Modern era", "modernOrLater": True}
        }
    }
    reference = player_strategy_reference(reads)
    guidance = era_guidance(reads)
    assert "## Medieval era: guidance" in reference and "## Modern era: guidance" not in reference
    assert "Education brings Universities" in reference and "Apollo" in reference
    assert "A known major civilization is in a later era (Modern era)" in guidance
    assert "a later era" not in era_guidance(
        {
            "player_turn": {
                "eraProgress": {"current": "Modern era", "highestKnownMajor": "Modern era", "modernOrLater": True}
            }
        }
    )


def test_unavailable_era_uses_core_without_guessing_an_opening_or_late_capability():
    for era in (None, "Unrecognized era"):
        reference = player_strategy_reference({"player_turn": {"eraProgress": {"current": era}}})
        assert reference == strategy_reference()
        assert "Apollo" not in reference and ": guidance for" not in reference


def test_current_constraints_prioritize_supplied_shortages_without_misclassifying_missing_data():
    reads = {
        "player_turn": {
            "economy": {"happiness": -2, "goldPerTurn": -7, "unitSupply": {"productionPenaltyPercent": -70}},
            "cities": [{"handle": "city1", "foodPerTurn": -3}, {"handle": "city2", "foodPerTurn": 0}],
        }
    }
    before = copy.deepcopy(reads)
    constraints = current_constraints(reads)
    assert "unit supply production penalty -70%" in constraints
    assert "happiness -2" in constraints and "net gold per turn -7" in constraints
    assert "negative food in city1" in constraints and "city2" not in constraints
    assert era_guidance(reads).startswith(constraints)
    assert reads == before
    for missing in ({}, {"player_turn": {"economy": {"happiness": 0, "goldPerTurn": 1}}}):
        assert current_constraints(missing) == ""
    assert "happiness -1" in current_constraints({"chat_read": {"context": {"economy": {"happiness": -1}}}})


def test_constraints_precede_inherited_plans_without_erasing_player_memory():
    prepared = {
        "coreTools": [],
        "readInstructions": {},
        "reads": {"player_turn": {"economy": {"unitSupply": {"productionPenaltyPercent": -70}}}},
        "strategicAdvice": {"assessment": "Inherited Apollo race"},
        "memoryContext": "PRIVATE_NOTE_SENTINEL",
    }
    before = copy.deepcopy(prepared)
    prompt = startup_prompt(prepared, "gameplay", "Choose next steps", strategy_guide=True)
    assert prompt.user.index("production penalty -70%") < prompt.user.index("Inherited Apollo race")
    assert prompt.user.count("Inherited Apollo race") == 1
    assert prompt.user.count("PRIVATE_NOTE_SENTINEL") == 1
    assert "Apollo" not in prompt.strategy_reference
    assert prepared == before


def test_vanilla_unlocks_and_early_victory_options_stay_in_the_appropriate_briefs():
    assert "National College" in era_brief("Classical era")
    assert "Education brings Universities" in era_brief("Medieval era")
    assert "Scientific Theory unlocks Public Schools" in era_brief("Renaissance era")
    assert "factories" in era_brief("Industrial era")
    assert "Plastics unlocks Research Labs" in era_brief("Modern era")
    assert "earlier victory opportunity" in strategy_reference()


def test_era_summary_survives_overview_and_missing_legacy_context_stays_early():
    snapshot = reviewed()
    snapshot["eraProgress"] = {"current": "Modern era", "highestKnownMajor": "Modern era", "modernOrLater": True}
    assert modern_era_reached({"player_turn": overview(snapshot)})
    assert not modern_era_reached({})
    assert not modern_era_reached({"chat_read": {"context": {}}})


def test_early_review_can_explicitly_inspect_science_victory_requirements():
    victory = position(10)
    victory["victories"][0]["milestones"] = [{"description": "Build Apollo Program", "status": "Current"}]
    inputs = {"reads": {"victory_progress": victory}, "civilization": "Rome"}
    response = frozen_read(inputs, "victory_progress", {"sections": ["our_status"], "names": ["Scientific"]})
    assert response["victories"][0]["milestones"][0]["description"] == "Build Apollo Program"
