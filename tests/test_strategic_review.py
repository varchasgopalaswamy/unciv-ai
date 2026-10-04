"""Periodic reports precede the next own turn and never overwrite private plans."""

import asyncio
import copy
import json
from types import SimpleNamespace

import pytest

from civ_agent.civilization_state import CivilizationState
from civ_agent.proposal_gateway import ProposalGateway, public_result
from civ_agent.protocol import JsonObject
from civ_agent.review_context import review_position
from civ_agent.review_store import ReviewStore
from civ_agent.service_io import ServiceIO
from civ_agent.startup_context import prepare_startup, startup_prompt
from civ_agent.strategic_review import advice_schema, frozen_read, parse_advice, review_cities, review_due, review_facts
from tests.test_agent_service import StartupHost
from tests.test_structured_memory import setup

VICTORIES = ["Scientific", "Cultural", "Domination", "Diplomatic", "Time"]


def assessment(victories=VICTORIES) -> JsonObject:
    return {
        "assessment": "Invest in production and compare our spaceship timing against Greece before committing to war.",
        "review": {
            "plans": [
                {
                    "name": "Expand production",
                    "objective": "Speed up our own spaceship construction.",
                    "investment": "Build factories before spending production on parts; postpone recruitment.",
                    "projection": "Factories first, then parallel parts; compare the finish with Greece's progress.",
                    "relativeOutcome": "Our output improves, but Greece can continue building without interruption.",
                    "risks": "Greece may finish before the factories repay their cost.",
                },
                {
                    "name": "Delay Greece",
                    "objective": "Disrupt Greece's spaceship production.",
                    "investment": "Recruit siege support and scout a safe approach; postpone factories.",
                    "projection": "Scout now, prepare a limited attack, then reassess our own finish.",
                    "relativeOutcome": "A successful attack buys time; France also gains from Greece's delay.",
                    "risks": "Defensive pacts or strong reinforcements could make the attack too expensive.",
                },
            ],
            "preferredPlan": None,
            "reconsiderWhen": "Reassess after inspecting production costs and the approach to Greece.",
            "victoryOutlooks": [
                {
                    "victory": victory,
                    "outlook": "Greece has installed a spaceship part; inspect costs and timing before committing.",
                }
                for victory in victories
            ],
            "diplomacy": {
                "assessment": "Avoid assuming a reciprocal research agreement improves our relative chances.",
                "initiatives": [
                    {
                        "players": ["France"],
                        "objective": "Find out whether France would help delay Greece's spaceship.",
                        "incentives": "France also loses if Greece finishes first, but might prefer that we bear the cost.",
                        "approach": "Offer shared reconnaissance; keep our troop shortage private until terms improve.",
                        "nextStep": "Ask France about its interest and available support this turn.",
                        "reconsiderWhen": "France refuses support or scouting shows the delay would cost too much.",
                    }
                ],
            },
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
    value["review"]["victoryOutlooks"].pop()
    with pytest.raises(ValueError, match="STRATEGIC_REVIEW_COVERAGE"):
        parse_advice(json.dumps(value), {"turn": 5, "rules": {"victoryTypes": VICTORIES}})


def test_report_schema_uses_exact_enabled_victory_names():
    for enabled in (VICTORIES, ["Domination", "Time"]):
        schema = advice_schema({"rules": {"victoryTypes": enabled}})
        assert schema["$defs"]["VictoryOutlook"]["properties"]["victory"]["enum"] == enabled
        paths = schema["$defs"]["StrategicReview"]["properties"]["victoryOutlooks"]
        assert paths["minItems"] == paths["maxItems"] == len(enabled)
    advice = assessment()
    advice["review"]["victoryOutlooks"][0]["victory"] = "Scientific — hedge"
    with pytest.raises(ValueError, match="STRATEGIC_REVIEW_COVERAGE"):
        parse_advice(json.dumps(advice), {"turn": 0, "rules": {"victoryTypes": VICTORIES}})


@pytest.mark.parametrize("count", [1, 2, 3])
def test_competing_plans_allow_a_preference_or_an_open_choice(count):
    advice = assessment()
    advice["review"]["plans"] = advice["review"]["plans"][:1] * count
    position = {"turn": 5, "rules": {"victoryTypes": VICTORIES}}
    for preferred in [None, 1, count]:
        advice["review"]["preferredPlan"] = preferred
        assert parse_advice(json.dumps(advice), position) == advice


@pytest.mark.parametrize("count,preferred", [(0, None), (4, None), (1, 2), (2, 3), (2, 0), (3, 4), (2, True)])
def test_invalid_plan_count_or_reference_is_rejected(count, preferred):
    advice = assessment()
    advice["review"]["plans"] = advice["review"]["plans"][:1] * count
    advice["review"]["preferredPlan"] = preferred
    with pytest.raises(ValueError):
        parse_advice(json.dumps(advice), {"turn": 5, "rules": {"victoryTypes": VICTORIES}})


def test_duplicate_victory_outlook_does_not_satisfy_coverage():
    advice = assessment()
    advice["review"]["victoryOutlooks"][1] = advice["review"]["victoryOutlooks"][0]
    with pytest.raises(ValueError, match="STRATEGIC_REVIEW_COVERAGE"):
        parse_advice(json.dumps(advice), {"turn": 5, "rules": {"victoryTypes": VICTORIES}})


def test_diplomatic_initiatives_are_optional_and_bounded():
    advice = assessment()
    position = {"turn": 5, "rules": {"victoryTypes": VICTORIES}}
    initiative = advice["review"]["diplomacy"]["initiatives"][0]
    for count in (0, 1, 2):
        advice["review"]["diplomacy"]["initiatives"] = [initiative] * count
        assert parse_advice(json.dumps(advice), position) == advice
    advice["review"]["diplomacy"]["initiatives"] = [initiative] * 3
    with pytest.raises(ValueError):
        parse_advice(json.dumps(advice), position)


def test_peaceful_single_plan_without_diplomatic_initiatives_remains_valid():
    advice = assessment()
    advice["review"]["plans"] = advice["review"]["plans"][:1]
    advice["review"]["preferredPlan"] = 1
    advice["review"]["diplomacy"]["initiatives"] = []
    assert parse_advice(json.dumps(advice), {"turn": 5, "rules": {"victoryTypes": VICTORIES}}) == advice


@pytest.mark.parametrize("field,value", [("players", []), ("approach", ""), ("objective", "x" * 601)])
def test_initiative_requires_contacts_and_concise_actionable_fields(field, value):
    advice = assessment()
    advice["review"]["diplomacy"]["initiatives"][0][field] = value
    with pytest.raises(ValueError):
        parse_advice(json.dumps(advice), {"turn": 5, "rules": {"victoryTypes": VICTORIES}})


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


def test_frozen_social_read_filters_projected_commitments_without_changing_terms():
    def offer(reference, version=1, **fields):
        return {
            "proposal": reference,
            "version": version,
            "author": "France",
            "participants": ["France", "Rome"],
            "effective": False,
            "terms": "Perhaps help when feasible",
            **fields,
        }

    native = {
        "civilization": "Rome",
        "channels": ["Rome", "France"],
        "context": {"turn": 6},
        "offers": [
            offer("pact12", effective=True),
            offer("pact12", 2),
            offer("pexp12", expiresTurn=6),
            offer("paway1"),
            offer("pnext1"),
            offer("pnext1", 2),
        ],
        "consents": [
            {"proposal": "pact12", "version": 1, "author": player, "decision": "accept"}
            for player in ("France", "Rome")
        ]
        + [
            {"proposal": "paway1", "version": 1, "author": "Rome", "decision": decision}
            for decision in ("accept", "withdraw")
        ],
        "messages": [{"text": "We may reconsider after meeting Egypt."}],
        "trades": [{"proposal": "dabc12", "author": "France", "partner": "Rome", "status": "PENDING"}],
    }
    # Capture stores the model projection, including proposalId, before prompt generation.
    inputs = {"reads": {"chat_read": public_result(native)}}
    saved = copy.deepcopy(inputs)
    current = frozen_read(inputs, "chat_read", {})
    assert current["turn"] == 6 and current["chatRecipients"] == ["France"]
    assert [(offer["proposalId"], offer["version"]) for offer in current["offers"]] == [
        ("pact12", 1),
        ("pact12", 2),
        ("pnext1", 2),
    ]
    assert current["consents"] == public_result(native["consents"][:2])
    assert all(offer["terms"] == "Perhaps help when feasible" for offer in current["offers"])
    assert current["messages"] == native["messages"]
    assert current["trades"][0]["proposalId"] == "dabc12"
    history = frozen_read(inputs, "chat_read", {"includeHistory": True})
    assert history["offers"] == public_result(native["offers"])
    assert history["consents"] == public_result(native["consents"])
    assert inputs == saved


def test_review_recovery_preserves_inputs_attempt_budget_and_boundary_publication(tmp_path):
    memory = setup(tmp_path)
    store = ReviewStore(memory)
    inputs = {"turn": 5, "facts": "FROZEN", "observation": {"turn": 5}}
    store.prepare("game", inputs, {"model": "m", "reasoningEffort": "max"}, 60)
    assert store.last_position() is None
    assert store.pending() is not None
    # Round preparation is durable immediately; the next turn waits for publication.
    assert store.blocking("g1", 5) is None
    assert store.blocking("g1", 6) == {"job": "game", "turn": 5, "status": "PENDING", "attempts": 0}
    blocking = store.blocking("g1", 6)
    assert blocking is not None and blocking["status"] == "PENDING"
    checkpoint = tmp_path / "boundary.json"
    digest = memory.backup(checkpoint)
    assert store.attempt("game", 3)["attempts"] == 1
    store.complete("game", assessment())
    assert store.last_position() == review_position(inputs["observation"])
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
    assert store.last_position() is None
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


def test_boundary_review_is_available_to_communication_before_next_gameplay(tmp_path):
    memory = setup(tmp_path)
    store = ReviewStore(memory)
    store.prepare("review-five", {"turn": 5, "observation": {"turn": 6}}, {}, 60)
    assert memory.snapshot("communication")["strategicAdvice"] is None
    store.complete("review-five", assessment())
    assert memory.snapshot("communication")["strategicAdvice"]["asOfTurn"] == 5
    # An explicit historical view still observes the normal report boundary.
    assert memory.snapshot("gameplay", 5)["strategicAdvice"] is None


def test_opening_review_precedes_first_move_and_survives_rewind(tmp_path):
    memory = CivilizationState(tmp_path / "private", "Rome")
    memory.bind("g1")
    store = ReviewStore(memory)
    inputs = {"turn": 0, "facts": "STARTING_LOCATION_AND_ABILITIES", "observation": {"turn": 0}}
    settings = {"model": "m", "reasoningEffort": "max"}
    store.prepare("opening", inputs, settings, 60)
    store.prepare("duplicate", {"turn": 0, "facts": "CHANGED"}, settings, 1)
    assert store.at_turn(0)
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
    store.prepare("duplicate", inputs, settings, 60)
    assert store.pending() is None
    assert memory.snapshot("gameplay", 0)["strategicAdvice"]["asOfTurn"] == 0
