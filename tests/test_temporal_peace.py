import pytest
from pydantic_ai.models.function import FunctionModel

from civ_agent.orchestration.contracts import (
    ModelSelection,
    PeacePhase,
    PeaceRequest,
    Scope,
    digest,
)
from civ_agent.orchestration.scripted_model import scripted_peace_model
from civ_agent.orchestration.store import WorkflowStore


def overview():
    return {
        "goalAssessment": "Current goals remain relevant.",
        "battlefield": "Only supplied friendly units are known.",
        "continuingWar": "Further costs are uncertain.",
        "peaceConsequences": "These exact terms end this war.",
        "uncertainties": "Enemy reserves are unknown.",
        "recommendation": "Main should consider these terms.",
    }


def test_phase_selection_defaults_to_main_and_fixture_guard_checks_the_selected_phase(tmp_path):
    store = WorkflowStore(tmp_path / "workflow", player="p0")
    scope = Scope(game="game", player="p0", epoch="e1", activation="phase-selection")
    store.bind_epoch(scope.game, scope.epoch)
    main = ModelSelection(model="scripted-v1")
    military = ModelSelection(
        provider="openrouter", authentication="api_key", model="vendor/military", reasoning_effort="low"
    )
    request = PeaceRequest(scope=scope, basis=digest("basis"), inputs=store.put(scope, {}), selection=main)
    try:
        assert PeacePhase(request=request, phase="military").selection == main
        assert PeacePhase(request=request, phase="gameplay").selection == main
        frozen = PeaceRequest.model_validate_json(
            request.model_copy(update={"military_selection": military}).model_dump_json()
        )
        phase = PeacePhase(request=frozen, phase="military")
        assert phase.selection == military and "selection" not in phase.model_dump()
        with pytest.raises(ValueError, match="SCRIPTED_MODEL_REQUIRED"):
            scripted_peace_model(phase)
        assert isinstance(scripted_peace_model(PeacePhase(request=frozen, phase="gameplay")), FunctionModel)
    finally:
        store.close()
