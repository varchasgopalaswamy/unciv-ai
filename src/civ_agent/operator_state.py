"""Read local-controller publications and retained game archives without services."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from civ_agent.container_identity import LOCAL_RUNTIME
from civ_agent.container_support import read_json
from civ_agent.game_config import ResolvedGameConfig
from civ_agent.orchestration.managed_player import NativePlayerSession
from civ_agent.runtime.state import ControllerState
from civ_agent.tls import regular_file

if TYPE_CHECKING:
    from civ_agent.orchestration.managed_game_contracts import ManagedGameResult, ManagedGameStatus
    from civ_agent.orchestration.native_status import HostGameResult


@dataclass(frozen=True)
class OperatorAuthority:
    game: str
    epoch: str


@dataclass(frozen=True)
class OperatorSeat:
    civilization: str
    session: NativePlayerSession


@dataclass(frozen=True)
class OperatorGame:
    authority: OperatorAuthority
    seats: list[OperatorSeat]


@dataclass(frozen=True)
class OperatorProjection:
    status: ControllerState | ManagedGameStatus | None
    completion: ControllerState | ManagedGameResult | None
    result: HostGameResult | None
    result_updated_at: float | None


def is_local(root: Path) -> bool:
    record = read_json(root / "container.json", {})
    return record.get("format") == 2 and record.get("runtime") == LOCAL_RUNTIME


def local_state(root: Path) -> ControllerState | None:
    path = root / "native-runtime/controller.json"
    if not path.exists() and not path.is_symlink():
        return None
    state = ControllerState.model_validate_json(regular_file(path, private=True).read_bytes())
    record = read_json(root / "container.json", {})
    if state.game != record.get("id"):
        raise ValueError("LOCAL_OPERATOR_GAME_CONFLICT")
    return state


def published_game(root: Path):
    if not is_local(root):
        from civ_agent.orchestration.managed_epochs import published_managed_epoch

        return published_managed_epoch(root)
    state = local_state(root)
    if state is None:
        raise ValueError("LOCAL_OPERATOR_STATE_PENDING")
    seats = [
        OperatorSeat(civilization=seat["civilization"], session=NativePlayerSession.model_validate(seat["session"]))
        for seat in state.seats
    ]
    if any((seat.session.game, seat.session.player) != (state.game, f"p{index}") for index, seat in enumerate(seats)):
        raise ValueError("LOCAL_OPERATOR_PLAYER_CONFLICT")
    config = ResolvedGameConfig.model_validate(read_json(root / "resolved.json"))
    if [seat.civilization for seat in seats] != [player.civilization for player in config.agent_players]:
        raise ValueError("LOCAL_OPERATOR_ROSTER_CONFLICT")
    return OperatorGame(OperatorAuthority(state.game, state.epoch), seats)


def publication_basis(root: Path):
    if not is_local(root):
        from civ_agent.orchestration.managed_epochs import epoch_publication_basis

        return epoch_publication_basis(root)
    state = local_state(root)
    return (state.game, state.epoch, state.config_sha256) if state else None


def projection(root: Path) -> OperatorProjection:
    from civ_agent.orchestration.native_status import host_result, native_projection

    if not is_local(root):
        retained = native_projection(root)
        return OperatorProjection(
            retained.workflow_status, retained.workflow_result, retained.result, retained.result_updated_at
        )
    state = local_state(root)
    host, updated = host_result(root, epoch=state.epoch if state else None)
    return OperatorProjection(state, state if state and state.mode == "FINISHED" else None, host, updated)
