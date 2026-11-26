"""Only trusted scripted sessions enable the native-store exercise."""

import asyncio
import json

import pytest

from civ_agent.orchestration.authored_memory import contribution_prefix
from civ_agent.orchestration.contracts import GatewayJob, PrepareRole, Scope
from civ_agent.orchestration.managed_player import PrepareManagedTurn
from tests.test_managed_player import coordinator, session


def scripted_session():
    return session(
        model="scripted-v1",
        scripted=True,
        civilization_guide=False,
        economy_model={"provider": "openai", "model": "scripted-v1", "reasoning_effort": "medium"},
        military_model={"provider": "openai", "model": "scripted-v1", "reasoning_effort": "medium"},
    )


@pytest.mark.parametrize(
    "managed,scripted,isolated", [(True, True, False), (True, False, True), (False, False, True), (False, False, False)]
)
def test_store_exercise_uses_trusted_managed_setting_or_explicit_isolated_flag(tmp_path, managed, scripted, isolated):
    async def run():
        async with coordinator(tmp_path) as item:
            item.root.game_state["request"] = {"exercise_native_stores": isolated}
            if managed:
                configured = scripted_session() if scripted else session()
                item.root.game_state["managedSession"] = configured.model_dump(mode="json")
            identity = GatewayJob(
                scope=Scope(game="game", player="p0", epoch="g1", activation="turn"),
                job="job",
                purpose="military",
            )
            actual = item.root.fixture_startup(identity)
            if scripted if managed else isolated:
                assert actual == {
                    "nativeStoreExercise": True,
                    "nativeStoreJob": identity.job,
                    "nativeStoreContributionFile": contribution_prefix(identity) + "native-stores.md",
                }
            else:
                assert actual == {}

    asyncio.run(run())


def test_managed_scripted_role_preparation_publishes_existing_native_store_fixture(tmp_path):
    async def run():
        async with coordinator(tmp_path) as item:
            configured = scripted_session()
            ticket = await item.root.prepare_managed_turn(
                PrepareManagedTurn(session=configured, index=0, expected_epoch="g1", expected_handoff=42)
            )
            for purpose in ("gameplay", "economy", "military"):
                if purpose != "gameplay":
                    item.root.turn_roles.resume(purpose, "Main requested the fixture role")
                prepared = await item.root.prepare_role(PrepareRole(ticket=ticket, purpose=purpose))
                assert prepared is not None and prepared.request_limit == configured.request_limit == 24
                startup = json.loads(item.store.get(prepared.scope, prepared.startup)["user"])
                assert startup["nativeStoreExercise"] is True and startup["nativeStoreJob"] == prepared.job
                assert (
                    startup["nativeStoreContributionFile"]
                    == contribution_prefix(GatewayJob(scope=prepared.scope, job=prepared.job, purpose=purpose))
                    + "native-stores.md"
                )
                assert prepared.selection.model == "scripted-v1"

    asyncio.run(run())
