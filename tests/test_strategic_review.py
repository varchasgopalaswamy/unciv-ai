"""Periodic reports precede the next own turn and never overwrite private plans."""

import asyncio
import copy
import json
from types import SimpleNamespace

import pytest

from civ_agent.civilization_state import CivilizationState
from civ_agent.proposal_gateway import ProposalGateway
from civ_agent.protocol import JsonObject
from civ_agent.review_store import ReviewStore
from civ_agent.service_io import ServiceIO
from civ_agent.startup_context import prepare_startup, startup_prompt
from civ_agent.strategic_review import advice_schema, frozen_read, parse_advice, review_cities, review_due, review_facts
from tests.test_agent_service import StartupHost
from tests.test_structured_memory import setup

VICTORIES = ["Scientific", "Cultural", "Domination", "Diplomatic", "Time"]


def assessment(victories=VICTORIES):
    return {
        "assessment": "Invest in production and compare our spaceship timing against Greece before committing to war.",
        "review": {
            "victoryPaths": [
                {
                    "victory": victory,
                    "remaining": "Inspect the remaining native milestones and resource requirements.",
                    "objective": "Establish whether investment or an attack offers the better next gain.",
                    "investment": "Compare production, technology and preparation time for the next objective.",
                    "payoff": "Estimate our gains and the effect on Greece's lead, subject to scouting.",
                    "projection": "Unknown until the remaining production and research costs are inspected.",
                    "rivals": "Greece has installed a spaceship part; its final timing is unknown.",
                }
                for victory in victories
            ],
            "race": "Greece could finish first. Our current pace does not establish that we can overtake it.",
            "alternative": "An attack may delay Greece but requires siege preparation and a safe approach.",
            "disruption": "Scout and price a siege force; account for defensive pacts before choosing war.",
            "agreements": "Avoid assuming a reciprocal research agreement improves our relative chances.",
            "shortTerm": [
                "Inspect production investments and rival approaches this turn before spending the treasury."
            ],
            "midTerm": "Build productive capacity over 5–15 turns, then reassess the race and military option.",
            "longTerm": "Aim to complete the spaceship within 40 turns; intervene if Greece's lead becomes decisive.",
        },
    }


def host_at(turn, victories=VICTORIES):
    host = StartupHost(
        SimpleNamespace(service=SimpleNamespace(active_id="job", records={"job": {"epoch": "g1", "gameTurn": turn}}))
    )
    host.view.update(turn=turn, rules={"victoryTypes": victories, "maxTurns": 330})
    return host


@pytest.mark.parametrize(
    "turn,interval,expected",
    [
        (0, 5, False),
        (4, 5, False),
        (5, 5, True),
        (6, 5, False),
        (10, 5, True),
        (100, 5, True),
        (3, 3, True),
        (5, 3, False),
        (1, 1, True),
    ],
)
def test_review_cadence(turn, interval, expected):
    assert review_due(turn, interval) is expected


@pytest.mark.parametrize("interval", [0, -1, True, 1.5])
def test_interval_must_be_positive_integer(interval):
    with pytest.raises(ValueError):
        review_due(5, interval)


def test_review_due_does_not_gate_actions_or_require_memory_write(tmp_path):
    async def run():
        io, memory = ServiceIO(), CivilizationState(tmp_path / "memory", "Rome")
        gateway = ProposalGateway(memory, io, tmp_path / "workspace")
        try:
            job = await gateway.open("job", "gameplay", "g1", host_at(5), {}, 5)
            prepared = await prepare_startup(gateway, job)
            prompt = startup_prompt(prepared, "gameplay", "Play this turn")
            assert "Strategic review due" not in prompt.user
            result = await gateway.call(job, "player_command", {"kind": "END_TURN"}, "end")
            assert result["kind"] == "END_TURN" and "memoryPhase" not in result
            assert await gateway.tools(job) == []
        finally:
            await io.close()
            memory.engine.dispose()

    asyncio.run(run())


def test_advice_requires_every_enabled_victory():
    value = assessment()
    assert parse_advice(json.dumps(value), {"turn": 5, "rules": {"victoryTypes": VICTORIES}}) == value
    value["review"]["victoryPaths"].pop()
    with pytest.raises(ValueError, match="STRATEGIC_REVIEW_COVERAGE"):
        parse_advice(json.dumps(value), {"turn": 5, "rules": {"victoryTypes": VICTORIES}})


