"""Provider clients owned by the trusted controller, separate from model inputs."""

import asyncio
from collections.abc import AsyncGenerator
from contextlib import AsyncExitStack
from hashlib import sha256
from pathlib import Path
from typing import Protocol, TypedDict

import httpx2
from openai import AsyncOpenAI
from pydantic_ai.models import Model
from pydantic_ai.profiles import ModelProfile
from pydantic_ai.settings import ModelSettings

from civ_agent.container_auth import TokenBrokerUnavailable
from civ_agent.orchestration.contracts import ModelSelection, digest
from civ_agent.orchestration.provider_capabilities import (
    ProviderModelContract,
    catalog_profile,
    freeze_model_contract,
    model_profile_overrides,
    preflight_model,
    provider_base_url,
    provider_model,
    selection_settings,
    verify_model_contract,
)
from civ_agent.orchestration.provider_contracts import ModelCapabilities
from civ_agent.orchestration.worker_configuration import GoogleCloudConfig
from civ_agent.protocol import JsonObject
from civ_agent.provider_credentials import read_api_key
from civ_agent.tls import regular_file


class _TransportOptions(TypedDict, total=False):
    transport: httpx2.AsyncBaseTransport


class TokenSource(Protocol):
    async def fetch(self, *, refresh: bool = False, generation: str | None = None) -> JsonObject: ...


class BrokerAuth(httpx2.Auth):
    """One bounded renewal after a definite 401; no transport/inference retries."""

    def __init__(self, client: TokenSource, *, renew_after_unauthorized: bool = True):
        self.client = client
        self.renew_after_unauthorized = renew_after_unauthorized
        self.cache_account_identity: str | None = None

    def bind_cache_account(self, tokens: JsonObject) -> str:
        account = tokens.get("chatgptAccountId")
        if not isinstance(account, str) or not account:
            raise ValueError("WORKFLOW_CACHE_ACCOUNT_REQUIRED")
        identity = sha256(("subscription-account-v1\0" + account).encode()).hexdigest()
        if self.cache_account_identity is not None and identity != self.cache_account_identity:
            raise ValueError("WORKFLOW_CACHE_ACCOUNT_CHANGED")
        self.cache_account_identity = identity
        return identity

    async def tokens(self, request: httpx2.Request, *, refresh=False, generation=None):
        try:
            tokens = await self.client.fetch(refresh=refresh, generation=generation)
            if self.cache_account_identity is not None:
                self.bind_cache_account(tokens)
            return tokens
        except TokenBrokerUnavailable:
            # Both token fetches precede their corresponding inference send.
            # After a definite 401, no accepted inference needs to be repeated.
            raise httpx2.ConnectError("AUTH_BROKER_UNAVAILABLE", request=request) from None

    async def async_auth_flow(self, request: httpx2.Request) -> AsyncGenerator[httpx2.Request, httpx2.Response]:
        if (
            request.url.scheme != "https"
            or request.url.host != "chatgpt.com"
            or not request.url.path.startswith("/backend-api/codex/")
        ):
            raise ValueError("WORKFLOW_AUTH_DESTINATION")
        tokens = await self.tokens(request)
        generation = sha256(tokens["accessToken"].encode()).hexdigest()
        request.headers["Authorization"] = "Bearer " + tokens["accessToken"]
        request.headers["chatgpt-account-id"] = tokens["chatgptAccountId"]
        request.headers["originator"] = "codex_cli_rs"
        response = yield request
        if response.status_code == 401 and self.renew_after_unauthorized:
            await response.aread()
            tokens = await self.tokens(request, refresh=True, generation=generation)
            request.headers["Authorization"] = "Bearer " + tokens["accessToken"]
            request.headers["chatgpt-account-id"] = tokens["chatgptAccountId"]
            yield request


