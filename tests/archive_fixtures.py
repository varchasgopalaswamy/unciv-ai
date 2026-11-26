"""Pure frozen-record factories for inspecting prior game archives."""

from civ_agent.game_config import EconomyModel, Limits
from civ_agent.orchestration.managed_game_contracts import ManagedGameRequest, ManagedSeat
from civ_agent.orchestration.managed_host import GameAuthority
from civ_agent.orchestration.managed_player import NativePlayerSession, NativePlayerSettings


def session(player):
    model = EconomyModel(provider="openai", model="gpt-6-luna", reasoning_effort="medium")
    return NativePlayerSession(
        game="game", player=player, settings=NativePlayerSettings(economy_model=model, military_model=model)
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
