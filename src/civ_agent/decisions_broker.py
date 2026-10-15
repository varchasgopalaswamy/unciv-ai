"""Trusted authenticated local socket; the API key never enters a player container."""

import asyncio
import hashlib
import hmac
import json
import os
import time
from pathlib import Path
from typing import Any

import aiohttp
from aiohttp import web

from civ_agent.advisory_answers import failed_answers
from civ_agent.decisions_ledger import RATE_VERSION, DecisionsLedger
from civ_agent.decisions_policy import routing_thresholds
from civ_agent.decisions_tasks import TASK_VERSION, parse_answers, request_body, request_id, request_metadata
from civ_agent.diplomacy_decisions import MODEL, POLICY_VERSION, DecisionsClient, fallback
from civ_agent.protocol import JsonObject
from civ_agent.providers import ProviderError, read_api_key
from civ_agent.tls import private_directory, write_private_json

BROKER_PROTOCOL_VERSION = 3
_PROVIDER_ERROR_KINDS = {
    "authentication",
    "access_denied",
    "rate_limited",
    "upstream_unavailable",
    "http_error",
    "transport",
    "timeout",
    "response_invalid",
}


def _valid_health(body):
    if not isinstance(body, dict) or set(body) != {
        "protocolVersion",
        "classifierModel",
        "routingPolicyVersion",
        "advisoryPolicyVersion",
        "accountingRateVersion",
        "status",
        "upstreamStatus",
        "circuit",
        "lastFailure",
    }:
        return False
    if (
        type(body["protocolVersion"]) is not int
        or body["protocolVersion"] != BROKER_PROTOCOL_VERSION
        or body["classifierModel"] != MODEL
        or body["routingPolicyVersion"] != POLICY_VERSION
        or body["advisoryPolicyVersion"] != TASK_VERSION
        or body["accountingRateVersion"] != RATE_VERSION
        or body["status"] != "ready"
        or body["upstreamStatus"] not in ("unchecked", "available", "unavailable")
    ):
        return False
    circuit = body["circuit"]
    if (
        not isinstance(circuit, dict)
        or set(circuit) != {"state", "consecutiveFailures", "retryAfterSeconds"}
        or circuit["state"] not in ("closed", "open")
        or type(circuit["consecutiveFailures"]) is not int
        or circuit["consecutiveFailures"] < 0
        or not isinstance(circuit["retryAfterSeconds"], (int, float))
        or isinstance(circuit["retryAfterSeconds"], bool)
        or not 0 <= circuit["retryAfterSeconds"] <= 30
    ):
        return False
    failure = body["lastFailure"]
    return failure is None or (
        isinstance(failure, dict)
        and set(failure) in ({"kind"}, {"kind", "httpStatus"})
        and isinstance(failure["kind"], str)
        and failure["kind"] in _PROVIDER_ERROR_KINDS
        and ("httpStatus" not in failure or type(failure["httpStatus"]) is int and 100 <= failure["httpStatus"] <= 599)
    )


def read_decisions_key(path):
    try:
        return read_api_key(path)
    except ProviderError:
        raise ValueError("Invalid Decisions API key file") from None


