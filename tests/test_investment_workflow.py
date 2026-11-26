"""Typed choices activate existing strategy/role tasks without inventing effects."""

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import ValidationError
from pydantic_ai import Agent, ToolFailed
from pydantic_ai.messages import ModelRequest, ModelResponse, TextPart, ToolCallPart, ToolReturnPart
from pydantic_ai.models.function import FunctionModel
from pydantic_ai.tools import ToolDefinition

from civ_agent.decision_context import choice_versions
from civ_agent.execution_policy import ExecutionPolicy
from civ_agent.memory_records import Strategy
from civ_agent.orchestration.authored_memory import AuthoredMemoryRequest, AuthoredMetadata
from civ_agent.orchestration.authored_plan_service import (
    AuthoredDocumentReply,
    ChooseInvestmentDocument,
    DocumentOperation,
    ReadInvestmentDocument,
    invoke_document,
)
from civ_agent.orchestration.authored_sources import project_authored_sources
from civ_agent.orchestration.contracts import ModelSelection, Reference, RoleRequest, digest
from civ_agent.orchestration.investment_quotes import quote_error, quote_sources
from civ_agent.orchestration.investment_records import InvestmentCheckpoint, InvestmentChoice
from civ_agent.orchestration.investment_workflow import InvestmentWorkflow, task_id, workflow_instructions
from civ_agent.orchestration.player_plans import PlayerPlanRequest, PlayerPlanService
from civ_agent.orchestration.role_instructions import NATIVE_STRATEGY
from civ_agent.orchestration.store import JournalConflict
from tests.domain_fixtures import run_async
from tests.test_authored_memory import operation, setup


def choice(**changes):
    option = dict(
        option="Produce Worker",
        investment="Current city production",
        expectedOutcome="A worker becomes available",
        timing="Native estimate is conditional",
        uncertainty="Safety and later yields may change",
    )
    value = dict(
        id="develop",
        objective="Improve a distinct luxury",
        investment="Prepare a worker and improvement",
        status="active",
        ownerRole="gameplay",
        nextStep="Inspect the current work choice",
        expectedObservations=["Native state shows the improved luxury connected"],
        reviewAfterTurns=3,
        reconsiderWhen="Threat, quote or available worker changes",
        subjects=["city2"],
        comparison=[
            dict(
                kind="production",
                **option,
                quote=dict(
                    availability="unknown",
                    limitation="Inspect the current native construction quote before spending",
                ),
            ),
            dict(
                kind="wait",
                option="Wait",
                investment="Delay this investment",
                expectedOutcome="Retain flexibility",
                timing="Reassess when facts change",
                uncertainty="Later availability is unknown",
            ),
        ],
    )
    return InvestmentChoice.model_validate({**value, **changes})


def same_kind_choice(case, **changes):
    if case == "france":
        retained = json.loads(Path("tests/fixtures/investment_choices/france-start-alternatives.json").read_text())
        return InvestmentChoice.model_validate({**retained["choice"], **changes})
    production = choice().model_dump()["comparison"]
    settler = {
        **production[0],
        "option": "Produce Settler",
        "investment": "Spend current production on expansion",
        "expectedOutcome": "A settler is produced, not a city founded",
    }
    return choice(comparison=[production[0], settler, production[1]], **changes)


def choose_request(name, *, selected=None, step=2, **changes):
    selected = selected or choice()
    return ChooseInvestmentDocument(
        choice=selected,
        operation=DocumentOperation(id=digest(name), fingerprint=digest([name, selected.model_dump()])),
        model_step=step,
        **changes,
    )


def context(identity, *, step=1, call="review", code=False):
    return SimpleNamespace(
        deps=SimpleNamespace(
            **identity.model_dump(),
            model_step_offset=0,
            execution_policy=SimpleNamespace(follow_through_workflow=True, code_mode=SimpleNamespace(enabled=code)),
        ),
        run_step=step,
        run_id="run",
        tool_call_id=call,
    )


def workflow(fixture, tmp_path):
    plans = PlayerPlanService(fixture.registry, tmp_path / "plans.sqlite3")

    class Client:
        async def post(self, route, request):
            if route == "memory":
                return (await fixture.service.invoke(AuthoredMemoryRequest.model_validate(request))).model_dump(
                    mode="json"
                )
            assert route == "plans"
            return (await plans.invoke(PlayerPlanRequest.model_validate(request))).model_dump(mode="json")

    return InvestmentWorkflow(Client()), plans


