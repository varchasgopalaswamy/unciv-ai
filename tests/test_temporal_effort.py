"""Optional effort advice remains scoped, stable and broker-accounted before inference."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from civ_agent.advisory_answers import parse_answers
from civ_agent.decisions_tasks import acceptance_threshold, request_body, request_metadata
from civ_agent.orchestration.contracts import (
    GatewayJob,
    ModelSelection,
    Scope,
)
from civ_agent.orchestration.effort import select_effort, supported_levels
from civ_agent.orchestration.store import JournalConflict, WorkflowStore
from civ_agent.startup_context import prepare_startup
from tests.domain_fixtures import managed
from tests.test_effort_decisions import answer, settings


def root_for(domain, store):
    async def get(key):
        with domain.memory.transaction() as db:
            return domain.memory._meta(db, key)

    async def put(key, value):
        with domain.memory.transaction() as db:
            domain.memory._set(db, key, value)

    def check_owner(scope):
        if (scope.game, scope.player) != ("game", "p0"):
            raise JournalConflict("COORDINATOR_OWNER_CONFLICT")

    return SimpleNamespace(
        game="game",
        player="p0",
        civilization="Rome",
        memory=domain.memory,
        gateway=domain.gateway,
        io=domain.gateway.io,
        store=store,
        get=get,
        put=put,
        check_owner=check_owner,
    )


def binding(job):
    return GatewayJob(
        scope=Scope(game="game", player="p0", epoch=job.epoch, activation=job.id), job=job.id, purpose=job.purpose
    )


def response(evidence, policy, *, mode="success"):
    body = request_body(evidence, policy)
    raw = answer(body)
    if mode == "refusal":
        raw = {"name": raw["name"], "type": "refusal"}
    elif mode == "ambiguous":
        raw = answer(body, {"low": 0.35, "medium": 0.34, "high": 0.21, "xhigh": 0.05, "max": 0.05})
    elif mode == "malformed":
        raw["probabilities"][0]["probability"] = True
    return {
        **request_metadata(body, evidence),
        **parse_answers(body, {"answers": [raw]}, acceptance_threshold(evidence, policy)),
    }


@pytest.mark.parametrize("purpose", ["gameplay", "economy"])
@pytest.mark.parametrize("mode", ["success", "refusal", "ambiguous", "malformed", "timeout", "changed"])
def test_effort_is_persisted_before_inference_and_restored_with_operator_default(tmp_path, purpose, mode):
    async def run():
        async with managed(tmp_path / "domain") as domain:
            job = domain.main if purpose == "gameplay" else domain.economy
            prepared = await prepare_startup(domain.gateway, job)
            store = WorkflowStore(tmp_path / "workflow", player="p0")
            store.bind_epoch("game", "g1")
            root, policy = root_for(domain, store), settings()

            async def evaluate(evidence, *, task=None):
                if mode == "timeout":
                    raise TimeoutError()
                if mode == "changed":
                    job.role_refresh_required = True
                return response(evidence, policy, mode=mode)

            runtime = SimpleNamespace(settings=policy, evaluate=AsyncMock(side_effect=evaluate))
            domain.gateway.advisories = runtime
            selected = ModelSelection(model="gpt-6.1-sol", authentication="api_key", fast=True)
            try:
                result = await select_effort(root, binding(job), job, prepared, selected)
                assert result.reasoning_effort == ("low" if mode == "success" else "medium")
                assert selected.reasoning_effort == "medium" and result.fast
                assert result.authentication == selected.authentication and not domain.host.executed
                store.close()
                store = WorkflowStore(tmp_path / "workflow", player="p0")
                root = root_for(domain, store)
                assert await select_effort(root, binding(job), job, prepared, selected) == result
                assert runtime.evaluate.await_count == 1
            finally:
                store.close()

    asyncio.run(run())


def test_capabilities_are_provider_specific_and_unknown_knobs_keep_the_operator_default():
    assert supported_levels(ModelSelection(model="gpt-6.1-sol", authentication="api_key")) == [
        "low",
        "medium",
        "high",
        "xhigh",
        "max",
    ]
    assert supported_levels(ModelSelection(model="gpt-6.1-sol")) == ["low", "medium", "high", "xhigh"]
    for model in ("gpt-6.1-terra", "gpt-6-luna"):
        assert supported_levels(ModelSelection(model=model)) == ["low", "medium", "high", "xhigh"]
        assert supported_levels(ModelSelection(model=model, authentication="api_key")) == [
            "low",
            "medium",
            "high",
            "xhigh",
            "max",
        ]
    for selection in (
        ModelSelection(model="unrecognized-model"),
        ModelSelection(model="gpt-4o", authentication="api_key"),
        ModelSelection(provider="openrouter", model="openai/gpt-6.1-sol", authentication="api_key"),
    ):
        assert supported_levels(selection) == []


@pytest.mark.parametrize("purpose", ["gameplay", "economy"])
def test_terra_subscription_effort_consultation_uses_four_distinct_sdk_levels(tmp_path, purpose):
    async def run():
        async with managed(tmp_path / "domain") as domain:
            job = domain.main if purpose == "gameplay" else domain.economy
            prepared = await prepare_startup(domain.gateway, job)
            store = WorkflowStore(tmp_path / "workflow", player="p0")
            store.bind_epoch("game", "g1")
            policy = settings(decisions_backend="jev")
            runtime = SimpleNamespace(
                settings=policy, evaluate=AsyncMock(side_effect=lambda e, **_: response(e, policy))
            )
            domain.gateway.advisories = runtime
            selected = ModelSelection(model="gpt-6.1-terra", reasoning_effort="medium")
            root, identity = root_for(domain, store), binding(job)
            try:
                adapted = await select_effort(root, identity, job, prepared, selected)
                assert adapted == selected.model_copy(update={"reasoning_effort": "low"})
                frozen = runtime.evaluate.await_args.args[0]["effortSelection"]
                assert frozen["levels"] == ["low", "medium", "high", "xhigh"]
                assert frozen["model"] == "gpt-6.1-terra" and frozen["role"] == purpose
                assert await select_effort(root, identity, job, prepared, selected) == adapted
                assert runtime.evaluate.await_count == 1 and not domain.host.executed
            finally:
                store.close()

    asyncio.run(run())


@pytest.mark.parametrize("authentication", ["api_key", "subscription"])
def test_adaptation_uses_configured_level_subset_without_replacing_role_defaults(tmp_path, authentication):
    async def run():
        async with managed(tmp_path / "domain") as domain:
            job = domain.economy
            prepared = await prepare_startup(domain.gateway, job)
            store = WorkflowStore(tmp_path / "workflow", player="p0")
            store.bind_epoch("game", "g1")
            policy = settings(decisions_effort_levels=["high", "low"])
            runtime = SimpleNamespace(
                settings=policy, evaluate=AsyncMock(side_effect=lambda e, **_: response(e, policy))
            )
            domain.gateway.advisories = runtime
            selected = ModelSelection(model="gpt-6.1-sol", authentication=authentication, reasoning_effort="high")
            try:
                adapted = await select_effort(root_for(domain, store), binding(job), job, prepared, selected)
                assert adapted.reasoning_effort == "low" and selected.reasoning_effort == "high"
                frozen = runtime.evaluate.await_args.args[0]["effortSelection"]
                assert frozen["levels"] == ["low", "high"] and frozen["role"] == "economy"
            finally:
                store.close()

    asyncio.run(run())


@pytest.mark.parametrize("kind", ["openrouter", "unknown", "disabled", "one_level", "none", "military"])
def test_unavailable_or_inapplicable_advice_has_no_broker_request(tmp_path, kind):
    async def run():
        async with managed(tmp_path / "domain") as domain:
            job = domain.main
            prepared = await prepare_startup(domain.gateway, job)
            store = WorkflowStore(tmp_path / "workflow", player="p0")
            store.bind_epoch("game", "g1")
            policy = settings()
            if kind == "disabled":
                policy.decisions_effort_enabled = False
            elif kind == "one_level":
                policy.decisions_effort_levels = ["low", "max"]
            selected = ModelSelection(model="gpt-6.1-sol")
            if kind == "openrouter":
                selected = ModelSelection(provider="openrouter", model="openai/gpt-6.1-sol", authentication="api_key")
            elif kind == "unknown":
                selected = ModelSelection(model="unknown")
            elif kind == "none":
                selected = selected.model_copy(update={"reasoning_effort": "none"})
            elif kind == "military":
                job.purpose = "military"
            runtime = SimpleNamespace(settings=policy, evaluate=AsyncMock())
            domain.gateway.advisories = runtime
            try:
                assert await select_effort(root_for(domain, store), binding(job), job, prepared, selected) == selected
                runtime.evaluate.assert_not_awaited()
            finally:
                store.close()

    asyncio.run(run())


@pytest.mark.parametrize("damage", ["player", "epoch", "selection", "policy", "revision"])
def test_recorded_effort_cannot_cross_authority_or_apply_to_refreshed_facts(tmp_path, damage):
    async def run():
        async with managed(tmp_path / "domain") as domain:
            job, policy = domain.main, settings()
            prepared = await prepare_startup(domain.gateway, job)
            store = WorkflowStore(tmp_path / "workflow", player="p0")
            store.bind_epoch("game", "g1")
            root = root_for(domain, store)
            runtime = SimpleNamespace(
                settings=policy, evaluate=AsyncMock(side_effect=lambda e, **_: response(e, policy))
            )
            domain.gateway.advisories = runtime
            selected = ModelSelection(model="gpt-6.1-sol", authentication="api_key")
            identity = binding(job)
            try:
                assert (await select_effort(root, identity, job, prepared, selected)).reasoning_effort == "low"
                if damage == "player":
                    identity = identity.model_copy(update={"scope": identity.scope.model_copy(update={"player": "p1"})})
                elif damage == "epoch":
                    store.bind_epoch("game", "g2", previous="g1")
                elif damage == "selection":
                    selected = selected.model_copy(update={"fast": True})
                elif damage == "policy":
                    policy.decisions_effort_min_margin = 0.2
                else:
                    job.observation["revision"] = "fresh-revision"
                if damage == "revision":
                    assert await select_effort(root, identity, job, prepared, selected) == selected
                else:
                    with pytest.raises(JournalConflict):
                        await select_effort(root, identity, job, prepared, selected)
                assert runtime.evaluate.await_count == 1
            finally:
                store.close()

    asyncio.run(run())
