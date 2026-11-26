"""Original native envelopes remain queryable while their durable batch settles."""

import asyncio

import pytest
from pydantic import ValidationError
from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart, ToolReturnPart

from civ_agent.game_commands import command_schema
from civ_agent.host_effects import PreparedHostEffect, host_request_id
from civ_agent.orchestration.contracts import (
    GatewayCall,
    GatewayCatalogue,
    GatewayJob,
    ModelSelection,
    Reference,
    RoleRequest,
    Scope,
    digest,
)
from civ_agent.orchestration.gateway_state import GatewayState
from civ_agent.orchestration.host_effect_contracts import (
    FrozenNativeCommand,
    NativeCommandApplied,
    NativeCommandPreparation,
    NativeCommandStep,
    PendingNativeCommandBatch,
)

CATALOGUE = [{"name": "player_command", "inputSchema": command_schema({"SET_CITY_FOCUS"})}]


def role_request():
    return RoleRequest(
        scope=Scope(game="game", player="p0", epoch="g1", activation="pending-command"),
        job="pending-command-job",
        purpose="gameplay",
        selection=ModelSelection(model="test"),
        startup=Reference(sha256=digest("startup")),
        catalogue=Reference(sha256=digest("catalogue")),
    )


def command(city):
    return {"kind": "SET_CITY_FOCUS", "target": city, "selection": "FoodFocus"}


def frozen(step):
    arguments = step.call.decision["operations"][step.index]["arguments"]["command"]
    return FrozenNativeCommand(
        step=step,
        prepared=PreparedHostEffect(
            name="player_command",
            arguments=arguments,
            transport="player_command",
            payload={
                **arguments,
                "epoch": step.identity.scope.epoch,
                "revision": "original-revision",
                "requestId": host_request_id(step.identity.job, str(step.index)),
            },
            intent={"note": "original authored intent"},
        ),
        state=GatewayState(
            job=step.identity.job,
            purpose=step.identity.purpose,
            epoch=step.identity.scope.epoch,
            data={},
            queries={},
            sets={},
            events={},
            elapsed={},
            captured_at=0,
            delivered_access={"revision": "original-revision"},
        ),
        seal=digest([step.index, step.call.model_dump(mode="json")]),
    )


class Gateway:
    def __init__(self, *, blocked=True):
        self.forbidden = False
        self.calls = []
        self.effects = []
        self.entered = {name: asyncio.Event() for name in ("execute0", "apply0", "execute1", "finish")}
        self.release = {name: asyncio.Event() for name in self.entered}
        if not blocked:
            self.unblock()

    def unblock(self):
        for event in self.release.values():
            event.set()

    async def wait(self, name):
        if name in self.entered:
            self.entered[name].set()
            await self.release[name].wait()

    async def catalogue(self, deps: RoleRequest) -> GatewayCatalogue:
        assert not self.forbidden, "A history replay must not call the gateway"
        self.calls.append("catalogue")
        return GatewayCatalogue(catalogue=CATALOGUE)

    async def post(self, name, body):
        assert not self.forbidden, "A history replay must not call the gateway"
        self.calls.append(name)
        if name == "command/prepare":
            effect = frozen(body)
            self.effects.append(effect.model_copy(deep=True))
            return NativeCommandPreparation(effect=effect).model_dump(mode="json")
        if name == "command/execute":
            await self.wait("execute" + str(body.step.index))
            return {"applied": True, "revision": "changed"}
        if name == "command/apply":
            await self.wait("apply" + str(body.effect.step.index))
            return NativeCommandApplied(result=body.receipt).model_dump(mode="json")
        assert name == "command/finish"
        await self.wait("finish")
        return {"finished": True}


_gateway: Gateway | None = None


def gateway():
    assert _gateway is not None
    return _gateway


def response(messages, info):
    if any(isinstance(part, ToolReturnPart) for message in messages for part in message.parts):
        return ModelResponse(parts=[TextPart("Commands settled.")])
    return ModelResponse(
        parts=[ToolCallPart("execute_commands", {"commands": [command("city1"), command("city2")]}, "commands")]
    )


def test_pending_batch_requires_original_owner_call_and_ordered_unique_children():
    request = role_request()
    identity = GatewayJob(scope=request.scope, job=request.job, purpose=request.purpose)
    call = GatewayCall(
        call_id="commands",
        model_step=1,
        decision={
            "kind": "act",
            "operations": [{"tool": "player_command", "arguments": {"command": command("city1")}}],
        },
    )
    first = frozen(NativeCommandStep(identity=identity, call=call, index=0))
    batch = PendingNativeCommandBatch(identity=identity, call=call, effects=[first])
    assert PendingNativeCommandBatch.model_validate_json(batch.model_dump_json()) == batch
    second = first.model_copy(update={"step": first.step.model_copy(update={"index": 1})})
    foreign = first.model_copy(
        update={"step": first.step.model_copy(update={"identity": identity.model_copy(update={"job": "other"})})}
    )
    changed = first.model_copy(
        update={"step": first.step.model_copy(update={"call": call.model_copy(update={"call_id": "other"})})}
    )
    for effects in ([first, first], [second, first], [foreign], [changed]):
        with pytest.raises(ValidationError, match="NATIVE_COMMAND_EXPOSURE_CONFLICT"):
            PendingNativeCommandBatch(identity=identity, call=call, effects=effects)
