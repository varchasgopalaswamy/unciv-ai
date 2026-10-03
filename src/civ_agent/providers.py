"""Provider configuration and credentials for the shared Codex player runner."""

import hashlib
import json
from pathlib import Path
from typing import Literal

import aiohttp
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from civ_agent.tls import regular_file, write_private_json

Provider = Literal["openai", "openrouter"]
PROVIDERS = ("openai", "openrouter")
OPENROUTER_URL = "https://openrouter.ai/api/v1"


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


def valid_api_key(value):
    return isinstance(value, str) and 1 <= len(value) <= 4096 and all("!" <= c <= "~" for c in value)


def read_api_key(path):
    regular_file(Path(path), private=True)
    value = Path(path).read_text().strip()
    if not valid_api_key(value):
        raise ProviderError("OPENROUTER_KEY_INVALID", "Invalid OpenRouter API key file")
    return value


def provider_environment(environment, provider):
    """Never expose another provider's credential to the model subprocess."""
    result = dict(environment)
    key_file = result.pop("CIV_OPENROUTER_KEY_FILE", None)
    if provider == "openrouter":
        for name in ("OPENAI_API_KEY", "CODEX_API_KEY"):
            result.pop(name, None)
        key = read_api_key(key_file) if key_file else result.get("OPENROUTER_API_KEY")
        if not valid_api_key(key):
            raise ProviderError(
                "OPENROUTER_KEY_MISSING", "OpenRouter API key missing; run civ-agent auth login --provider openrouter"
            )
        result["OPENROUTER_API_KEY"] = key
    else:
        result.pop("OPENROUTER_API_KEY", None)
    return result


def provider_arguments(provider):
    config = {"model_provider": provider}
    if provider == "openrouter":
        config.update(
            {
                "model_providers.openrouter.name": "OpenRouter",
                "model_providers.openrouter.base_url": OPENROUTER_URL,
                "model_providers.openrouter.wire_api": "responses",
                "model_providers.openrouter.env_key": "OPENROUTER_API_KEY",
                "model_providers.openrouter.requires_openai_auth": False,
                "model_providers.openrouter.supports_websockets": False,
            }
        )
    return [item for key, value in config.items() for item in ("-c", key + "=" + json.dumps(value))]


async def openrouter_model_arguments(model, effort, directory):
    """Freeze the model's public capabilities per player, including on resume.

    Unknown models otherwise inherit Codex's context limit and can silently lose
    reasoning settings. The public catalog needs no credential or game context.
    """
    path = Path(directory) / ("openrouter-" + hashlib.sha256(model.encode()).hexdigest()[:16] + ".json")
    if path.exists():
        regular_file(path, private=True)
        data = path.read_text()
    else:
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30)) as session:
                async with session.get(OPENROUTER_URL + "/models") as response:
                    response.raise_for_status()
                    catalog = await response.json()
            data = next((row for row in catalog["data"] if row["id"] == model), None)
        except (aiohttp.ClientError, TimeoutError, KeyError, TypeError, ValueError):
            raise ProviderError(
                "OPENROUTER_CATALOG_UNAVAILABLE", "Could not load OpenRouter model capabilities"
            ) from None
        if data is None:
            raise ProviderError(
                "OPENROUTER_MODEL_NOT_FOUND",
                "OpenRouter model is absent from its public catalog; choose an explicit model ID",
            )
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
    if "structured_outputs" not in parameters:
        raise ProviderError(
            "OPENROUTER_STRUCTURED_OUTPUT_UNSUPPORTED",
            "Choose an OpenRouter model with structured outputs for strategic reviews",
        )
    reasoning = "reasoning" in parameters or "reasoning_effort" in parameters
    if effort not in (None, "none") and not reasoning:
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
    if not path.exists():
        write_private_json(path, capability.model_dump())
    config = {"model_context_window": capability.context_length, "model_supports_reasoning_summaries": reasoning}
    return [item for key, value in config.items() for item in ("-c", key + "=" + json.dumps(value))]
