"""Code-owned native bindings retain exact upstream SDK run identities."""

import pytest
from pydantic import ValidationError

from civ_agent.orchestration.contracts import (
    ModelSelection,
    Reference,
    RoleRequest,
    Scope,
    digest,
)
from civ_agent.orchestration.host_effect_contracts import (
    NativeCommandStep,
)

_model_calls = []


def command(city):
    return {
        "tool": "player_command",
        "arguments": {"command": {"kind": "SET_CITY_FOCUS", "target": city, "selection": "FoodFocus"}},
    }


def role_request():
    return RoleRequest(
        scope=Scope(game="game", player="p0", epoch="g1", activation="native-test"),
        job="native-job",
        purpose="gameplay",
        selection=ModelSelection(model="test"),
        startup=Reference(sha256=digest("startup")),
        catalogue=Reference(sha256=digest("catalogue")),
    )


@pytest.mark.parametrize(
    "run_id",
    [
        "01234567-89ab-cdef-0123-456789abcdef:fedcba98-7654-3210-fedc-ba9876543210",
        "upstream / 実行:step.1",
    ],
)
def test_native_command_step_preserves_sdk_run_identity_verbatim(run_id):
    from civ_agent.orchestration.contracts import GatewayCall, GatewayJob

    request = role_request()
    step = NativeCommandStep(
        identity=GatewayJob(scope=request.scope, job=request.job, purpose=request.purpose),
        call=GatewayCall(call_id="command", model_step=1, decision={"kind": "act", "operations": [command("city1")]}),
        index=0,
        run_id=run_id,
    )
    assert NativeCommandStep.model_validate_json(step.model_dump_json()).run_id == run_id
    for invalid in ("", "x" * 1025, 123):
        with pytest.raises(ValidationError):
            NativeCommandStep.model_validate({**step.model_dump(), "run_id": invalid})
