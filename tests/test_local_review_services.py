import asyncio
import copy

import pytest
from pydantic_ai.exceptions import ModelRetry
from pydantic_ai.messages import ModelRequest, ModelResponse, ToolCallPart, ToolReturnPart

from civ_agent.orchestration.contracts import ModelSelection, ReviewRequest, Scope
from civ_agent.orchestration.providers import ProviderClients
from civ_agent.orchestration.review import (
    ReviewServices,
    StableEvidence,
    delivered_reads,
    read_tool,
    validate_report,
)
from civ_agent.orchestration.store import JournalConflict, WorkflowStore
from civ_agent.strategic_review import FrozenReadArgumentError, StrategicAdvice
from civ_agent.victory_snapshot import SECTIONS
from tests.test_strategic_review import assessment


def frozen_inputs():
    observation = {"turn": 5, "cities": [], "map": {"tiles": []}}
    return {
        "turn": 5,
        "civilization": "Rome",
        "observation": observation,
        "actions": [],
        "reads": {
            "player_turn": observation,
            "chat_read": {"turn": 5, "messages": []},
            "victory_progress": {
                "turn": 5,
                "victories": [],
                "sections": list(SECTIONS),
                "availability": {section: {"available": False} for section in SECTIONS},
            },
            "research_details": {"technologies": [{"name": "Writing", "cost": 20}]},
        },
    }


@pytest.fixture
def review_setup(tmp_path):
    store = WorkflowStore(tmp_path / "workflow", player="p0")
    scope = Scope(game="game", player="p0", epoch="e1", activation="review5")
    store.bind_epoch(scope.game, scope.epoch)
    request = ReviewRequest(
        scope=scope, inputs=store.put(scope, frozen_inputs()), selection=ModelSelection(model="test-model")
    )
    clients = ProviderClients()
    worker = ReviewServices(store, clients)
    yield worker, request
    asyncio.run(clients.close())
    store.close()


def test_frozen_review_sources_are_stable_and_undelivered_sources_rejected(review_setup):
    worker, request = review_setup
    _, evidence, prompt = worker.prepare(request)
    _, again, recovered = worker.prepare(request)
    assert evidence.sources == again.sources
    assert prompt.user == recovered.user
    advice = assessment()
    undisclosed = StableEvidence(frozen_inputs()).deliver("research_details", {"hidden": True})["source"]
    advice["review"]["materialAssumptions"][0].update(status="inferred", sources=[undisclosed])
    with pytest.raises(ValueError, match="STRATEGIC_REVIEW_SOURCE"):
        validate_report(request, StrategicAdvice.model_validate(advice), [], worker=worker)


def test_framework_skipped_tools_do_not_confer_evidence():
    response = ModelResponse(parts=[ToolCallPart("research_details", {"arguments": {}}, tool_call_id="read1")])
    skipped = ModelRequest(parts=[ToolReturnPart("research_details", "Tool not executed", tool_call_id="read1")])
    assert delivered_reads([response, skipped]) == []
    delivered = ModelRequest(
        parts=[ToolReturnPart("research_details", {"source": "source1", "data": {}}, tool_call_id="read1")]
    )
    assert delivered_reads([response, delivered]) == [{"name": "research_details", "arguments": {}}]


@pytest.mark.parametrize("view", ["current", "full", "main", "economy", "military", "communication"])
@pytest.mark.parametrize("cities", [None, [], ["city2"]])
def test_frozen_turn_selectors_keep_permitted_facts_and_owned_city_index(review_setup, view, cities):
    worker, original = review_setup
    inputs = frozen_inputs()
    overview: dict = inputs["reads"]["player_turn"]
    overview.update(
        cities=[
            {"handle": "city1", "name": "Rome", "constructionQueue": ["Worker"]},
            {"handle": "city2", "name": "Antium", "constructionQueue": ["Granary"]},
        ],
        economy={"treasury": 14},
        research={"queue": ["Writing"]},
    )
    original_inputs = copy.deepcopy(inputs)
    request = original.model_copy(update={"inputs": worker.store.put(original.scope, inputs)})
    params = read_tool("player_turn").function_schema.validator.validate_python({"view": view, "cities": cities})
    result = worker.read(request, "player_turn", params["arguments"])
    data = result["data"]
    assert data["turn"] == 5 and data["economy"] == {"treasury": 14}
    if view in {"current", "full"}:
        assert "roleBrief" not in data and data["research"] == {"queue": ["Writing"]}
        if cities is None:
            assert data == overview
    else:
        assert data["roleBrief"]["view"] == view
        assert data["roleBrief"]["fullRead"] == {"tool": "player_turn", "arguments": {"view": "full"}}
    expected = ["city1", "city2"] if cities is None else cities
    assert [city["handle"] for city in data["cities"]] == expected
    if cities is not None:
        assert data["cityScope"] == {
            "included": cities,
            "ownedCities": [{"handle": "city1", "name": "Rome"}, {"handle": "city2", "name": "Antium"}],
        }
    data["economy"]["treasury"] = 999
    assert worker.inputs(request) == original_inputs


def test_saved_native_five_read_calls_use_canonical_validators_and_frozen_sources(review_setup):
    worker, request = review_setup
    inputs = worker.inputs(request)
    before = copy.deepcopy(inputs)
    calls = [
        ("player_turn", {"view": "full"}),
        ("player_details", {"section": "economy"}),
        ("player_details", {"section": "research"}),
        ("player_details", {"section": "map_index"}),
        ("chat_read", {"includeContext": True, "includeHistory": True}),
    ]
    messages = []
    sources = []
    for index, (name, arguments) in enumerate(calls):
        validated = read_tool(name).function_schema.validator.validate_python(arguments)
        result = worker.read(request, name, validated["arguments"])
        assert "error" not in result["data"]
        sources.append(result["source"])
        messages.extend(
            [
                ModelResponse(parts=[ToolCallPart(name, arguments, str(index))]),
                ModelRequest(parts=[ToolReturnPart(name, result, str(index))]),
            ]
        )
    assert delivered_reads(messages) == [{"name": name, "arguments": arguments} for name, arguments in calls]
    assert len(set(sources)) == 5 and worker.inputs(request) == before


def test_only_known_invalid_frozen_selector_is_a_model_correction(review_setup, monkeypatch):
    worker, request = review_setup
    arguments = read_tool("victory_progress").function_schema.validator.validate_python(
        {"sections": ["rankings"], "names": ["PRIVATE_INVALID_SELECTOR"]}
    )["arguments"]
    with pytest.raises(ModelRetry) as error:
        worker.read(request, "victory_progress", arguments)
    assert "victory_progress" in error.value.message and "PRIVATE_INVALID_SELECTOR" not in error.value.message
    assert isinstance(error.value.__cause__, FrozenReadArgumentError)
    assert type(error.value.__cause__.__cause__) is ValueError
    unexpected = ValueError("STRATEGIC_REVIEW_POSITION_MISMATCH")

    def bad_inputs(_):
        raise unexpected

    with monkeypatch.context() as patch:
        patch.setattr(worker, "inputs", bad_inputs)
        with pytest.raises(ValueError) as captured:
            worker.read(request, "victory_progress", arguments)
    assert captured.value is unexpected
    worker.store.bind_epoch(request.scope.game, "e2", previous=request.scope.epoch)
    with pytest.raises(JournalConflict, match="EPOCH_CONFLICT"):
        worker.read(request, "victory_progress", arguments)
