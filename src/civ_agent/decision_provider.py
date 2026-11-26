"""Native Decision clients with the broker's independent-question response policy."""

import httpx2
from openai import AsyncOpenAI
from pydantic_ai.exceptions import UnexpectedModelBehavior
from pydantic_ai.models.decision import ChoiceQuestion, DecisionRequest
from pydantic_ai.models.openai_decisions import OpenAIDecisionsModel
from pydantic_ai.models.system_one import SystemOneModel
from pydantic_ai.providers.openai import OpenAIProvider
from pydantic_ai.providers.system_one import SystemOneProvider

from civ_agent.decisions_backends import BACKENDS, DecisionBackend, provider_answers
from civ_agent.protocol import JsonObject


class DecisionExchange:
    """One paid exchange, with invocation-local response capture and no repair calls.

    Native clients own question serialization, authentication, HTTP and typed
    answers. Their all-or-nothing response contract cannot represent the broker's
    partial answers/refusals. Recover those from the same successful HTTP reply,
    using the existing probability policy, without issuing another request.
    """

    def __init__(self, backend: DecisionBackend, api_key: str | None, endpoint: str):
        self.backend = backend
        self.api_key = api_key
        self.endpoint = endpoint
        self.response: JsonObject | None = None

    @property
    def usage(self) -> JsonObject:
        # SDK RequestUsage fills absent tokens with zero. Only observed provider
        # usage may settle the broker's reservation, including on malformed replies.
        usage = self.response.get("usage") if self.response is not None else None
        return usage if isinstance(usage, dict) else {}

    async def _target(self, request: httpx2.Request) -> None:
        # Preserve the configured full endpoint, including test/local overrides.
        request.url = httpx2.URL(self.endpoint)
        if self.backend == "jev":
            # SystemOneProvider otherwise inherits SYSTEM_ONE_API_KEY from the
            # environment. This backend's existing contract is unauthenticated.
            request.headers.pop("Authorization", None)

    async def _capture(self, response: httpx2.Response) -> None:
        # A redirect is a failure, never an instruction to forward credentials.
        response.raise_for_status()
        await response.aread()
        supplied = response.json()
        if not isinstance(supplied, dict):
            raise ValueError("Invalid Decisions response")
        self.response = supplied

    async def decide(self, body: JsonObject, *, timeout: float) -> list[JsonObject]:
        if body.get("model") != BACKENDS[self.backend].model:
            raise ValueError("Decisions request model mismatch")
        request = DecisionRequest(
            state=body["input"],
            questions={
                q["name"]: ChoiceQuestion(
                    instructions=q["instructions"],
                    criteria={c["value"]: c["description"] for c in q["choices"]},
                )
                for q in body["questions"]
            },
        )
        if len(request.questions) != len(body["questions"]):
            raise ValueError("Duplicate Decisions question")
        base_url = str(httpx2.URL(self.endpoint).copy_with(path="/v1", query=None, fragment=None))
        async with httpx2.AsyncClient(
            follow_redirects=False,
            trust_env=False,
            timeout=timeout,
            event_hooks={"request": [self._target], "response": [self._capture]},
        ) as http:
            if self.backend == "luna":
                if not self.api_key:
                    raise ValueError("The Luna Decisions backend requires a separate API key")
                async with AsyncOpenAI(
                    api_key=self.api_key, base_url=base_url, http_client=http, max_retries=0
                ) as client:
                    model = OpenAIDecisionsModel(body["model"], provider=OpenAIProvider(openai_client=client))
                    try:
                        result = await model.decide(request, {"timeout": timeout, "max_retries": 0})
                    except UnexpectedModelBehavior:
                        return self._partial_answers(body)
            else:
                model = SystemOneModel(body["model"], provider=SystemOneProvider(base_url=base_url, http_client=http))
                try:
                    result = await model.decide(request, {"timeout": timeout})
                except UnexpectedModelBehavior:
                    return self._partial_answers(body)
        if result.model_name != body["model"]:
            raise ValueError("Decisions response model mismatch")
        # Revalidate the raw success too: the native decoder permits coercion
        # (e.g. a boolean confidence to float). Broker probabilities must retain
        # their original types, and provider confidence remains diagnostic only.
        return self._partial_answers(body)

    def _partial_answers(self, body: JsonObject) -> list[JsonObject]:
        if self.response is None:
            raise ValueError("Missing Decisions response")
        return provider_answers(body, self.response, self.backend)
