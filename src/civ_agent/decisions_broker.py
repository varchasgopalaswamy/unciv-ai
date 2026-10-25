"""Trusted authenticated local socket; the API key never enters a player container."""

import asyncio
import hashlib
import hmac
import json
import os
import re
import time
from pathlib import Path
from typing import Any

import aiohttp
from aiohttp import web

from civ_agent.advisory_answers import failed_answers
from civ_agent.decisions_backends import BACKENDS, DEFAULT_BACKEND
from civ_agent.decisions_ledger import RATE_VERSION, DecisionsLedger
from civ_agent.decisions_policy import routing_thresholds
from civ_agent.decisions_tasks import TASK_VERSION, parse_answers, request_body, request_id, request_metadata
from civ_agent.diplomacy_decisions import MODEL, POLICY_VERSION, DecisionsClient, fallback
from civ_agent.protocol import JsonObject
from civ_agent.providers import ProviderError, read_api_key
from civ_agent.tls import private_directory, write_private_json

BROKER_PROTOCOL_VERSION = 4
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
_HEALTH_VERSIONS = {
    "protocolVersion": BROKER_PROTOCOL_VERSION,
    "classifierModel": MODEL,
    "routingPolicyVersion": POLICY_VERSION,
    "advisoryPolicyVersion": TASK_VERSION,
    "accountingRateVersion": RATE_VERSION,
}
_VERSION_PATTERNS = {
    "classifierModel": r"(?:gpt-[1-9][0-9]{0,2}(?:\.[0-9]{1,2})?-(?:luna|sol|terra)|Winnow-12B)",
    "routingPolicyVersion": r"diplomacy-routing-v[1-9][0-9]{0,2}",
    "advisoryPolicyVersion": r"player-advisories-v[1-9][0-9]{0,2}",
    "accountingRateVersion": r"decisions-(?:backends-v[1-9][0-9]{0,2}|standard-[0-9]{4}-[0-9]{2}-[0-9]{2}-v[1-9][0-9]{0,2})",
}


def version_contract(backend=DEFAULT_BACKEND):
    return {**_HEALTH_VERSIONS, "classifierModel": BACKENDS[backend].model}


def health_failure(body, backend=DEFAULT_BACKEND):
    """Return version diagnostics without reflecting an untrusted health body."""
    mismatches = {}
    if isinstance(body, dict):
        for field, expected in version_contract(backend).items():
            if field not in body or body[field] == expected and type(body[field]) is type(expected):
                continue
            actual = body[field]
            safe = (
                type(actual) is int and 0 <= actual <= 999
                if field == "protocolVersion"
                else isinstance(actual, str) and re.fullmatch(_VERSION_PATTERNS[field], actual) is not None
            )
            mismatches[field] = {"expected": expected, "actual": actual if safe else None}
    result: JsonObject = {
        "status": "incompatible",
        "reason": "broker_version_mismatch" if mismatches else "invalid_health_contract",
        "action": "rebuild_runtime_image",
    }
    if mismatches:
        result["versionMismatches"] = mismatches
    return result


def _valid_health(body, backend=DEFAULT_BACKEND):
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
        or body["classifierModel"] != BACKENDS[backend].model
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
        self.client = client
        self.clients = {}
        self.ledger = DecisionsLedger(self.root)
        self.locks = {}

    def _client(self, backend=DEFAULT_BACKEND):
        if self.client is not None:
            return self.client
        if backend not in self.clients:
            key = read_decisions_key(self.root / "decisions.key") if backend == "luna" else None
            self.clients[backend] = DecisionsClient(key, backend=backend)
        return self.clients[backend]

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
        principal = self._authenticate(request)
        try:
            backend = self.ledger.settings(principal).decisions_backend
        except ValueError:
            backend = DEFAULT_BACKEND
        spec = version_contract(backend)
        return web.json_response(
            {
                **spec,
                "status": "ready",
                **self._client(backend).health_snapshot(),
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
                raise web.HTTPForbidden(text="Current Decisions account settings are required") from None
            if not policy.decisions_enabled:
                raise web.HTTPForbidden(text="Decisions is disabled for this account")
            options = {
                "timeout": policy.decisions_timeout_seconds,
                "thresholds": routing_thresholds(policy, body["direction"]),
                "policy_version": policy.decisions_routing_policy,
            }
            return web.json_response(
                await self._client(policy.decisions_backend).classify(body["direction"], body["bundle"], **options)
            )

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
            if envelope["requestId"] != request_id(evidence, model=BACKENDS[settings.decisions_backend].model):
                raise ValueError("Invalid stable request ID")
            body = request_body(evidence, settings)
        except (ValueError, TypeError, KeyError, UnicodeError):
            raise web.HTTPBadRequest() from None
        metadata = request_metadata(body, evidence)
        from civ_agent.decisions_tasks import acceptance_threshold

        threshold = acceptance_threshold(evidence, settings)
        pending = {
            **metadata,
            **failed_answers(body, threshold, "completion_unknown"),
            "replayEvidence": json.loads(body["input"]),
        }
        async with self.locks.setdefault(principal, asyncio.Lock()):
            cached = self.ledger.reserve(
                principal, envelope["requestId"], metadata["inputHash"], body, pending_result=pending
            )
            if cached is not None:
                return web.json_response(self._reply(principal, cached))
            # No provider retries. A cancelled/unknown call retains its reservation.
            raw = await self._client(settings.decisions_backend).evaluate(
                body, timeout=settings.decisions_timeout_seconds
            )
            parsed = (
                failed_answers(body, threshold, raw["fallback"])
                if raw.get("fallback")
                else parse_answers(body, raw, threshold)
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
        if result.get("fallback") != "not_submitted" and (
            result.get("policyVersion") != TASK_VERSION
            or result.get("model") != BACKENDS[self.ledger.settings(principal).decisions_backend].model
            or not isinstance(result.get("questionResults"), list)
        ):
            raise web.HTTPConflict(text="Incompatible Decisions receipt; its reservation remains retained")
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
    def __init__(self, socket_path, token_file, *, backend=DEFAULT_BACKEND, timeout=120):
        self.socket_path, self.token_file = str(socket_path), Path(token_file)
        self.backend, self.timeout = backend, timeout
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
            raise ValueError("Decisions requires an authenticated trusted broker")
        return cls(socket, token, backend=os.environ.get("CIV_DECISIONS_BACKEND", DEFAULT_BACKEND))

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
                if isinstance(body, dict) and _valid_health(body, self.backend)
                else health_failure(body, self.backend)
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
                connector=aiohttp.UnixConnector(path=self.socket_path),
                timeout=aiohttp.ClientTimeout(total=self.timeout + 1),
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

    async def batch(self, evidence, *, timeout=120) -> JsonObject:
        stable_id = request_id(evidence, model=BACKENDS[self.backend].model)
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