def test_choice_requires_actual_comparison_owner_and_observable_checkpoint():
    options = choice().model_dump()["comparison"]
    options[0]["quote"] = {
        "availability": "observed",
        "source": "native-source",
        "version": "v1",
        "observedTurn": 0,
        "facts": [{"field": "/productionCost", "value": 46}],
    }
    observations = ["Worker produced", "Worker arrived", "Luxury improved", "Luxury connected"]
    good = choice(status="waiting", comparison=options, expectedObservations=observations)
    stored = good.decision()
    assert stored.expectedObservations == observations
    assert stored.followThrough is not None
    assert stored.followThrough.alternatives[0].nativeQuote is not None
    assert stored.followThrough.ownerRole == "gameplay" and stored.status == "waiting"
    assert stored.followThrough.alternatives[1].waiting
    assert stored.followThrough.alternatives[0].nativeQuote.facts[0].value == 46
    for edit in ({"comparison": []}, {"ownerRole": None}, {"nextStep": ""}, {"expectedObservations": []}):
        with pytest.raises(ValidationError):
            choice(**edit)
    options = good.model_dump()["comparison"]
    options[1]["kind"] = "other"
    with pytest.raises(ValidationError, match="waiting"):
        choice(comparison=options)
    options = good.model_dump()["comparison"]
    options[0]["quote"] = {"availability": "observed", "source": "native", "facts": []}
    with pytest.raises(ValidationError, match="quoteSources"):
        choice(comparison=options)
    options[0]["quote"] = {"availability": "unknown", "limitation": "No current native quote was obtained"}
    unknown = choice(comparison=options).decision()
    assert unknown.followThrough is not None
    assert unknown.followThrough.alternatives[0].nativeQuote is not None
    assert unknown.followThrough.alternatives[0].nativeQuote.availability == "unknown"


@run_async
async def test_choice_merge_requires_delivered_cas_and_never_executes_native_action(tmp_path):
    async with setup(tmp_path) as f:
        assert (await invoke_document(f.service, f.identity, choose_request("unread"))).error == "read_required"
        before = json.loads(json.dumps(f.job.memory))
        reviewed = await invoke_document(f.service, f.identity, ReadInvestmentDocument(model_step=2))
        assert reviewed.progress is not None
        assert reviewed.progress["receipts"] == []
        assert (
            await invoke_document(f.service, f.identity, choose_request("same-response", step=2))
        ).error == "read_required"
        saved = await invoke_document(
            f.service, f.identity, choose_request("chosen", step=3, assessment="Broad development intent")
        )
        assert saved.applied and saved.id == "strategy.md"
        actual = await f.service.store.read(f.prefix + "strategy.md", max_chars=8000)
        body = json.loads(actual.content)
        assert body["assessment"] == "Broad development intent" and body["decisions"][0]["followThrough"]["nextStep"]
        assert f.domain.host.executed == []
        assert f.job.memory.get("delivered") == before.get("delivered")


@run_async
async def test_code_delivered_review_allows_same_program_choice_and_preserves_waiting(tmp_path):
    async with setup(tmp_path) as f:
        await invoke_document(f.service, f.identity, ReadInvestmentDocument(model_step=1, program_delivered=True))
        saved = await invoke_document(
            f.service, f.identity, choose_request("hold", step=1, selected=choice(status="waiting"))
        )
        assert saved.applied
        actual = await f.service.store.read(f.prefix + "strategy.md", max_chars=8000)
        assert json.loads(actual.content)["decisions"][0]["status"] == "waiting"
        assert not f.domain.host.executed


@run_async
async def test_lost_ack_returns_exact_receipt_after_later_choice_and_retirement(tmp_path):
    async with setup(tmp_path) as f:
        await invoke_document(f.service, f.identity, ReadInvestmentDocument(model_step=1))
        request = choose_request("first")
        first = await invoke_document(f.service, f.identity, request)
        await invoke_document(f.service, f.identity, ReadInvestmentDocument(model_step=3))
        later = await invoke_document(
            f.service, f.identity, choose_request("later", step=4, selected=choice(status="abandoned"))
        )
        assert later.version != first.version
        await f.registry.retire(f.identity)
        replay = await invoke_document(f.service, f.identity, request)
        assert replay.replayed and replay.version == first.version
        current = await f.service.store.read(f.prefix + "strategy.md", max_chars=8000)
        assert current.version == later.version
        with pytest.raises(JournalConflict, match="OPERATION_CONFLICT"):
            await invoke_document(
                f.service, f.identity, request.model_copy(update={"choice": choice(investment="Different choice")})
            )


