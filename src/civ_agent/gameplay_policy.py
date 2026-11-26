"""Frozen opt-in gameplay architecture; omitted settings preserve legacy sessions."""

import hashlib
from typing import Literal

from pydantic import ConfigDict

from civ_agent.schemas import BoundaryModel


class GameplayPolicy(BoundaryModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    version: Literal[0, 1] = 1
    architecture: Literal["repaired_roles"] = "repaired_roles"

    @property
    def fingerprint(self):
        return hashlib.sha256(self.model_dump_json().encode()).hexdigest()


def legacy_gameplay_policy():
    return GameplayPolicy(version=0)


def coordinator_gameplay_policy(coordinator):
    session = getattr(coordinator, "managed_session", None)
    return (
        getattr(session.settings, "gameplay_policy", legacy_gameplay_policy()) if session else legacy_gameplay_policy()
    )


OPPORTUNITY_GUIDANCE = (
    "Assess scouting and information gained, escorts and civilian route exposure, settlement progress, "
    "reachable worker improvements, production intent versus its native queue, and opportunities that expire. "
    "Zero mandatory military targets does not mean there is no useful scouting or protection to consider. "
    "Use recentProgress/action history to notice repeated moves without progress across turns. Compare waiting "
    "with acting, including costs, uncertainty and timing. Deliberate holds, risks, deferred native-permitted "
    "choices and wonders are valid strategic judgments. No fixed build order or compulsory movement applies. "
    "Read receipts before claiming effects; partial or unmet outcomes require reconsideration, never automatic "
    "undo or replay. Social promises are stated intent and advisory context; interpret ambiguity yourself."
)
