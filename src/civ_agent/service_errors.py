"""Lightweight domain-service failure boundary, without a model runtime import."""

from civ_agent.protocol import JsonObject


class ServiceError(Exception):
    def __init__(self, code: str, status: int = 400) -> None:
        self.code, self.status = code, status
        self.request_failure: JsonObject | None = None
        super().__init__(code)