def test_report_schema_uses_exact_enabled_victory_names():
    for enabled in (VICTORIES, ["Domination", "Time"]):
        schema = advice_schema({"rules": {"victoryTypes": enabled}})
        assert schema["$defs"]["VictoryProjection"]["properties"]["victory"]["enum"] == enabled
        paths = schema["$defs"]["StrategicReview"]["properties"]["victoryPaths"]
        assert paths["minItems"] == paths["maxItems"] == len(enabled)
    advice = assessment()
    advice["review"]["victoryPaths"][0]["victory"] = "Scientific — hedge"
    with pytest.raises(ValueError, match="STRATEGIC_REVIEW_COVERAGE"):
        parse_advice(json.dumps(advice), {"turn": 0, "rules": {"victoryTypes": VICTORIES}})


def test_review_filter_preserves_player_authored_terms():
    terms = {"strategyReview": "Talk before invading", "memoryUpdates": "Keep this promise"}
    assert review_facts({"strategyReview": {"private": "old plan"}, "promises": [terms]}) == {"promises": [terms]}


def test_city_summary_uses_observed_identity_and_visibility_with_wrapped_distances():
    snapshot: JsonObject = {
        "cities": [{"name": "Rome", "development": {"position": {"x": 0, "y": 0}}}],
        "map": {
            "wrapOffset": {"x": 10, "y": 0},
            "tiles": [
                {"cityName": "Rome", "owner": "Rome", "position": {"x": 0, "y": 0}},
                {"cityName": None, "owner": "Egypt", "position": {"x": 8, "y": 0}},
                {
                    "cityName": "Thebes",
                    "owner": "Egypt",
                    "position": {"x": 9, "y": 0},
                    "visible": True,
                    "observedTurn": 20,
                    "cityIsOriginalMajorCapital": True,
                    "cityPopulation": 4,
                    "cityHealth": 160,
                    "cityMaxHealth": 200,
                    "cityDefensiveStrength": 12,
                    "constructionQueue": ["PRIVATE"],
                    "units": [{"name": "Warrior"}],
                },
                {
                    # A duplicate name is a different city, not a capital match.
                    "cityName": "Thebes",
                    "owner": "Greece",
                    "position": {"x": 4, "y": 0},
                    "visible": False,
                    "observedTurn": 12,
                    "cityIsOriginalMajorCapital": False,
                    "cityPopulation": 9,
                    "cityHealth": 20,
                    "cityMaxHealth": 300,
                    "cityDefensiveStrength": 25,
                },
                {
                    "cityName": "Sidon",
                    "owner": "Sidon",
                    "position": {"x": 2, "y": 0},
                    "visible": False,
                    "observedTurn": 15,
                    "cityIsOriginalMajorCapital": False,
                },
            ],
        },
    }
    before = copy.deepcopy(snapshot)
    cities = review_cities(snapshot, "Rome")
    assert snapshot == before
    assert [city["civilization"] for city in cities] == ["Egypt", "Sidon", "Greece"]
    assert cities[0] == {
        "name": "Thebes",
        "civilization": "Egypt",
        "position": {"x": 9, "y": 0},
        "visible": True,
        "observedTurn": 20,
        "originalMajorCapital": True,
        "population": 4,
        "health": 160,
        "maxHealth": 200,
        "defensiveStrength": 12,
        "nearestOwnCity": "Rome",
        "distanceInHexes": 1,
    }
    assert cities[-1] == {
        "name": "Thebes",
        "civilization": "Greece",
        "position": {"x": 4, "y": 0},
        "visible": False,
        "observedTurn": 12,
        "originalMajorCapital": False,
        "nearestOwnCity": "Rome",
        "distanceInHexes": 4,
    }
    snapshot["map"]["tiles"][2]["position"]["x"] = 100
    assert cities[0]["position"]["x"] == 9
    snapshot["cities"] = []
    assert all("distanceInHexes" not in city for city in review_cities(snapshot, "Rome"))


