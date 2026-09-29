import asyncio

import pytest
from jsonschema import Draft202012Validator
from pydantic import ValidationError

from civ_agent.civilization_state import CivilizationState
from civ_agent.game_commands import command_arguments, command_schema
from civ_agent.observations import DetailQuery, detail_scope, overview, required_detail
from civ_agent.proposal_gateway import ProposalGateway
from civ_agent.protocol import JsonObject
from civ_agent.service_io import ServiceIO
from tests.test_proposal_gateway import Host


@pytest.mark.parametrize("kind", ["MOVE_UNIT", "SET_UNIT_DESTINATION"])
def test_capture_risk_confirmation_is_explicit_and_scoped_to_movement(kind):
    command = {"kind": kind, "target": "unit1", "destination": {"x": 2, "y": 0}, "confirmAction": True}
    assert command_arguments(command) == command
    for value in ("true", 1, None):
        with pytest.raises(ValidationError):
            command_arguments({**command, "confirmAction": value})
    with pytest.raises(ValidationError):
        command_arguments({**command, "kind": "REBASE_UNIT"})


@pytest.mark.parametrize("first_stop_risky", [True, False])
def test_route_confirmation_only_applies_to_current_stop_and_never_prefills_consent(first_stop_risky):
    from civ_agent.action_templates import add_decision_commands, add_unit_commands

    risk = {"destination": {"x": 2, "y": 0}, "threats": [{"name": "Brute"}]}
    move = {"destination": {"x": 2, "y": 0}, "afterMove": {"captureRisk": risk if first_stop_risky else None}}
    unit = {"handle": "unit1", "moves": [move]}
    add_unit_commands(unit)
    assert (move.get("confirmationField") == "confirmAction") is first_stop_risky
    assert "confirmAction" not in move["command"]
    preview: JsonObject = {"available": True, "turns": [move, {"afterMove": {"captureRisk": risk}}]}
    add_decision_commands("movement_preview", preview, {"target": "unit1", "destination": {"x": 7, "y": 0}})
    assert (preview.get("confirmationField") == "confirmAction") is first_stop_risky
    assert "confirmAction" not in preview["command"]


@pytest.mark.parametrize(
    "command,read",
    [
        ({"kind": "SET_UNIT_DESTINATION", "target": "unit1", "destination": {"x": 8, "y": 0}}, "movement_preview"),
        ({"kind": "CANCEL_UNIT_MOVEMENT", "target": "unit1"}, "unit_details"),
        ({"kind": "CREATE_WATER_IMPROVEMENT", "target": "unit1", "selection": "Fishing Boats"}, "unit_details"),
    ],
)
def test_naval_commands_have_explicit_shapes_and_offer_reads(command, read):
    assert Draft202012Validator(command_schema()).is_valid(command)
    assert command_arguments(command) == command
    detail = required_detail(command)
    assert detail is not None and detail.tool == read
    with pytest.raises(ValidationError):
        command_arguments({**command, "attackFrom": {"x": 0, "y": 0}})


class MovementHost(Host):
    settlement = None

    async def tool(self, name, args):
        if name == "movement_preview":
            return {
                "epoch": "g1",
                "revision": self.revision,
                "turn": 0,
                **args,
                "available": args["destination"]["x"] >= 0,
                "estimatedTurns": 4,
                "turns": [
                    {
                        "destination": args["destination"],
                        "path": [args["destination"]],
                        "unexploredTiles": 1,
                        "tilesOutsideCurrentSight": 1,
                    }
                ],
                "settlement": self.settlement,
            }
        return await super().tool(name, args)


@pytest.mark.parametrize("kind", ["MOVE_UNIT", "SET_UNIT_DESTINATION"])
def test_capture_refusal_preserves_inspection_and_requires_agent_confirmation(tmp_path, kind):
    risk = {"destination": {"x": 2, "y": 0}, "threats": [{"name": "Brute", "movement": 2}]}

    class RiskHost(MovementHost):
        async def tool(self, name, args):
            if name == "player_command" and not args.get("confirmAction"):
                return {
                    "error": {"code": "CIVILIAN_CAPTURE_RISK", "message": "Possible capture"},
                    "applied": False,
                    "recoverable": True,
                    "captureRisk": risk,
                    "recovery": {"confirmationField": "confirmAction"},
                }
            result = await super().tool(name, args)
            if name == "movement_preview":
                result["turns"][0]["afterMove"] = {"captureRisk": risk}
            return result

    async def run():
        io = ServiceIO()
        state = CivilizationState(tmp_path / "state", "Rome")
        gateway = ProposalGateway(state, io, tmp_path / "workspace")
        host = RiskHost()
        host.view["map"]["units"][0].update(
            name="Settler",
            military=False,
            moves=[{"destination": risk["destination"], "afterMove": {"captureRisk": risk}}],
        )
        job = await gateway.open("game", "gameplay", "g1", host, {})
        try:
            await gateway.call(job, "player_turn", {}, "start")
            query = {"target": "unit1", "destination": risk["destination"]}
            if kind == "MOVE_UNIT":
                detail = await gateway.call(
                    job, "unit_details", {"target": "unit1", "sections": ["movement"]}, "inspect"
                )
                offer = detail["data"]["entity"]["moves"][0]
            else:
                offer = await gateway.call(job, "movement_preview", query, "inspect")
            assert offer["confirmationField"] == "confirmAction"
            action = {**offer["command"], "intent": {"reason": "Reach the site"}}
            rejected = await gateway.call(job, "player_command", action, "refused")
            assert rejected["error"]["code"] == "CIVILIAN_CAPTURE_RISK"
            assert rejected["applied"] is False and rejected["captureRisk"] == risk
            assert not host.executed and host.revision == "rev1"
            # Refusal did not move anything or invalidate the preview; accepting is a new decision.
            accepted = {**action, "confirmAction": True, "intent": {"reason": "Accept the exposed stop"}}
            receipt = await gateway.call(job, "player_command", accepted, "confirmed")
            assert "error" not in receipt and host.executed[-1]["confirmAction"] is True
            assert await gateway.call(job, "player_command", accepted, "confirmed") == receipt
            assert len(host.executed) == 1
        finally:
            await gateway.close(job, True)
            await io.close()

    asyncio.run(run())


