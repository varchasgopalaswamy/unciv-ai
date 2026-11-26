"""Separate local resource ownership from a restored game's logical identity."""

from typing import Annotated, Literal

from pydantic import Field

from civ_agent.schemas import BoundaryModel

type ContainerId = Annotated[str, Field(pattern=r"^[a-f0-9]{10}$")]


class DeploymentIdentity(BoundaryModel):
    version: Literal[1] = 1
    id: ContainerId
    game: ContainerId


def deployment_identity(record) -> DeploymentIdentity:
    """Validate every resource binding before cleanup, launch or configuration writes."""
    logical = DeploymentIdentity(id=record["id"], game=record["id"])
    deployment = (
        DeploymentIdentity.model_validate(record["deployment"]) if record.get("deployment") is not None else logical
    )
    if deployment.game != logical.game:
        raise ValueError("Physical deployment belongs to a different logical game")
    native = record.get("native")
    if (
        record.get("format", 1) == 1
        and native is not None
        and (not isinstance(native, dict) or native.get("postgresVolume") != f"civ-{deployment.id}-postgres")
    ):
        raise ValueError("Native PostgreSQL volume differs from its physical deployment identity")
    return deployment


LOCAL_RUNTIME = "pydantic-local-v1"


def require_local_runtime(record) -> DeploymentIdentity:
    """Old deployments remain inspectable, but cannot enter the new executor."""
    if not isinstance(record, dict) or not isinstance(record.get("id"), str):
        raise ValueError("A saved game manifest is required")
    identity = deployment_identity(record)
    if record.get("format") != 2 or record.get("runtime") != LOCAL_RUNTIME or record.get("native") is not None:
        raise ValueError(
            "This saved game uses the retired Temporal runtime and is a read-only archive; start a fresh game"
        )
    return identity
