"""Copyable arguments attached to native offers after selecting observation fields."""

from typing import TypeGuard

from civ_agent.game_commands import add_combat_commands, command_arguments
from civ_agent.protocol import JsonObject


def attach(option: JsonObject, kind: str, *, confirmation: str | None = None, **arguments) -> None:
    option["command"] = command_arguments({"kind": kind, **arguments})
    if confirmation:
        # Deliberate consent must come from the agent after reading consequences.
        option["confirmationField"] = confirmation


def available(option: JsonObject | None) -> TypeGuard[JsonObject]:
    return bool(option and option.get("available") is True)


def add_overview_commands(value: JsonObject) -> None:
    """Use complete visible decisions; never prefill intent, consent or deferral."""
    for alert in value.get("alerts", []):
        if alert.get("canAcknowledge") is True and alert.get("content") is not None:
            attach(alert, "ACKNOWLEDGE_ALERT", target=alert["handle"])
    if value.get("canEndTurn") is True:
        value["endTurn"] = {
            "command": command_arguments({"kind": "END_TURN"}),
            "instruction": "Read turnReview and tacticalReview. Add intent and a top-level deferReason explaining any intentional deferral. Save useful memory changes before executing.",
        }
    for unit in value.get("units", []):
        if unit.get("canFoundCity"):
            unit.setdefault("inspections", {})["FOUND_CITY"] = {
                "tool": "unit_details",
                "arguments": {"target": unit["handle"], "sections": ["settlement"]},
            }


def add_unit_commands(unit: JsonObject) -> None:
    target = unit["handle"]
    add_combat_commands({"map": {"units": [unit]}})
    for move in unit.get("moves", []):
        attach(move, "MOVE_UNIT", target=target, destination=move["destination"])
        move["destinationInspection"] = {
            "tool": "movement_preview",
            "arguments": {"target": target, "destination": move["destination"]},
        }
    order = unit.get("movementOrder")
    if order and order.get("canCancel"):
        attach(order, "CANCEL_UNIT_MOVEMENT", target=target)
    if unit.get("canFoundCity"):
        unit["foundCityCommand"] = command_arguments({"kind": "FOUND_CITY", "target": target})
        if unit.get("settlementPromises"):
            unit["foundCityConfirmationField"] = "confirmBreakPromise"
    worker = unit.get("worker") or {}
    for option in worker.get("options", []):
        if available(option):
            attach(option, "START_WORKER_JOB", target=target, selection=option["name"])
    if worker.get("canStop"):
        worker["stopCommand"] = command_arguments({"kind": "STOP_WORKER_JOB", "target": target})
    water = unit.get("waterImprovement")
    if available(water):
        attach(water, "CREATE_WATER_IMPROVEMENT", target=target, selection=water["name"])
    economy = unit.get("economy") or {}
    for option in economy.get("upgrades", []):
        if available(option):
            attach(
                option,
                "UPGRADE_UNIT",
                target=target,
                selection=option["targetName"],
                confirmation="confirmAction" if option.get("confirmation") else None,
            )
    for key, kind in (("pillage", "PILLAGE"), ("disband", "DISBAND_UNIT")):
        option = economy.get(key)
        if available(option):
            attach(option, kind, target=target, confirmation="confirmAction" if option.get("confirmation") else None)
    for option in (unit.get("greatPerson") or {}).get("abilities", []):
        if available(option):
            attach(option, "ACTIVATE_GREAT_PERSON", target=target, selection=option["name"])
    assembly = unit.get("spaceshipAssembly")
    if available(assembly):
        attach(assembly, "ADD_SPACESHIP_PART", target=target)
    gift = unit.get("cityStateGift")
    if gift and available(gift.get("action")):
        attach(gift, "GIFT_UNIT", target=target)
    for base in (unit.get("air") or {}).get("bases", []):
        if base.get("canRebase"):
            base["inspection"] = {
                "tool": "mission_preview",
                "arguments": {
                    "target": target,
                    "mission": "REBASE",
                    "destination": base["position"],
                },
            }


def add_purchase_command(option: JsonObject, target: str, *, tile: bool = False) -> None:
    if available(option):
        arguments = {"destination": option["position"]} if tile else {"selection": option["name"]}
        attach(
            option,
            "BUY_TILE" if tile else "BUY_CONSTRUCTION",
            target=target,
            **arguments,
            confirmation="confirmAction" if option.get("confirmation") else None,
        )


