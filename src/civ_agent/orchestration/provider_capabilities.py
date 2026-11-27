"""Frozen provider metadata and public SDK preflight; no inference or credential I/O."""

from importlib.metadata import version
from typing import cast

import httpx2
from openai import AsyncOpenAI
from pydantic_ai.models import Model, ModelRequestParameters
from pydantic_ai.models.openai import OpenAIResponsesModel
from pydantic_ai.models.openai_codex import OpenAICodexModel
from pydantic_ai.profiles import ModelProfile, merge_profile
from pydantic_ai.profiles.openai import OpenAIModelProfile, OpenAIReasoningEffort
from pydantic_ai.providers.openai import OpenAIProvider
from pydantic_ai.providers.openai_codex import OpenAICodexProvider
from pydantic_ai.providers.openrouter import OpenRouterProvider
from pydantic_ai.settings import ModelSettings

from civ_agent.orchestration.contracts import ModelSelection, digest
from civ_agent.orchestration.provider_contracts import (
    ModelCapabilities,
    ProviderModelContract,
    validate_model_capabilities,
)
from civ_agent.protocol import JsonObject

# These are public profile properties that affect our text/function-tool requests.
# Types/callables elsewhere in ModelProfile are deliberately never serialized by repr.
PROFILE_FIELDS = (
    "supports_tools",
    "supports_text_output",
    "supports_json_schema_output",
    "supports_json_object_output",
    "default_structured_output_mode",
    "supports_thinking",
    "thinking_always_enabled",
    "thinking_enabled_by_default",
    "supports_forced_tool_choice",
    "supports_forced_tool_choice_with_thinking",
    "forced_tool_choice_disables_thinking",
    "supports_inline_system_prompts",
    "context_window",
    "openai_system_prompt_role",
    "openai_supports_reasoning_effort_none",
    "openai_supports_minimal_reasoning_effort",
    "openai_responses_requires_streaming",
    "openai_responses_requires_store_false",
    "openai_responses_supports_max_reasoning_effort",
    "openai_responses_supports_json_schema_output",
)


def provider_base_url(selection: ModelSelection) -> str:
    if selection.provider == "google-cloud":
        return "https://aiplatform.googleapis.com"
    if selection.authentication == "subscription":
        return "https://chatgpt.com/backend-api/codex"
    return "https://openrouter.ai/api/v1" if selection.provider == "openrouter" else "https://api.openai.com/v1"


def provider_model(selection: ModelSelection, client: AsyncOpenAI, *, profile: ModelProfile | None = None) -> Model:
    if selection.provider == "openai" and selection.model == "gpt-6.1-terra":
        # The SDK describes Terra's reasoning settings but its bundled catalog
        # lacks the model window. An already frozen contract takes precedence.
        # https://developers.openai.com/api/docs/models/gpt-6.1-terra
        profile = merge_profile(ModelProfile(context_window=1_050_000), profile)
    elif selection.provider == "openai" and selection.model in {"gpt-6.2-astra", "gpt-6.2-sol"}:
        # The pinned SDK predates these IDs. Use its public profile extension
        # without changing the requested model or subscription endpoint dialect.
        # https://developers.openai.com/api/docs/guides/latest-model
        # https://developers.openai.com/api/docs/models/gpt-6.2-astra
        # https://developers.openai.com/api/docs/models/gpt-6.2-sol
        documented: OpenAIModelProfile = {
            "context_window": 1050000,
            "supports_thinking": True,
            "thinking_always_enabled": True,
            "thinking_enabled_by_default": True,
            "openai_supports_reasoning": True,
            "openai_supports_encrypted_reasoning_content": True,
            "openai_supports_reasoning_effort_none": False,
            "openai_supports_minimal_reasoning_effort": False,
            "openai_reasoning_efforts": frozenset({"low", "medium", "high", "xhigh", "max"}),
            "openai_supports_phase": True,
            "openai_responses_supports_max_reasoning_effort": selection.authentication == "api_key",
        }
        profile = merge_profile(documented, profile)
    if selection.authentication == "subscription":
        return OpenAICodexModel(selection.model, provider=OpenAICodexProvider(openai_client=client), profile=profile)
    provider = (
        OpenRouterProvider(openai_client=client)
        if selection.provider == "openrouter"
        else OpenAIProvider(openai_client=client)
    )
    return OpenAIResponsesModel(selection.model, provider=provider, profile=profile)


class _NoInference(httpx2.AsyncBaseTransport):
    async def handle_async_request(self, request: httpx2.Request) -> httpx2.Response:
        raise RuntimeError("PROVIDER_PREFLIGHT_REQUEST_FORBIDDEN")


