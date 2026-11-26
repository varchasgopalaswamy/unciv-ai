"""Advertised procedures execute against canonical DTOs in the actual sandbox."""

from typing import cast

from pydantic_ai.models.function import FunctionModel
from pydantic_ai_harness.step_persistence import SqliteStepStore

from civ_agent.execution_policy import ExecutionPolicy
from civ_agent.execution_windows import ExecutionWindowScheduler
from civ_agent.game_commands import command_schema
from civ_agent.observations import ConstructionDetails, DetailQuery, project_detail
from civ_agent.orchestration.contracts import GatewayJob, GatewayReceipt
from civ_agent.protocol import JsonObject
from civ_agent.runtime.agents import LocalAgentRunner, LocalAgentServices
from civ_agent.runtime.code_mode_instructions import (
    QUOTE_COMPARISON_EXAMPLE,
    ROUTE_PROCEDURE_EXAMPLE,
    with_code_mode_instructions,
)
from civ_agent.tool_schemas import MovementPreview
from tests.domain_fixtures import run_async
from tests.test_construction_estimates import snapshot
from tests.test_local_agents import call, request, returns


async def run_procedure(tmp_path, *, code, inspect, check, movement=False, focused=False, reject=False, partial=False):
    decisions = []
    selected = request().model_copy(update={"execution_policy": ExecutionPolicy.current()})
    scheduler = ExecutionWindowScheduler(game=selected.scope.game, player=selected.scope.player)

    async def invoke(deps, identifier, _step, decision):
        decisions.append(decision)
        if decision["kind"] == "finish":
            return GatewayReceipt(result={"roleFinished": True})

        async def operation():
            operation = decision["operations"][0]
            tool = operation["tool"]
            result: JsonObject = inspect(operation) if decision["kind"] == "inspect" else {"applied": not reject}
            if reject and decision["kind"] == "act":
                result["error"] = {"code": "TARGET_CHANGED"}
            return GatewayReceipt(
                result={
                    "decisionId": identifier,
                    "receipts": [
                        {
                            "operationId": identifier,
                            "tool": tool,
                            "status": "rejected" if reject and decision["kind"] == "act" else "confirmed",
                            "result": result,
                        }
                    ]
                    + (
                        [
                            {
                                "operationId": identifier + "-unfinished",
                                "status": "unexecuted",
                                "reason": "POSITION_CHANGED",
                            }
                        ]
                        if partial and decision["kind"] == "act"
                        else []
                    ),
                    "unexecuted": [identifier + "-unfinished"] if partial and decision["kind"] == "act" else [],
                }
            )

        return await scheduler.run(
            GatewayJob(scope=deps.scope, job=deps.job, purpose=deps.purpose),
            identifier,
            operation,
            write=decision["kind"] == "act",
        )

    def model(messages, info):
        history = returns(messages)
        if not history:
            return call(info, "run_code", {"code": code, "focused": focused}, "procedure")
        assert isinstance(history[-1].content, dict), history[-1].content
        returned = cast(JsonObject, history[-1].content)
        # The application also surfaces committed child receipts after a write.
        content = returned["output"] if "action_outcomes" in returned else returned
        assert isinstance(content, dict)
        assert "reconsider" in content or "options" in content, returned
        check(content, history[-1].metadata)
        return call(info, "finish_role", {"summary": "Assessed actual procedure outcomes."}, "finish")

    name, schema = (
        ("movement_preview", MovementPreview.model_json_schema())
        if movement
        else ("construction_details", ConstructionDetails.model_json_schema())
    )
    catalogue = [{"name": "finish_role", "inputSchema": {}}, {"name": name, "inputSchema": schema}]
    if movement:
        catalogue.append({"name": "player_command", "inputSchema": command_schema({"SET_UNIT_DESTINATION"})})
    await LocalAgentRunner(
        LocalAgentServices(
            invoke=invoke,
            step_store=SqliteStepStore(database=tmp_path / "steps.sqlite3"),
            overflow_root=tmp_path / "overflow",
            execution_windows=scheduler,
        )
    ).run_role(
        selected,
        startup={"user": "Assess your chosen intent.", "instructions": "Use permitted evidence."},
        catalogue=catalogue,
        model=FunctionModel(model),
    )
    assert scheduler.owner is None and not scheduler.queue
    return decisions


