"""Peace preparation freezes exact prerequisites without dispatching effects."""

import asyncio
import copy
from types import SimpleNamespace
from typing import Literal

import pytest

from civ_agent.orchestration.contracts import ModelSelection, PeacePhaseResult, PeaceResult
from civ_agent.orchestration.peace_authorization import NativePeace
from civ_agent.orchestration.store import JournalConflict, WorkflowStore
from civ_agent.peace_contracts import peace_terms
from tests.test_native_command_service import native
from tests.test_prepare_host_effect import counts
from tests.test_temporal_peace import overview


async def peace_service(scope, path):
    store = WorkflowStore(path, player="p0")
    store.bind_epoch("game", "g1")

    async def get(key):
        with scope.memory.transaction() as db:
            return copy.deepcopy(scope.memory._meta(db, key))

    async def put(key, value):
        with scope.memory.transaction() as db:
            scope.memory._set(db, key, value)

    def owner(value):
        assert (value.game, value.player, value.epoch) == ("game", "p0", "g1")

    root = SimpleNamespace(
        game="game",
        player="p0",
        memory=scope.memory,
        store=store,
        io=scope.gateway.io,
        registry=scope.owner,
        gateway=scope.gateway,
        external_lease_id="shared-lease",
        check_owner=owner,
        get=get,
        put=put,
        get_selection=lambda _: ModelSelection(model="scripted-v1"),
        get_provider_contract=lambda _: None,
        get_request_limit=lambda _: 2,
    )
    return NativePeace(root), store


def payload():
    return {
        "partner": "Greece",
        "give": [{"type": "Treaty", "name": "Peace Treaty", "amount": 1, "duration": 10}],
        "receive": [],
    }


def sealed(store, request, *, approve=True):
    reports = [overview(), {"approve": approve, "reason": "Review exact frozen terms."}]
    names: tuple[Literal["military"], Literal["gameplay"]] = ("military", "gameplay")
    phases = [
        PeacePhaseResult(
            phase=phase, status="COMPLETED", report=report, history=store.put(request.scope, []), usage={"requests": 1}
        )
        for phase, report in zip(names, reports, strict=True)
    ]
    result = PeaceResult(
        scope=request.scope,
        basis=request.basis,
        status="COMPLETED",
        overview=reports[0],
        approval=reports[1],
        phases=phases,
    )
    body = request.model_dump(mode="json")
    store.reserve(request.scope, "peace-result", body)
    store.complete(request.scope, "peace-result", body, result.model_dump(mode="json"))
    return result


@pytest.mark.parametrize("managed", [True, False])
def test_results_only_project_sealed_consultations(tmp_path, managed):
    async def run():
        async with native(tmp_path / "domain", purpose="gameplay") as scope:
            service, store = await peace_service(scope, tmp_path / "artifacts")
            try:
                prepared = await service._prepare(scope.main, peace_terms("trade_offer", payload()), managed=managed)
                assert prepared is not None
                pending = prepared[-1]
                if not managed:
                    with pytest.raises(JournalConflict, match="PEACE_COMPLETION_UNCONFIRMED"):
                        await service.results(scope.identity.scope)
                else:
                    assert await service.results(scope.identity.scope) == []
                published = sealed(store, pending)
                assert await service.results(scope.identity.scope) == [published]
            finally:
                store.close()

    asyncio.run(run())


@pytest.mark.parametrize("outcome", ["approved", "declined", "stale", "unsealed", "wrong_receipt"])
def test_local_consultation_requires_exact_terms_sealed_receipt_and_current_basis(tmp_path, outcome):
    async def run():
        async with native(tmp_path / "domain", purpose="gameplay") as scope:
            service, store = await peace_service(scope, tmp_path / "artifacts")
            consulted = []

            async def consult(request):
                consulted.append(request)
                frozen = store.get(request.scope, request.inputs)
                assert frozen["exactPeaceTerms"] == peace_terms("trade_offer", payload())
                assert request.managed and request.scope.epoch == scope.main.epoch
                if outcome == "stale":
                    scope.host.view["cities"] = []
                result = sealed(store, request, approve=outcome != "declined")
                if outcome == "unsealed":
                    # An in-memory report cannot authorize a native effect.
                    with store.transaction(request.scope) as db:
                        from civ_agent.orchestration.store import calls

                        db.execute(calls.delete().where(calls.c.id == request.scope.identity("call", "peace-result")))
                if outcome == "wrong_receipt":
                    return result.model_copy(update={"basis": "0" * 64})
                return result

            service.consult = consult
            try:
                before = counts(scope.memory)
                arguments = payload()
                if outcome in {"unsealed", "wrong_receipt"}:
                    with pytest.raises(JournalConflict, match="PEACE_RECEIPT_REQUIRED"):
                        await service.authorize(scope.main, "trade_offer", arguments)
                else:
                    problem = await service.authorize(scope.main, "trade_offer", arguments)
                    if outcome == "approved":
                        assert problem is None
                        assert arguments["expectedRevision"] == (await scope.host.tool("player_turn", {}))["revision"]
                        assert await service.authorize(scope.main, "trade_offer", payload()) is None
                    else:
                        assert (
                            problem["error"]
                            == {"declined": "MAIN_DECLINED_PEACE", "stale": "PEACE_REVIEW_STALE"}[outcome]
                        )
                assert len(consulted) == 1
                assert counts(scope.memory) == before and not scope.host.executed
                assert scope.gateway.consultation_hold.is_set() and scope.main.wait_started is None
            finally:
                store.close()

    asyncio.run(run())


def test_local_consultation_freezes_changed_terms_separately_and_requires_runner(tmp_path):
    async def run():
        async with native(tmp_path / "domain", purpose="gameplay") as scope:
            service, store = await peace_service(scope, tmp_path / "artifacts")
            try:
                before = counts(scope.memory)
                first = await service._prepare(scope.main, peace_terms("trade_offer", payload()), managed=True)
                changed = payload()
                changed["receive"] = [{"type": "Gold", "name": "Gold", "amount": 50, "duration": 0}]
                second = await service._prepare(scope.main, peace_terms("trade_offer", changed), managed=True)
                assert first is not None and second is not None
                assert first[-1].basis != second[-1].basis
                assert first[-1].scope != second[-1].scope
                with pytest.raises(JournalConflict, match="PEACE_RUNNER_NOT_CONFIGURED"):
                    await service.authorize(scope.main, "trade_offer", payload())
                assert (
                    await service.authorize(scope.main, "chat_send", {"to": "Greece", "text": "Discuss peace."}) is None
                )
                assert counts(scope.memory) == before and not scope.host.executed
            finally:
                store.close()

    asyncio.run(run())
