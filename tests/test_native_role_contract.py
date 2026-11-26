"""Native SDK inputs and completion use the same frozen role mission."""

import asyncio
import copy
import json
from typing import cast

import pytest
from pydantic_ai.messages import ModelRequest, ToolReturnPart, UserPromptPart

from civ_agent.orchestration.contracts import GatewayJob, PrepareRole, canonical
from civ_agent.orchestration.gateway_registry import job_key
from civ_agent.protocol import JsonObject
from tests.test_native_player_stores import production_stores


def tool_returns(messages, name):
    return [
        part
        for message in messages
        if isinstance(message, ModelRequest)
        for part in message.parts
        if isinstance(part, ToolReturnPart) and part.tool_name == name
    ]


def role_prompt(messages):
    for message in messages:
        if not isinstance(message, ModelRequest):
            continue
        for part in message.parts:
            if isinstance(part, UserPromptPart) and isinstance(part.content, str):
                try:
                    value = json.loads(part.content)
                except ValueError:
                    continue
                if isinstance(value, dict) and "roleTask" in value:
                    return value
    raise AssertionError("The SDK model request did not contain the structured role task")


def receipt_result(part):
    content = cast(JsonObject, part.content)
    assert isinstance(content, dict)
    return content["receipts"][0]["result"]


def test_accepted_pass_stays_complete_when_a_sibling_reopens_the_role(tmp_path):
    async def run():
        async with production_stores(tmp_path) as p:
            request = await p.root.prepare_role(PrepareRole(ticket=p.ticket, purpose="gameplay", pass_number=0))
            assert request is not None
            binding = GatewayJob(scope=request.scope, job=request.job, purpose=request.purpose)
            assert not (await p.root.check_role(request)).finished
            # Represent an accepted completion whose model response is still
            # settling, followed by new work from a concurrent specialist.
            registered = p.root.registry.load(binding)
            registered.state.events["role_finished"] = True
            with p.root.memory.transaction() as db:
                p.root.memory._set(db, job_key(binding), registered.model_dump(mode="json"))
            roles = p.root.turn_roles
            roles.ready["gameplay"] = True
            roles.resume("gameplay", "New specialist coordination needs attention.")
            assert not roles.ready["gameplay"]
            assert (await p.root.check_role(request)).finished
            followup = await p.root.prepare_role(PrepareRole(ticket=p.ticket, purpose="gameplay", pass_number=1))
            assert followup is not None and followup.job != request.job
            assert not (await p.root.check_role(followup)).finished

    asyncio.run(run())


@pytest.mark.parametrize("retained", ["request", "bootstrap"])
def test_retained_role_keeps_legacy_startup_and_completion_policy(tmp_path, monkeypatch, retained):
    async def run():
        async with production_stores(tmp_path) as p:
            command = PrepareRole(ticket=p.ticket, purpose="gameplay", pass_number=0)
            register = p.root.registry.register

            async def before_admission(*args, **kwargs):
                pass

            # Freeze a bootstrap before any gateway admission so the fixture
            # can represent old saved input without rewriting a registered job.
            monkeypatch.setattr(p.root.registry, "register", before_admission)
            created = await p.root.prepare_role(command)
            monkeypatch.setattr(p.root.registry, "register", register)
            assert created is not None
            legacy_startup = {
                "instructions": "Legacy recorded mission fixture.",
                "user": json.dumps({"role": "gameplay", "legacyTask": "Continue the saved turn."}),
            }
            legacy = created.model_copy(
                update={"startup": p.store.put(created.scope, legacy_startup), "typed_authored_plans": False}
            )
            wire = legacy.model_dump(mode="json")
            record = p.root.record
            assert record is not None
            bootstrap = record["bootstraps"]["gameplay:0"]
            bootstrap["request"] = wire
            bootstrap["state"]["data"]["memory"].pop("nativeRoleContract")
            frozen_bootstrap = copy.deepcopy(bootstrap)
            frozen_startup = canonical(p.store.get(legacy.scope, legacy.startup))
            if retained == "request":
                record["requests"]["gameplay:0"] = wire
            else:
                record["requests"].pop("gameplay:0")

            restored = await p.root.prepare_role(command)
            assert restored is not None and restored.model_dump_json() == legacy.model_dump_json()
            assert record["requests"]["gameplay:0"] == wire
            assert record["bootstraps"]["gameplay:0"] == frozen_bootstrap
            saved = p.store.get(restored.scope, restored.startup)
            assert saved == legacy_startup and canonical(saved) == frozen_startup
            assert "roleTask" not in json.loads(saved["user"])
            if retained == "bootstrap":
                identity = GatewayJob(scope=restored.scope, job=restored.job, purpose=restored.purpose)
                assert "nativeRoleContract" not in p.root.registry.load(identity).state.data["memory"]

    asyncio.run(run())