@run_async
async def test_quote_procedure_preserves_native_numeric_basis_sources_waiting_and_unknowns(tmp_path):
    view = snapshot().view
    view["constructionCatalogue"].append("Laboratory")
    view["cities"][0]["purchases"] = {"constructions": [{"name": "Library", "available": True, "goldCost": 200}]}

    def inspect(operation):
        assert operation["arguments"] == {"target": "city1", "names": ["Library", "Laboratory"], "horizonTurns": 10}
        return project_detail(view, DetailQuery.model_validate(operation))

    def check(result, _metadata):
        assert result["sources"]["evidence"].startswith("observation:")
        assert len(result["sources"]["operationIds"]) == 1
        library, laboratory = result["options"]
        assert library["build"]["completionInTurns"] == 5
        assert library["build"]["yieldDifferenceOverHorizon"] == {"Science": 5}
        assert library["purchase"]["goldCostNow"] == 200
        assert library["buyMinusBuild"] == {"Science": 15}
        assert library["continueCurrent"] == {"Science": 110}
        assert library["unknown"] is None
        assert laboratory["unknown"] == "No native comparison" and laboratory["buyMinusBuild"] == {}
        assert result["wait"] and result["basis"] and result["unknownNames"] == []

    decisions = await run_procedure(
        tmp_path,
        code=QUOTE_COMPARISON_EXAMPLE + '\ncompare_quotes("city1", ["Library", "Laboratory"], 10)',
        inspect=inspect,
        check=check,
    )
    assert [decision["kind"] for decision in decisions] == ["inspect", "finish"]


def preview(*, available=True, risk=None):
    return {
        "available": available,
        "estimatedTurns": 4,
        "turns": [{"destination": {"x": 7, "y": 0}, "afterMove": {"captureRisk": risk}}],
        **(
            {"command": {"kind": "SET_UNIT_DESTINATION", "target": "unit1", "destination": {"x": 7, "y": 0}}}
            if available
            else {}
        ),
        **({"confirmationField": "confirmAction"} if risk else {}),
    }


@run_async
async def test_route_procedure_executes_once_inside_focus_and_keeps_actual_receipt(tmp_path):
    def inspect(operation):
        assert operation["arguments"] == {"target": "unit1", "destination": {"x": 7, "y": 0}}
        return preview()

    def check(result, metadata):
        assert result["reconsider"] is False and len(result["sources"]) == 1
        assert result["feedback"]["receipts"][0]["result"]["applied"] is True
        assert metadata["focused_window"]["acquired"] is True
        assert metadata["focused_window"]["operation_count"] == 2
        assert metadata["focused_window"]["end_reason"] == "mutation_settled"

    decisions = await run_procedure(
        tmp_path,
        code=ROUTE_PROCEDURE_EXAMPLE + '\nroute_once("unit1", {"x": 7, "y": 0})',
        inspect=inspect,
        check=check,
        movement=True,
        focused=True,
    )
    assert [decision["kind"] for decision in decisions] == ["inspect", "act", "finish"]


@run_async
async def test_route_rejection_returns_for_reconsideration_without_automatic_replay(tmp_path):
    def check(result, _metadata):
        assert result["reconsider"] is True
        assert result["feedback"]["receipts"][0]["status"] == "rejected"
        assert result["feedback"]["receipts"][0]["result"]["error"]["code"] == "TARGET_CHANGED"

    decisions = await run_procedure(
        tmp_path,
        code=ROUTE_PROCEDURE_EXAMPLE + '\nroute_once("unit1", {"x": 7, "y": 0})',
        inspect=lambda _operation: preview(),
        check=check,
        movement=True,
        focused=True,
        reject=True,
    )
    assert [decision["kind"] for decision in decisions].count("act") == 1