class DecisionsBroker:
    def __init__(self, root, client=None):
        self.root = Path(root)
        self.client = client or DecisionsClient(read_decisions_key(self.root / "decisions.key"))
        self.ledger = DecisionsLedger(self.root)
        self.locks = {}

    def _authenticate(self, request):
        authorization = request.headers.get("Authorization", "")
        if not authorization.startswith("Bearer "):
            raise web.HTTPUnauthorized()
        supplied = authorization.removeprefix("Bearer ")
        if not supplied:
            raise web.HTTPUnauthorized()
        digest = hashlib.sha256(supplied.encode()).hexdigest()
        try:
            clients = json.loads((self.root / "decisions-clients.json").read_text())
        except (OSError, ValueError):
            raise web.HTTPUnauthorized() from None
        if not isinstance(clients, dict):
            raise web.HTTPUnauthorized()
        for principal, value in clients.items():
            if isinstance(value, str) and value.isascii() and hmac.compare_digest(digest, value):
                return principal
        raise web.HTTPUnauthorized()

    async def health(self, request):
        self._authenticate(request)
        return web.json_response(
            {
                "protocolVersion": BROKER_PROTOCOL_VERSION,
                "classifierModel": MODEL,
                "routingPolicyVersion": POLICY_VERSION,
                "advisoryPolicyVersion": TASK_VERSION,
                "accountingRateVersion": RATE_VERSION,
                "status": "ready",
                **self.client.health_snapshot(),
            }
        )

    async def classify(self, request):
        digest = self._authenticate(request)
        try:
            body = await request.json()
        except (ValueError, UnicodeError):
            raise web.HTTPBadRequest() from None
        if (
            not isinstance(body, dict)
            or set(body) != {"direction", "bundle"}
            or body["direction"] not in ("incoming", "outgoing")
            or not isinstance(body["bundle"], dict)
        ):
            raise web.HTTPBadRequest()
        # Identity comes from the broker credential, never a player-supplied name.
        async with self.locks.setdefault(digest, asyncio.Lock()):
            try:
                policy = self.ledger.settings(digest)
            except ValueError:
                policy = None
            options = (
                {
                    "timeout": policy.decisions_timeout_seconds,
                    "thresholds": routing_thresholds(policy, body["direction"]),
                    "policy_version": policy.decisions_routing_policy,
                }
                if policy is not None
                else {}
            )
            return web.json_response(await self.client.classify(body["direction"], body["bundle"], **options))

    async def batch(self, request):
        principal = self._authenticate(request)
        try:
            settings = self.ledger.settings(principal)
        except ValueError:
            raise web.HTTPForbidden() from None
        if not settings.decisions_enabled:
            raise web.HTTPForbidden()
        try:
            envelope = await request.json()
            if not isinstance(envelope, dict) or set(envelope) != {"requestId", "evidence"}:
                raise ValueError("Invalid envelope")
            evidence = envelope["evidence"]
            if not isinstance(evidence, dict) or evidence.get("player") != self.ledger.player(principal):
                raise ValueError("Invalid player perspective")
            if envelope["requestId"] != request_id(evidence):
                raise ValueError("Invalid stable request ID")
            body = request_body(evidence, settings)
        except (ValueError, TypeError, KeyError, UnicodeError):
            raise web.HTTPBadRequest() from None
        metadata = request_metadata(body, evidence)
        pending = {
            **metadata,
            **failed_answers(body, settings.decisions_min_confidence, "completion_unknown"),
            "replayEvidence": json.loads(body["input"]),
        }
        async with self.locks.setdefault(principal, asyncio.Lock()):
            cached = self.ledger.reserve(
                principal, envelope["requestId"], metadata["inputHash"], body, pending_result=pending
            )
            if cached is not None:
                if "questionResults" not in cached:
                    cached = {
                        **pending,
                        **failed_answers(body, settings.decisions_min_confidence, cached["fallback"]),
                    }
                    if cached["fallback"] == "budget_exhausted":
                        self.ledger.complete(principal, envelope["requestId"], cached)
                return web.json_response(self._reply(principal, cached))
            # No provider retries. A cancelled/unknown call retains its reservation.
            raw = await self.client.evaluate(body, timeout=settings.decisions_timeout_seconds)
            parsed = (
                failed_answers(body, settings.decisions_min_confidence, raw["fallback"])
                if raw.get("fallback")
                else parse_answers(body, raw, settings.decisions_min_confidence)
            )
            result = {
                **parsed,
                **metadata,
                "providerError": raw.get("providerError"),
                "usage": raw.get("usage", {}),
                "latencyMs": raw.get("latencyMs"),
                "replayEvidence": pending["replayEvidence"],
            }
            self.ledger.complete(principal, envelope["requestId"], result)
            return web.json_response(self._reply(principal, result))

    def _reply(self, principal, result):
        return {
            **{k: v for k, v in result.items() if k not in {"replayEvidence", "pending"}},
            "accounting": self.ledger.status(principal),
        }

    async def lookup(self, request):
        principal = self._authenticate(request)
        identifier = request.match_info["request_id"]
        if len(identifier) != 64 or any(c not in "0123456789abcdef" for c in identifier):
            raise web.HTTPBadRequest()
        try:
            self.ledger.settings(principal)
        except ValueError:
            raise web.HTTPForbidden() from None
        return web.json_response(self._reply(principal, self.ledger.lookup(principal, identifier)))

    def application(self):
        app = web.Application(client_max_size=256 * 1024)
        app.router.add_get("/health", self.health)
        app.router.add_post("/classify", self.classify)
        app.router.add_post("/batch", self.batch)
        app.router.add_get("/batch/{request_id}", self.lookup)
        return app


