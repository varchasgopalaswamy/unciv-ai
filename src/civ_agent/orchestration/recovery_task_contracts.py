"""Small recovery references shared by workflows and private inventory services."""

from typing import Literal

from civ_agent.orchestration.contracts import Digest, Scope
from civ_agent.schemas import BoundaryModel


class RecoveryTaskInventoryReference(BoundaryModel):
    version: Literal[1] = 1
    scope: Scope
    request_sha256: Digest
    inventory_sha256: Digest
    sha256: Digest