async def prepare_profile_contract(
    selection: ModelSelection,
    *,
    catalog: ModelCapabilities | None = None,
    existing: ProviderModelContract | None = None,
) -> ProviderModelContract:
    """Use the real SDK adapter with an HTTP transport that can never send."""
    if existing is not None:
        catalog = existing.catalog
    if selection.provider == "openrouter" and catalog is None:
        raise ValueError("WORKFLOW_CATALOG_REQUIRED")
    profile = (
        model_profile_overrides(selection, existing) if existing is not None else catalog_profile(selection, catalog)
    )
    async with httpx2.AsyncClient(transport=_NoInference(), follow_redirects=False, trust_env=False) as http:
        if selection.provider == "google-cloud":
            from google.auth.credentials import AnonymousCredentials
            from google.genai.types import HttpRetryOptions
            from pydantic_ai.providers.google_cloud import GoogleCloudProvider

            from civ_agent.orchestration.google_cloud_model import GoogleCloudModel

            provider = GoogleCloudProvider(
                credentials=AnonymousCredentials(),
                project="preflight-no-credentials",
                location="global",
                http_client=http,
                retry_options=HttpRetryOptions(attempts=1),
            )
            try:
                model = GoogleCloudModel(selection.model, provider=provider, profile=profile)
                if existing is not None:
                    verify_model_contract(selection, model, existing)
                    return existing
                contract = freeze_model_contract(selection, model, catalog=catalog)
                if contract.context_window is None:
                    raise ValueError("WORKFLOW_CONTEXT_WINDOW_UNKNOWN")
                return contract
            finally:
                await provider.client.aio.aclose()
                provider.client.close()
        async with AsyncOpenAI(
            api_key="preflight-no-credentials", base_url=provider_base_url(selection), http_client=http, max_retries=0
        ) as client:
            model = provider_model(selection, client, profile=profile)
            if existing is not None:
                verify_model_contract(selection, model, existing)
                return existing
            contract = freeze_model_contract(selection, model, catalog=catalog)
            if contract.context_window is None:
                raise ValueError("WORKFLOW_CONTEXT_WINDOW_UNKNOWN")
            return contract


def selection_settings(selection: ModelSelection) -> ModelSettings:
    if selection.fast and selection.provider != "openai":
        raise ValueError("WORKFLOW_FAST_UNSUPPORTED")
    settings: ModelSettings = {
        "thinking": False if selection.reasoning_effort == "none" else selection.reasoning_effort,
        "max_retries": 0,
    }
    if selection.provider == "openrouter":
        # Responses accepts the provider-routing object at the top level. It is
        # an OpenRouter extension, passed through the SDK's public extra_body.
        settings["extra_body"] = {"provider": {"require_parameters": True}}
    elif selection.fast:
        settings["extra_body"] = {"service_tier": "priority"}
    return settings


def catalog_profile(selection: ModelSelection, catalog: ModelCapabilities | None) -> ModelProfile:
    if catalog is None:
        return {}
    catalog = validate_model_capabilities(selection.model, selection.reasoning_effort, catalog)
    if catalog.endpoints is not None and "/api/v1/responses" not in catalog.endpoints:
        raise ValueError("WORKFLOW_RESPONSES_UNSUPPORTED")
    reasoning = any(value in catalog.supported_parameters for value in ("reasoning", "reasoning_effort"))
    profile: OpenAIModelProfile = {
        "supports_tools": "tools" in catalog.supported_parameters,
        "supports_json_schema_output": "structured_outputs" in catalog.supported_parameters,
        "supports_thinking": reasoning,
        "context_window": catalog.context_length,
    }
    if catalog.reasoning is not None:
        profile["thinking_always_enabled"] = catalog.reasoning.mandatory
        if catalog.reasoning.supported_efforts is not None:
            known = {"none", "minimal", "low", "medium", "high", "xhigh", "max"}
            profile["openai_reasoning_efforts"] = frozenset(
                cast(OpenAIReasoningEffort, value) for value in catalog.reasoning.supported_efforts if value in known
            )
            # Only an explicit catalog claim can extend an endpoint's effort
            # dialect. Absence never upgrades max to a supported value.
            profile["openai_responses_supports_max_reasoning_effort"] = "max" in catalog.reasoning.supported_efforts
    return profile


def model_profile_overrides(selection: ModelSelection, contract: ProviderModelContract) -> ModelProfile:
    if digest(selection.model_dump(mode="json")) != contract.selection_sha256:
        raise ValueError("WORKFLOW_MODEL_CONTRACT_SELECTION_CONFLICT")
    profile = catalog_profile(selection, contract.catalog)
    # Pin the measured/advertised context value, including unknown. A later
    # pricing-data snapshot must not silently change a retained role's window.
    profile["context_window"] = contract.context_window
    return profile


