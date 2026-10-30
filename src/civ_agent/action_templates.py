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
    controls = value.get("turnControls") or {}
    if controls.get("canDeferChoices"):
        controls["deferChoicesCommand"] = command_arguments({"kind": "END_TURN", "deferChoices": True})
    if controls.get("canMoveAutomatedUnits"):
        controls["moveAutomatedCommand"] = command_arguments({"kind": "MOVE_AUTOMATED_UNITS"})
    controls["autoplayCommands"] = [
        command_arguments({"kind": "AUTOPLAY", "selection": scope}) for scope in controls.get("autoplayScopes", [])
    ]
    for alert in value.get("alerts", []):
        if alert.get("canAcknowledge") is True and alert.get("content") is not None:
            attach(alert, "ACKNOWLEDGE_ALERT", target=alert["handle"])
        for option in (alert.get("capture") or {}).get("options", []):
            if available(option):
                attach(option, "RESOLVE_CAPTURED_CITY", target=alert["handle"], selection=option["name"])
        friendship = alert.get("friendship")
        if friendship:
            if available(friendship.get("accept")):
                attach(friendship["accept"], "RESPOND_FRIENDSHIP", target=alert["handle"], selection="Accept")
            if friendship.get("canDecline") is True:
                friendship["declineCommand"] = command_arguments(
                    {"kind": "RESPOND_FRIENDSHIP", "target": alert["handle"], "selection": "Decline"}
                )
        native = alert.get("nativeDiplomacy") or {}
        if native.get("responseCommand") == "RESPOND_CITY_STATE_ALERT":
            for option in native.get("options", []):
                if available(option):
                    attach(option, "RESPOND_CITY_STATE_ALERT", target=alert["handle"], selection=option["name"])
        elif native.get("active") and native.get("civilizationId"):
            for option in native.get("options", []):
                if available(option):
                    option["responseTool"] = {
                        "tool": "chat_send",
                        "arguments": {
                            "to": native["civilizationId"],
                            "nativeAction": {
                                "kind": "RESPOND_DIPLOMACY",
                                "target": alert["handle"],
                                "selection": option["name"],
                            },
                        },
                        "instruction": "Read chat_send's definition and this option's consequences; add your own text and optional intent.",
                    }
    if value.get("canEndTurn") is True:
        value["endTurn"] = {
            "command": command_arguments({"kind": "END_TURN"}),
            "instruction": "Review the current unit roster and pending decisions. Idle units and tactical warnings are advisory; deferReason is optional. After END_TURN succeeds, finish.",
        }
    for unit in value.get("units", []):
        add_protection_inspections(unit)
        if unit.get("worker"):
            unit.setdefault("inspections", {})["START_WORKER_JOB"] = {
                "tool": "unit_details",
                "arguments": {"target": unit["handle"], "sections": ["work"]},
            }
        if unit.get("promotions"):
            unit.setdefault("inspections", {})["PROMOTE_UNIT"] = {
                "tool": "unit_details",
                "arguments": {"target": unit["handle"], "sections": ["promotions"]},
            }
            for choice in unit["promotions"].get("choices", []):
                attach(choice, "PROMOTE_UNIT", target=unit["handle"], selection=choice["name"])
        if unit.get("escort"):
            unit.setdefault("inspections", {})["SET_UNIT_ORDER"] = {
                "tool": "unit_details",
                "arguments": {"target": unit["handle"], "sections": ["orders"]},
            }
        if unit.get("upgrades"):
            unit.setdefault("inspections", {})["UPGRADE_UNIT"] = {
                "tool": "unit_details",
                "arguments": {"target": unit["handle"], "sections": ["economy"]},
            }
        if unit.get("canFoundCity") or unit.get("settlementStatus"):
            unit.setdefault("inspections", {})["FOUND_CITY"] = {
                "tool": "unit_details",
                "arguments": {"target": unit["handle"], "sections": ["settlement"]},
            }


def add_protection_inspections(unit: JsonObject) -> None:
    for candidate in (unit.get("protection") or {}).get("escortCandidates", []):
        candidate["ordersInspection"] = {
            "tool": "unit_details",
            "arguments": {"target": candidate["unit"], "sections": ["orders"]},
        }
        if candidate["atSettler"]:
            candidate["pairingInspection"] = {
                "tool": "unit_details",
                "arguments": {"target": unit["handle"], "sections": ["orders"]},
            }
        else:
            candidate["rendezvousPreview"] = {
                "tool": "movement_preview",
                "arguments": {"target": candidate["unit"], "destination": unit["position"]},
            }