class ProviderClients:
    def __init__(
        self,
        *,
        broker: TokenSource | None = None,
        openai_key_file: Path | None = None,
        openrouter_key_file: Path | None = None,
        google_cloud: GoogleCloudConfig | None = None,
        transport: httpx2.AsyncBaseTransport | None = None,
        renew_after_unauthorized: bool = True,
    ):
        self.broker = broker
        self.key_files = {"openai": openai_key_file, "openrouter": openrouter_key_file}
        self.google_cloud = google_cloud
        self.google_cloud_endpoint: str | None = None
        self.stack = AsyncExitStack()
        self.models: dict[str, Model] = {}
        self.model_cache_accounts: dict[int, str] = {}
        self.model_auth: dict[int, BrokerAuth] = {}
        self.transport = transport
        self.renew_after_unauthorized = renew_after_unauthorized

    async def close(self):
        await self.stack.aclose()

    async def cache_account_identity(self, selection: ModelSelection, *, model: Model | None = None) -> str | None:
        """Bind cache sharing to the account/client actually used, without retaining credentials.

        Key fingerprints conservatively separate keys on the same API account.
        Subscription access-token renewal keeps the stable broker account ID.
        Fixture models and unknown clients get no verified sharing identity.
        """
        if model is not None:
            authentication = self.model_auth.get(id(model))
            if authentication is not None:
                return authentication.bind_cache_account(await authentication.client.fetch())
            return self.model_cache_accounts.get(id(model))
        if selection.provider == "google-cloud":
            config = self.google_cloud
            if config is None or config.credentials_file is None:
                return None
            regular_file(config.credentials_file, private=True)
            return sha256(
                b"google-cloud-adc-v1\0"
                + config.project.encode()
                + b"\0"
                + config.location.encode()
                + b"\0"
                + config.credentials_file.read_bytes()
            ).hexdigest()
        if selection.authentication == "subscription":
            if self.broker is None:
                return None
            tokens = await self.broker.fetch()
            return BrokerAuth(self.broker).bind_cache_account(tokens)
        path = self.key_files[selection.provider]
        if path is None:
            return None
        key = read_api_key(path)
        return sha256((selection.provider + "\0api-key-v1\0" + key).encode()).hexdigest()

    def cache_endpoint(self, selection: ModelSelection) -> str:
        if selection.provider == "google-cloud" and self.google_cloud is not None:
            return (self.google_cloud_endpoint or provider_base_url(selection)).rstrip(
                "/"
            ) + f"/projects/{self.google_cloud.project}/locations/{self.google_cloud.location}"
        return provider_base_url(selection)

    async def model(self, selection: ModelSelection, *, contract: ProviderModelContract | None = None) -> Model:
        if contract is not None and contract.selection_sha256 != digest(selection.model_dump(mode="json")):
            raise ValueError("WORKFLOW_MODEL_CONTRACT_SELECTION_CONFLICT")
        if contract is not None and selection.provider == "openrouter" and contract.catalog is None:
            raise ValueError("WORKFLOW_CATALOG_REQUIRED")
        key = selection.model_dump_json() + (":" + contract.fingerprint if contract else ":unfrozen")
        model = await self._model(
            selection, key=key, profile=model_profile_overrides(selection, contract) if contract else None
        )
        if contract is not None:
            verify_model_contract(selection, model, contract)
        else:
            preflight_model(selection, model)
        return model

    async def prepare(
        self, selection: ModelSelection, *, catalog: ModelCapabilities | None = None
    ) -> ProviderModelContract:
        """Freeze metadata before admission; this does not make an inference call."""
        if selection.provider == "openrouter" and catalog is None:
            raise ValueError("WORKFLOW_CATALOG_REQUIRED")
        profile = catalog_profile(selection, catalog)
        key = selection.model_dump_json() + ":preparation:" + (catalog.model_dump_json() if catalog else "sdk")
        model = await self._model(selection, key=key, profile=profile)
        contract = freeze_model_contract(selection, model, catalog=catalog)
        if contract.context_window is None:
            raise ValueError("WORKFLOW_CONTEXT_WINDOW_UNKNOWN")
        return contract

    async def _model(self, selection: ModelSelection, *, key: str, profile: ModelProfile | None) -> Model:
        if key in self.models:
            return self.models[key]
        if selection.provider == "google-cloud":
            return await self._google_model(selection, key=key, profile=profile)
        if selection.provider == "openrouter" and selection.authentication != "api_key":
            raise ValueError("OPENROUTER_API_KEY_REQUIRED")
        if selection.fast and selection.provider != "openai":
            raise ValueError("WORKFLOW_FAST_UNSUPPORTED")
        if selection.authentication == "adc":
            raise ValueError("WORKFLOW_ADC_PROVIDER_REQUIRED")
        subscription = selection.authentication == "subscription"
        if subscription and self.broker is None:
            raise ValueError("WORKFLOW_BROKER_REQUIRED")
        api_key_file = self.key_files[selection.provider]
        if not subscription and api_key_file is None:
            raise ValueError("WORKFLOW_API_KEY_REQUIRED")
        transport: _TransportOptions = {"transport": self.transport} if self.transport is not None else {}
        authentication = (
            BrokerAuth(self.broker, renew_after_unauthorized=self.renew_after_unauthorized)
            if subscription and self.broker
            else None
        )
        http = await self.stack.enter_async_context(
            httpx2.AsyncClient(
                auth=authentication,
                follow_redirects=False,
                trust_env=False,
                timeout=120,
                **transport,
            )
        )
        base_url = provider_base_url(selection)
        key_value = "broker-managed" if subscription else read_api_key(api_key_file)
        client = await self.stack.enter_async_context(
            AsyncOpenAI(
                # httpx auth replaces this sentinel before any subscription request leaves the client.
                api_key=key_value,
                base_url=base_url,
                http_client=http,
                max_retries=0,
            )
        )
        model = provider_model(selection, client, profile=profile)
        if authentication is not None:
            self.model_auth[id(model)] = authentication
        else:
            self.model_cache_accounts[id(model)] = sha256(
                (selection.provider + "\0api-key-v1\0" + key_value).encode()
            ).hexdigest()
        self.models[key] = model
        return model

    async def _google_model(self, selection: ModelSelection, *, key: str, profile: ModelProfile | None) -> Model:
        from google.genai.types import HttpRetryOptions
        from pydantic_ai.providers.google_cloud import GoogleCloudProvider

        from civ_agent.google_cloud_auth import load_credentials
        from civ_agent.orchestration.google_cloud_model import GoogleCloudModel

        if selection.authentication != "adc":
            raise ValueError("GOOGLE_CLOUD_ADC_REQUIRED")
        if selection.fast:
            raise ValueError("WORKFLOW_FAST_UNSUPPORTED")
        config = self.google_cloud
        if config is None:
            raise ValueError("GOOGLE_CLOUD_CONFIGURATION_REQUIRED")
        credentials = None
        if config.credentials_file is not None:
            credentials = await asyncio.to_thread(load_credentials, config.credentials_file, project=config.project)
        # Let the Google provider own its production HTTP client and its long
        # decode timeouts. A generic 120-second client causes valid slow Google
        # responses to be cancelled and restarted repeatedly.
        http = None
        if self.transport is not None:
            http = await self.stack.enter_async_context(
                httpx2.AsyncClient(
                    follow_redirects=False,
                    trust_env=False,
                    timeout=httpx2.Timeout(20 * 60, connect=5),
                    transport=self.transport,
                )
            )
        provider = GoogleCloudProvider(
            credentials=credentials,
            project=config.project,
            location=config.location,
            http_client=http,
            # The agent policy owns retries and records each attempt.
            retry_options=HttpRetryOptions(attempts=1),
        )
        await self.stack.enter_async_context(provider)
        self.stack.callback(provider.client.close)
        await self.stack.enter_async_context(provider.client.aio)
        self.google_cloud_endpoint = provider.base_url
        model = GoogleCloudModel(selection.model, provider=provider, profile=profile)
        account = await self.cache_account_identity(selection)
        if account is not None:
            self.model_cache_accounts[id(model)] = account
        self.models[key] = model
        return model


def model_settings(selection: ModelSelection) -> ModelSettings:
    return selection_settings(selection)
