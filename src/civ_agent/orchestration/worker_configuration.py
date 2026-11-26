"""Trusted worker credential paths and explicit credential-free fixture models."""

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Annotated

from pydantic import AfterValidator, Field, HttpUrl, UrlConstraints, model_validator

from civ_agent.schemas import BoundaryModel

if TYPE_CHECKING:
    from pydantic_ai.models import Model

    from civ_agent.orchestration.contracts import PeacePhase, ReviewRequest, RoleRequest
    from civ_agent.orchestration.providers import ProviderClients


def _absolute_path(value: Path) -> Path:
    if not value.is_absolute() or any(ord(char) < 32 or ord(char) == 127 for char in str(value)):
        raise ValueError("WORKFLOW_CREDENTIAL_PATH")
    return value


type CredentialPath = Annotated[Path, Field(strict=False), AfterValidator(_absolute_path)]

TRACING_TOKEN_PATH = Path("/run/secrets/logfire.token")
LOCAL_TRACING_NETWORK = "civ-local-telemetry"


class WorkerTracingConfig(BoundaryModel):
    """Optional process-level export references, never part of agent inputs."""

    token_file: CredentialPath | None = Field(default=None, repr=False, exclude_if=lambda value: value is None)
    endpoint: Annotated[HttpUrl, UrlConstraints(allowed_schemes=["https", "http"])]
    local: bool = Field(default=False, exclude_if=lambda value: not value)

    @model_validator(mode="after")
    def endpoint_policy(self):
        if any(
            value is not None
            for value in (self.endpoint.username, self.endpoint.password, self.endpoint.query, self.endpoint.fragment)
        ):
            raise ValueError("WORKFLOW_TRACING_ENDPOINT")
        if self.local:
            if self.token_file is not None or self.endpoint.scheme != "http" or self.endpoint.host != "phoenix":
                raise ValueError("WORKFLOW_TRACING_LOCAL_ENDPOINT")
        elif self.token_file is None or self.endpoint.scheme != "https":
            raise ValueError("WORKFLOW_TRACING_CREDENTIALS")
        return self


class GoogleCloudConfig(BoundaryModel):
    """Cloud billing/routing and an optional ADC reference; never raw credentials."""

    project: Annotated[str, Field(min_length=1, max_length=128, pattern=r"^[a-zA-Z0-9][a-zA-Z0-9.:-]*$")]
    location: Annotated[str, Field(min_length=1, max_length=64, pattern=r"^[a-z0-9-]+$")] = "global"
    credentials_file: CredentialPath | None = Field(default=None, repr=False, exclude_if=lambda value: value is None)


class WorkerProviderConfig(BoundaryModel):
    """Only references cross worker configuration; renewable tokens stay central."""

    scripted: bool = False
    broker_socket: CredentialPath | None = None
    broker_token_file: CredentialPath | None = None
    openai_key_file: CredentialPath | None = None
    openrouter_key_file: CredentialPath | None = None
    google_cloud: GoogleCloudConfig | None = Field(default=None, exclude_if=lambda value: value is None)

    @model_validator(mode="after")
    def credential_policy(self):
        if (self.broker_socket is None) != (self.broker_token_file is None):
            raise ValueError("WORKFLOW_BROKER_CONFIGURATION")
        credentials = (
            self.broker_socket is not None
            or self.openai_key_file is not None
            or self.openrouter_key_file is not None
            or self.google_cloud is not None
        )
        if self.scripted and credentials:
            raise ValueError("WORKFLOW_SCRIPTED_CREDENTIALS")
        if not self.scripted and not credentials:
            raise ValueError("WORKFLOW_PROVIDER_CREDENTIALS_REQUIRED")
        return self


@dataclass(frozen=True)
class ModelFactories:
    role: "Callable[[RoleRequest], Model] | None" = None
    review: "Callable[[ReviewRequest], Model] | None" = None
    peace: "Callable[[PeacePhase], Model] | None" = None


def provider_clients(config: WorkerProviderConfig) -> "ProviderClients":
    # Native SDK imports and credential resolution belong in the worker, never
    # in CLI/configuration discovery or deterministic workflow execution.
    from civ_agent.container_auth import TokenClient
    from civ_agent.orchestration.providers import ProviderClients

    broker = TokenClient(config.broker_socket, config.broker_token_file) if config.broker_socket is not None else None
    return ProviderClients(
        broker=broker,
        openai_key_file=config.openai_key_file,
        openrouter_key_file=config.openrouter_key_file,
        google_cloud=config.google_cloud,
    )


def model_factories(config: WorkerProviderConfig) -> ModelFactories:
    if not config.scripted:
        return ModelFactories()
    # Each existing fixture factory independently rejects a real model selection.
    from civ_agent.orchestration.scripted_model import scripted_model, scripted_peace_model, scripted_review_model

    return ModelFactories(role=scripted_model, review=scripted_review_model, peace=scripted_peace_model)
