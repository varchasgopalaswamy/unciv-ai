"""Activity payloads separated from worker implementations for sandbox imports."""

from civ_agent.orchestration.contracts import ReviewRequest, Transcript
from civ_agent.protocol import JsonObject
from civ_agent.schemas import BoundaryModel
from civ_agent.strategic_review import StrategicAdvice


class PreparedReview(BoundaryModel):
    instructions: str
    user: str


class FinishReview(BoundaryModel):
    request: ReviewRequest
    report: StrategicAdvice | None
    reads: list[JsonObject]
    history: Transcript
    usage: JsonObject


class CheckReview(BoundaryModel):
    request: ReviewRequest
    report: StrategicAdvice
    reads: list[JsonObject]


class ReviewValidation(BoundaryModel):
    accepted: bool
    feedback: str = ""
