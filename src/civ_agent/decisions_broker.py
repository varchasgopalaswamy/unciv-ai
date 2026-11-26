"""Trusted authenticated local socket; the API key never enters a player container."""

import asyncio
import hashlib
import hmac
import json
import os
import re
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import aiohttp
from aiohttp import web
from filelock import AsyncFileLock

from civ_agent.advisory_answers import failed_answers
from civ_agent.decisions_backends import BACKENDS, DEFAULT_BACKEND, DecisionBackend
from civ_agent.decisions_ledger import (
    RATE_VERSION,
    DecisionsLedger,
    NativeAuxiliaryTaskUnavailable,
    NativeBrokerEpochFenced,
    fingerprint,
)
from civ_agent.decisions_policy import routing_thresholds
from civ_agent.decisions_tasks import TASK_VERSION, parse_answers, request_body, request_id, request_metadata
from civ_agent.diplomacy_decisions import MODEL, POLICY_VERSION, DecisionsClient, fallback
from civ_agent.diplomacy_decisions import request_body as routing_body
from civ_agent.native_broker_contracts import CloseNativeBrokerEpoch, NativeBrokerEpochClosed, NativeBrokerScope
from civ_agent.orchestration.contracts import NativeAuxiliaryTask, Scope
from civ_agent.orchestration.provider_contracts import ProviderError
from civ_agent.orchestration.routing_contracts import (
    ROUTING_VERSION,
    RoutingSubmission,
    routing_metadata,
    routing_request_id,
    routing_submission,
    validate_routing_reply,
)
from civ_agent.protocol import JsonObject
from civ_agent.provider_credentials import read_api_key
from civ_agent.tls import private_directory, regular_file, write_private_json

