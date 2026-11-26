"""Credential-free provider metadata shared by workflow contracts and startup."""

import hashlib
import json
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from civ_agent.protocol import JsonObject
from civ_agent.schemas import BoundaryModel

MAX_PROVIDER_CONTRACT_BYTES = 8 * 1024


class ProviderError(ValueError):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


class ReasoningCapabilities(BaseModel):
    mandatory: bool = False
    supported_efforts: list[str] | None = None


class ModelCapabilities(BaseModel):
    model_config = ConfigDict(hide_input_in_errors=True, strict=True)
    id: str
    context_length: int = Field(gt=0)
    supported_parameters: list[str]
    reasoning: ReasoningCapabilities | None = None
    endpoints: list[str] | None = None


def validate_model_capabilities(model, effort, data, *, structured_output=True):
    """Validate provider claims without I/O or subprocess configuration."""
    try:
        capability = (
            ModelCapabilities.model_validate_json(data)
            if isinstance(data, str)
            else ModelCapabilities.model_validate(data)
        )
    except ValidationError:
        raise ProviderError("OPENROUTER_CAPABILITIES_INVALID", "Invalid OpenRouter model capabilities") from None
    if capability.id != model:
        raise ProviderError("OPENROUTER_CAPABILITIES_INVALID", "Invalid OpenRouter model capabilities")
    parameters = capability.supported_parameters
    if "tools" not in parameters:
        raise ProviderError("OPENROUTER_TOOLS_UNSUPPORTED", "Choose an OpenRouter model that supports tool calling")
    if structured_output and "structured_outputs" not in parameters:
        raise ProviderError(
            "OPENROUTER_STRUCTURED_OUTPUT_UNSUPPORTED",
            "Choose an OpenRouter model with structured outputs for strategic reviews",
        )
    if effort not in (None, "none") and not any(
        parameter in parameters for parameter in ("reasoning", "reasoning_effort")
    ):
        raise ProviderError(
            "OPENROUTER_REASONING_UNSUPPORTED",
            "This OpenRouter model does not support reasoning; set reasoning_effort = 'none'",
        )
    if capability.reasoning is not None:
        supported = capability.reasoning.supported_efforts
        if (capability.reasoning.mandatory and effort == "none") or (
            supported is not None and effort is not None and effort not in supported
        ):
            raise ProviderError(
                "OPENROUTER_EFFORT_UNSUPPORTED",
                "The requested reasoning effort is not supported by this OpenRouter model",
            )
    return capability


class ProviderModelContract(BoundaryModel):
    """Frozen request policy; source claims do not establish account availability."""

    version: Literal[1] = 1
    selection_sha256: Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]
    api_family: Literal["responses", "generate-content"] = "responses"
    source: Literal["sdk-profile", "provider-catalog"]
    sdk_version: str = Field(min_length=1)
    profile: JsonObject
    settings: JsonObject
    catalog: ModelCapabilities | None = None

    @model_validator(mode="after")
    def source_binding(self):
        if (self.source == "provider-catalog") != (self.catalog is not None):
            raise ValueError("PROVIDER_CATALOG_SOURCE_CONFLICT")
        if len(self.model_dump_json().encode()) > MAX_PROVIDER_CONTRACT_BYTES:
            raise ValueError("PROVIDER_CONTRACT_TOO_LARGE")
        return self

    @property
    def fingerprint(self) -> str:
        value = json.dumps(
            self.model_dump(mode="json"), sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
        )
        return hashlib.sha256(value.encode()).hexdigest()

    @property
    def context_window(self) -> int | None:
        value = self.profile.get("context_window")
        return value if type(value) is int else None
