import asyncio
import copy

import pytest
from jsonschema import Draft202012Validator
from pydantic import ValidationError

from civ_agent.civilization_state import CivilizationState
from civ_agent.game_commands import command_arguments, command_schema
from civ_agent.observations import TurnOverview
from civ_agent.proposal_gateway import ProposalGateway
from civ_agent.service_io import ServiceIO
from tests.domain_fixtures import Host


def notice(handle, text):
    return {
        "handle": handle,
        "text": text,
        "category": "General",
        "icons": ["StatIcons/Gold"],
        "locations": [{"x": 0, "y": 7}],
        "canDismiss": True,
    }


def test_notifications_are_delivered_in_full_and_remain_evidence_after_dismissal(tmp_path):
    async def run():
        memory, io, host = CivilizationState(tmp_path / "private", "Rome"), ServiceIO(), Host()
        gateway = ProposalGateway(memory, io, tmp_path / "workspace")
        notifications = [notice("notice1", "A detailed message\n" * 100), notice("notice2", "Economy changed")]
        host.view["notifications"] = copy.deepcopy(notifications)
        original = host.tool

        async def tool(name, args):
            result = await original(name, args)
            if name == "player_command" and args["kind"] == "DISMISS_NOTIFICATIONS":
                result["dismissedNotifications"] = [
                    copy.deepcopy(n) for n in host.view["notifications"] if n["handle"] in args["targets"]
                ]
                host.view["notifications"] = [
                    n for n in host.view["notifications"] if n["handle"] not in args["targets"]
                ]
            return result

        host.tool = tool
        job = await gateway.open("notification-test", "gameplay", "g1", host, {}, 0)
        try:
            arguments = {"kind": "DISMISS_NOTIFICATIONS", "targets": ["notice1"]}
            rejected = await gateway.call(job, "player_command", arguments, "unread")
            assert rejected["error"] == "OBSERVATION_REQUIRED" and not rejected["applied"]
            assert rejected["recovery"]["reads"] == [{"tool": "player_turn", "arguments": {}}]
            assert not host.executed
            shown = await gateway.call(job, "player_turn", {}, "read")
            TurnOverview.model_validate(shown)
            assert shown["notifications"] == notifications
            invalid = await gateway.call(
                job, "player_command", {**arguments, "targets": ["notice1", "notice1"]}, "duplicate"
            )
            assert invalid["error"] == "INVALID_COMMAND_ARGUMENTS" and not host.executed
            # Updating an assessment is an agent choice, supported by the exact delivered evidence.
            update = await gateway.call(
                job,
                "memory_update",
                {
                    "changes": [
                        {
                            "op": "create",
                            "content": {
                                "type": "belief",
                                "claim": "The economic change may require revising purchases.",
                                "uncertainty": "high",
                                "sources": ["observation:overview"],
                            },
                        }
                    ]
                },
                "assessment",
            )
            assert update["changes"][0]["accepted"]
            receipt = await gateway.call(job, "player_command", arguments, "dismiss")
            assert receipt["dismissedNotifications"] == notifications[:1]
            assert await gateway.call(job, "player_command", arguments, "dismiss") == receipt
            assert len(host.executed) == 1
            assert host.executed[0]["revision"] == "rev1"
            current = await gateway.call(job, "player_turn", {}, "refresh")
            assert current["notifications"] == notifications[1:]
            await gateway.close(job, True)
            restarted = CivilizationState(tmp_path / "private", "Rome")
            action = restarted.summary(0)["actions"][0]
            assert action["action"] == "Dismissed 1 ordinary notifications."
            assert action["receipt"]["dismissedNotifications"] == notifications[:1]
        finally:
            await io.close()

    asyncio.run(run())


@pytest.mark.parametrize("targets", [[], ["notice1", "notice1"], [""], [" "], ["n" * 129], [1]])
def test_notification_batch_schema_rejects_invalid_targets(targets):
    arguments = {"kind": "DISMISS_NOTIFICATIONS", "targets": targets}
    if targets != ["notice1", "notice1"]:
        assert not Draft202012Validator(command_schema()).is_valid(arguments)
    with pytest.raises(ValidationError):
        command_arguments(arguments)
