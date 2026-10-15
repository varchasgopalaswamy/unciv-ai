import asyncio
import copy
from pathlib import Path
from types import SimpleNamespace

from civ_agent import experiment_metrics
from civ_agent.civilization_state import CivilizationState
from civ_agent.game_config import GameConfig, load_config
from civ_agent.memory_store import RecordStore
from civ_agent.proposal_gateway import ProposalGateway
from civ_agent.service_io import ServiceIO
from civ_agent.startup_context import prepare_startup
from tests.test_agent_service import StartupHost


def test_matched_variants_and_legacy_defaults():
    assert not GameConfig().agents.native_advice_enabled
    assert not GameConfig().agents.decisions_enabled
    root = Path(__file__).resolve().parents[1] / "examples/native-routing"
    baseline = None
    for name, guide, routing in (
        ("baseline", False, False),
        ("guide-only", True, False),
        ("decisions-only", False, True),
        ("combined", True, True),
    ):
        config = load_config(root / (name + ".toml")).resolved().model_dump()
        assert config["agents"].pop("native_advice_enabled") == guide
        assert config["agents"].pop("decisions_enabled") == routing
        if baseline is None:
            baseline = config
        assert config == baseline


def test_startup_eight_filtered_refresh_failure_and_noncausal_metrics(tmp_path):
    class Host(StartupHost):
        def __init__(self):
            super().__init__(
                SimpleNamespace(
                    service=SimpleNamespace(active_id="job", records={"job": {"epoch": "g1", "gameTurn": 0}})
                )
            )
            self.advice_calls = []
            self.fail = False

        async def tool(self, name, args):
            if name == "native_advice":
                self.advice_calls.append(copy.deepcopy(args))
                if self.fail:
                    raise TimeoutError()
                recommendations = [
                    {
                        "id": f"n{i}",
                        "subject": "city1",
                        "command": {
                            "tool": "player_command",
                            "arguments": {"kind": "QUEUE_CONSTRUCTION", "target": "city1", "selection": f"Building{i}"},
                        },
                    }
                    for i in range(12)
                ]
                return {
                    "epoch": args["epoch"],
                    "revision": args["revision"],
                    "turn": 0,
                    "available": True,
                    "recommendations": recommendations,
                }
            return await super().tool(name, args)

    async def run():
        io = ServiceIO()
        memory = CivilizationState(tmp_path / "memory", "Rome")
        gateway = ProposalGateway(memory, io, tmp_path / "workspace")
        host = Host()
        try:
            gateway.native_advice_enabled = True
            job = await gateway.open("job", "gameplay", "g1", host, {}, 0)
            prepared = await prepare_startup(gateway, job)
            advice = prepared["reads"]["native_advice"]
            assert job.observation is not None
            assert len(advice["recommendations"]) == 8 and advice["remainingRecommendations"] == 4
            assert advice["epoch"] == "g1" and advice["revision"] == job.observation["revision"]
            revision = job.observation["revision"]
            arguments = {"kind": "QUEUE_CONSTRUCTION", "target": "city1", "selection": "Building0"}
            experiment_metrics.action_match(memory, job, "action", arguments, {"kind": "QUEUE_CONSTRUCTION"}, revision)
            experiment_metrics.action_match(memory, job, "action", arguments, {"kind": "QUEUE_CONSTRUCTION"}, revision)
            experiment_metrics.action_match(memory, job, "stale", arguments, {"kind": "QUEUE_CONSTRUCTION"}, "stale")
            counts = experiment_metrics.summarize(memory)["reachableOutcomes"]
            assert (
                counts["recommendations_exposed"] == 8
                and counts["action_match"] == counts["actions_matching_advice"] == 1
            )
            fresh = await gateway.call(job, "native_advice", {"cities": ["city1"]}, "refresh")
            assert len(fresh["recommendations"]) == 12 and host.advice_calls[-1]["cities"] == ["city1"]
            host.fail = True
            failed = await gateway.call(job, "native_advice", {}, "failure")
            assert failed["available"] is False and failed["recommendations"] == []
            assert experiment_metrics.summarize(memory)["reachableOutcomes"]["advice_unavailable"] == 1
            with memory.transaction() as db:
                matches = [
                    r["body"]
                    for r in RecordStore(db).all(categories={"guidance"}).values()
                    if r["body"]["type"] == "action_match"
                ]
                assert "not evidence" in matches[0]["interpretation"]
        finally:
            await io.close()

    asyncio.run(run())


def test_tools_are_independently_enabled_and_never_in_communication_catalogue():
    for guide in (False, True):
        for routing in (False, True):
            catalogue = ProposalGateway.tool_catalogue(
                [], "gameplay", routing_enabled=routing, native_advice_enabled=guide
            )
            names = {tool["name"] for tool in catalogue}
            assert ("native_advice" in names) == guide
            assert ("chat_delivery" in names) == routing
            communication = ProposalGateway.tool_catalogue(
                [], "communication", routing_enabled=routing, native_advice_enabled=guide
            )
            assert "native_advice" not in {tool["name"] for tool in communication}
