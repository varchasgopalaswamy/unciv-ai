"""Strict schemas for data owned by this service, not extensible upstream events."""

from typing import Annotated

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, ValidationError

from civ_agent.agent_settings import valid_setting


class BoundaryModel(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", hide_input_in_errors=True)


def _setting(value: str) -> str:
    if not valid_setting(value):
        raise ValueError("INVALID_SETTING")
    return value


type Identifier = Annotated[str, Field(min_length=1, max_length=128, pattern=r"^[!-~]+$")]
type PlayerToken = Annotated[str, Field(min_length=43, max_length=43, pattern=r"^[A-Za-z0-9_-]+$")]
type Setting = Annotated[str, Field(min_length=1, max_length=128), AfterValidator(_setting)]
type NonnegativeInt = Annotated[int, Field(ge=0)]
type Timeout = Annotated[int, Field(ge=1, le=86400)]


class StartRequest(BoundaryModel):
    activationId: Identifier
    epoch: Identifier
    civilization: Annotated[str, Field(min_length=1, max_length=128)]
    gameTurn: NonnegativeInt
    handoff: NonnegativeInt
    threadId: Identifier | None
    mcpUrl: str
    playerToken: PlayerToken = Field(repr=False)
    prompt: Annotated[str, Field(min_length=1, max_length=1_000_000, repr=False)]
    model: Setting | None
    reasoningEffort: Setting | None = None
    timeoutSeconds: Timeout
    leaseSeconds: Annotated[int, Field(ge=1, le=300)]


class ConnectRequest(BoundaryModel):
    epoch: Identifier
    playerToken: PlayerToken = Field(repr=False)
    model: Setting | None
    reasoningEffort: Setting | None
    timeoutSeconds: Timeout


class InteractiveRequest(BoundaryModel):
    playerToken: PlayerToken = Field(repr=False)
    hostUrl: str
    model: Setting | None
    reasoningEffort: Setting | None


def validate[M: BoundaryModel](model: type[M], payload: object) -> M:
    """Never pass validation inputs, field names, or exception contexts to a caller."""
    try:
        return model.model_validate(payload)
    except ValidationError:
        raise ValueError("INVALID_ARGUMENT") from None


def start_error(error: ValidationError) -> str:
    """Retain actionable service codes without exposing credentials or prompt text."""
    errors = error.errors(include_input=False, include_context=False, include_url=False)
    if any(item["type"] in {"missing", "extra_forbidden"} for item in errors):
        return "INVALID_ARGUMENT"
    codes = {
        "activationId": "INVALID_BINDING",
        "epoch": "INVALID_BINDING",
        "civilization": "INVALID_BINDING",
        "mcpUrl": "INVALID_MCP_ENDPOINT",
        "playerToken": "INVALID_PLAYER_TOKEN",
        "threadId": "INVALID_THREAD",
        "gameTurn": "INVALID_GAME_TURN",
        "handoff": "INVALID_GAME_TURN",
        "prompt": "INVALID_PROMPT",
        "model": "INVALID_MODEL",
        "reasoningEffort": "INVALID_REASONING_EFFORT",
        "timeoutSeconds": "INVALID_TIMEOUT",
        "leaseSeconds": "INVALID_TIMEOUT",
    }
    return codes.get(str(errors[0]["loc"][0]), "INVALID_ARGUMENT") if errors[0]["loc"] else "INVALID_ARGUMENT"