def profile_projection(model: Model) -> JsonObject:
    profile = model.profile
    projected = {field: profile.get(field) for field in PROFILE_FIELDS}
    set_fields = ["openai_reasoning_efforts", "openai_unsupported_model_settings"]
    if model.system == "google-cloud":
        # Keep retained OpenAI/OpenRouter projections byte-for-byte compatible.
        for field in (
            "google_supports_thinking_level",
            "google_supports_minimal_thinking_level",
            "google_disables_thinking_with_zero_budget",
            "google_supports_strict_tool_definition",
            "google_supports_tool_combination",
        ):
            projected[field] = profile.get(field)
        set_fields.append("google_thinking_levels")
    for field in set_fields:
        values = profile.get(field)
        if values is not None and not (
            isinstance(values, (list, tuple, set, frozenset)) and all(isinstance(value, str) for value in values)
        ):
            raise ValueError("WORKFLOW_PROFILE_INVALID")
        projected[field] = sorted(cast(list[str], values)) if values is not None else None
    transformer = profile.get("json_schema_transformer")
    projected["json_schema_transformer"] = (
        transformer.__module__ + "." + transformer.__qualname__ if transformer is not None else None
    )
    return cast(JsonObject, projected)


def preflight_model(selection: ModelSelection, model: Model) -> None:
    """Check actual public preparation rather than inferring support from a name."""
    if selection.provider == "google-cloud" and selection.authentication != "adc":
        raise ValueError("GOOGLE_CLOUD_ADC_REQUIRED")
    if selection.provider != "google-cloud" and selection.authentication == "adc":
        raise ValueError("WORKFLOW_ADC_PROVIDER_REQUIRED")
    system = "openai-codex" if selection.authentication == "subscription" else selection.provider
    if model.model_name != selection.model or model.system != system:
        raise ValueError("WORKFLOW_MODEL_IDENTITY_CONFLICT")
    profile, effort = cast(OpenAIModelProfile, model.profile), selection.reasoning_effort
    if not profile.get("supports_tools", True):
        raise ValueError("WORKFLOW_TOOLS_UNSUPPORTED")
    if effort == "none":
        if profile.get("thinking_always_enabled", False):
            raise ValueError("WORKFLOW_REASONING_REQUIRED")
    elif not profile.get("supports_thinking", False):
        raise ValueError("WORKFLOW_REASONING_UNSUPPORTED")
    accepted = profile.get("openai_reasoning_efforts")
    if accepted is not None and effort not in accepted:
        raise ValueError("WORKFLOW_EFFORT_UNSUPPORTED")
    if effort == "minimal" and not profile.get("openai_supports_minimal_reasoning_effort", True):
        raise ValueError("WORKFLOW_EFFORT_UNSUPPORTED")
    if (
        selection.provider != "google-cloud"
        and effort == "max"
        and not profile.get("openai_responses_supports_max_reasoning_effort", False)
    ):
        raise ValueError("WORKFLOW_EFFORT_UNSUPPORTED")
    _, parameters = model.prepare_request(selection_settings(selection), ModelRequestParameters(output_mode="tool"))
    if effort != "none" and parameters.thinking != effort:
        raise ValueError("WORKFLOW_REASONING_CHANGED")


def freeze_model_contract(
    selection: ModelSelection, model: Model, *, catalog: ModelCapabilities | None = None
) -> ProviderModelContract:
    if catalog is not None:
        catalog = validate_model_capabilities(selection.model, selection.reasoning_effort, catalog)
        # The caller must construct the model with catalog_profile first. Verify
        # the effective profile instead of merely recording unused metadata.
        for name, value in catalog_profile(selection, catalog).items():
            if model.profile.get(name) != value:
                raise ValueError("WORKFLOW_CATALOG_PROFILE_CONFLICT")
    preflight_model(selection, model)
    return ProviderModelContract(
        selection_sha256=digest(selection.model_dump(mode="json")),
        api_family="generate-content" if selection.provider == "google-cloud" else "responses",
        source="provider-catalog" if catalog is not None else "sdk-profile",
        sdk_version=version("pydantic-ai-slim"),
        profile=profile_projection(model),
        settings=cast(JsonObject, selection_settings(selection)),
        catalog=catalog,
    )


def verify_model_contract(selection: ModelSelection, model: Model, contract: ProviderModelContract) -> None:
    # Revalidate mutable nested objects received from a config/file boundary.
    contract = ProviderModelContract.model_validate_json(contract.model_dump_json())
    if digest(selection.model_dump(mode="json")) != contract.selection_sha256:
        raise ValueError("WORKFLOW_MODEL_CONTRACT_SELECTION_CONFLICT")
    current = freeze_model_contract(selection, model, catalog=contract.catalog)
    if current != contract:
        raise ValueError("WORKFLOW_MODEL_CONTRACT_CHANGED")