def test_distant_order_requires_exact_current_preview_and_invalidates_memory_on_change(tmp_path):
    async def run():
        io = ServiceIO()
        state = CivilizationState(tmp_path / "state", "Rome")
        gateway = ProposalGateway(state, io, tmp_path / "workspace")
        host = MovementHost()
        job = await gateway.open("game", "gameplay", "g1", host, {})
        query = {"target": "unit1", "destination": {"x": 8, "y": 0}}
        action = {"kind": "SET_UNIT_DESTINATION", **query, "intent": {"reason": "Sail to the chosen island"}}
        try:
            await gateway.call(job, "player_turn", {}, "read")
            await gateway.call(job, "unit_details", {"target": "unit1"}, "unit")
            missing = await gateway.call(job, "player_command", action, "no-preview")
            assert missing["error"] == "DETAIL_REQUIRED"
            assert missing["recovery"]["reads"] == [{"tool": "movement_preview", "arguments": query}]
            assert not host.executed
            preview = await gateway.call(job, "movement_preview", query, "preview")
            assert preview["available"] and "revision" not in preview
            canonical = "observation:" + detail_scope(DetailQuery.model_validate(preview["query"]))
            assert canonical in job.memory["records"]
            elsewhere = {**action, "destination": {"x": 9, "y": 0}}
            assert (await gateway.call(job, "player_command", elsewhere, "wrong-route"))["error"] == "DETAIL_REQUIRED"
            await gateway.call(job, "player_turn", {}, "same-view")
            assert job.memory["records"][canonical]["body"]["available"]
            receipt = await gateway.call(job, "player_command", action, "move")
            assert host.executed[-1]["destination"] == query["destination"]
            assert await gateway.call(job, "player_command", action, "move") == receipt
            await gateway.call(job, "player_turn", {}, "changed-view")
            assert not job.memory["records"][canonical]["body"]["available"]
            assert (await gateway.call(job, "player_command", action, "old-preview"))["error"] == "DETAIL_REQUIRED"
            host.revision = "rev99"
            stale = await gateway.call(job, "movement_preview", query, "stale-read")
            assert stale["error"] == "OBSERVATION_REQUIRED"
            assert not job.details_read
            await gateway.call(job, "player_turn", {}, "new-view")
            unavailable = {"target": "unit1", "destination": {"x": -1, "y": 0}}
            assert not (await gateway.call(job, "movement_preview", unavailable, "unavailable"))["available"]
            assert (await gateway.call(job, "player_command", {**action, **unavailable}, "blocked"))[
                "error"
            ] == "DETAIL_REQUIRED"
        finally:
            await gateway.close(job, True)
            await io.close()

    asyncio.run(run())


def test_overview_exposes_destination_progress_and_work_boat_offer():
    host = MovementHost()
    host.view["map"]["units"][0]["movementOrder"] = {"destination": {"x": 8, "y": 0}, "canCancel": True}
    host.view["map"]["units"][0]["waterImprovement"] = {"name": "Fishing Boats", "available": True}
    host.view["phase"] = "ACTIVE"
    shown = overview(host.view)["units"][0]
    assert shown["movementOrder"] == host.view["map"]["units"][0]["movementOrder"]
    assert shown["canCreateWaterImprovement"]


@pytest.mark.parametrize("status", ["BLOCKED", "UNKNOWN"])
def test_settlement_advice_does_not_prevent_legal_movement(tmp_path, status):
    async def run():
        io = ServiceIO()
        gateway = ProposalGateway(CivilizationState(tmp_path / "state", "Rome"), io, tmp_path / "workspace")
        host = MovementHost()
        host.settlement = {"siteStatus": status, "canFoundNow": None}
        job = await gateway.open("game", "gameplay", "g1", host, {})
        query = {"target": "unit1", "destination": {"x": 8, "y": 0}}
        try:
            await gateway.call(job, "player_turn", {}, "read")
            preview = await gateway.call(job, "movement_preview", query, "preview")
            assert preview["settlement"] == host.settlement
            assert preview["command"] == {"kind": "SET_UNIT_DESTINATION", **query}
            action = {**preview["command"], "intent": {"reason": "Scout beyond this blocked city site"}}
            await gateway.call(job, "player_command", action, "move")
            assert host.executed[-1]["destination"] == query["destination"]
        finally:
            await gateway.close(job, True)
            await io.close()

    asyncio.run(run())
