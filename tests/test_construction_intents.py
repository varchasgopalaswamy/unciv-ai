import copy
import json
from types import SimpleNamespace
from typing import cast

import pytest
from pydantic import TypeAdapter, ValidationError
from pydantic_ai import ModelRetry
from pydantic_ai.messages import ModelResponse, ToolCallPart
from pydantic_ai.models.function import FunctionModel
from pydantic_ai_harness.step_persistence import SqliteStepStore

from civ_agent.construction_intents import ConstructionIntent, construction_state, observed_construction
from civ_agent.game_commands import command_schema
from civ_agent.gameplay_policy import GameplayPolicy
from civ_agent.orchestration.contracts import GatewayReceipt
from civ_agent.runtime.agents import LocalAgentRunner, LocalAgentServices
from civ_agent.runtime.outcomes import VerifiedOutcomes
from civ_agent.runtime.player_observations import PlayerObservations
from tests.domain_fixtures import run_async
from tests.test_local_agents import request, returns


def test_queue_references_distinguish_duplicates_city_and_revision():
    state = construction_state("city", ["Worker", "Worker", "Monument"], "rev1")
    cap = PlayerObservations(invoke=None)
    cap.overview = {"cities": [{"handle": "city", "constructionState": state}]}
    ctx = SimpleNamespace(capabilities={"observations": cap})
    parsed = TypeAdapter(ConstructionIntent)
    second = parsed.validate_python({"kind": "prioritize_existing", "city": "city", "entry": state["queue"][1]})
    assert observed_construction(ctx, second) == state
    assert state["queue"][0] != state["queue"][1]
    for invalid in (
        {**state["queue"][1], "city": "foreign"},
        {**state["queue"][1], "index": 99},
        construction_state("city", ["Worker", "Worker", "Monument"], "rev2")["queue"][1],
    ):
        with pytest.raises(ModelRetry, match="stale"):
            observed_construction(
                ctx, parsed.validate_python({"kind": "remove_existing", "city": "city", "entry": invalid})
            )
    with pytest.raises(ValidationError):
        parsed.validate_python({"kind": "remove_existing", "city": "city", "entry": {"name": "Worker"}})


@pytest.mark.parametrize(
    "kind,native,queue",
    [
        ("start_new", "START_CONSTRUCTION", ["Worker", "Monument"]),
        ("append_new", "QUEUE_CONSTRUCTION", ["Monument", "Worker"]),
        ("start_new", "START_CONSTRUCTION", ["Monument", "Worker"]),
    ],
)
@run_async
async def test_sdk_intents_issue_one_atomic_command_and_ground_effect_claim(tmp_path, kind, native, queue):
    calls = []
    initial = {
        "stateId": "s1",
        "revision": "rev1",
        "turn": 1,
        "cities": [{"handle": "city", "constructionState": construction_state("city", ["Monument"], "rev1")}],
    }

    async def invoke(deps, identifier, step, decision):
        calls.append(decision)
        if decision["kind"] == "finish":
            assert "effectClaims" not in decision["arguments"]
            return GatewayReceipt(result={"roleFinished": True})
        command = decision["operations"][0]["arguments"]["command"]
        assert command == {"kind": native, "target": "city", "selection": "Worker"}
        return GatewayReceipt(
            result={
                "receipts": [
                    {
                        "operationId": "one",
                        "status": "confirmed",
                        "tool": "player_command",
                        "result": {
                            "kind": native,
                            "construction": {"city": "city", "before": ["Monument"], "after": queue},
                        },
                    }
                ]
            }
        )

    def model(messages, info):
        history = returns(messages)
        if not history:
            return ModelResponse(
                parts=[
                    ToolCallPart(
                        "construction_intent",
                        {"intent": {"kind": kind, "city": "city", "selection": "Worker"}},
                        "build",
                    )
                ]
            )
        outcome = cast(dict, history[-1].content)["constructionOutcome"]
        assert outcome["queue"] == queue
        assert (outcome["unmetIntendedOutcome"] is None) == (kind != "start_new" or queue[0] == "Worker")
        return ModelResponse(
            parts=[
                ToolCallPart(
                    "finish_role",
                    {
                        "summary": "Selected production.",
                        "effectClaims": [
                            {"operationId": "one", "kind": native, "summary": "Updated the native queue."}
                        ],
                    },
                    "finish",
                )
            ]
        )

    runner = LocalAgentRunner(
        LocalAgentServices(invoke=invoke, step_store=SqliteStepStore(database=tmp_path / "steps.sqlite3"))
    )
    await runner.run_role(
        request().model_copy(update={"gameplay_policy": GameplayPolicy()}),
        startup={
            "user": json.dumps({"role": "gameplay", "reads": {"player_turn": initial}}),
            "instructions": "Choose production.",
        },
        catalogue=[
            {"name": "finish_role", "inputSchema": {}},
            {
                "name": "player_command",
                "inputSchema": command_schema(
                    {"QUEUE_CONSTRUCTION", "START_CONSTRUCTION", "MOVE_CONSTRUCTION", "REMOVE_CONSTRUCTION"}
                ),
            },
        ],
        model=FunctionModel(model),
    )
    assert len(calls) == 2


def test_claims_do_not_promote_rejections_unknown_effects_or_other_runs():
    cap = VerifiedOutcomes()
    claim = {"operationId": "action", "kind": "START_CONSTRUCTION", "summary": "Claim"}
    for row in (
        None,
        {"status": "rejected", "result": {"kind": "START_CONSTRUCTION"}},
        {"status": "confirmed", "result": {"kind": "QUEUE_CONSTRUCTION"}},
        {"status": "confirmed", "result": {"kind": "START_CONSTRUCTION", "error": "UNKNOWN"}},
    ):
        cap.receipts = {"action": {"tool": "player_command", **row}} if row else {}
        with pytest.raises(ModelRetry):
            cap.validate([claim])
    cap.receipts = {
        "action": {"tool": "player_command", "status": "confirmed", "result": {"kind": "START_CONSTRUCTION"}}
    }
    cap.validate([claim])
    assert copy.deepcopy(cap.receipts) == cap.receipts