def test_frozen_social_read_keeps_old_ledger_and_duplicate_overview_out_of_preload():
    inputs = {
        "reads": {
            "chat_read": {
                "context": {"turn": 5, "economy": {"gold": 120}},
                "messages": [{"text": "Current discussion"}],
                "agreements": [{"status": "ENDED", "id": "old"}, {"status": "ACTIVE", "id": "current"}],
                "outcomes": [{"id": "old-outcome"}],
            }
        }
    }
    current = frozen_read(inputs, "chat_read", {})
    assert current["turn"] == 5 and "context" not in current and "outcomes" not in current
    assert current["agreements"] == [{"status": "ACTIVE", "id": "current"}]
    full = frozen_read(inputs, "chat_read", {"includeContext": True, "includeHistory": True})
    assert full["context"]["economy"]["gold"] == 120
    assert full["outcomes"] == [{"id": "old-outcome"}]
    assert len(full["agreements"]) == 2


def test_review_recovery_preserves_inputs_attempt_budget_and_boundary_publication(tmp_path):
    memory = setup(tmp_path)
    store = ReviewStore(memory)
    inputs = {"turn": 5, "facts": "FROZEN"}
    store.prepare("game", inputs, {"model": "m", "reasoningEffort": "max"}, 60)
    assert store.pending() is None
    # A prepared review doesn't block retrying its own END_TURN, but an earlier
    # unconfirmed task must not silently disappear from next-turn readiness.
    assert store.blocking("g1", 5) is None
    assert store.blocking("g1", 6) == {"job": "game", "turn": 5, "status": "PREPARED", "attempts": 0}
    store.confirm("game")
    blocking = store.blocking("g1", 6)
    assert blocking is not None and blocking["status"] == "PENDING"
    assert not store.available("another")
    checkpoint = tmp_path / "boundary.json"
    digest = memory.backup(checkpoint)
    assert store.attempt("game", 3)["attempts"] == 1
    store.complete("game", assessment())
    assert store.pending() is None
    assert store.blocking("g1", 6) is None
    assert memory.snapshot("gameplay", 5)["strategicAdvice"] is None
    before = memory.snapshot("gameplay", 6)
    assert before["strategicAdvice"]["asOfTurn"] == 5
    with memory.transaction() as db:
        from sqlalchemy import select

        from civ_agent import memory_schema as tables

        assert db.execute(select(tables.reviews.c.inputs)).scalar_one() is None
    assert memory.restore(checkpoint, digest, "restored") == ["game"]
    blocking = store.blocking("restored", 6)
    assert blocking is not None and blocking["attempts"] == 1
    with pytest.raises(ValueError, match="WORKSPACE_EPOCH_CONFLICT"):
        store.blocking("g1", 6)
    assert memory.snapshot("gameplay", 6)["strategicAdvice"] is None
    pending = store.pending()
    assert pending is not None and json.loads(pending["inputs"]) == inputs
    assert pending["attempts"] == 1
    store.attempt("game", 3)
    store.attempt("game", 3)
    with pytest.raises(ValueError, match="ATTEMPTS_EXHAUSTED"):
        store.attempt("game", 3)


def test_opening_review_precedes_first_move_and_survives_rewind(tmp_path):
    memory = CivilizationState(tmp_path / "private", "Rome")
    memory.bind("g1")
    store = ReviewStore(memory)
    inputs = {"turn": 0, "facts": "STARTING_LOCATION_AND_ABILITIES"}
    settings = {"model": "m", "reasoningEffort": "max"}
    store.prepare_opening("opening", inputs, settings, 60)
    store.prepare_opening("duplicate", {"turn": 0, "facts": "CHANGED"}, settings, 1)
    assert store.has_opening()
    assert store.blocking("g1", 0) == {"job": "opening", "turn": 0, "status": "PENDING", "attempts": 0}
    pending = store.pending()
    assert pending is not None and pending["timeout"] == 60
    checkpoint = tmp_path / "boundary.json"
    digest = memory.backup(checkpoint)
    store.attempt("opening", 3)
    store.complete("opening", assessment())
    assert store.blocking("g1", 0) is None
    assert memory.snapshot("gameplay", 0)["strategicAdvice"]["asOfTurn"] == 0
    assert memory.restore(checkpoint, digest, "restored") == ["opening"]
    blocking, pending = store.blocking("restored", 0), store.pending()
    assert blocking is not None and blocking["attempts"] == 1
    assert pending is not None and json.loads(pending["inputs"]) == inputs
    assert memory.snapshot("gameplay", 0)["strategicAdvice"] is None
    store.attempt("opening", 3)
    store.complete("opening", assessment())
    store.prepare_opening("duplicate", inputs, settings, 60)
    assert store.pending() is None
    assert memory.snapshot("gameplay", 0)["strategicAdvice"]["asOfTurn"] == 0
