"""Required semantic inputs for the opt-in investment workflow, not game rules."""

from typing import Annotated, Literal

from pydantic import Field, model_validator

from civ_agent.memory_records import (
    ChosenDecision,
    DecisionFollowThrough,
    InvestmentAlternative,
    NativeInvestmentQuote,
    ShortThought,
    Source,
    Sources,
)
from civ_agent.schemas import BoundaryModel

InvestmentQuote = NativeInvestmentQuote


class InvestmentComparison(BoundaryModel):
    kind: Literal["production", "purchase", "wait", "other"]
    option: ShortThought
    investment: ShortThought
    expectedOutcome: ShortThought
    timing: ShortThought
    uncertainty: ShortThought
    quote: InvestmentQuote | None = None


class InvestmentChoice(BoundaryModel):
    id: Annotated[str, Field(min_length=1, max_length=40, pattern=r"^[a-zA-Z0-9_-]+$")]
    objective: ShortThought
    investment: ShortThought
    status: Literal["active", "waiting", "completed", "abandoned"]
    ownerRole: Literal["gameplay", "economy", "military", "communication"]
    nextStep: ShortThought
    expectedObservations: Annotated[list[ShortThought], Field(min_length=1)]
    reviewAfterTurns: Annotated[int, Field(ge=1, le=100)]
    reconsiderWhen: ShortThought
    comparison: Annotated[list[InvestmentComparison], Field(min_length=2, max_length=4)]
    subjects: Annotated[list[Source], Field(max_length=8)] = Field(default_factory=list)
    blockedBy: Annotated[list[ShortThought], Field(max_length=3)] = Field(default_factory=list)
    routeAssumptions: Annotated[list[ShortThought], Field(max_length=3)] = Field(default_factory=list)
    sources: Sources = Field(default_factory=list)

    @model_validator(mode="after")
    def comparable_choices(self):
        kinds = [item.kind for item in self.comparison]
        labels = [item.option.strip().casefold() for item in self.comparison]
        if "wait" not in kinds:
            raise ValueError("Include a waiting alternative in the comparison")
        if not all(labels) or len(labels) != len(set(labels)):
            raise ValueError("Give each alternative a distinct nonempty option label; categories may repeat")
        for item in self.comparison:
            if item.kind in {"production", "purchase"} and item.quote is None:
                raise ValueError(
                    "A production/purchase comparison needs a native quote or an explicit unknown/unavailable reason"
                )
        return self

    def decision(self) -> ChosenDecision:
        return ChosenDecision(
            id=self.id,
            objective=self.objective,
            investment=self.investment,
            expectedObservations=self.expectedObservations,
            reviewAfterTurns=self.reviewAfterTurns,
            reconsiderWhen=self.reconsiderWhen,
            subjects=self.subjects,
            status=self.status,
            followThrough=DecisionFollowThrough(
                ownerRole=self.ownerRole,
                nextStep=self.nextStep,
                blockedBy=self.blockedBy,
                routeAssumptions=self.routeAssumptions,
                reassessWhen=[self.reconsiderWhen],
                alternatives=[
                    InvestmentAlternative(
                        kind=item.kind,
                        option=item.option,
                        investment=item.investment,
                        expectedOutcome=item.expectedOutcome,
                        timing=item.timing,
                        uncertainty=item.uncertainty,
                        waiting=item.kind == "wait",
                        nativeQuote=item.quote,
                    )
                    for item in self.comparison
                ],
            ),
        )


class InvestmentCheckpoint(BoundaryModel):
    decisionId: Annotated[str, Field(min_length=1, max_length=40)]
    status: Literal["active", "waiting", "completed", "abandoned"]
    nextStep: ShortThought
    assessment: ShortThought
    receiptRefs: Annotated[list[Source], Field(max_length=8)] = Field(default_factory=list)