@run_async
async def test_partial_route_outcome_preserves_commit_and_returns_without_repeating_it(tmp_path):
    def check(result, _metadata):
        assert result["reconsider"] is True
        assert result["feedback"]["receipts"][0]["status"] == "confirmed"
        assert result["feedback"]["receipts"][0]["result"]["applied"] is True
        assert result["feedback"]["unexecuted"]

    decisions = await run_procedure(
        tmp_path,
        code=ROUTE_PROCEDURE_EXAMPLE + '\nroute_once("unit1", {"x": 7, "y": 0})',
        inspect=lambda _operation: preview(),
        check=check,
        movement=True,
        focused=True,
        partial=True,
    )
    assert [decision["kind"] for decision in decisions].count("act") == 1


@run_async
async def test_unavailable_route_returns_waiting_alternative_without_dispatching_a_command(tmp_path):
    def check(result, _metadata):
        assert result["reconsider"] is True and result["reason"] == "Route unavailable"
        assert result["preview"]["available"] is False

    decisions = await run_procedure(
        tmp_path,
        code=ROUTE_PROCEDURE_EXAMPLE + '\nroute_once("unit1", {"x": 7, "y": 0})',
        inspect=lambda _operation: preview(available=False),
        check=check,
        movement=True,
        focused=True,
    )
    assert [decision["kind"] for decision in decisions] == ["inspect", "finish"]


@run_async
async def test_route_changed_risk_returns_without_consent_or_movement(tmp_path):
    def check(result, metadata):
        assert result["reconsider"] is True and result["reason"] == "Assess changed capture risk"
        assert result["risk"] == {"destination": {"x": 7, "y": 0}, "threats": [{"name": "Brute"}]}
        assert metadata["focused_window"]["operation_count"] == 1

    decisions = await run_procedure(
        tmp_path,
        code=ROUTE_PROCEDURE_EXAMPLE + '\nroute_once("unit1", {"x": 7, "y": 0})',
        inspect=lambda _operation: preview(risk={"destination": {"x": 7, "y": 0}, "threats": [{"name": "Brute"}]}),
        check=check,
        movement=True,
        focused=True,
    )
    assert [decision["kind"] for decision in decisions] == ["inspect", "finish"]


@run_async
async def test_route_deliberately_accepted_native_risk_remains_executable(tmp_path):
    risk = {"destination": {"x": 7, "y": 0}, "threats": [{"name": "Brute"}]}

    def check(result, _metadata):
        assert result["reconsider"] is False

    decisions = await run_procedure(
        tmp_path,
        code=ROUTE_PROCEDURE_EXAMPLE + '\nroute_once("unit1", {"x": 7, "y": 0}, ' + repr(risk) + ")",
        inspect=lambda _operation: preview(risk=risk),
        check=check,
        movement=True,
        focused=True,
    )
    action = next(decision for decision in decisions if decision["kind"] == "act")
    assert action["operations"][0]["arguments"]["command"]["confirmAction"] is True


def test_procedure_guidance_is_explicitly_opted_in_and_excluded_from_reviews():
    legacy = with_code_mode_instructions("base", enabled=True, focused=True)
    candidate = with_code_mode_instructions("base", enabled=True, focused=True, procedures=True)
    assert QUOTE_COMPARISON_EXAMPLE not in legacy and ROUTE_PROCEDURE_EXAMPLE not in legacy
    assert QUOTE_COMPARISON_EXAMPLE in candidate and ROUTE_PROCEDURE_EXAMPLE in candidate
    assert with_code_mode_instructions("base", enabled=False, procedures=True) == "base"
    assert QUOTE_COMPARISON_EXAMPLE not in with_code_mode_instructions(
        "base", enabled=True, report=True, procedures=True
    )
