"""Frozen resolved player settings for a managed native game session."""

from typing import Annotated, Literal

from pydantic import ConfigDict, Field, model_validator

from civ_agent.game_config import AgentDefaults, EconomyModel
from civ_agent.orchestration.contracts import ModelSelection, OpaqueId, digest
from civ_agent.orchestration.provider_contracts import ProviderModelContract
from civ_agent.player_settings import communication_effort
from civ_agent.protocol import JsonObject
from civ_agent.schemas import BoundaryModel, NonnegativeInt

type NativeModelRole = Literal[
    "gameplay", "economy", "military", "communication", "review", "peace", "peace_main", "peace_military"
]
MODEL_ROLES: tuple[NativeModelRole, ...] = (
    "gameplay",
    "economy",
    "military",
    "communication",
    "review",
    "peace_main",
    "peace_military",
)
MAX_PLAYER_SESSION_BYTES = 48 * 1024


class NativePlayerSettings(AgentDefaults):
    model_config = ConfigDict(frozen=True, strict=True, hide_input_in_errors=True)
    economy_model: EconomyModel
    military_model: EconomyModel
    scripted: bool = False

    def get_selection(self, role: NativeModelRole) -> ModelSelection:
        selected = (
            self.military_model
            if role in {"military", "peace_military"}
            else self.economy_model
            if role == "economy"
            else self
        )
        effort = (
            communication_effort(self.reasoning_effort, self.communication_reasoning_effort)
            if role == "communication"
            else selected.reasoning_effort
        )
        return ModelSelection.model_validate(
            {
                "provider": selected.provider,
                "authentication": (
                    "subscription"
                    if selected.provider == "openai"
                    else "adc"
                    if selected.provider == "google-cloud"
                    else "api_key"
                ),
                "model": selected.model,
                "reasoning_effort": effort,
                "fast": self.fast and selected.provider == "openai",
            }
        )

    @model_validator(mode="after")
    def native_selections(self):
        try:
            for role in ("gameplay", "economy", "military", "communication"):
                selection = self.get_selection(role)
                if (
                    (selection.model == "scripted-v1") != self.scripted
                    or self.scripted
                    and selection.provider != "openai"
                ):
                    raise ValueError("MANAGED_SCRIPTED_MODEL_FORBIDDEN")
        except ValueError:
            raise ValueError("MANAGED_MODEL_SETTINGS_UNSUPPORTED") from None
        return self


class NativePlayerSession(BoundaryModel):
    model_config = ConfigDict(frozen=True)
    game: OpaqueId
    player: OpaqueId
    settings: NativePlayerSettings
    request_limit: Annotated[int, Field(ge=1, le=100)] = 24
    provider_contracts: Annotated[dict[str, ProviderModelContract], Field(max_length=32)] = Field(default_factory=dict)

    def get_selection(self, role: NativeModelRole) -> ModelSelection:
        return self.settings.get_selection(role)

    def get_provider_contract(
        self, role: NativeModelRole, *, selection: ModelSelection | None = None
    ) -> ProviderModelContract | None:
        chosen = selection or self.get_selection(role)
        key = digest(chosen.model_dump(mode="json"))
        contract = self.provider_contracts.get(key)
        if contract is not None and contract.selection_sha256 != key:
            raise ValueError("MANAGED_PROVIDER_CONTRACT_CONFLICT")
        return contract

    def require_provider_contracts(self):
        # The controller and isolated players have separate private codecs.
        # This input must stay inline below their 64 KiB storage threshold.
        if len(self.model_dump_json().encode()) > MAX_PLAYER_SESSION_BYTES:
            raise ValueError("MANAGED_PLAYER_SESSION_TOO_LARGE")
        if self.settings.scripted:
            if self.provider_contracts:
                raise ValueError("MANAGED_SCRIPTED_PROVIDER_CONTRACT")
            return
        if any(self.get_provider_contract(role) is None for role in MODEL_ROLES) or any(
            key != contract.selection_sha256 for key, contract in self.provider_contracts.items()
        ):
            raise ValueError("MANAGED_PROVIDER_CONTRACT_REQUIRED")

    def get_request_limit(self, role: NativeModelRole) -> int:
        # Preserve immutable request metadata for retained sessions and journals.
        # The local runtime accounts for requests without enforcing this value.
        return min(
            self.request_limit,
            2 if role in {"peace", "peace_main", "peace_military"} else 12 if role == "review" else 100,
        )

    def job_settings(self, role: NativeModelRole) -> JsonObject:
        return {**self.settings.model_dump(mode="json"), **self.get_selection(role).model_dump(mode="json")}

    @property
    def review_interval(self) -> int:
        return self.settings.review_interval


class PrepareManagedTurn(BoundaryModel):
    session: NativePlayerSession
    index: NonnegativeInt
    expected_epoch: OpaqueId
    expected_handoff: NonnegativeInt


class ConfigureManagedPlayer(BoundaryModel):
    session: NativePlayerSession
    expected_epoch: OpaqueId