def add_unit_commands(unit: JsonObject) -> None:
    add_protection_inspections(unit)
    target = unit["handle"]
    controls = unit.get("controls") or {}
    if available(controls.get("rename")):
        controls["rename"]["commandTemplate"] = {
            "kind": "RENAME_UNIT",
            "target": target,
            "selection": "<new name; empty clears custom name>",
        }
    if available(controls.get("savePromotions")):
        attach(controls["savePromotions"], "SAVE_UNIT_PROMOTIONS", target=target)
    for swap in controls.get("swaps", []):
        attach(
            swap,
            "SWAP_UNITS",
            target=target,
            destination=swap["destination"],
            confirmation="confirmAction" if swap.get("captureRisks") else None,
        )
    if controls.get("canConnectRoad"):
        controls["roadInspection"] = {
            "tool": "player_inspect",
            "arguments": {"subject": "ROAD", "target": target},
            "requiredArgument": "destination",
        }
    if unit.get("escort"):
        unit["escortInstruction"] = (
            "ESCORT links the civilian and military unit on this tile; STOP_ESCORT separates them. "
            "While active, moving either member moves the pair using native pathfinding. "
            "A new movement command replaces both members' independent travel/automation orders. "
            "Preview the destination after pairing; afterMove.escort shows the partner's remaining movement "
            "at the shared turn stop. A formation keeps units together but can still be defeated."
        )
    add_combat_commands({"map": {"units": [unit]}})
    for move in unit.get("moves", []):
        attach(
            move,
            "MOVE_UNIT",
            target=target,
            destination=move["destination"],
            confirmation="confirmAction" if (move.get("afterMove") or {}).get("captureRisk") else None,
        )
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
    if unit.get("settlement") is not None:
        unit["settlementInstruction"] = (
            "settlement describes the current tile; movementOrder.settlement describes the active destination "
            "and refreshes with each observation. "
            "For another site, read movement_preview(target, destination) and inspect its settlement field. "
            "Movement availability does not imply founding eligibility. Waiting restores movement, not site eligibility. "
            "Explored tiles report current native site eligibility, including restrictions from undiscovered cities. "
            "UNKNOWN means the destination itself is unexplored. Recommendations include only eligible sites. "
            "Recheck canFoundCity on arrival; movement and unit requirements are separate from site eligibility. "
            "Own territory and adjacent foreign settlers "
            "do not by themselves prohibit founding."
        )
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
    gift = unit.get("gift")
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
    for option in (city.get("management") or {}).get("defaultPromotions", []):
        if available(option.get("action")):
            attach(
                option,
                "SET_DEFAULT_PROMOTIONS",
                target=target,
                selection=option["unitType"],
                enabled=not option["enabled"],
            )
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
    if data.get("unknownNames"):
        data["instruction"] = "Correct unknownNames using the suggestions or discovery read before choosing a command."
        return
    if data["detailLevel"] != "named":
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
    if name == "player_inspect":
        value["available"] = value.get("data", {}).get("available", True)
    if name == "player_inspect" and arguments.get("subject") == "ROAD" and available(value.get("data")):
        attach(value["data"], "CONNECT_ROAD", target=arguments["target"], destination=arguments["destination"])
    if name == "research_details" and arguments.get("names"):
        value["queueInstruction"] = (
            "SET_RESEARCH_QUEUE replaces the entire queue, including current research. "
            "The first technology becomes active; stored science is retained. "
            "Choose command to replace it, or appendToQueue to preserve the existing queue."
        )
        for option in value.get("technologies", []):
            if option.get("queueAvailable") and option.get("prerequisiteQueue"):
                attach(option, "SET_RESEARCH_QUEUE", queue=option["prerequisiteQueue"])
            elif available(option.get("researchNow")):
                attach(option, "SET_RESEARCH_QUEUE", queue=[option["name"]])
            if "command" in option:
                path = option["command"]["queue"]
                option["commandLabel"] = f"Replace queue; research {path[0]} now"
                # Both ordered paths come from the engine. Preserve every
                # existing entry, then add missing prerequisites and the target.
                queue = value.get("researchQueue", [])
                appended = list(dict.fromkeys([*queue, *path]))
                if queue and appended != queue and appended != path:
                    option["appendToQueue"] = {"label": "Append after the existing queue; preserve current research"}
                    attach(option["appendToQueue"], "SET_RESEARCH_QUEUE", queue=appended)
                if option["name"] in queue:
                    option["alreadyQueued"] = True
            if available(option.get("freeChoice")):
                attach(option["freeChoice"], "CHOOSE_FREE_TECHNOLOGY", selection=option["name"])
    elif name == "policy_details" and arguments.get("names"):
        for option in value.get("policies", []):
            if available(option.get("adoption")):
                attach(option, "ADOPT_POLICY", selection=option["name"])
    elif name in {"movement_preview", "mission_preview"} and available(value):
        stops = value.get("turns", [])
        capture_risk = bool(stops and (stops[0].get("afterMove") or {}).get("captureRisk"))
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
            confirmation="confirmAction" if kind == "NUCLEAR_STRIKE" or capture_risk else None,
        )


def research_change(previous: JsonObject | None, current: JsonObject) -> JsonObject:
    """Report actual visible progress from the refresh already required by actions."""
    research = current.get("research") or {}
    active = research.get("current")
    if active is None:
        active = next(iter(current.get("researchQueue", [])), None)
    result: JsonObject = {"current": active}
    before = (previous or {}).get("research") or {}
    prior = before.get("current") or next(iter((previous or {}).get("researchQueue", [])), None)
    if prior and prior != active:
        progress = next((tech for tech in research.get("technologies", []) if tech["name"] == prior), None)
        if progress is not None and not progress.get("researched"):
            result["paused"] = {"technology": prior, "scienceStored": progress["scienceStored"]}
    return result
