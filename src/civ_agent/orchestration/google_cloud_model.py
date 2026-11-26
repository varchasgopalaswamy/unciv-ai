"""Native Gemini transport with lossless handling of application JSON schemas."""

from collections.abc import AsyncGenerator, Iterator
from contextlib import asynccontextmanager, contextmanager
from dataclasses import replace
from typing import Any

from google.auth.exceptions import GoogleAuthError, TransportError
from pydantic_ai import RunContext
from pydantic_ai.exceptions import ModelAuthenticationError, ModelConnectionError, ModelUnavailableError
from pydantic_ai.messages import ModelMessage, ModelRequest, ModelResponse, ToolReturnPart
from pydantic_ai.models import ModelRequestParameters, StreamedResponse
from pydantic_ai.models.google import GoogleModel
from pydantic_ai.settings import ModelSettings, merge_model_settings
from pydantic_ai.usage import RequestUsage


def _contains_reference(value: Any) -> bool:
    if isinstance(value, dict):
        return "$ref" in value or any(_contains_reference(item) for item in value.values())
    if isinstance(value, list):
        return any(_contains_reference(item) for item in value)
    return False


def _wire_tool_return(part: ToolReturnPart) -> ToolReturnPart:
    if not _contains_reference(part.model_response_object(wrap_if_error=False)):
        return part
    text = part.model_response_str(wrap_if_error=False)
    return replace(part, content=[text, *part.files] if part.files else text)


def _wire_messages(messages: list[ModelMessage]) -> list[ModelMessage]:
    """Keep JSON data references out of Gemini's multimodal reference namespace.

    Only the transport copy changes. Canonical tool results, nested Code Mode
    values, metadata, files, and persisted histories retain their original types.
    https://docs.cloud.google.com/gemini-enterprise-agent-platform/models/tools/function-calling#multimodal-function-responses
    """
    result: list[ModelMessage] = []
    for message in messages:
        if not isinstance(message, ModelRequest):
            result.append(message)
            continue
        parts = []
        for part in message.parts:
            if isinstance(part, ToolReturnPart):
                part = _wire_tool_return(part)
            parts.append(part)
        result.append(replace(message, parts=parts))
    return result


@contextmanager
def _auth_errors(model_name: str) -> Iterator[None]:
    """ADC errors bypass the SDK's API mapping; expose the common model errors."""
    try:
        yield
    except GoogleAuthError as error:
        category = (
            ModelConnectionError
            if isinstance(error, TransportError)
            else ModelUnavailableError
            if error.retryable
            else ModelAuthenticationError
        )
        raise category(model_name, "GOOGLE_CLOUD_AUTH_ERROR") from None


class GoogleCloudModel(GoogleModel):
    """Specialize the SDK Model interface; shared agent policy stays provider neutral."""

    async def request(
        self,
        messages: list[ModelMessage],
        model_settings: ModelSettings | None,
        model_request_parameters: ModelRequestParameters,
    ) -> ModelResponse:
        with _auth_errors(self.model_name):
            return await super().request(_wire_messages(messages), model_settings, model_request_parameters)

    @asynccontextmanager
    async def request_stream(
        self,
        messages: list[ModelMessage],
        model_settings: ModelSettings | None,
        model_request_parameters: ModelRequestParameters,
        run_context: RunContext[Any] | None = None,
    ) -> AsyncGenerator[StreamedResponse]:
        with _auth_errors(self.model_name):
            async with super().request_stream(
                _wire_messages(messages), model_settings, model_request_parameters, run_context
            ) as response:
                yield response

    async def count_tokens(
        self,
        messages: list[ModelMessage],
        model_settings: ModelSettings | None,
        model_request_parameters: ModelRequestParameters,
    ) -> RequestUsage:
        with _auth_errors(self.model_name):
            return await super().count_tokens(_wire_messages(messages), model_settings, model_request_parameters)

    def prepare_request(
        self, model_settings: ModelSettings | None, model_request_parameters: ModelRequestParameters
    ) -> tuple[ModelSettings | None, ModelRequestParameters]:
        # Gemini's ANY/VALIDATED constrained decoders reject valid schemas with
        # nested bounds, root object unions, or large action unions. AUTO accepts
        # the full schemas. Pydantic validates every call and report locally.
        # Preserve explicit strict/forced choices; do not silently override them.
        # https://ai.google.dev/gemini-api/docs/function-calling#function_calling_modes
        definitions = [*model_request_parameters.function_tools, *model_request_parameters.output_tools]
        effective = merge_model_settings(self.settings, model_settings) or {}
        if effective.get("tool_choice") in (None, "auto") and not any(
            definition.strict is True for definition in definitions
        ):
            model_settings = {**(model_settings or {}), "tool_choice": "auto"}
            model_request_parameters = replace(
                model_request_parameters,
                # The SDK infers ANY when tool output rules out text. Permit
                # text on the transport only; the Agent's typed output schema
                # still rejects it and requests the required completion tool.
                allow_text_output=True,
                function_tools=[replace(tool, strict=False) for tool in model_request_parameters.function_tools],
                output_tools=[replace(tool, strict=False) for tool in model_request_parameters.output_tools],
            )
        return super().prepare_request(model_settings, model_request_parameters)
