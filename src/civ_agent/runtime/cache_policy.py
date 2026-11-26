"""Portable SDK cache controls and evidence from the request actually prepared."""

from dataclasses import asdict, replace
from datetime import timedelta
from hashlib import sha256
from typing import Any, Literal, cast
from uuid import uuid4

from pydantic_ai.capabilities import AbstractCapability, Caching
from pydantic_ai.models import ModelRequestContext, ModelRequestParameters
from pydantic_ai.settings import ModelSettings

from civ_agent.execution_policy import CachePolicy, ExecutionPolicy
from civ_agent.orchestration.contracts import ModelSelection, canonical
from civ_agent.orchestration.provider_capabilities import provider_base_url
from civ_agent.runtime.preload import COMMON_INSTRUCTIONS


def cache_retention(policy: CachePolicy, *, provider: str, authentication: str) -> bool | Literal["5m", "30m", "1h"]:
    """Keep unverified subscription retention untouched; let adapters snap API tiers."""
    if not policy.enabled:
        return False
    if authentication == "subscription":
        return True
    if policy.retention != "default":
        return policy.retention
    if provider == "openai":
        return "30m"
    if provider in {"anthropic", "openrouter"}:
        return "5m"
    # Gemini/Vertex start with implicit caching. No resource is created here.
    return True


def prepared_prefix(parameters: ModelRequestParameters) -> dict[str, Any]:
    """Project public SDK definitions after schema transformation, in their wire order.

    All injected instructions participate conservatively, including dynamic ones.
    Only fingerprints are persisted; no memory, identifiers or text are published.
    Conversation messages are deliberately outside the reusable prefix.
    """

    def tool(definition):
        return {
            "name": definition.name,
            "description": definition.description,
            "parameters": definition.parameters_json_schema,
            "strict": definition.strict,
            "kind": definition.kind,
            "return_schema": definition.return_schema,
        }

    # Agent preparation has already sorted its parts. Direct model callers may
    # supply another order; preserve it exactly rather than inventing a prefix.
    instructions = parameters.instruction_parts or []
    output = parameters.output_object
    return {
        "function_tools": [tool(definition) for definition in parameters.function_tools],
        "output_tools": [tool(definition) for definition in parameters.output_tools],
        "instructions": [
            {"content": part.content, "dynamic": part.dynamic, "on_change": part.on_change} for part in instructions
        ],
        "native_tools": [asdict(native) for native in parameters.native_tools],
        "output_mode": parameters.output_mode,
        "output_object": (
            {"name": output.name, "description": output.description, "json_schema": output.json_schema}
            if output is not None
            else None
        ),
        "allow_text_output": parameters.allow_text_output,
        "thinking": parameters.thinking,
    }


def reusable_prefix(projection: dict[str, Any]) -> dict[str, Any]:
    """The actual public leading material before any changing instruction block."""
    leading = []
    for part in projection["instructions"]:
        if part["dynamic"] or part["on_change"] != "rewrite":
            break
        leading.append(part)
    return {**projection, "instructions": leading}


