"""Versioned execution choices retained with authorized work, never inferred on resume."""

import hashlib
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class PolicyModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, validate_default=True)


class FocusWindowPolicy(PolicyModel):
    """Bound a short programmatic read/check/write turn, never model inference."""

    enabled: bool = False
    max_seconds: Annotated[float, Field(gt=0, le=30, allow_inf_nan=False)] = 10
    max_calls: Annotated[int, Field(ge=1, le=12)] = Field(
        default=6,
        description="Underlying gateway operations in the window; a batch counts its reads/commands and local pure helpers do not count.",
    )
    queue_timeout: Annotated[float, Field(gt=0, le=300, allow_inf_nan=False)] = 30


class CodeModePolicy(PolicyModel):
    enabled: bool = False
    max_tool_calls: Annotated[int, Field(ge=1, le=100)] = 100
    execution_timeout: Annotated[float, Field(gt=0, le=30, allow_inf_nan=False)] = 30
    memory_bytes: Annotated[int, Field(ge=1, le=268435456)] = 268435456
    max_suspensions: Annotated[int, Field(ge=1, le=1000)] = 1000
    focused_windows: FocusWindowPolicy = Field(
        default_factory=FocusWindowPolicy,
        exclude_if=lambda value: value == FocusWindowPolicy(),
    )
    delivered_context: bool = Field(default=False, exclude_if=lambda value: not value)
    procedure_examples: bool = Field(default=False, exclude_if=lambda value: not value)
    typed_selectors: bool = Field(default=False, exclude_if=lambda value: not value)


class OutputPolicy(PolicyModel):
    enabled: bool = False
    max_characters: Annotated[int, Field(ge=1000, le=1000000)] = 20000
    preview_characters: Annotated[int, Field(ge=100, le=2000)] = 2000

    @model_validator(mode="after")
    def bounded_preview(self):
        if self.preview_characters > self.max_characters:
            raise ValueError("OUTPUT_PREVIEW_EXCEEDS_LIMIT")
        return self


class CompactionPolicy(PolicyModel):
    summarize: bool = False
    target_fraction: Annotated[float, Field(gt=0, lt=1, allow_inf_nan=False)] = 0.75
    keep_recent_messages: Annotated[int, Field(ge=2, le=100)] = 12
    keep_tool_pairs: Annotated[int, Field(ge=1, le=20)] = 3
    # Legacy policy metadata preserves frozen game fingerprints. Summaries
    # share usage accounting without a request-count allowance or reserve.
    summary_request_limit: Annotated[int, Field(ge=1, le=3)] = 1
    summary_output_tokens: Annotated[int, Field(ge=128, le=8192)] = 2048
    parent_request_reserve: Annotated[int, Field(ge=1, le=10)] = 1


class CachePolicy(PolicyModel):
    enabled: bool = True
    retention: Literal["default", "5m", "30m", "1h"] = "default"
    messages: bool = True
    # A longer tier is selected only from a retained pricing/gap analysis. The
    # reason/reference lives alongside the policy, not in a mutable global.
    selection_reason: Literal["provider-default", "shortest-supported", "explicit-override", "measured-savings"] = (
        "provider-default"
    )
    evidence_sha256: Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")] | None = None

    @model_validator(mode="after")
    def selection_evidence(self):
        if self.selection_reason == "measured-savings" and self.evidence_sha256 is None:
            raise ValueError("CACHE_SELECTION_EVIDENCE_REQUIRED")
        return self


class ExecutionPolicy(PolicyModel):
    version: Literal[0, 1, 2, 3] = 1
    code_mode: CodeModePolicy = Field(default_factory=CodeModePolicy)
    output: OutputPolicy = Field(default_factory=OutputPolicy)
    compaction: CompactionPolicy = Field(default_factory=CompactionPolicy)
    cache: CachePolicy = Field(default_factory=CachePolicy)
    intent_handoff_review: bool = Field(default=False, exclude_if=lambda value: not value)
    follow_through_workflow: bool = Field(default=False, exclude_if=lambda value: not value)

    @model_validator(mode="after")
    def legacy_settings(self):
        if self.version == 0 and (
            self.code_mode != CodeModePolicy()
            or self.output != OutputPolicy()
            or self.compaction != CompactionPolicy()
            or self.cache != CachePolicy()
        ):
            raise ValueError("LEGACY_EXECUTION_POLICY_CHANGED")
        if self.version < 2 and self.code_mode.focused_windows != FocusWindowPolicy():
            raise ValueError("FOCUSED_WINDOWS_REQUIRE_EXECUTION_POLICY_V2")
        if self.code_mode.focused_windows.enabled and not self.code_mode.enabled:
            raise ValueError("FOCUSED_WINDOWS_REQUIRE_CODE_MODE")
        if self.version < 2 and self.intent_handoff_review:
            raise ValueError("INTENT_HANDOFF_REVIEW_REQUIRES_EXECUTION_POLICY_V2")
        procedures = any(
            (self.code_mode.delivered_context, self.code_mode.procedure_examples, self.code_mode.typed_selectors)
        )
        if self.version < 3 and (procedures or self.follow_through_workflow):
            raise ValueError("DECISION_WORKFLOWS_REQUIRE_EXECUTION_POLICY_V3")
        if procedures and not self.code_mode.enabled:
            raise ValueError("CODE_PROCEDURES_REQUIRE_CODE_MODE")
        return self

    @classmethod
    def current(cls):
        """Candidate policy for isolated trials; adoption is an explicit game choice."""
        return cls(
            version=2,
            intent_handoff_review=True,
            code_mode=CodeModePolicy(enabled=True, focused_windows=FocusWindowPolicy(enabled=True)),
            output=OutputPolicy(enabled=True),
            compaction=CompactionPolicy(summarize=True),
            cache=CachePolicy(selection_reason="shortest-supported"),
        )

    @classmethod
    def workflows(cls, *, code_mode: bool = True):
        """Explicit isolated comparison: shared follow-through plus code procedures."""
        base = cls.current() if code_mode else cls()
        values = base.model_dump(mode="json")
        values.update(version=3, follow_through_workflow=True)
        if code_mode:
            values["code_mode"].update(delivered_context=True, procedure_examples=True, typed_selectors=True)
        return cls.model_validate(values)

    @property
    def fingerprint(self) -> str:
        return hashlib.sha256(self.model_dump_json().encode()).hexdigest()


def legacy_execution_policy() -> ExecutionPolicy:
    """Missing additive fields retain the old direct tools and no-model compaction."""
    return ExecutionPolicy(version=0)


def coordinator_execution_policy(coordinator) -> ExecutionPolicy:
    session = getattr(coordinator, "managed_session", None)
    return (
        getattr(session.settings, "execution_policy", legacy_execution_policy())
        if session
        else legacy_execution_policy()
    )