@run_async
async def test_follow_through_updates_preserve_original_choice_baseline(tmp_path):
    async with setup(tmp_path) as f:
        await invoke_document(f.service, f.identity, ReadInvestmentDocument(model_step=1))
        await invoke_document(f.service, f.identity, choose_request("first"))
        first = await f.service.store.read(f.prefix + "strategy.md", max_chars=8000)
        baseline = first.metadata["decisionBaselines"]
        await invoke_document(f.service, f.identity, ReadInvestmentDocument(model_step=3))
        await invoke_document(
            f.service,
            f.identity,
            choose_request("hold", step=4, selected=choice(status="waiting", nextStep="Wait for safer access")),
        )
        second = await f.service.store.read(f.prefix + "strategy.md", max_chars=8000)
        assert second.metadata["decisionBaselines"] == baseline
        await invoke_document(f.service, f.identity, ReadInvestmentDocument(model_step=5))
        await invoke_document(
            f.service,
            f.identity,
            choose_request("revise", step=6, selected=choice(investment="Buy instead of producing")),
        )
        third = await f.service.store.read(f.prefix + "strategy.md", max_chars=8000)
        assert third.metadata["decisionBaselines"] != baseline


@run_async
async def test_choose_creates_one_real_existing_role_task_and_replay_never_duplicates(tmp_path):
    async with setup(tmp_path) as f:
        capability, plans = workflow(f, tmp_path)
        tools = capability.get_toolset().tools
        await tools["review_investments"].function(context(f.identity))
        ctx = context(f.identity, step=2, call="choose")
        result = await tools["choose_investment"].function(ctx, choice(), None, None)
        assert result["applied"] and result["planning"]["task"]["id"] == task_id("develop")
        again = await tools["choose_investment"].function(ctx, choice(), None, None)
        assert again["replayed"]
        assert len(await plans.store(f.identity).get_items()) == 1
        await tools["review_investments"].function(context(f.identity, step=3, call="review-again"))
        tracked = await tools["track_investment"].function(
            context(f.identity, step=4, call="track"),
            InvestmentCheckpoint(
                decisionId="develop",
                status="waiting",
                nextStep="Hold for more information",
                assessment="Route is uncertain",
            ),
        )
        assert tracked["nativeObjectiveVerified"] is False
        assert tracked["planning"]["task"]["status"] == "blocked"
        assert len(await plans.store(f.identity).get_items()) == 1 and not f.domain.host.executed


@run_async
async def test_investments_can_exceed_three_and_document_limit_preserves_existing_choices(tmp_path):
    from civ_agent.tool_recovery import RecoverableToolError

    async with setup(tmp_path) as f:
        capability, _plans = workflow(f, tmp_path)
        tools = capability.get_toolset().tools
        for index in range(4):
            await tools["review_investments"].function(context(f.identity, step=index * 2 + 1, call=f"read{index}"))
            await tools["choose_investment"].function(
                context(f.identity, step=index * 2 + 2, call=f"choose{index}"), choice(id=f"choice{index}"), None, None
            )
        # Replaying the fourth choice cannot duplicate it or remove another ID.
        await tools["choose_investment"].function(
            context(f.identity, step=8, call="choose3"), choice(id="choice3"), None, None
        )
        saved = await f.service.store.read(f.prefix + "strategy.md", max_chars=8000)
        assert [item["id"] for item in json.loads(saved.content)["decisions"]] == [f"choice{i}" for i in range(4)]
        for index in range(4, 40):
            before = await f.service.store.read(f.prefix + "strategy.md", max_chars=8000)
            await tools["review_investments"].function(context(f.identity, step=index * 2 + 1, call=f"read{index}"))
            try:
                await tools["choose_investment"].function(
                    context(f.identity, step=index * 2 + 2, call=f"choose{index}"),
                    choice(id=f"choice{index}"),
                    None,
                    None,
                )
            except RecoverableToolError as error:
                assert error.response["error"]["code"] == "INVESTMENT_DOCUMENT_FULL"
                limit = error.response["recovery"]["limits"][0]
                assert limit["maximum"] == 8000 and limit["received"] > 8000
                after = await f.service.store.read(f.prefix + "strategy.md", max_chars=8000)
                assert after == before
                assert [item["id"] for item in json.loads(after.content)["decisions"]] == [
                    f"choice{i}" for i in range(index)
                ]
                break
        else:
            pytest.fail("Expected the document character limit to reject an oversized strategy")


