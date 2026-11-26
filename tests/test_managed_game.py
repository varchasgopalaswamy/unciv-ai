"""Managed roots follow native turns and retain absolute budgets across live pause."""

import asyncio
import copy
from datetime import timedelta

import pytest

from civ_agent.game_config import EconomyModel, Limits
from civ_agent.operator_connection import OperatorError
from civ_agent.orchestration.managed_game_contracts import (
    ManagedGameContinuation,
    ManagedGameRequest,
    ManagedSeat,
)
from civ_agent.orchestration.managed_host import (
    GameAuthority,
    ManagedHostStatus,
)
from civ_agent.orchestration.managed_player import NativePlayerSession, NativePlayerSettings


def session(player):
    model = EconomyModel(provider="openai", model="gpt-6-luna", reasoning_effort="medium")
    return NativePlayerSession(
        game="game",
        player=player,
        settings=NativePlayerSettings(economy_model=model, military_model=model),
    )


def request(**limits):
    return ManagedGameRequest(
        authority=GameAuthority(
            game="game",
            epoch="e1",
            lease_id="shared-lease",
            limits=Limits(**{"max_seconds": 60, "max_invocations": 50, **limits}),
        ),
        seats=[
            ManagedSeat(civilization="Rome", session=session("p0")),
            ManagedSeat(civilization="Greece", session=session("p1")),
        ],
        poll_seconds=0.05,
    )


class Operator:
    def __init__(self, *, finish=True, auto_finish=True):
        self.calls, self.events = [], []
        self.finish, self.auto_finish = finish, auto_finish
        self.status = {
            "epoch": "e1",
            "serverInstance": "server1",
            "phase": "PAUSED",
            "turn": 10,
            "handoff": 0,
            "activeCivilization": "Rome",
            "activeController": "AGENT",
            "scheduled": True,
            "leaseId": None,
            "leaseExpiresInMs": 0,
            "modelInvocations": 0,
            "modelUsage": {},
        }
        self.positions = [
            ("Rome", "AGENT", 10),
            ("Greece", "AGENT", 10),
            ("Human", "HUMAN", 10),
            ("Egypt", "BUILT_IN_AI", 10),
            ("Rome", "AGENT", 11),
        ]
        self.position = 0
        self.human_active = False
        self.wait_polls = 0
        self.forbidden = False
        self.lose_limits_reply: int | None = None
        self.lost_limit_values = []
        self.turn_started = asyncio.Event()

    def advance(self):
        self.status["handoff"] += 1
        if self.position + 1 == len(self.positions):
            self.status["phase"] = "FINISHED" if self.finish else "PAUSED"
            return
        self.position += 1
        civilization, controller, turn = self.positions[self.position]
        self.status.update(activeCivilization=civilization, activeController=controller, turn=turn, phase="PAUSED")
        self.human_active, self.wait_polls = False, 0

    def call(self, operation, **fields):
        assert not self.forbidden, "Replay cannot touch host authority"
        self.calls.append((operation, copy.deepcopy(fields)))
        if operation == "acquire":
            assert self.status["leaseId"] is None
            self.status.update(leaseId=fields["leaseId"], leaseExpiresInMs=60000)
        elif operation != "status":
            assert fields["leaseId"] == "shared-lease" == self.status["leaseId"]
            if operation == "pause":
                self.status["phase"] = "PAUSED"
            elif operation == "job_limits" and fields["maxInvocations"] == self.lose_limits_reply:
                self.lost_limit_values.append(fields["maxInvocations"])
                self.lose_limits_reply = None
                raise OperatorError("Synthetic lost limits acknowledgement", "TRANSPORT_TIMEOUT")
            elif operation == "activate":
                self.human_active = True
                self.status["phase"] = "ACTIVE"
            elif operation == "release":
                self.status["leaseId"] = None
            elif operation == "heartbeat":
                if (self.status["activeController"] == "HUMAN" and self.human_active) or self.status[
                    "activeController"
                ] == "BUILT_IN_AI":
                    self.wait_polls += 1
                    if self.wait_polls >= 3:
                        self.advance()
        return {"status": copy.deepcopy(self.status), "playerToken": "private-human-activation-token"}


async def histories(client, identifier, first_run):
    runs = []
    current = first_run
    while current:
        history = await client.get_workflow_handle(identifier, run_id=current).fetch_history()
        runs.append(history)
        continued = [
            event for event in history.events if event.HasField("workflow_execution_continued_as_new_event_attributes")
        ]
        current = (
            continued[-1].workflow_execution_continued_as_new_event_attributes.new_execution_run_id
            if continued
            else None
        )
    return runs


def test_continuation_rejects_deadline_reset_foreign_seats_and_missing_live_child_identity():
    from datetime import UTC, datetime

    from pydantic import ValidationError

    configured = request()
    now = datetime.now(UTC)
    status = Operator().status
    continuation = ManagedGameContinuation(
        initial_started_at=now,
        deadline=now + timedelta(seconds=60),
        initial_turn=10,
        initial_handoff=0,
        host=ManagedHostStatus.model_validate(status),
        model_invocations=0,
    )
    assert ManagedGameRequest.model_validate({**configured.model_dump(), "continuation": continuation})
    for altered in (
        continuation.model_copy(update={"deadline": now + timedelta(seconds=120)}),
        continuation.model_copy(update={"active_turn_id": "child-without-input"}),
    ):
        with pytest.raises(ValidationError):
            ManagedGameRequest.model_validate_json(
                configured.model_copy(update={"continuation": altered}).model_dump_json()
            )