BROKER_PROTOCOL_VERSION = 6
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
        self.epoch_locks = {}

    def _legacy_admission(self, principal):
        if self.ledger.native_owner(principal) is not None:
            raise web.HTTPForbidden(text="Native classifier scope required")

    @asynccontextmanager
    async def _provider_admission(self, principal, scope: NativeBrokerScope | None):
        if scope is None:
            yield
            return
        key = (principal, scope.epoch)
        directory = private_directory(self.root / "native-epoch-locks")
        path = directory / (fingerprint([principal, scope.model_dump(mode="json")]) + ".lock")
        if path.exists() or path.is_symlink():
            regular_file(path, private=True)
        async with (
            self.epoch_locks.setdefault(key, asyncio.Lock()),
            AsyncFileLock(path, mode=0o600, run_in_executor=False, fallback_to_soft=False),
        ):
            yield

    def _client(self, backend: DecisionBackend = DEFAULT_BACKEND):
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
        self._legacy_admission(digest)
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
            self._legacy_admission(digest)
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
        return await self._batch(request)

    async def native_batch(self, request):
        return await self._batch(request, native=True)

    async def _batch(self, request, *, native=False):
        principal = self._authenticate(request)
        if not native:
            self._legacy_admission(principal)
        try:
            settings = self.ledger.settings(principal)
        except ValueError:
            raise web.HTTPForbidden() from None
        if not settings.decisions_enabled:
            raise web.HTTPForbidden()
        try:
            envelope = await request.json()
            if not isinstance(envelope, dict) or set(envelope) != (
                {"scope", "task", "requestId", "evidence"} if native else {"requestId", "evidence"}
            ):
                raise ValueError("Invalid envelope")
            scope = NativeBrokerScope.model_validate(envelope["scope"]) if native else None
            task = NativeAuxiliaryTask.model_validate(envelope["task"]) if native else None
            if task is not None and task.kind not in {"effort_selection", "military_triage"}:
                raise ValueError("Invalid advisory task")
            if scope is not None:
                self.ledger.check_native_scope(principal, scope)
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
        async with self.locks.setdefault(principal, asyncio.Lock()), self._provider_admission(principal, scope):
            try:
                cached = self.ledger.reserve(
                    principal,
                    envelope["requestId"],
                    metadata["inputHash"],
                    body,
                    pending_result=pending,
                    native_scope=scope,
                    native_task=task,
                )
            except NativeBrokerEpochFenced:
                return web.json_response(
                    self._reply(principal, {**metadata, **failed_answers(body, threshold, "source_epoch_closed")})
                )
            except NativeAuxiliaryTaskUnavailable as error:
                return web.json_response(
                    self._reply(principal, {**metadata, **failed_answers(body, threshold, error.reason)})
                )
            if cached is not None:
                return web.json_response(self._reply(principal, cached))
            if scope is not None:
                try:
                    self.ledger.native_dispatch_open(principal, scope)
                except NativeBrokerEpochFenced:
                    result = {**metadata, **failed_answers(body, threshold, "source_epoch_closed")}
                    self.ledger.complete(principal, envelope["requestId"], result)
                    return web.json_response(self._reply(principal, result))
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

    async def route(self, request):
        return await self._route(request)

    async def native_route(self, request):
        return await self._route(request, native=True)

    async def _route(self, request, *, native=False):
        principal = self._authenticate(request)
        if not native:
            self._legacy_admission(principal)
        try:
            settings = self.ledger.settings(principal)
        except ValueError:
            raise web.HTTPForbidden() from None
        if not settings.decisions_enabled:
            raise web.HTTPForbidden()
        try:
            supplied = await request.json()
            scope = None
            task = None
            if native:
                if not isinstance(supplied, dict) or set(supplied) != {"scope", "task", "submission"}:
                    raise ValueError("Invalid envelope")
                scope = NativeBrokerScope.model_validate(supplied["scope"])
                task = NativeAuxiliaryTask.model_validate(supplied["task"])
                self.ledger.check_native_scope(principal, scope)
                supplied = supplied["submission"]
            submission = RoutingSubmission.model_validate(supplied)
            if task is not None and task.kind != submission.direction + "_routing":
                raise ValueError("Invalid routing task")
            if submission.bundle.get("player") != self.ledger.player(
                principal
            ) or submission.requestId != routing_request_id(
                submission.operationId, submission.direction, submission.bundle, settings.decisions_backend
            ):
                raise ValueError("Invalid routing perspective or identity")
            body = routing_body(submission.direction, submission.bundle, backend=settings.decisions_backend)
            metadata = routing_metadata(submission, settings)
        except (ValueError, TypeError, KeyError, UnicodeError):
            raise web.HTTPBadRequest() from None
        pending = {
            **metadata,
            **fallback(submission.direction, "completion_unknown"),
            "usage": {},
            "replayEvidence": submission.bundle,
        }
        async with self.locks.setdefault(principal, asyncio.Lock()), self._provider_admission(principal, scope):
            try:
                cached = self.ledger.routing_reserve(
                    principal,
                    submission.operationId,
                    submission.requestId,
                    body,
                    pending,
                    native_scope=scope,
                    native_task=task,
                )
            except NativeBrokerEpochFenced:
                return web.json_response(
                    self._routing_reply(principal, {**pending, **fallback(submission.direction, "source_epoch_closed")})
                )
            except NativeAuxiliaryTaskUnavailable as error:
                return web.json_response(
                    self._routing_reply(principal, {**pending, **fallback(submission.direction, error.reason)})
                )
            except ValueError:
                raise web.HTTPConflict(text="Routing immutable operation conflict") from None
            if cached is not None:
                return web.json_response(self._routing_reply(principal, cached))
            if scope is not None:
                try:
                    self.ledger.native_dispatch_open(principal, scope)
                except NativeBrokerEpochFenced:
                    result = {**pending, **fallback(submission.direction, "source_epoch_closed")}
                    self.ledger.routing_complete(principal, submission.requestId, result)
                    return web.json_response(self._routing_reply(principal, result))
            raw = await self._client(settings.decisions_backend).classify(
                submission.direction,
                submission.bundle,
                timeout=settings.decisions_timeout_seconds,
                thresholds=metadata["thresholds"],
                policy_version=settings.decisions_routing_policy,
            )
            if any(
                key in raw and raw[key] != metadata[key]
                for key in ("model", "policyVersion", "inputHash", "promptHash", "contextSchemaVersion")
            ):
                result = {**metadata, **fallback(submission.direction, "malformed_broker_output")}
            else:
                result = validate_routing_reply(submission, {**raw, **metadata}, settings)
            # Usage settles the one recorded attempt even if its answers fail
            # validation. No caller can resubmit it under a new operation body.
            result = {**result, "usage": raw.get("usage", {}), "replayEvidence": submission.bundle}
            self.ledger.routing_complete(principal, submission.requestId, result)
            return web.json_response(self._routing_reply(principal, result))

    def _routing_reply(self, principal, result):
        if result.get("fallback") == "not_submitted":
            return {**result, "version": ROUTING_VERSION, "routingAccounting": self.ledger.routing_status(principal)}
        try:
            submission = RoutingSubmission(
                requestId=result["requestId"],
                operationId=result["operationId"],
                direction=result["direction"],
                bundle=result["replayEvidence"],
            )
            metadata = routing_metadata(submission, self.ledger.settings(principal))
            if any(result.get(key) != value for key, value in metadata.items()):
                raise ValueError("Routing receipt binding conflict")
        except (ValueError, TypeError, KeyError):
            raise web.HTTPConflict(text="Incompatible routing receipt") from None
        return {
            **{
                key: value
                for key, value in result.items()
                if key not in {"pending", "replayEvidence", "nativeScope", "nativeTask"}
            },
            "routingAccounting": self.ledger.routing_status(principal),
        }

    async def lookup_route(self, request):
        principal = self._authenticate(request)
        identifier = request.match_info["request_id"]
        if re.fullmatch(r"[a-f0-9]{64}", identifier) is None:
            raise web.HTTPBadRequest()
        try:
            settings = self.ledger.settings(principal)
        except ValueError:
            raise web.HTTPForbidden() from None
        if not settings.decisions_enabled:
            raise web.HTTPForbidden()
        return web.json_response(self._routing_reply(principal, self.ledger.routing_lookup(principal, identifier)))

    def _reply(self, principal, result):
        if result.get("fallback") != "not_submitted" and (
            result.get("policyVersion") != TASK_VERSION
            or result.get("model") != BACKENDS[self.ledger.settings(principal).decisions_backend].model
            or not isinstance(result.get("questionResults"), list)
        ):
            raise web.HTTPConflict(text="Incompatible Decisions receipt; its reservation remains retained")
        return {
            **{k: v for k, v in result.items() if k not in {"replayEvidence", "pending", "nativeScope", "nativeTask"}},
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

    async def close_native_epoch(self, request):
        principal = self._authenticate(request)
        try:
            command = CloseNativeBrokerEpoch.model_validate(await request.json())
        except (ValueError, TypeError, UnicodeError):
            raise web.HTTPBadRequest() from None
        try:
            # Fence immediately, without waiting for the accepted provider lock.
            self.ledger.close_native_epoch(principal, command)
        except ValueError:
            raise web.HTTPConflict(text="Native classifier epoch owner conflict") from None
        async with self._provider_admission(principal, command.scope):
            result = self.ledger.native_epoch_status(principal, command)
        return web.json_response(result.model_dump(mode="json"))

    def application(self):
        app = web.Application(client_max_size=256 * 1024)
        app.router.add_get("/health", self.health)
        app.router.add_post("/classify", self.classify)
        app.router.add_post("/batch", self.batch)
        app.router.add_get("/batch/{request_id}", self.lookup)
        app.router.add_post("/routing", self.route)
        app.router.add_get("/routing/{request_id}", self.lookup_route)
        app.router.add_post("/native/batch", self.native_batch)
        app.router.add_post("/native/routing", self.native_route)
        app.router.add_post("/native/epochs/close", self.close_native_epoch)
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

    async def batch(
        self, evidence, *, timeout=120, scope: Scope | None = None, task: NativeAuxiliaryTask | None = None
    ) -> JsonObject:
        stable_id = request_id(evidence, model=BACKENDS[self.backend].model)
        envelope = {"requestId": stable_id, "evidence": evidence}
        if scope is not None:
            envelope["scope"] = NativeBrokerScope.from_scope(scope).model_dump(mode="json")
            envelope["task"] = task.model_dump(mode="json") if task is not None else None
        try:
            async with aiohttp.ClientSession(
                connector=aiohttp.UnixConnector(path=self.socket_path),
                timeout=aiohttp.ClientTimeout(total=timeout + 1),
            ) as session:
                async with session.post(
                    "http://localhost/native/batch" if scope is not None else "http://localhost/batch",
                    headers={"Authorization": "Bearer " + self._token()},
                    json=envelope,
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

    async def route(
        self,
        operation_id,
        direction,
        bundle,
        *,
        timeout=120,
        scope: Scope | None = None,
        task: NativeAuxiliaryTask | None = None,
    ) -> JsonObject:
        submission = routing_submission(operation_id, direction, bundle, self.backend)
        payload = submission.model_dump(mode="json")
        if scope is not None:
            payload = {
                "scope": NativeBrokerScope.from_scope(scope).model_dump(mode="json"),
                "task": task.model_dump(mode="json") if task is not None else None,
                "submission": payload,
            }
        return await self._routing_exchange(
            "/native/routing" if scope is not None else "/routing",
            submission.requestId,
            timeout=timeout + 1,
            payload=payload,
        )

    async def close_epoch(self, command: CloseNativeBrokerEpoch, *, timeout=120) -> NativeBrokerEpochClosed:
        command = CloseNativeBrokerEpoch.model_validate_json(command.model_dump_json())
        async with aiohttp.ClientSession(
            connector=aiohttp.UnixConnector(path=self.socket_path), timeout=aiohttp.ClientTimeout(total=timeout)
        ) as session:
            async with session.post(
                "http://localhost/native/epochs/close",
                headers={"Authorization": "Bearer " + self._token()},
                json=command.model_dump(mode="json"),
                allow_redirects=False,
            ) as response:
                response.raise_for_status()
                result = NativeBrokerEpochClosed.model_validate(await response.json())
        if result.scope != command.scope or result.owner_sha256 != fingerprint(command.owner.model_dump(mode="json")):
            raise ValueError("Native classifier close response conflict")
        return result

    async def lookup_route(self, identifier, *, timeout=5) -> JsonObject:
        if not isinstance(identifier, str) or re.fullmatch(r"[a-f0-9]{64}", identifier) is None:
            raise ValueError("Invalid routing request ID")
        return await self._routing_exchange("/routing/" + identifier, identifier, timeout=timeout)

    async def _routing_exchange(self, path, identifier, *, timeout, payload=None) -> JsonObject:
        try:
            async with aiohttp.ClientSession(
                connector=aiohttp.UnixConnector(path=self.socket_path), timeout=aiohttp.ClientTimeout(total=timeout)
            ) as session:
                async with session.request(
                    "POST" if payload is not None else "GET",
                    "http://localhost" + path,
                    headers={"Authorization": "Bearer " + self._token()},
                    json=payload,
                    allow_redirects=False,
                ) as response:
                    response.raise_for_status()
                    result = await response.json()
            if (
                not isinstance(result, dict)
                or result.get("version") != ROUTING_VERSION
                or result.get("requestId") != identifier
            ):
                raise ValueError("Invalid routing broker reply")
            return result
        except (TimeoutError, OSError, ValueError, aiohttp.ClientError):
            return {"version": ROUTING_VERSION, "requestId": identifier, "fallback": "broker_unavailable"}


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