@run_async
async def test_economy_tracks_own_task_but_cannot_choose_or_write_main_strategy(tmp_path):
    async with setup(tmp_path, purpose="economy") as f:
        selected = choice(ownerRole="economy")
        await f.service.store.write_with_metadata(
            f.prefix + "strategy.md",
            Strategy(
                type="strategy", assessment="Main's chosen objective", decisions=[selected.decision()]
            ).model_dump_json(),
            expected_version=None,
            operation=operation("seed"),
            metadata=AuthoredMetadata(
                game=f.identity.scope.game,
                player=f.identity.scope.player,
                epoch=f.identity.scope.epoch,
                job="main",
                purpose="gameplay",
                kind="strategy",
                createdTurn=0,
                updatedTurn=0,
            ).strings(),
        )
        capability, plans = workflow(f, tmp_path)
        tools = capability.get_toolset().tools
        before = await f.service.store.read(f.prefix + "strategy.md", max_chars=8000)
        with pytest.raises(ToolFailed, match="Receipt references"):
            await tools["track_investment"].function(
                context(f.identity),
                InvestmentCheckpoint(
                    decisionId="develop",
                    status="completed",
                    nextStep="Review next target",
                    assessment="Claimed complete",
                    receiptRefs=["invented-receipt"],
                ),
            )
        assert await plans.store(f.identity).get_items() == []
        result = await tools["track_investment"].function(
            context(f.identity, step=2),
            InvestmentCheckpoint(
                decisionId="develop",
                status="completed",
                nextStep="Inspect the actual native outcome",
                assessment="Author believes the task finished",
            ),
        )
        assert result["nativeObjectiveVerified"] is False and result["receiptEvidence"] == []
        assert len(await plans.store(f.identity).get_items()) == 1
        assert (await f.service.store.read(f.prefix + "strategy.md", max_chars=8000)).version == before.version
        with pytest.raises(JournalConflict, match="ROLE_CONFLICT"):
            await invoke_document(f.service, f.identity, choose_request("economy-write"))


def test_workflow_instructions_preserve_legacy_and_replace_contradictory_save_guidance():
    original = "Start\n" + NATIVE_STRATEGY + "\nFinish"
    assert workflow_instructions(original, enabled=False) == original
    updated = workflow_instructions(original, enabled=True)
    assert NATIVE_STRATEGY not in updated and "choose_investment" in updated and "Waiting" in updated
    assert "save_strategy(assessment=" not in updated


def test_native_quote_binding_rejects_private_preparation_wrong_values_and_old_turns():
    snapshot = {
        "records": {
            "quote": {
                "version": "v1",
                "category": "observation",
                "body": {"turn": 4, "productionCost": 46, "purchase": {"cost": 190}},
            }
        },
        "delivered": {},
    }
    options = choice().model_dump()["comparison"]
    options[0]["quote"] = {
        "availability": "observed",
        "source": "quote",
        "version": "v1",
        "observedTurn": 4,
        "facts": [{"field": "/productionCost", "value": 46}],
    }
    selected = choice(comparison=options)
    assert quote_sources(snapshot) == []
    assert "not delivered" in str(quote_error(selected, snapshot))
    snapshot["delivered"]["quote"] = "v1"
    assert quote_error(selected, snapshot) is None
    options[0]["quote"]["facts"][0]["value"] = 45
    assert "differs" in str(quote_error(choice(comparison=options), snapshot))
    options[0]["quote"]["facts"][0]["value"] = 46
    options[0]["quote"]["observedTurn"] = 3
    assert "turn differs" in str(quote_error(choice(comparison=options), snapshot))
    snapshot["delivered"]["quote"] = "invalidated"
    assert "not delivered" in str(quote_error(selected, snapshot))


