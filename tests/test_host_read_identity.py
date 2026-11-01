"""Fresh host observations cannot alias completed reads or action receipts."""

import asyncio
import json
from unittest.mock import AsyncMock

from sqlalchemy import select

from civ_agent import memory_schema as tables
from civ_agent.proposal_gateway import ProposalGateway
from tests.test_economy_optimization import managed
from tests.test_economy_roles import run_async


@run_async
async def test_host_refreshes_use_current_facts_with_changed_or_identical_arguments(tmp_path):
    async with managed(tmp_path) as m:
        await m.gateway.call(m.main, "player_turn", {"delivery": "auto"}, "resume:2")
        original = m.gateway.call
        ids = []

        async def tracked(job, name, arguments, call_id, **kwargs):
            ids.append(call_id)
            return await original(job, name, arguments, call_id, **kwargs)

        m.gateway.call = tracked
        for index, delivery in enumerate(("auto", "full", "full")):
            m.host.view["cities"][0]["name"] = "Fresh " + str(index)
            m.host.revision = "refresh-" + str(index)
            result = await m.gateway.fresh_turn(m.main, {"delivery": delivery}, "resume-role")
            assert m.main.observation["cities"][0]["name"] == "Fresh " + str(index)
            if delivery == "full":
                assert result["cities"][0]["name"] == "Fresh " + str(index)
        assert len(set(ids)) == 3
        # A new gateway instance must not restart a counter and replay old reads.
        restarted = ProposalGateway(m.memory, m.gateway.io, tmp_path / "restarted")
        restarted.jobs[m.main.token] = m.main
        restarted.role_coordinator = m.roles
        m.host.view["cities"][0]["name"] = "After restart"
        m.host.revision = "after-restart"
        result = await restarted.fresh_turn(m.main, {"delivery": "full"}, "resume-role")
        assert result["cities"][0]["name"] == "After restart"
        with m.memory.transaction() as db:
            rows = list(
                db.execute(
                    select(tables.calls.c.id, tables.calls.c.arguments).where(
                        tables.calls.c.job == m.main.id, tables.calls.c.id.like("host:resume-role:%")
                    )
                )
            )
        assert len(rows) == 4
        assert not m.host.executed


@run_async
async def test_concurrent_host_refreshes_have_distinct_receipts(tmp_path):
    async with managed(tmp_path) as m:
        results = await asyncio.gather(
            *(m.gateway.fresh_turn(m.main, {"delivery": "full"}, "resume-role") for _ in range(12))
        )
        assert all("cities" in result for result in results)
        with m.memory.transaction() as db:
            identities = list(
                db.scalars(
                    select(tables.calls.c.id).where(
                        tables.calls.c.job == m.main.id, tables.calls.c.id.like("host:resume-role:%")
                    )
                )
            )
        assert len(set(identities)) == 12
        assert not m.host.executed


@run_async
async def test_final_review_refreshes_when_readiness_changes_without_a_new_handoff_pass(tmp_path):
    async with managed(tmp_path) as m:
        roles = m.roles
        seen = []
        observed = asyncio.Queue()
        roles.synchronize_war = AsyncMock()

        async def reopen(view):
            seen.append(view["cities"][0]["name"])
            roles.ready["gameplay"] = False
            observed.put_nowait(len(seen))

        roles.synchronize_economy = reopen
        advance = asyncio.create_task(roles.advance())
        try:
            for index in range(2):
                m.host.view["cities"][0]["name"] = "Review " + str(index)
                m.host.revision = "review-" + str(index)
                roles.ready.update({role: True for role in roles.ready})
                roles.changed.set()
                assert await asyncio.wait_for(observed.get(), 2) == index + 1
            assert seen == ["Review 0", "Review 1"]
            assert roles.record.get("handoffPass", 0) == 0
            assert not roles.committed and not m.host.executed
            with m.memory.transaction() as db:
                receipts = list(
                    db.scalars(
                        select(tables.calls.c.result)
                        .where(tables.calls.c.job == m.main.id, tables.calls.c.id.like("host:handoff-review:%"))
                        .order_by(tables.call_order)
                    )
                )
            assert [json.loads(r)["cities"][0]["name"] for r in receipts] == seen
        finally:
            advance.cancel()
            await asyncio.gather(advance, return_exceptions=True)