class ProviderCaching(AbstractCapability[Any]):
    """Use Caching for translation and attach request/response cache provenance.

    The account scope must be a code-owned, verified account/client fingerprint.
    Unknown accounts use a fresh run boundary and never share across runs.
    Credentials are never hashed by this capability.
    """

    id = "provider-caching"

    def __init__(
        self,
        policy: ExecutionPolicy,
        selection: ModelSelection,
        *,
        account_scope: str | None,
        endpoint: str | None = None,
    ):
        self.policy, self.selection = policy, selection
        self.account_scope = account_scope
        self._unverified_session = uuid4().hex
        self.endpoint = endpoint or provider_base_url(selection)
        retention = (
            True
            if policy.version == 0
            else cache_retention(policy.cache, provider=selection.provider, authentication=selection.authentication)
        )
        self.caching = Caching(retention, messages=policy.cache.messages)
        self._prepared: dict[tuple[str | None, int, int], dict[str, Any]] = {}

    def get_model_settings(self) -> ModelSettings | None:
        return self.caching.get_model_settings()

    async def prepare_model_request(self, ctx, request_context: ModelRequestContext):
        subscription = self.selection.provider == "openai" and self.selection.authentication == "subscription"
        if self.policy.version == 0 and not subscription:
            return request_context
        settings, parameters = request_context.model.prepare_request(
            request_context.model_settings, request_context.model_request_parameters
        )
        affinity_hash = None
        if subscription:
            # Transport affinity is independent of prompt keys and logical
            # conversation identity, including for legacy execution policies.
            affinity = sha256(
                canonical(
                    [
                        "subscription-affinity-v1",
                        self.selection.provider,
                        self.account_scope or ["unverified-run", ctx.run_id or self._unverified_session],
                        self.endpoint,
                        request_context.model.model_name,
                    ]
                ).encode()
            ).hexdigest()
            headers = dict((settings or {}).get("extra_headers", {}))
            supplied = {name.lower() for name in headers}
            for name, value in (
                ("session-id", affinity),
                ("thread-id", ctx.conversation_id),
                ("x-client-request-id", ctx.conversation_id),
            ):
                if name not in supplied and value is not None:
                    headers[name] = value
            settings = cast(ModelSettings, {**(settings or {}), "extra_headers": headers})
            affinity_hash = sha256(
                headers[next(name for name in headers if name.lower() == "session-id")].encode()
            ).hexdigest()
        if self.policy.version == 0:
            return replace(request_context, model_settings=settings)
        projection = prepared_prefix(parameters)
        prefix = sha256(canonical(projection).encode()).hexdigest()
        public_projection = reusable_prefix(projection)
        public_prefix = sha256(canonical(public_projection).encode()).hexdigest()
        family = sha256(
            canonical(
                [
                    self.selection.provider,
                    self.selection.authentication,
                    self.account_scope or ["unverified-run", ctx.run_id or self._unverified_session],
                    self.endpoint,
                    request_context.model.model_name,
                    public_prefix,
                ]
            ).encode()
        ).hexdigest()
        effective: timedelta | None = (
            request_context.model.resolve_cache_retention(settings)
            if self.selection.authentication != "subscription"
            else None
        )
        metadata = {
            "version": 1,
            "policy_sha256": self.policy.fingerprint,
            "requested_retention": self.policy.cache.retention,
            "requested_enabled": self.policy.cache.enabled,
            "account_identity_verified": self.account_scope is not None,
            "effective_retention_seconds": effective.total_seconds() if effective is not None else None,
            "retention_source": (
                "sdk-adapter"
                if effective is not None
                else "subscription-default-unverified"
                if self.selection.authentication == "subscription"
                else "unverified-provider-default"
            ),
            "public_prefix_sha256": public_prefix,
            "public_prefix_bytes": len(canonical(public_projection).encode()),
            "common_instructions_sha256": sha256(COMMON_INSTRUCTIONS.encode()).hexdigest(),
            "prepared_prefix_sha256": prefix,
            "prepared_prefix_bytes": len(canonical(projection).encode()),
            "routing_family_sha256": family,
        }
        if affinity_hash is not None:
            metadata["subscription_affinity_sha256"] = affinity_hash
        self._prepared[(ctx.run_id, ctx.run_step, request_context.attempt)] = metadata
        if self.selection.provider == "openai":
            settings = cast(
                ModelSettings, {**(settings or {}), "openai_prompt_cache_key": "civ-prepared-v1-" + family[:40]}
            )
        return replace(request_context, model_settings=settings)

    async def after_model_request(self, ctx, *, request_context, response):
        if self.policy.version == 0:
            return response
        prepared = self._prepared.pop((ctx.run_id, ctx.run_step, request_context.attempt), {})
        usage = response.usage
        measured = not usage.unmeasured_requests and usage.has_values()
        # The pinned SDK has no standard 5m bucket. Some adapters retain an
        # explicit detail; consume it when available, never infer it by
        # subtracting an absent/default one-hour bucket from total writes.
        writes_5m = getattr(usage, "cache_write_5m_tokens", None)
        if writes_5m is None:
            writes_5m = usage.details.get("cache_write_5m_tokens")
        # SDK defaults erase whether a zero cache field was absent on the wire.
        # Preserve that uncertainty; a positive field remains measured evidence.
        observed = {
            "input_tokens": usage.input_tokens if measured else None,
            "cache_read_tokens": usage.cache_read_tokens if measured and usage.cache_read_tokens else None,
            "cache_write_tokens": usage.cache_write_tokens if measured and usage.cache_write_tokens else None,
            "cache_write_1h_tokens": usage.cache_write_1h_tokens if measured and usage.cache_write_1h_tokens else None,
            "cache_write_5m_tokens": writes_5m if measured and type(writes_5m) is int and writes_5m > 0 else None,
            "cost_usd": str(usage.cost) if usage.cost is not None else None,
            "cost_provenance": "sdk-usage" if usage.cost is not None else None,
        }
        return replace(response, metadata={**(response.metadata or {}), "cache": {**prepared, **observed}})