class RoutingClient:
    def __init__(self, socket_path, token_file):
        self.socket_path, self.token_file = str(socket_path), Path(token_file)
        self.last_health: dict[str, Any] = {"status": "unchecked", "reason": "not_checked"}

    def _token(self):
        body = json.loads(self.token_file.read_text())
        token = body.get("token") if isinstance(body, dict) else None
        if not isinstance(token, str) or not token:
            raise ValueError("Invalid broker credential")
        return token

    @classmethod
    def from_environment(cls):
        socket = os.environ.get("CIV_DECISIONS_SOCKET")
        token = os.environ.get("CIV_DECISIONS_TOKEN_FILE")
        if not socket or not token or not Path(token).is_file():
            raise ValueError("Decisions requires an authenticated trusted broker and separate API key")
        return cls(socket, token)

    async def health(self, timeout=5):
        """Free local preflight. Timeout must be positive and no greater than five seconds.

        Ready validates local protocol/authentication, not upstream access.
        """
        if not isinstance(timeout, (int, float)) or isinstance(timeout, bool) or not 0 < timeout <= 5:
            raise ValueError("Broker health timeout must be positive and at most five seconds")
        try:
            token = self._token()
            async with aiohttp.ClientSession(
                connector=aiohttp.UnixConnector(path=self.socket_path), timeout=aiohttp.ClientTimeout(total=timeout)
            ) as session:
                async with session.get(
                    "http://localhost/health", headers={"Authorization": "Bearer " + token}, allow_redirects=False
                ) as response:
                    response.raise_for_status()
                    try:
                        body = await response.json()
                    except (ValueError, aiohttp.ContentTypeError):
                        body = None
            result: dict[str, Any] = (
                body
                if isinstance(body, dict) and _valid_health(body)
                else {"status": "incompatible", "reason": "invalid_health_contract"}
            )
        except TimeoutError:
            result = {"status": "unavailable", "reason": "broker_timeout"}
        except aiohttp.ClientResponseError as error:
            reason = {
                401: "broker_access_denied",
                403: "broker_access_denied",
                429: "broker_rate_limited",
            }.get(error.status, "broker_http_error")
            result = {
                "status": "unavailable",
                "reason": reason,
                "httpStatus": error.status,
            }
        except (OSError, ValueError, aiohttp.ClientError):
            result = {"status": "unavailable", "reason": "broker_unavailable"}
        self.last_health = result
        return result

    async def classify(self, direction, bundle):
        try:
            token = self._token()
            async with aiohttp.ClientSession(
                connector=aiohttp.UnixConnector(path=self.socket_path), timeout=aiohttp.ClientTimeout(total=5)
            ) as session:
                async with session.post(
                    "http://localhost/classify",
                    headers={"Authorization": "Bearer " + token},
                    json={"direction": direction, "bundle": bundle},
                    allow_redirects=False,
                ) as response:
                    response.raise_for_status()
                    return await response.json()
        except TimeoutError:
            return fallback(direction, "broker_timeout")
        except aiohttp.ClientResponseError as error:
            reason = {
                401: "broker_access_denied",
                403: "broker_access_denied",
                429: "broker_rate_limited",
            }.get(error.status, "broker_http_error")
            return {**fallback(direction, reason), "brokerHttpStatus": error.status}
        except (OSError, ValueError, aiohttp.ClientError):
            return fallback(direction, "broker_unavailable")

    async def batch(self, evidence, *, timeout=5) -> JsonObject:
        stable_id = request_id(evidence)
        try:
            async with aiohttp.ClientSession(
                connector=aiohttp.UnixConnector(path=self.socket_path),
                timeout=aiohttp.ClientTimeout(total=timeout + 1),
            ) as session:
                async with session.post(
                    "http://localhost/batch",
                    headers={"Authorization": "Bearer " + self._token()},
                    json={"requestId": stable_id, "evidence": evidence},
                    allow_redirects=False,
                ) as response:
                    response.raise_for_status()
                    result = await response.json()
            if not isinstance(result, dict) or result.get("requestId") != stable_id:
                raise ValueError("Invalid Decisions batch reply")
            return result
        except (TimeoutError, OSError, ValueError, aiohttp.ClientError):
            return {"answers": [], "fallback": "broker_unavailable", "requestId": stable_id}

    async def lookup(self, identifier, *, timeout=5) -> JsonObject:
        """A free receipt lookup can never start a Decisions provider request."""
        if (
            not isinstance(identifier, str)
            or len(identifier) != 64
            or any(c not in "0123456789abcdef" for c in identifier)
        ):
            raise ValueError("Invalid Decisions request ID")
        try:
            async with aiohttp.ClientSession(
                connector=aiohttp.UnixConnector(path=self.socket_path),
                timeout=aiohttp.ClientTimeout(total=timeout),
            ) as session:
                async with session.get(
                    "http://localhost/batch/" + identifier,
                    headers={"Authorization": "Bearer " + self._token()},
                    allow_redirects=False,
                ) as response:
                    response.raise_for_status()
                    result = await response.json()
            if not isinstance(result, dict) or result.get("requestId") != identifier:
                raise ValueError("Invalid Decisions lookup reply")
            return result
        except (TimeoutError, OSError, ValueError, aiohttp.ClientError):
            return {"answers": [], "fallback": "broker_unavailable", "requestId": identifier}


def main():
    root = Path("/auth")
    os.umask(0o077)
    socket = private_directory(root / "decisions-ipc") / "broker.sock"
    socket.unlink(missing_ok=True)
    web.run_app(
        DecisionsBroker(root).application(),
        path=str(socket),
        access_log=None,
        print=lambda _: write_private_json(
            root / "decisions-ready.json",
            {
                "ready": True,
                "at": time.time(),
                "protocolVersion": BROKER_PROTOCOL_VERSION,
                "classifierModel": MODEL,
                "routingPolicyVersion": POLICY_VERSION,
            },
        ),
    )