@pytest.mark.parametrize(
    "status,expected", [("active", "active"), ("waiting", "active"), ("completed", "ended"), ("abandoned", "ended")]
)
def test_disposition_closes_the_authored_choice_without_requiring_native_action(status, expected):
    rows = choice_versions(
        [
            {
                "key": "strategy.md",
                "version": "v2",
                "body": {
                    "state": "active",
                    "updatedTurn": 4,
                    "decisions": [choice(status=status).decision().model_dump()],
                    "_decisionBaselines": {"develop": {"id": "baseline", "authoredTurn": 0}},
                },
            }
        ],
        0,
    )
    assert rows[0]["state"] == expected
    assert rows[0]["endedTurn"] == (4 if expected == "ended" else None)


@run_async
async def test_v3_tool_exposure_replaces_only_main_generic_strategy_writer():
    capability = InvestmentWorkflow(None)
    names = ["read_strategy", "save_strategy", "review_investments", "choose_investment", "track_investment"]
    tools = [ToolDefinition(name=name) for name in names]
    for role in ("gameplay", "economy", "military", "communication"):
        for enabled in (False, True):
            ctx = SimpleNamespace(
                deps=SimpleNamespace(purpose=role, execution_policy=SimpleNamespace(follow_through_workflow=enabled))
            )
            selected = {tool.name for tool in await capability.prepare_tools(ctx, tools)}
            assert ("review_investments" in selected) == enabled
            assert ("track_investment" in selected) == enabled
            assert ("choose_investment" in selected) == (enabled and role == "gameplay")
            assert ("save_strategy" in selected) == (not enabled or role != "gameplay")


@run_async
async def test_replayed_strategy_receipt_does_not_attempt_retired_task_mutation(tmp_path):
    async with setup(tmp_path) as f:
        capability, plans = workflow(f, tmp_path)
        tools = capability.get_toolset().tools
        await tools["review_investments"].function(context(f.identity))
        ctx = context(f.identity, step=2, call="choose")
        first = await tools["choose_investment"].function(ctx, choice(), None, None)
        await f.registry.retire(f.identity)
        replay = await tools["choose_investment"].function(ctx, choice(), None, None)
        assert replay["replayed"] and replay["version"] == first["version"]
        assert len(await plans.store(f.identity).get_items()) == 1
        assert not f.domain.host.executed


def test_review_is_a_delivered_authored_intent_source_not_native_execution_proof():
    document = Strategy(type="strategy", assessment="Chosen objective", decisions=[choice().decision()])
    reply = AuthoredDocumentReply(
        id="strategy.md",
        content=document,
        version="1",
        existed=True,
        progress={"authority": "Historical receipts only"},
        planning={},
    )
    messages = [
        ModelResponse(parts=[ToolCallPart("review_investments", {}, "read")]),
        ModelRequest(
            parts=[ToolReturnPart("review_investments", reply.model_dump(mode="json", exclude_none=True), "read")]
        ),
        ModelResponse(parts=[ToolCallPart("execute_commands", {}, "action")]),
    ]
    projected = project_authored_sources(messages, "action")
    assert projected["strategy.md"]["body"]["decisions"][0]["id"] == "develop"
    assert projected["strategy.md"]["source"]["toolName"] == "review_investments"
    assert "progress" not in projected["strategy.md"]["body"]


def test_additive_choice_fields_keep_legacy_empty_serialization():
    body = choice().decision().model_dump()
    body.pop("followThrough")
    assert "status" not in body
    from civ_agent.memory_records import InvestmentAlternative

    alternative = InvestmentAlternative(
        option="Wait",
        investment="Nothing now",
        expectedOutcome="More information",
        timing="Later",
        uncertainty="Unknown",
    )
    assert "kind" not in alternative.model_dump() and "nativeQuote" not in alternative.model_dump()