def add_city_commands(city: JsonObject) -> None:
    target = city["handle"]
    add_combat_commands({"cities": [city]})
    purchases = city.get("purchases") or {}
    for option in purchases.get("constructions", []):
        add_purchase_command(option, target)
    for option in purchases.get("tiles", []):
        add_purchase_command(option, target, tile=True)
    development = city.get("development") or {}
    for option in development.get("constructions", []):
        if option.get("canRemove"):
            option["removeCommand"] = command_arguments(
                {"kind": "REMOVE_CONSTRUCTION", "target": target, "index": option["index"]}
            )
        if option.get("moveToIndices"):
            option["moveCommands"] = [
                command_arguments(
                    {"kind": "MOVE_CONSTRUCTION", "target": target, "index": option["index"], "toIndex": index}
                )
                for index in option["moveToIndices"]
            ]
    for tile in development.get("tiles", []):
        commands = []
        for flag, kind, field, value in (
            ("canWork", "SET_WORKED_TILE", "worked", True),
            ("canUnwork", "SET_WORKED_TILE", "worked", False),
            ("canLock", "SET_TILE_LOCKED", "locked", True),
            ("canUnlock", "SET_TILE_LOCKED", "locked", False),
        ):
            if tile.get(flag):
                commands.append(
                    command_arguments({"kind": kind, "target": target, "destination": tile["position"], field: value})
                )
        if commands:
            tile["commands"] = commands
    management = city.get("management") or {}
    for option in management.get("focusOptions", []):
        if available(option.get("action")):
            attach(option, "SET_CITY_FOCUS", target=target, selection=option["name"])
    for option in management.get("specialists", []):
        for field, kind in (("assign", "ASSIGN_SPECIALIST"), ("unassign", "UNASSIGN_SPECIALIST")):
            if available(option.get(field)):
                attach(option[field], kind, target=target, selection=option["name"])
    for field, kind, enabled in (
        ("avoidGrowth", "SET_AVOID_GROWTH", not development.get("avoidGrowth", False)),
        ("specialistControl", "SET_MANUAL_SPECIALISTS", not management.get("manualSpecialists", False)),
    ):
        if available(management.get(field)):
            attach(management[field], kind, target=target, enabled=enabled)
    if available(management.get("resetCitizens")):
        attach(management["resetCitizens"], "RESET_CITIZENS", target=target)
    for option in management.get("sharedTiles", []):
        if available(option.get("action")):
            attach(option, "REASSIGN_CITY_TILE", target=target, destination=option["position"])
    for option in management.get("buildingSales", []):
        if available(option.get("action")):
            attach(option, "SELL_BUILDING", target=target, selection=option["name"], confirmation="confirmAction")
    for option in city.get("captureOptions", []):
        if available(option):
            attach(option, "MANAGE_CAPTURED_CITY", target=target, selection=option["name"])


def add_construction_commands(data: JsonObject) -> None:
    if not data["descriptionsIncluded"]:
        data["instruction"] = (
            "Read construction_details with names=[chosen names] for descriptions, commands and purchase confirmations."
        )
        return
    for option in data["constructions"]:
        if option["canQueue"]:
            attach(option, "QUEUE_CONSTRUCTION", target=data["city"], selection=option["name"])
        if option.get("purchase"):
            add_purchase_command(option["purchase"], data["city"])


def add_decision_commands(name: str, value: JsonObject, arguments: JsonObject) -> None:
    if name == "research_details" and arguments.get("names"):
        for option in value.get("technologies", []):
            if option.get("queueAvailable") and option.get("prerequisiteQueue"):
                attach(option, "SET_RESEARCH_QUEUE", queue=option["prerequisiteQueue"])
            elif available(option.get("researchNow")):
                attach(option, "SET_RESEARCH_QUEUE", queue=[option["name"]])
            if available(option.get("freeChoice")):
                attach(option["freeChoice"], "CHOOSE_FREE_TECHNOLOGY", selection=option["name"])
    elif name == "policy_details" and arguments.get("names"):
        for option in value.get("policies", []):
            if available(option.get("adoption")):
                attach(option, "ADOPT_POLICY", selection=option["name"])
    elif name in {"movement_preview", "mission_preview"} and available(value):
        kind = (
            "SET_UNIT_DESTINATION"
            if name == "movement_preview"
            else {
                "REBASE": "REBASE_UNIT",
                "PARADROP": "PARADROP",
                "AIR_SWEEP": "AIR_SWEEP",
                "NUCLEAR_STRIKE": "NUCLEAR_STRIKE",
            }[arguments["mission"]]
        )
        attach(
            value,
            kind,
            target=arguments["target"],
            destination=arguments["destination"],
            confirmation="confirmAction" if kind == "NUCLEAR_STRIKE" else None,
        )
