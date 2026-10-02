import asyncio
import copy

import pytest
from jsonschema import Draft202012Validator

from civ_agent.civilization_state import CivilizationState
from civ_agent.proposal_gateway import ProposalGateway
from civ_agent.service_io import ServiceIO
from tests.test_proposal_gateway import Host


@pytest.mark.parametrize("purpose", ["gameplay", "communication"])
def test_prepare_exposes_exact_publishable_terms_without_publishing(tmp_path, purpose):
    async def run():
        memory = CivilizationState(tmp_path / "private", "Rome")
        io = ServiceIO()
        gateway = ProposalGateway(memory, io, tmp_path / "workspace")
        requests = []
        treaty = {"type": "Treaty", "name": "Research Agreement", "amount": 200, "duration": 25}
        payment = {"type": "Gold", "name": "Gold", "amount": 200, "duration": -1}
        prepared = {"partner": "Greece", "give": [treaty, payment], "receive": [treaty], "valid": True}

        class DraftHost(Host):
            async def tool(self, name, args):
                requests.append((name, copy.deepcopy(args)))
                if name == "player_communication" and args["op"] == "trade_prepare":
                    return copy.deepcopy(prepared)
                return await super().tool(name, args)

        try:
            job = await gateway.open("draft", purpose, "g1", DraftHost(), {})
            catalogue = {tool["name"]: tool for tool in await gateway.tools(job)}
            assert catalogue["trade_prepare"]["annotations"]["readOnlyHint"]
            requests.clear()
            arguments = {"partner": "Greece", "give": [treaty], "receive": [], "fundResearch": True}
            result = await gateway.call(job, "trade_prepare", arguments, "prepare")
            assert result["give"] == prepared["give"] and result["receive"] == prepared["receive"]
            assert result["publish"] == {
                "tool": "trade_offer",
                "arguments": {key: prepared[key] for key in ("partner", "give", "receive")},
            }
            Draft202012Validator(catalogue["trade_offer"]["inputSchema"]).validate(
                {**result["publish"]["arguments"], "intent": {"reason": "Fund our research agreement"}}
            )
            assert requests == [
                ("player_communication", {"op": "state", "epoch": "g1"}),
                ("player_communication", {**arguments, "op": "trade_prepare", "epoch": "g1"}),
            ]
            assert arguments["give"] == [treaty] and arguments["receive"] == []
        finally:
            await io.close()
            memory.engine.dispose()

    asyncio.run(run())