@pytest.mark.parametrize("purpose", ["gameplay", "economy"])
@pytest.mark.parametrize("comparison_case", ["france", "production"])
@run_async
async def test_real_sdk_main_choice_and_economy_checkpoint_use_existing_stores(tmp_path, purpose, comparison_case):
    async with setup(tmp_path, purpose=purpose) as f:
        selected = same_kind_choice(comparison_case, ownerRole=purpose)
        if purpose == "economy":
            await f.service.store.write_with_metadata(
                f.prefix + "strategy.md",
                Strategy(
                    type="strategy", assessment="Main delegated this investment", decisions=[selected.decision()]
                ).model_dump_json(),
                expected_version=None,
                operation=operation("sdk-seed"),
                metadata=AuthoredMetadata(
                    game=f.identity.scope.game,
                    player=f.identity.scope.player,
                    epoch=f.identity.scope.epoch,
                    job="main",
                    purpose="gameplay",
                    kind="strategy",
                    createdTurn=0,
                    updatedTurn=0,
                ).strings(),
            )
        capability, plans = workflow(f, tmp_path)
        seen = []
        assessment = "Current route uncertainty warrants a hold. " * 20

        def model(messages, info):
            names = {tool.name for tool in info.function_tools}
            assert "review_investments" in names and "track_investment" in names
            assert "save_strategy" not in names
            assert ("choose_investment" in names) == (purpose == "gameplay")
            seen.append(messages)
            if len(seen) == 1:
                return ModelResponse(parts=[ToolCallPart("review_investments", {}, "review")])
            if len(seen) == 2 and purpose == "gameplay":
                return ModelResponse(
                    parts=[ToolCallPart("choose_investment", {"choice": selected.model_dump(mode="json")}, "choose")]
                )
            if len(seen) == (3 if purpose == "gameplay" else 2):
                return ModelResponse(
                    parts=[
                        ToolCallPart(
                            "track_investment",
                            {
                                "checkpoint": {
                                    "decisionId": selected.id,
                                    "status": "waiting",
                                    "nextStep": "Wait for safer access",
                                    "assessment": assessment,
                                    "receiptRefs": [],
                                }
                            },
                            "track",
                        )
                    ]
                )
            return ModelResponse(parts=[TextPart("Required native work remains separate from authored progress.")])

        agent = Agent(
            FunctionModel(model), deps_type=RoleRequest, retries=0, capabilities=[capability.documents, capability]
        )
        result = await agent.run(
            "Review the chosen investment and explicitly hold if needed.",
            deps=RoleRequest(
                **f.identity.model_dump(),
                startup=Reference(sha256="a" * 64),
                catalogue=Reference(sha256="b" * 64),
                selection=ModelSelection(model="fixture"),
                typed_authored_plans=True,
                execution_policy=ExecutionPolicy.workflows(code_mode=False),
            ),
        )
        assert result.usage.requests == (4 if purpose == "gameplay" else 3)
        items = await plans.store(f.identity).get_items()
        assert len(items) == 1 and items[0].id == task_id(selected.id) and items[0].status.value == "blocked"
        assert assessment in items[0].content
        assert not f.domain.host.executed


@pytest.mark.parametrize("case", ["france", "production"])
def test_distinct_options_may_share_an_investment_kind(case):
    selected = same_kind_choice(case)
    assert [option.kind for option in selected.comparison] == (
        ["other", "other", "wait"] if case == "france" else ["production", "production", "wait"]
    )
    assert len({option.option for option in selected.comparison}) == 3
    values = selected.model_dump()
    values["comparison"][1]["option"] = " " + selected.comparison[0].option.upper() + " "
    with pytest.raises(ValidationError, match="distinct nonempty option label"):
        InvestmentChoice.model_validate(values)
    values["comparison"][1]["option"] = "   "
    with pytest.raises(ValidationError, match="distinct nonempty option label"):
        InvestmentChoice.model_validate(values)


@run_async
async def test_semantic_validation_feedback_includes_reason_without_echoing_input():
    selected = same_kind_choice("france").model_dump()
    selected["comparison"][1]["option"] = selected["comparison"][0]["option"]
    try:
        InvestmentChoice.model_validate(selected)
    except ValidationError as error:
        cap = InvestmentWorkflow(None)
        with pytest.raises(ToolFailed, match="distinct nonempty option label") as reply:
            await cap.on_tool_validate_error(
                SimpleNamespace(
                    deps=SimpleNamespace(
                        purpose="gameplay", execution_policy=SimpleNamespace(follow_through_workflow=True)
                    )
                ),
                call=ToolCallPart("choose_investment", {}, "invalid"),
                tool_def=ToolDefinition(name="choose_investment", parameters_json_schema={}),
                args={},
                error=error,
            )
        assert "Establish a productive capital" not in str(reply.value)
    else:
        pytest.fail("Duplicate labels should get precise nonmutating feedback")
