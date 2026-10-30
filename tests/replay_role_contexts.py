"""Replay frozen, permitted current-game contexts through a selected source tree.

Run this module once per frozen source using PYTHONPATH. It makes no native or
model calls. Captures contain private notebook material and stay outside Git.
"""

import argparse
import asyncio
import copy
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from jsonschema import Draft202012Validator
from sqlalchemy import insert

from civ_agent import memory_schema as tables
from civ_agent.civilization_state import CivilizationState
from civ_agent.economy_roles import TurnRoles
from civ_agent.game_commands import command_arguments
from civ_agent.memory_store import RecordStore, encode
from civ_agent.observations import overview
from civ_agent.proposal_gateway import ProposalGateway, public_result
from civ_agent.service_io import ServiceIO
from civ_agent.startup_context import prepare_startup, startup_prompt
from tests.test_agent_service import StartupHost


class ReplayHost(StartupHost):
    def __init__(self, capture, service):
        super().__init__(SimpleNamespace(service=service))
        self.capture = capture
        self.view = copy.deepcopy(capture["observation"])
        self.revision = self.view["revision"]

    async def tool(self, name, args):
        key = {"player_capabilities": "capabilities", "victory_progress": "victory"}.get(name)
        if key:
            return copy.deepcopy(self.capture[key] or {})
        if name == "player_communication" and args.get("op") == "state":
            return copy.deepcopy(self.capture["social"])
        return await super().tool(name, args)


def audit_startup(prepared, catalogue, job, roles):
    schemas = {tool["name"]: tool.get("outputSchema") for tool in catalogue}
    errors, ownership, commands = [], [], []
    for name, value in prepared["reads"].items():
        if schema := schemas.get(name):
            errors.extend(
                {
                    "tool": name,
                    "path": list(error.absolute_path),
                    "schemaPath": list(error.absolute_schema_path),
                    "validator": error.validator,
                }
                for error in Draft202012Validator(schema).iter_errors(value)
            )

    def visit(value):
        if isinstance(value, list):
            for row in value:
                visit(row)
        elif isinstance(value, dict):
            for key, child in value.items():
                if isinstance(child, dict) and isinstance(child.get("kind"), str) and "command" in key.lower():
                    commands.append(child)
                    if label := value.get(key + "Access"):
                        problem = roles.ownership_problem(job, child)
                        if (label["mode"] == "direct") != (problem is None):
                            ownership.append({"command": child, "label": label, "authority": problem})
                else:
                    visit(child)

    visit(prepared["reads"])
    visit(prepared["decisionDetails"])
    for command in commands:
        try:
            command_arguments(command)
        except ValueError as error:
            errors.append({"command": command, "message": str(error)})

    def facts(value):
        if isinstance(value, list):
            return [facts(row) for row in value]
        if isinstance(value, dict):
            return {key: facts(child) for key, child in value.items() if not key.endswith("Access")}
        return value

    complete, delivered = public_result(overview(job.observation)), prepared["reads"]["player_turn"]
    return {
        "commandOffersChecked": len(commands),
        "schemaErrors": errors,
        "ownershipLabelErrors": ownership,
        "mandatoryInformationChanges": [
            key
            for key in ("pending", "alerts", "notifications", "combatReports", "rules", "economy", "outputSummary")
            if key in complete and facts(delivered.get(key)) != facts(complete[key])
        ],
    }


async def replay(capture, root, role):
    raw, civ = capture["observation"], capture["case"]["civilization"]
    memory, io = CivilizationState(root / "memory", civ), ServiceIO()
    epoch, turn = raw["epoch"], raw["turn"]
    memory.bind(epoch)
    with memory.transaction() as db:
        for key, record in capture["records"].items():
            RecordStore(db).write(key, record["category"], record["body"], None)
        memory._set(db, "war:state", capture["warState"])
        advice = capture.get("review")
        if advice:
            db.execute(
                insert(tables.reviews).values(
                    job="captured-review",
                    turn=advice["asOfTurn"],
                    status="COMPLETED",
                    attempts=1,
                    advice=encode({key: value for key, value in advice.items() if key != "asOfTurn"}),
                    settings="{}",
                    timeout=60,
                )
            )
    record = {"epoch": epoch, "gameTurn": turn, "activationId": "replay", "settings": {}}
    service = SimpleNamespace(
        memory=memory, io=io, records={"replay": record}, active_id="replay", _save=AsyncMock(), state_dir=root
    )
    host = ReplayHost(capture, service)
    gateway = ProposalGateway(memory, io, root / "workspace")
    service.gateway = gateway
    roles = TurnRoles(service, record)
    gateway.role_coordinator = roles
    roles.war_state = copy.deepcopy(capture["warState"])
    coordination = capture["coordination"]
    roles.requests = copy.deepcopy(coordination.get("requests", []))
    roles.overrides = copy.deepcopy(coordination.get("overrides", {}))
    roles.economy_status = copy.deepcopy(coordination.get("economySchedule", {}))
    if not roles.economy_status.get("cities"):
        roles.economy_status.update(fullReview=True, cities=[city["handle"] for city in raw["cities"]])
    roles.military_status = copy.deepcopy(coordination.get("militarySchedule", {}))
    try:
        job = await gateway.open("replay", role, epoch, host, {}, turn)
        roles.register(job)
        prepared = await prepare_startup(gateway, job)
        prompt = startup_prompt(
            prepared, role, "Play this turn", strategy_guide=True, civilization_guide=True, civilization=civ
        )
        audit = audit_startup(prepared, await gateway.tools(job), job, roles)
        gateway.commit_startup_coordination(job)
        reads = []
        for tool, arguments in [
            ("player_turn", {}),
            ("city_details", {"target": raw["cities"][0]["handle"], "sections": ["status"]}),
            ("player_turn", {"view": "full"}),
        ]:
            reply = await gateway.call(job, tool, arguments, f"read-{len(reads)}")
            reads.append(
                {"tool": tool, "arguments": arguments, "characters": len(encode(reply)), "error": reply.get("error")}
            )
        return {
            "case": capture["case"]["id"],
            "role": role,
            "capturedTurn": turn,
            "audit": audit,
            **prompt.metadata(),
            "subsequentReads": reads,
            "omitted": (
                prepared["reads"]["player_turn"].get("roleBrief")
                or prepared["reads"]["player_turn"].get("economyBrief")
                or {}
            ).get("omitted", []),
            "selectedMemory": job.memory["contextIds"],
            "pendingWork": json.loads(prepared["memoryContext"].split("\n", 1)[1]),
        }
    finally:
        await io.close()
        memory.engine.dispose()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--captures", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    results = []
    for path in sorted(args.captures.glob("*.json")):
        capture = json.loads(path.read_text())
        for role in ("gameplay", "economy", "military"):
            result = asyncio.run(replay(capture, args.output / path.stem / role, role))
            # Exact notebook records are already retained in the private capture.
            result["pendingWork"].pop("records", None)
            result["pendingWork"].pop("memoryDelivery", None)
            results.append(result)
            print(path.stem, role, result["totalPromptCharacters"], flush=True)
    (args.output / "results.json").write_text(json.dumps(results, indent=2) + "\n")


if __name__ == "__main__":
    main()
