"""One civilization's HTTPS control service and private Unix-socket Codex client.

Codex app-server uses WebSocket frames over a private Unix socket. Standard streams
are never used as a control protocol, and no unencrypted network listener is opened.
"""

import asyncio
import contextlib
import copy
import ctypes
import fcntl
import functools
import hashlib
import hmac
import json
import os
import signal
import ssl
import tempfile
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import aiohttp
from aiohttp import web
from pydantic import ValidationError
from sqlalchemy.exc import SQLAlchemyError

from civ_agent import service_recovery
from civ_agent.agent_history import HISTORY_METHODS, HistoryStore
from civ_agent.agent_results import PlayerResultStore, ResultError, validate_result
from civ_agent.agent_settings import (
    codex_thread_settings,
    player_mcp_configuration,
    player_tool_arguments,
    scheduled_prompt_arguments,
    valid_setting,
)
from civ_agent.chat_runtime import ChatRuntime
from civ_agent.civilization_guide import civilization_reference
from civ_agent.civilization_state import CivilizationState, StateConflict
from civ_agent.coordination_updates import context_compacted
from civ_agent.diagnostics import Diagnostics, codex_request_error
from civ_agent.host_connection import HostConnection
from civ_agent.llm_metrics import LlmMetricsCollector, record_llm_metrics
from civ_agent.model_status import LogLines, ModelStatus, quota_failure
from civ_agent.proposal_gateway import ProposalGateway
from civ_agent.protocol import AgentProcess, JsonObject, Notification
from civ_agent.provider_usage import ProviderUsageReader, reconcile_provider_usage
from civ_agent.providers import (
    PROVIDERS,
    ProviderError,
    openrouter_model_arguments,
    provider_arguments,
    provider_environment,
)
from civ_agent.review_output_guard import ReviewOutputGuard
from civ_agent.review_runtime import ReviewRuntime
from civ_agent.schemas import StartRequest, start_error
from civ_agent.service_io import ServiceIO
from civ_agent.startup_context import prepare_startup, startup_prompt
from civ_agent.telemetry import JobAccounting, merge_model_usage
from civ_agent.tls import codex_trust_environment, https_url, private_directory, regular_file, write_private_json
from civ_agent.turn_archive import TurnArchive

TERMINAL = {"COMPLETED", "INTERRUPTED", "FAILED"}
TELEMETRY_SAMPLE_INTERVAL = 2.0
JOB_PERMIT_RENEW_INTERVAL = 5.0
MODEL_INTERRUPT_TIMEOUT = 30.0
CODEX_NOTIFICATION_BYTES = 16 * 1024 * 1024
CODEX_NOTIFICATION_COUNT = 4096
CODEX_NOTIFICATION_FLUSH_SECONDS = 30.0
CODEX_STREAM_BATCH_BYTES = 8 * 1024
CODEX_TEXT_DELTAS = frozenset(
    {"item/agentMessage/delta", "item/reasoning/summaryTextDelta", "item/commandExecution/outputDelta"}
)
USAGE_KEYS = {
    "input_tokens": "inputTokens",
    "cached_input_tokens": "cachedInputTokens",
    "output_tokens": "outputTokens",
    "reasoning_output_tokens": "reasoningOutputTokens",
}


class ServiceError(Exception):
    def __init__(self, code: str, status: int = 400) -> None:
        self.code, self.status = code, status
        super().__init__(code)


class ProcessDrainError(ServiceError):
    """Inference exited, but accepted notifications still belong to this process."""

    process_exited = True

    def __init__(self) -> None:
        super().__init__("CODEX_NOTIFICATION_FLUSH_TIMEOUT", 503)


@dataclass
class QueuedNotification:
    method: str
    params: JsonObject
    size: int
    queued: float


def identifier(value):
    return isinstance(value, str) and 1 <= len(value) <= 128 and all(32 < ord(c) < 127 for c in value)


def parent_death_signal(parent):
    """Linux: prevent an orphaned app-server from surviving abrupt service death."""
    if ctypes.CDLL(None, use_errno=True).prctl(1, signal.SIGKILL) != 0:
        raise OSError("Cannot set app-server parent-death signal")
    if os.getppid() != parent:
        os.kill(os.getpid(), signal.SIGKILL)


class CodexProcess:
    """JSON-RPC over native app-server WebSocket-over-Unix, never stdio."""

    def __init__(
        self, executable: str, workspace: Path, mcp_url: str, ca_file: Path | None, notification: Notification
    ) -> None:
        self.executable, self.workspace, self.mcp_url = executable, Path(workspace), mcp_url
        self.ca_file, self.notification = ca_file, notification
        self.process: asyncio.subprocess.Process | None = None
        self.session: aiohttp.ClientSession | None = None
        self.socket: aiohttp.ClientWebSocketResponse | None = None
        self.reader_task: asyncio.Task[None] | None = None
        self.runtime_task: asyncio.Task[None] | None = None
        self.notification_task: asyncio.Task[None] | None = None
        self.transport_failure_task: asyncio.Task[None] | None = None
        self.auth_task: asyncio.Task[None] | None = None
        self.notifications: asyncio.Queue[QueuedNotification | None] = asyncio.Queue()
        self.notification_tail: QueuedNotification | None = None
        self.notification_bytes = self.notification_count = 0
        self.notification_peak_bytes = self.notification_peak_count = 0
        self.notification_delivered = self.notification_failures = 0
        self.notification_coalesced = 0
        self.notification_lag = self.notification_duration = 0.0
        self.notifications_ended = False
        self.transport_error: str | None = None
        self.model_status = ModelStatus()
        self.closing = False
        self.temporary: tempfile.TemporaryDirectory[str] | None = None
        self.pending: dict[int, asyncio.Future[JsonObject]] = {}
        self.requests: dict[int, tuple[str, JsonObject]] = {}
        self.sequence = 0
        self.auth_client = None
        self.output_guard: ReviewOutputGuard | None = None
        self.metrics = LlmMetricsCollector(notification)

    @property
    def process_exited(self) -> bool:
        return self.process is None or self.process.returncode is not None

    @property
    def notification_drained(self) -> bool:
        return self.notifications_ended and self.notification_task is not None and self.notification_task.done()

    async def start(self, token: str, settings: JsonObject) -> None:
        provider = settings["provider"]
        if provider == "openai" and os.environ.get("CIV_AUTH_SOCKET") and os.environ.get("CIV_AUTH_TOKEN_FILE"):
            from civ_agent.container_auth import TokenClient

            self.auth_client = TokenClient(os.environ["CIV_AUTH_SOCKET"], os.environ["CIV_AUTH_TOKEN_FILE"])
        self.temporary = tempfile.TemporaryDirectory(prefix="unciv-agent-ipc-")
        socket_path = Path(self.temporary.name) / "codex.sock"
        try:
            environment = provider_environment(
                codex_trust_environment(self.ca_file, os.environ, self.workspace), provider
            )
            provider_config = provider_arguments(provider)
            if provider == "openrouter":
                provider_config += await openrouter_model_arguments(
                    settings["model"],
                    settings.get("reasoningEffort"),
                    environment.get("CODEX_HOME", self.workspace.parent / ".provider-cache"),
                )
        except ProviderError as error:
            raise ServiceError(error.code) from None
        for name in list(environment):
            if name.startswith(("UNCIV_", "CIV_AUTH_")):
                del environment[name]
        environment["UNCIV_PLAYER_TOKEN"] = token
        # Some automatic provider retries have no app-server notification. Only
        # enable their warning target; never copy arbitrary child logs to history.
        environment["RUST_LOG"] = "off,codex_core::responses_retry=warn"
        # Periodic export also retains timings when an interrupted runner cannot
        # flush on exit. This overrides only this child's export interval.
        environment["OTEL_METRIC_EXPORT_INTERVAL"] = "1000"
        await self.metrics.start()
        config = player_mcp_configuration(self.mcp_url, tool_timeout_seconds=86400)
        # Models use player tools and private memory tools. Disable general host
        # inspection/execution surfaces in these narrowly scoped jobs.
        arguments = [
            self.executable,
            "-c",
            config,
            *provider_config,
            *self.metrics.arguments(),
            *player_tool_arguments(),
            *scheduled_prompt_arguments(),
            "app-server",
            "--listen",
            "unix://" + str(socket_path),
        ]
        self.process = await asyncio.create_subprocess_exec(
            *arguments,
            cwd=self.workspace,
            env=environment,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
            preexec_fn=functools.partial(parent_death_signal, os.getpid()),
        )
        self.runtime_task = asyncio.create_task(self._runtime())
        deadline = time.monotonic() + 15
        while not socket_path.exists():
            if self.process.returncode is not None or time.monotonic() >= deadline:
                raise ServiceError("CODEX_START_FAILED", 503)
            await asyncio.sleep(0.05)
        self.session = aiohttp.ClientSession(connector=aiohttp.UnixConnector(path=str(socket_path)))
        # The HTTP-shaped URL supplies WebSocket framing only. UnixConnector never opens TCP.
        self.socket = await self.session.ws_connect("http://localhost/", max_msg_size=8 * 1024 * 1024)
        self.reader_task = asyncio.create_task(self._read())
        await self.request(
            "initialize",
            {"clientInfo": {"name": "civ-agent-service", "version": "1"}, "capabilities": {"experimentalApi": True}},
        )
        await self.socket.send_json({"method": "initialized", "params": {}})
        if self.auth_client is not None:
            await self.request("account/login/start", {"type": "chatgptAuthTokens", **await self.auth_client.fetch()})

    async def request(self, method: str, parameters: JsonObject) -> JsonObject:
        assert self.socket is not None
        if self.transport_error is not None or self.notifications_ended:
            raise ServiceError(self.transport_error or "CODEX_DISCONNECTED", 502)
        self.sequence += 1
        request_id = self.sequence
        future: asyncio.Future[JsonObject] = asyncio.get_running_loop().create_future()
        self.pending[request_id] = future
        self.requests[request_id] = method, parameters
        if method == "turn/start":
            self.output_guard = (
                ReviewOutputGuard(parameters["threadId"]) if parameters.get("outputSchema") is not None else None
            )
        try:
            await self.socket.send_json({"id": request_id, "method": method, "params": parameters})
            return await asyncio.wait_for(future, 30)
        except TimeoutError as error:
            raise TimeoutError(f"Codex {method} did not respond within 30 seconds") from error
        finally:
            self.pending.pop(request_id, None)
            self.requests.pop(request_id, None)

    async def _model_notice(self, notice: JsonObject | None) -> None:
        if notice is not None and not self.closing:
            self._queue_notification("service/modelStatus", notice)

    def _transport_failed(self, reason: str) -> None:
        if self.transport_error is None:
            self.transport_error = reason
            # Failure control bypasses the blocked data callback. The caller
            # stops inference, then close() still flushes accepted events.
            self.transport_failure_task = asyncio.create_task(self._report_transport_failure())
        for future in list(self.pending.values()):
            if not future.done():
                future.set_exception(ServiceError(self.transport_error, 502))
        if self.reader_task is not None and self.reader_task is not asyncio.current_task():
            self.reader_task.cancel()

    async def _report_transport_failure(self) -> None:
        with contextlib.suppress(Exception):
            await self.notification("service/transportFailure", {"reason": self.transport_error})

    def _queue_notification(self, method: str, params: JsonObject) -> bool:
        """Never make socket reads wait for history/accounting writes.

        Count the in-flight callback too. Overflow is an explicit transport
        failure, rather than dropping a usage, tool or completion event.
        """
        if self.transport_error is not None or self.notifications_ended:
            return False
        size = len(json.dumps(params, ensure_ascii=False).encode("utf-8")) + len(method) + 64
        tail = self.notification_tail
        if (
            method in CODEX_TEXT_DELTAS
            and isinstance(params.get("delta"), str)
            and tail is not None
            and tail.method == method
            and isinstance(tail.params.get("delta"), str)
            and {key: value for key, value in params.items() if key != "delta"}
            == {key: value for key, value in tail.params.items() if key != "delta"}
        ):
            combined = {**params, "delta": tail.params["delta"] + params["delta"]}
            combined_size = len(json.dumps(combined, ensure_ascii=False).encode("utf-8")) + len(method) + 64
            if combined_size <= CODEX_STREAM_BATCH_BYTES:
                extra = combined_size - tail.size
                if self.notification_bytes + extra > CODEX_NOTIFICATION_BYTES:
                    self._transport_failed("CODEX_NOTIFICATION_OVERFLOW")
                    return False
                tail.params = combined
                tail.size = combined_size
                self.notification_bytes += extra
                self.notification_peak_bytes = max(self.notification_peak_bytes, self.notification_bytes)
                self.notification_coalesced += 1
                return True
        if (
            self.notification_bytes + size > CODEX_NOTIFICATION_BYTES
            or self.notification_count >= CODEX_NOTIFICATION_COUNT
        ):
            self._transport_failed("CODEX_NOTIFICATION_OVERFLOW")
            return False
        self.notification_bytes += size
        self.notification_count += 1
        self.notification_peak_bytes = max(self.notification_peak_bytes, self.notification_bytes)
        self.notification_peak_count = max(self.notification_peak_count, self.notification_count)
        item = QueuedNotification(method, params, size, time.monotonic())
        self.notification_tail = item
        self.notifications.put_nowait(item)
        if self.notification_task is None:
            self.notification_task = asyncio.create_task(self._deliver_notifications())
        return True

    def _end_notifications(self) -> None:
        if not self.notifications_ended:
            self.notifications_ended = True
            self.notifications.put_nowait(None)
            if self.notification_task is None:
                self.notification_task = asyncio.create_task(self._deliver_notifications())

    async def _deliver_notifications(self) -> None:
        while (item := await self.notifications.get()) is not None:
            if self.notification_tail is item:
                # Never mutate an event already being delivered to a callback.
                self.notification_tail = None
            method, params, size, queued = item.method, item.params, item.size, item.queued
            started = time.monotonic()
            self.notification_lag = max(self.notification_lag, started - queued)
            try:
                await self.notification(method, params)
                self.notification_delivered += 1
            except Exception:
                # Stop inference, but still attempt the other accepted events.
                # A callback failure must never look like successful completion.
                self.notification_failures += 1
                self._transport_failed("CODEX_NOTIFICATION_FAILED")
            finally:
                self.notification_duration = max(self.notification_duration, time.monotonic() - started)
                self.notification_bytes -= size
                self.notification_count -= 1
        metrics: JsonObject = {
            "peakQueuedBytes": self.notification_peak_bytes,
            "peakQueuedNotifications": self.notification_peak_count,
            "deliveredNotifications": self.notification_delivered,
            "failedNotifications": self.notification_failures,
            "coalescedNotifications": self.notification_coalesced,
            "maxQueueLagMs": round(self.notification_lag * 1000),
            "maxCallbackMs": round(self.notification_duration * 1000),
        }
        if self.transport_error is not None:
            metrics["reason"] = self.transport_error
        try:
            await self.notification("service/transportStatus", metrics)
        finally:
            await self.notification(
                "service/disconnected", {"reason": self.transport_error} if self.transport_error else {}
            )

    async def _refresh_auth(self, request_id, previous) -> None:
        assert self.socket is not None and self.auth_client is not None
        try:
            tokens = await self.auth_client.fetch(refresh=True)
            if previous is not None and previous != tokens["chatgptAccountId"]:
                raise RuntimeError("Benchmark account changed")
            await self.socket.send_json({"id": request_id, "result": tokens})
        except (OSError, ValueError, RuntimeError, TimeoutError, aiohttp.ClientError):
            try:
                await self.socket.send_json(
                    {"id": request_id, "error": {"code": -32000, "message": "Benchmark login unavailable"}}
                )
            except (OSError, RuntimeError, aiohttp.ClientError):
                self._transport_failed("CODEX_DISCONNECTED")
            self._queue_notification("service/authRequired", {})

    async def _runtime(self) -> None:
        assert self.process is not None and self.process.stderr is not None
        lines = LogLines()
        while True:
            try:
                chunk = await asyncio.wait_for(self.process.stderr.read(4096), 1)
            except TimeoutError:
                chunk = None
            if chunk == b"":
                return
            if chunk:
                for fields in lines.feed(chunk):
                    await self._model_notice(self.model_status.retry(fields, "stderr"))
            await self._model_notice(self.model_status.poll())

    async def _read(self) -> None:
        assert self.socket is not None
        try:
            async for frame in self.socket:
                if frame.type != aiohttp.WSMsgType.TEXT:
                    continue
                message = json.loads(frame.data)
                if not isinstance(message, dict):
                    raise ValueError("Invalid Codex message")
                if "method" in message:
                    if "id" in message:
                        if message["method"] == "account/chatgptAuthTokens/refresh" and self.auth_client is not None:
                            if self.auth_task is not None and not self.auth_task.done():
                                await self.socket.send_json(
                                    {
                                        "id": message["id"],
                                        "error": {"code": -32000, "message": "Benchmark login refresh pending"},
                                    }
                                )
                            else:
                                self.auth_task = asyncio.create_task(
                                    self._refresh_auth(
                                        message["id"], message.get("params", {}).get("previousAccountId")
                                    )
                                )
                            continue
                        # No unattended approvals, user input, or external token requests.
                        await self.socket.send_json(
                            {
                                "id": message["id"],
                                "error": {
                                    "code": -32000,
                                    "message": "Interactive decision requires operator attention",
                                },
                            }
                        )
                        if not self._queue_notification("service/approvalRequired", {}):
                            break
                    else:
                        if self.output_guard is not None:
                            failure = self.output_guard.observe(message["method"], message.get("params", {}))
                            if failure is not None:
                                self._transport_failed(failure)
                                break
                        notice = self.model_status.observe(message["method"], message.get("params", {}))
                        await self._model_notice(notice)
                        if self.transport_error is not None:
                            break
                        if message["method"] in {"error", "warning"}:
                            # The normalized notice replaces arbitrary provider text.
                            continue
                        if not self._queue_notification(message["method"], message.get("params", {})):
                            break
                else:
                    response_id = message.get("id")
                    if not isinstance(response_id, int):
                        continue
                    future = self.pending.get(response_id)
                    if future is not None and not future.done():
                        if "error" in message:
                            method, params = self.requests.get(response_id, ("unknown", {}))
                            detail = codex_request_error(method, response_id, message["error"])
                            for key in ("threadId", "turnId"):
                                if identifier(params.get(key)):
                                    detail[key] = params[key]
                            self._queue_notification("service/requestFailed", detail)
                            if not future.done():
                                future.set_exception(ServiceError("CODEX_REQUEST_FAILED", 502))
                        else:
                            result = message.get("result", {})
                            method, params = self.requests.get(response_id, (None, {}))
                            if method == "turn/start" and identifier(result.get("turn", {}).get("id")):
                                # Bind before consuming following frames, which can
                                # arrive before request()'s caller resumes.
                                self.model_status.begin(params["threadId"], result["turn"]["id"])
                                if self.output_guard is not None:
                                    self.output_guard.bind_turn(result["turn"]["id"])
                            future.set_result(result)
        except (ValueError, OSError, aiohttp.ClientError):
            pass
        finally:
            for future in list(self.pending.values()):
                if not future.done():
                    future.set_exception(ServiceError(self.transport_error or "CODEX_DISCONNECTED", 502))
            self._end_notifications()

    async def close(self) -> None:
        self.closing = True
        if self.process is not None:
            # The process group is dedicated to this activation. Wait for actual exit before
            # publishing a terminal phase, even when turn/interrupt did not answer.
            with contextlib.suppress(ProcessLookupError):
                os.killpg(self.process.pid, signal.SIGTERM)
            try:
                await asyncio.wait_for(self.process.wait(), 2)
            except TimeoutError:
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(self.process.pid, signal.SIGKILL)
                await self.process.wait()
            with contextlib.suppress(ProcessLookupError):
                os.killpg(self.process.pid, signal.SIGKILL)
        if self.runtime_task is not None:
            self.runtime_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self.runtime_task
        if self.auth_task is not None:
            self.auth_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self.auth_task
        if self.reader_task is not None:
            self.reader_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self.reader_task
        await self.metrics.close()
        self._end_notifications()
        # Preserve already received usage/tool/lifecycle events before the
        # caller publishes a terminal record. Keep timed-out workers owned by
        # this process so a later close can finish the flush safely.
        try:
            async with asyncio.timeout(CODEX_NOTIFICATION_FLUSH_SECONDS):
                if self.transport_failure_task is not None:
                    await asyncio.shield(self.transport_failure_task)
                if self.notification_task is not None:
                    await asyncio.shield(self.notification_task)
        except TimeoutError:
            raise ProcessDrainError() from None
        if self.socket is not None:
            await self.socket.close()
        if self.session is not None:
            await self.session.close()
        if self.temporary is not None:
            self.temporary.cleanup()


class AgentService:
    def __init__(
        self,
        civilization: str,
        workspace: Path,
        state_dir: Path,
        token: str,
        mcp_url: str,
        ca_file: Path | None,
        executable: str = "codex",
        process_factory: Callable[[str, Path, str, Path | None, Notification], AgentProcess] = CodexProcess,
        *,
        model: str | None = None,
        provider: str = "openai",
        reasoning_effort: str | None = None,
        communication_reasoning_effort: str | None = None,
        economy_model: JsonObject | None = None,
        economy_interval: int = 5,
        military_model: JsonObject | None = None,
        military_interval: int = 5,
        memory_context_chars: int = 12000,
        review_interval: int = 5,
        review_attempts: int = 3,
        strategy_guide: bool = False,
        civilization_guide: bool = False,
        native_advice_enabled: bool = False,
        decisions_enabled: bool = False,
        fast: bool = False,
        gateway_url: str = "https://localhost:8865/mcp",
        gateway_ca_file: Path | None = None,
        connection_factory=HostConnection,
        sessions_root: Path | None = None,
        **decisions_options,
    ) -> None:
        if model is not None and not valid_setting(model):
            raise ServiceError("INVALID_MODEL")
        if reasoning_effort is not None and not valid_setting(reasoning_effort):
            raise ServiceError("INVALID_REASONING_EFFORT")
        if communication_reasoning_effort is not None and not valid_setting(communication_reasoning_effort):
            raise ServiceError("INVALID_REASONING_EFFORT")
        if provider not in PROVIDERS:
            raise ServiceError("INVALID_PROVIDER")
        if provider == "openrouter" and (model is None or "/" not in model):
            raise ServiceError("OPENROUTER_MODEL_REQUIRED")
        if civilization_guide:
            civilization_reference(civilization)
        self.default_settings = {"provider": provider, "model": model, "reasoningEffort": reasoning_effort}
        self.communication_reasoning_effort = communication_reasoning_effort
        from civ_agent.game_config import EconomyModel

        self.economy_model = EconomyModel.model_validate(economy_model or {}).model_dump(exclude_none=True)
        if type(economy_interval) is not int or economy_interval <= 0:
            raise ServiceError("INVALID_ECONOMY_INTERVAL")
        self.economy_interval = economy_interval
        self.economy_runtime = None
        self.military_model = EconomyModel.model_validate(military_model or {}).model_dump(exclude_none=True)
        if type(military_interval) is not int or military_interval <= 0:
            raise ServiceError("INVALID_MILITARY_INTERVAL")
        self.military_interval = military_interval
        self.military_runtime = None
        from civ_agent.peace_review import PeaceReviews

        self.peace_reviews = PeaceReviews(self)
        self.turn_roles = None
        self.strategy_guide = strategy_guide
        self.civilization_guide = civilization_guide
        from civ_agent.decisions_policy import DecisionsSettings

        self.decisions_settings = DecisionsSettings(
            decisions_enabled=decisions_enabled, memory_context_chars=memory_context_chars, **decisions_options
        )
        self.native_advice_enabled = native_advice_enabled
        self.decisions_enabled = decisions_enabled
        routing_client = None
        if decisions_enabled:
            from civ_agent.decisions_broker import RoutingClient

            routing_client = RoutingClient.from_environment()
            routing_client.backend = self.decisions_settings.decisions_backend
            routing_client.timeout = self.decisions_settings.decisions_timeout_seconds
        self.fast = fast and provider == "openai"
        self.civilization = civilization
        self.workspace, self.state_dir = private_directory(workspace), private_directory(state_dir)
        self.provider_usage = ProviderUsageReader(
            sessions_root or Path(os.environ.get("CODEX_HOME", Path.home() / ".codex")) / "sessions"
        )
        if self.state_dir.resolve().is_relative_to(self.workspace.resolve()):
            raise ServiceError("PRIVATE_STATE_INSIDE_WORKSPACE")
        self.token, self.mcp_url, self.ca_file = token, https_url(mcp_url, ("/mcp",)), ca_file
        self.executable, self.process_factory = executable, process_factory
        self.connection_factory = connection_factory
        self.history = HistoryStore(self.state_dir / "history", token)
        self.io = ServiceIO()
        self.finalization_tasks: dict[str, asyncio.Task] = {}
        self.finalization_errors: dict[str, str] = {}
        self.records_dir = private_directory(self.state_dir / "activations")
        self.lock: int | None = os.open(self.state_dir / ".lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        try:
            fcntl.flock(self.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            os.close(self.lock)
            raise ServiceError("SERVICE_ALREADY_RUNNING", 409) from None
        self.records: dict[str, JsonObject] = {}
        self.quota_failures: dict[str, JsonObject] = {}
        self.known_threads: set[str] = set()
        self.match: JsonObject | None = None
        self.match_file = self.state_dir / "match.json"
        self.results = PlayerResultStore(self.state_dir, self.workspace, self.civilization)
        self.active_task: asyncio.Task[None] | None = None
        self.active_id: str | None = None
        self.process: AgentProcess | None = None
        self.gameplay_permit = None
        self.stop_event = asyncio.Event()
        self.last_heartbeat = 0.0
        self.turn_id: str | None = None
        self.completion: str | None = None
        self.early_completions = {}
        self.early_usage = {}
        self.failure: str | None = None
        self.accept_usage = False
        self.stopping = False
        self.run_started = asyncio.Event()
        self.guard = asyncio.Lock()
        binding = {"civilization": civilization, "workspace": str(self.workspace.resolve()), "mcpUrl": self.mcp_url}
        binding_file = self.state_dir / "binding.json"
        if binding_file.exists():
            regular_file(binding_file, private=True)
            if json.loads(binding_file.read_text()) != binding:
                os.close(self.lock)
                self.lock = None
                raise ServiceError("SERVICE_BINDING_CONFLICT", 409)
        else:
            write_private_json(binding_file, binding)
        self.memory = CivilizationState(self.state_dir / "civilization", civilization, memory_context_chars)
        self.gateway = ProposalGateway(self.memory, self.io, self.workspace)
        self.gateway.peace_reviews = self.peace_reviews
        self.gateway.native_advice_enabled = self.native_advice_enabled
        if routing_client is not None:
            from civ_agent.diplomacy_routing import DiplomacyRouting

            self.memory.routing_enabled = True
            self.gateway.routing = DiplomacyRouting(self.gateway, routing_client, settings=self.decisions_settings)
            from civ_agent.advisory_runtime import AdvisoryRuntime

            self.gateway.advisories = AdvisoryRuntime(self.gateway, routing_client, self.decisions_settings)
        self.gateway.history = self.history
        self.accounting = JobAccounting(self.state_dir / "telemetry", self.memory.memory_edit_counts)
        self.gateway.accounting = self.accounting
        self.telemetry_lock = asyncio.Lock()
        self.gateway.paused = self.memory.is_paused()
        self.gateway_url, self.gateway_ca_file = gateway_url, gateway_ca_file
        if (self.state_dir / "restore.json").exists():
            self.gateway.paused = True
        self.chat = ChatRuntime(self)
        self.reviews = ReviewRuntime(self, review_interval, review_attempts)
        if self.match_file.exists():
            regular_file(self.match_file, private=True)
            self.match = json.loads(self.match_file.read_text())
        for file in sorted(self.records_dir.glob("*.json")):
            regular_file(file, private=True)
            record = json.loads(file.read_text())
            self.records[record["activationId"]] = record
            if record.get("threadId"):
                self.known_threads.add(record["threadId"])
            was_reconciled = record.get("providerUsageReconciled") is True
            reconciled = reconcile_provider_usage(record, self.provider_usage)
            if record["phase"] not in TERMINAL:
                record["phase"], record["error"] = "FAILED", "SERVICE_RESTARTED"
                record["usageComplete"] = reconciled
                if record.pop("roleWaitStartedAtMs", None) is not None:
                    # No live monotonic timer survives a process restart. Keep
                    # recorded durations, without counting downtime as waiting.
                    record["roleWaitIncomplete"] = True
                record.pop("economyWait", None)
                record.pop("roleState", None)
                self._persist(record)
            elif reconciled and not was_reconciled:
                record["usageComplete"] = True
                self._persist(record)
        chat_records = self.state_dir / "chat-jobs"
        if chat_records.is_dir():
            for file in sorted(chat_records.glob("*.json")):
                regular_file(file, private=True)
                record = json.loads(file.read_text())
                was_reconciled = record.get("providerUsageReconciled") is True
                reconciled = reconcile_provider_usage(record, self.provider_usage)
                restarted = record.get("phase") not in TERMINAL
                if restarted:
                    record["phase"], record["error"] = "FAILED", "SERVICE_RESTARTED"
                if reconciled:
                    record["usageComplete"] = True
                if (reconciled and not was_reconciled) or restarted:
                    self.history.lifecycle(record)
                    self.accounting.sample(record)
                    write_private_json(file, record)
        received = self.results.load()
        if received is not None:
            if self.match is None or self.match["epoch"] != received["epoch"]:
                raise ServiceError("RESULT_BINDING_CONFLICT", 409)
            self.results.receive(received)
        self.diagnostics = Diagnostics(self.state_dir, self.civilization)
        self.io.slow_callback = self.diagnostics.emit

    def _persist(self, record):
        self.history.lifecycle(record)
        trace = self.history.review_trace(record["activationId"]) if record.get("purpose") == "review" else None
        self.accounting.sample(
            {**record, "reviewTrace": trace} if trace is not None else record,
            record.get("negotiationWaitSeconds", 0),
        )
        # Commit lifecycle events and accounting before terminal metadata.
        write_private_json(
            self.records_dir / (hashlib.sha256(record["activationId"].encode()).hexdigest() + ".json"), record
        )
        # Persist the directory entry before a newly created thread can be used by turn/start.
        descriptor = os.open(self.records_dir, os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    async def _save(self, record):
        # The worker owns all history access. Pass a detached record so another
        # HTTPS request cannot change an operation already queued for persistence.
        await self.io.call(self._persist, copy.deepcopy(record))

    def _export(self, record):
        if self.match is None:
            raise ServiceError("ARCHIVE_NOT_AVAILABLE", 409)
        archive = TurnArchive(
            self.state_dir / "turn-archive",
            self.workspace,
            self.civilization,
            self.match["epoch"],
            secrets=(self.token,),
        )
        path = self.history.path(record["activationId"]) / "events.jsonl"
        events = []
        if path.exists():
            regular_file(path, private=True)
            events = [json.loads(line) for line in path.read_text().splitlines()]
        # Transport identities and authoritative receipts are controller records,
        # deliberately absent from the model's tool inputs and outputs.
        for request in self.memory.requests_for_job(record["activationId"]):
            item = {"itemId": "host:" + request["id"], "type": "mcpToolCall", "server": "civ", "source": "controller"}
            events.append(
                {
                    "sequence": len(events),
                    "kind": "tool_call",
                    "data": {
                        **item,
                        "tool": request["transport"],
                        "arguments": json.loads(request["payload"]),
                    },
                }
            )
            if request["result"] is not None:
                events.append(
                    {
                        "sequence": len(events),
                        "kind": "tool_result",
                        "data": {
                            **item,
                            "result": {
                                "structuredContent": json.loads(request["result"]),
                                "isError": "error" in json.loads(request["result"]),
                            },
                        },
                    }
                )
        return archive.update(
            record["gameTurn"],
            record["activationId"],
            events,
            phase=record["phase"],
            handoff=record.get("committedHandoff"),
            terminal=record.get("terminalOutcome", False),
            structured_summary=self.memory.summary(record["gameTurn"]),
        )

    async def result(self, payload):
        try:
            validate_result(payload)
            async with self.guard:
                if payload["civilization"] != self.civilization:
                    raise ServiceError("INVALID_BINDING", 409)
                if self.match is not None and payload["epoch"] != self.match["epoch"]:
                    raise ServiceError("WORKSPACE_EPOCH_CONFLICT", 409)
                if self.active_id is not None and self.records[self.active_id]["phase"] not in TERMINAL:
                    raise ServiceError("AGENT_BUSY", 409)
                if self.match is None:
                    self.match = {"epoch": payload["epoch"]}
                    await self.io.call(write_private_json, self.match_file, dict(self.match))
                return await self.io.call(self.results.receive, payload)
        except ResultError as failure:
            raise ServiceError(str(failure), 409 if str(failure) == "RESULT_CONFLICT" else 400) from None

    async def finalize_terminal(self, payload):
        if (
            set(payload) != {"activationId", "epoch", "gameTurn", "handoff"}
            or not identifier(payload.get("activationId"))
            or not identifier(payload.get("epoch"))
            or any(type(payload[key]) is not int or payload[key] < 0 for key in ("gameTurn", "handoff"))
        ):
            raise ServiceError("INVALID_ARGUMENT")
        async with self.guard:
            self.status(payload["activationId"])
            record = self.records[payload["activationId"]]
            if any(payload[key] != record[key] for key in ("epoch", "gameTurn", "handoff")):
                raise ServiceError("INVALID_BINDING", 409)
            if record["phase"] not in TERMINAL:
                raise ServiceError("AGENT_BUSY", 409)
            if "committedHandoff" in record:
                raise ServiceError("FINALIZATION_CONFLICT", 409)
            record["terminalOutcome"] = True
            await self._save(record)
            return self.status(payload["activationId"])

    async def finalize(self, payload):
        if set(payload) != {"activationId", "epoch", "gameTurn", "committedHandoff"}:
            raise ServiceError("INVALID_ARGUMENT")
        if (
            not identifier(payload["activationId"])
            or type(payload["gameTurn"]) is not int
            or type(payload["committedHandoff"]) is not int
        ):
            raise ServiceError("INVALID_ARGUMENT")
        async with self.guard:
            self.status(payload["activationId"])
            record = self.records[payload["activationId"]]
            if (payload["epoch"], payload["gameTurn"]) != (record["epoch"], record["gameTurn"]):
                raise ServiceError("INVALID_BINDING", 409)
            if record["phase"] not in TERMINAL:
                raise ServiceError("AGENT_BUSY", 409)
            if record.get("terminalOutcome"):
                raise ServiceError("FINALIZATION_CONFLICT", 409)
            if payload["committedHandoff"] != record["handoff"] + 1:
                raise ServiceError("INVALID_HANDOFF", 409)
            if record.get("committedHandoff", payload["committedHandoff"]) != payload["committedHandoff"]:
                raise ServiceError("FINALIZATION_CONFLICT", 409)
            record["committedHandoff"] = payload["committedHandoff"]
            if "finalizationFinishedAtMs" not in record:
                await self._save(record)
                finished = {**record, "finalizationFinishedAtMs": int(time.time() * 1000)}
                if "gameplayEndedAtMs" in record:
                    finished["finalizationElapsedMs"] = max(
                        0, finished["finalizationFinishedAtMs"] - record["gameplayEndedAtMs"]
                    )
                # Publish completion only after its marker is durable. A failed
                # save must leave this operation retryable in the same process.
                await self._save(finished)
                record.update(finished)
            # A lost telemetry acknowledgement is independently retryable even
            # when the completion marker was already saved.
            if self.chat.running:
                await self.flush_telemetry()
            return self.status(payload["activationId"])

    def begin_finalize(self, payload):
        """Finish durable accounting independently of the control request."""
        if set(payload) != {"activationId", "epoch", "gameTurn", "committedHandoff"}:
            raise ServiceError("INVALID_ARGUMENT")
        if (
            not identifier(payload["activationId"])
            or not identifier(payload["epoch"])
            or type(payload["gameTurn"]) is not int
            or type(payload["committedHandoff"]) is not int
        ):
            raise ServiceError("INVALID_ARGUMENT")
        self.status(payload["activationId"])
        record = self.records[payload["activationId"]]
        if (payload["epoch"], payload["gameTurn"]) != (record["epoch"], record["gameTurn"]):
            raise ServiceError("INVALID_BINDING", 409)
        if payload["committedHandoff"] != record["handoff"] + 1:
            raise ServiceError("INVALID_HANDOFF", 409)
        if record["phase"] not in TERMINAL:
            raise ServiceError("AGENT_BUSY", 409)
        if record.get("terminalOutcome"):
            raise ServiceError("FINALIZATION_CONFLICT", 409)
        key = record["activationId"]
        task = self.finalization_tasks.get(key)
        if task is None or task.done():
            self.finalization_errors.pop(key, None)

            async def finish():
                try:
                    await self.finalize(payload)
                except (OSError, ValueError, SQLAlchemyError, ServiceError, aiohttp.ClientError) as error:
                    self.finalization_errors[key] = str(error)
                    self.diagnostics.emit("finalization_failed", error=error, activationId=key)

            self.finalization_tasks[key] = asyncio.create_task(finish())
        return self.status(key)

    async def finish_handoff(self, payload):
        """Accept the native handoff and seal gameplay."""
        if set(payload) != {"activationId", "epoch", "gameTurn", "committedHandoff"}:
            raise ServiceError("INVALID_ARGUMENT")
        if (
            not identifier(payload.get("activationId"))
            or type(payload.get("gameTurn")) is not int
            or type(payload.get("committedHandoff")) is not int
        ):
            raise ServiceError("INVALID_ARGUMENT")
        async with self.guard:
            self.status(payload["activationId"])
            record = self.records[payload["activationId"]]
            if (payload["epoch"], payload["gameTurn"]) != (record["epoch"], record["gameTurn"]):
                raise ServiceError("INVALID_BINDING", 409)
            if record.get("terminalOutcome"):
                raise ServiceError("FINALIZATION_CONFLICT", 409)
            if payload["committedHandoff"] != record["handoff"] + 1:
                raise ServiceError("INVALID_HANDOFF", 409)
            # The proof can be repeated after a lost response or service restart.
            if record.get("confirmedHandoff") != payload["committedHandoff"]:
                if self.chat.running:
                    await self.chat.reconcile()
                for job in list(self.gateway.jobs.values()):
                    if (
                        job.id == record["activationId"]
                        or self.records.get(job.id, {}).get("parentActivation") == record["activationId"]
                    ):
                        async with job.lock:
                            job.gameplay_fenced = True
                            if job.handoff_at is None:
                                job.handoff_at = time.monotonic()
                            job.gameplay_ended.set()
            record["confirmedHandoff"] = payload["committedHandoff"]
            if self.turn_roles is not None and self.turn_roles.record["activationId"] == record["activationId"]:
                self.turn_roles.committed = True
                self.turn_roles.changed.set()
                record["roles"] = copy.deepcopy(self.turn_roles.state())
            await self._save(record)
            return self.status(record["activationId"])

    async def archived(self, activation_id: str) -> JsonObject:
        self.status(activation_id)
        archive = await self.io.call(self._export, copy.deepcopy(self.records[activation_id]))
        if archive is None:
            raise ServiceError("ARCHIVE_NOT_AVAILABLE", 409)
        return {**archive, "civilization": self.civilization}

    def status(self, activation_id: str | None = None) -> JsonObject:
        if activation_id is None:
            activation_id = self.active_id
        if activation_id is None:
            return {
                "civilization": self.civilization,
                "activationId": None,
                "epoch": None,
                "threadId": None,
                "phase": "IDLE",
                "defaultSettings": dict(self.default_settings),
                "decisionsSettings": self.decisions_settings.model_dump(),
                "usage": dict.fromkeys(USAGE_KEYS, 0),
            }
        record = self.records.get(activation_id)
        if record is None:
            raise ServiceError("ACTIVATION_NOT_FOUND", 404)
        result = {
            key: (dict(value) if isinstance(value, dict) else value)
            for key, value in record.items()
            if key != "fingerprint"
        }
        if "effectiveSettings" in record:
            from civ_agent.effort_decisions import base_settings

            result["configuredSettings"] = base_settings(record)
        task = self.finalization_tasks.get(activation_id)
        result["finalizationState"] = (
            "RUNNING"
            if task is not None and not task.done()
            else "FAILED"
            if activation_id in self.finalization_errors
            else "COMPLETED"
            if "finalizationFinishedAtMs" in record
            else "PENDING"
        )
        if activation_id in self.finalization_errors:
            result["finalizationError"] = self.finalization_errors[activation_id]
        job = next((job for job in self.gateway.jobs.values() if job.id == activation_id), None)
        result["negotiationWaitSeconds"] = job.waiting_seconds() if job else record.get("negotiationWaitSeconds", 0)
        result["roleWaitSeconds"] = record.get("roleWaitSeconds", 0) + (
            max(0, time.time() - record["roleWaitStartedAtMs"] / 1000)
            if record.get("roleWaitStartedAtMs") is not None
            else 0
        )
        result["decisionsSettings"] = self.decisions_settings.model_dump()
        if record.get("purpose", "gameplay") == "gameplay":
            children = [r for r in self.records.values() if r.get("parentActivation") == activation_id]
            economy_children = [r for r in children if r.get("purpose") == "economy"]
            military_children = [r for r in children if r.get("purpose") == "military"]
            economy_usage = {key: sum(r.get("usage", {}).get(key, 0) for r in economy_children) for key in USAGE_KEYS}
            military_usage = {key: sum(r.get("usage", {}).get(key, 0) for r in military_children) for key in USAGE_KEYS}
            result["militaryUsage"] = military_usage
            result["militaryUsageComplete"] = all(r.get("usageComplete", False) for r in military_children)
            result["militaryModelRequests"] = sum(r.get("modelRequests", 0) for r in military_children)
            result["mainUsage"] = dict(record.get("usage", {}))
            result["economyUsage"] = economy_usage
            result["usage"] = {
                key: result["mainUsage"].get(key, 0) + economy_usage[key] + military_usage[key] for key in USAGE_KEYS
            }
            result["mainUsageComplete"] = record.get("usageComplete", False)
            result["economyUsageComplete"] = all(r.get("usageComplete", False) for r in economy_children)
            result["usageComplete"] = (
                result["mainUsageComplete"]
                and result["economyUsageComplete"]
                and all(r.get("usageComplete", False) for r in military_children)
            )
            result["economyModelRequests"] = sum(r.get("modelRequests", 0) for r in economy_children)
        return result

    def health(self):
        failure = self.gateway.run_failure or self.reviews.failure
        if failure is None and self.peace_reviews.unconfirmed_process is not None:
            failure = {"error": "STOP_UNCONFIRMED", "diagnosticId": "peace-consultation", "job": "peace-consultation"}
        if failure is None and self.military_runtime is not None and self.military_runtime.process is not None:
            military_record = self.records.get(self.military_runtime.active_id, {})
            if military_record.get("error") == "STOP_UNCONFIRMED":
                failure = {
                    "error": "STOP_UNCONFIRMED",
                    "diagnosticId": self.military_runtime.active_id,
                    "job": self.military_runtime.active_id,
                }
        if failure is None and self.economy_runtime is not None and self.economy_runtime.process is not None:
            economy_record = self.records.get(self.economy_runtime.active_id, {})
            if economy_record.get("error") == "STOP_UNCONFIRMED":
                failure = {
                    "error": "STOP_UNCONFIRMED",
                    "diagnosticId": self.economy_runtime.active_id,
                    "job": self.economy_runtime.active_id,
                }
        if failure is None and self.active_id is not None and self.active_task is not None and self.active_task.done():
            record = self.records[self.active_id]
            if record["phase"] not in TERMINAL:
                # Failure to save terminal metadata must not look like healthy
                # model work until a long turn timeout expires.
                failure = {
                    "error": record.get("error", "JOB_CRASHED"),
                    "diagnosticId": record["activationId"],
                    "job": record["activationId"],
                }
        return {
            "civilization": self.civilization,
            "runFailure": {key: failure[key] for key in ("error", "diagnosticId", "job")} if failure else None,
            "quotaFailure": next(
                (
                    value
                    for value in self.quota_failures.values()
                    if self.match is None or value["epoch"] == self.match["epoch"]
                ),
                None,
            ),
            "communicationSubscription": self.chat.subscription_health(),
            "communicationJobError": self.chat.error,
        }

    def _validate_start(self, payload):
        try:
            request = StartRequest.model_validate(payload)
        except ValidationError as error:
            raise ServiceError(start_error(error)) from None
        if request.civilization != self.civilization:
            raise ServiceError("INVALID_BINDING")
        if request.mcpUrl != self.mcp_url:
            raise ServiceError("INVALID_MCP_ENDPOINT")

    async def review_phase(self, payload):
        if (
            not {"epoch", "gameTurn", "stage"} <= set(payload)
            or set(payload) - {"epoch", "gameTurn", "stage", "forceReview", "earlyReview"}
            or not identifier(payload.get("epoch"))
            or type(payload.get("gameTurn")) is not int
            or payload["gameTurn"] < 0
            or payload.get("stage") not in {"hold", "start", "status", "release"}
            or type(payload.get("forceReview", False)) is not bool
            or type(payload.get("earlyReview", False)) is not bool
        ):
            raise ServiceError("INVALID_ARGUMENT")
        async with self.guard:
            try:
                return await self.reviews.phase(
                    payload["epoch"],
                    payload["gameTurn"],
                    payload["stage"],
                    payload.get("forceReview", False),
                    early_review=payload.get("earlyReview", False),
                )
            except ValueError as error:
                raise ServiceError(str(error), 409) from None

    async def _blocking_review(self, payload):
        try:
            return await self.io.call(self.reviews.store.blocking, payload["epoch"], payload["gameTurn"])
        except ValueError as error:
            raise ServiceError(str(error), 409) from None

    async def review_triage(self, payload):
        if (
            set(payload) != {"epoch", "gameTurn", "scheduledSoon", "nextReviewTurn"}
            or payload["epoch"] != self.chat.epoch
            or type(payload["gameTurn"]) is not int
            or payload["gameTurn"] < 1
            or type(payload["scheduledSoon"]) is not bool
            or type(payload["nextReviewTurn"]) is not int
            or payload["nextReviewTurn"] < payload["gameTurn"]
        ):
            raise ServiceError("INVALID_ARGUMENT")
        advisory = self.gateway.advisories
        if advisory is None:
            return {"earlyReview": False, "reason": "disabled"}
        if self.gateway.paused or self.gateway.checkpoint_requested or self.gateway.run_failure:
            return {"earlyReview": False, "reason": "barrier"}
        # Accepted triage drains before checkpoint capture, just like gateway
        # reads. A late result must never write into a restored epoch.
        async with self.gateway.admit():
            return await advisory.triage(
                payload["gameTurn"] - 1,
                scheduled_soon=payload["scheduledSoon"],
                next_review_turn=payload["nextReviewTurn"],
            )

    async def start(self, payload: JsonObject) -> JsonObject:
        self._validate_start(payload)
        failure = self.health()["runFailure"]
        if failure:
            raise ServiceError(failure["error"], 409)
        fingerprint = hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        async with self.guard:
            if await self.io.call(self.results.load) is not None or any(
                record.get("terminalOutcome") for record in self.records.values()
            ):
                raise ServiceError("GAME_FINISHED", 409)
            previous = self.records.get(payload["activationId"])
            if previous is not None:
                if previous["fingerprint"] != fingerprint:
                    raise ServiceError("ACTIVATION_CONFLICT", 409)
                return self.status(payload["activationId"])
            if self.active_id is not None and self.records[self.active_id]["phase"] not in TERMINAL:
                raise ServiceError("AGENT_BUSY", 409)
            if self.match is not None and self.match["epoch"] != payload["epoch"]:
                raise ServiceError("WORKSPACE_EPOCH_CONFLICT", 409)
            same_turn = [
                record
                for record in self.records.values()
                if record.get("purpose", "gameplay") == "gameplay"
                and (record["epoch"], record.get("gameTurn")) == (payload["epoch"], payload["gameTurn"])
            ]
            if any("committedHandoff" in record for record in same_turn):
                raise ServiceError("GAME_TURN_ALREADY_COMMITTED", 409)
            game_turns = {
                record["gameTurn"]
                for record in self.records.values()
                if record["epoch"] == payload["epoch"]
                and "gameTurn" in record
                and record.get("purpose", "gameplay") == "gameplay"
            }
            if game_turns and payload["gameTurn"] < max(game_turns):
                raise ServiceError("GAME_TURN_STALE", 409)
            committed_turns = {record["gameTurn"] for record in self.records.values() if "committedHandoff" in record}
            if any(turn < payload["gameTurn"] and turn not in committed_turns for turn in game_turns):
                raise ServiceError("PREVIOUS_TURN_NOT_FINALIZED", 409)
            if await self._blocking_review(payload) is not None:
                raise ServiceError("STRATEGIC_REVIEW_PENDING", 409)
            if self.chat.running and self.chat.held:
                raise ServiceError("REVIEW_PHASE_ACTIVE", 409)
            defaults = (
                self.match.get("effectiveSettings", self.default_settings) if self.match else self.default_settings
            )
            settings = {
                key: payload.get(key) if payload.get(key) is not None else default for key, default in defaults.items()
            }
            if self.match is None:
                self.match = {"epoch": payload["epoch"]}
                await self.io.call(write_private_json, self.match_file, dict(self.match))
            prompt = payload["prompt"]
            await self.io.call(self.memory.bind, payload["epoch"])
            snapshot = await self.io.call(self.memory.snapshot, "gameplay", payload["gameTurn"])
            payload = {**payload, "_memorySnapshot": snapshot}
            payload = {**payload, "prompt": prompt}
            record: JsonObject = {
                "activationId": payload["activationId"],
                "epoch": payload["epoch"],
                "civilization": self.civilization,
                "gameTurn": payload["gameTurn"],
                "handoff": payload["handoff"],
                "turnDirectory": f"turn_{payload['gameTurn']}",
                "threadId": payload["threadId"],
                "phase": "STARTING",
                "usage": dict.fromkeys(USAGE_KEYS, 0),
                "modelRequests": 0,
                "fingerprint": fingerprint,
                "settings": settings,
                "requestedServiceTier": "fast" if self.fast else None,
                "timeoutSeconds": payload["timeoutSeconds"],
            }
            self.records[record["activationId"]] = record
            await self._save(record)
            # Record model input after preparation, in its actual developer/user order.
            await self.io.call(self.history.begin, copy.deepcopy(record), None, payload["playerToken"])
            self.active_id = record["activationId"]
            self.stop_event = asyncio.Event()
            self.last_heartbeat = time.monotonic()
            self.turn_id = self.completion = self.failure = None
            self.early_completions = {}
            self.early_usage = {}
            self.accept_usage = self.stopping = False
            self.run_started = asyncio.Event()
            self.active_task = asyncio.create_task(self._run(payload))
            return self.status()

    async def flush_telemetry(self, *, connection=None, epoch=None):
        if connection is None:
            connection, epoch = self.chat.connection, self.chat.epoch
        if connection is None:
            return
        async with self.telemetry_lock:
            for report in await self.io.call(self.accounting.pending):
                fields = {"job": report["job"], "usage": report["usage"], "telemetry": report["telemetry"]}
                operation = "job_release" if report["telemetry"]["phase"] in TERMINAL else "job_report"
                response = await connection.tool("player_communication", {"op": operation, "epoch": epoch, **fields})
                if response.get("granted") is not True:
                    raise ServiceError("TELEMETRY_NOT_ACCEPTED", 503)
                await self.io.call(self.accounting.acknowledge, report["job"], report["telemetry"]["sequence"])

    async def flush_accounting(self, payload):
        """Reconcile late reports after a finished-game restart without waking chat."""
        if (
            set(payload) != {"epoch", "playerToken"}
            or not identifier(payload.get("epoch"))
            or not isinstance(payload.get("playerToken"), str)
            or len(payload["playerToken"]) != 43
        ):
            raise ServiceError("INVALID_ARGUMENT")
        async with self.connection_factory(self.mcp_url, payload["playerToken"], self.ca_file) as connection:
            identity = await connection.tool("player_communication", {"op": "state", "epoch": payload["epoch"]})
            if identity.get("civilization") != self.civilization or identity.get("epoch") != payload["epoch"]:
                raise ServiceError("INVALID_BINDING", 409)
            await self.flush_telemetry(connection=connection, epoch=payload["epoch"])
        return {"flushed": True}

    async def heartbeat(self, activation_id):
        status = self.status(activation_id)
        if activation_id == self.active_id and status["phase"] not in TERMINAL:
            self.last_heartbeat = time.monotonic()
        return status

    async def renew_gameplay_permit(self, activation_id):
        """The host's 90-second permit has its own lifetime, independent of HTTP health checks."""
        while True:
            await asyncio.sleep(JOB_PERMIT_RENEW_INTERVAL)
            if not self.chat.running or self.gameplay_permit != activation_id:
                continue
            try:
                async with asyncio.timeout(30):
                    grant = await self.chat.social("job_heartbeat", job=activation_id)
                if grant.get("granted") is not True:
                    raise ServiceError("JOB_PERMIT_EXPIRED", 503)
            except (OSError, ValueError, TimeoutError, aiohttp.ClientError, ServiceError) as error:
                self.failure = "JOB_PERMIT_EXPIRED"
                self.diagnostics.emit("job_permit_failed", error=error, activationId=activation_id)
                self.stop_event.set()
                for job in self.gateway.jobs.values():
                    if job.id == activation_id:
                        job.closed = True
                if self.active_task is not None:
                    self.active_task.cancel()
                return

    async def interrupt(self, activation_id):
        status = self.status(activation_id)
        if activation_id == self.active_id and status["phase"] not in TERMINAL:
            assert self.active_task is not None
            if not self.stopping:
                self.failure = "INTERRUPTED"
                self.stop_event.set()
                # Ensure _run has installed its shutdown/finally path before cancellation.
                await self.run_started.wait()
                self.active_task.cancel()
            # Cancellation waits for CodexProcess.close() and its process-group exit.
            with contextlib.suppress(asyncio.CancelledError):
                await asyncio.shield(self.active_task)
        return self.status(activation_id)

    async def _notification(self, method: str, params: JsonObject) -> None:
        assert self.active_id is not None
        record = self.records[self.active_id]
        if method == "service/llmMetrics":
            await record_llm_metrics(self, record, params)
            return
        model_record = record
        thread_id = model_record.get("threadId")
        if thread_id and params.get("threadId") == thread_id and context_compacted(method, params):
            job = next((job for job in self.gateway.jobs.values() if job.id == self.active_id), None)
            if job is not None:
                async with job.lock:
                    self.gateway.reset_coordination(job)
        if method == "service/transportFailure":
            if params.get("reason") in {
                "CODEX_NOTIFICATION_OVERFLOW",
                "CODEX_NOTIFICATION_FAILED",
                "CODEX_DISCONNECTED",
            }:
                self.failure = self.failure or params["reason"]
                self.stop_event.set()
            return
        if method == "service/transportStatus":
            self.diagnostics.emit("codex_transport", activationId=self.active_id, **params)
            return
        if method == "service/requestFailed":
            record["requestFailure"] = params
            if quota := quota_failure(record, params):
                self.quota_failures[record["activationId"]] = quota
            self.diagnostics.emit(
                "codex_request_failed",
                activationId=self.active_id,
                purpose=record.get("purpose", "gameplay"),
                **params,
            )
            await self._save(record)
            return
        try:
            if method in HISTORY_METHODS:
                await self.io.call(self.history.notify, self.active_id, thread_id, method, params)
            if (
                method == "service/modelStatus"
                and params.get("threadId") == thread_id
                and params.get("turnId") == model_record.get("turnId")
            ):
                record["modelStatus"] = {
                    key: value for key, value in params.items() if key not in {"threadId", "turnId"}
                }
                if quota := quota_failure(record, params):
                    self.quota_failures[record["activationId"]] = quota
                self.diagnostics.emit("model_status", activationId=self.active_id, **params)
                await self._save(record)
        except (OSError, ValueError):
            self.failure = "DURABLE_RECORD_FAILED"
            self.stop_event.set()
            raise
        if method == "service/approvalRequired":
            self.failure = "INTERACTIVE_DECISION_REQUIRED"
            self.stop_event.set()
        elif method == "service/authRequired":
            self.failure = "BENCHMARK_LOGIN_UNAVAILABLE"
            self.stop_event.set()
        elif method == "service/disconnected":
            if params.get("reason") in {"CODEX_NOTIFICATION_OVERFLOW", "CODEX_NOTIFICATION_FAILED"}:
                self.failure = self.failure or params["reason"]
                self.stop_event.set()
                return
            if (
                (self.completion is None or record.get("negotiationWait") or record.get("economyWait"))
                and not self.stopping
                and self.failure is None
            ):
                self.failure = "CODEX_DISCONNECTED"
                self.stop_event.set()
        elif method == "thread/tokenUsage/updated" and self.accept_usage and params.get("threadId") == thread_id:
            turn_id = params.get("turnId")
            usage = params.get("tokenUsage")
            if not identifier(turn_id) or not isinstance(usage, dict):
                return
            if self.turn_id is None:
                self.early_usage[turn_id] = usage
            elif turn_id == self.turn_id:
                await self._record_usage(record, usage)
        elif method == "turn/completed" and self.accept_usage and params.get("threadId") == thread_id:
            turn = params.get("turn", {})
            if not identifier(turn.get("id")):
                return
            if self.turn_id is None:
                self.early_completions[turn.get("id")] = turn.get("status")
            elif turn.get("id") == self.turn_id:
                self.completion = turn.get("status")
                self.stop_event.set()

    async def _record_usage(self, record, token_usage):
        total = token_usage.get("total", {})
        if not merge_model_usage(record, total, USAGE_KEYS):
            return
        await self._save(record)

    async def _model_turn(self, record, job, prompt, workspace, selected, prepared=None):
        """One gameplay conversation, with no post-turn model bookkeeping."""
        if self.failure:
            raise ServiceError(self.failure)
        job.suspend_for_negotiation = True
        self.stop_event.clear()
        self.turn_id = self.completion = None
        self.early_usage.clear()
        self.early_completions.clear()
        self.accept_usage = False
        record["usageReported"] = False
        record.pop("modelStatus", None)
        model_record = record
        model_record.pop("turnId", None)
        await self.io.call(self.history.next_conversation, record["activationId"], record["modelPhase"])
        process = self.process_factory(
            self.executable, workspace, self.gateway_url, self.gateway_ca_file, self._notification
        )
        self.process = process
        await self.io.call(self.history.startup_context, record["activationId"], prompt.instructions, role="developer")
        await self.io.call(self.history.startup_context, record["activationId"], prompt.user)
        await process.start(job.token, selected)
        from civ_agent.effort_decisions import base_settings, select_effort

        selected = await select_effort(self, record, job, prepared, selected, process)
        options: JsonObject = {
            "cwd": str(workspace),
            "approvalPolicy": "never",
            "sandbox": "workspace-write",
            "developerInstructions": prompt.instructions,
            **codex_thread_settings(selected, fast=self.fast),
        }
        result = await process.request("thread/start", options)
        thread_id = result.get("thread", {}).get("id")
        if not identifier(thread_id) or thread_id in self.known_threads:
            raise ServiceError("INVALID_CODEX_THREAD", 502)
        effective_settings = {
            "provider": result.get("modelProvider"),
            "model": result.get("model"),
            "reasoningEffort": result.get("reasoningEffort"),
        }
        record["serviceTier"] = result.get("serviceTier")
        if (
            effective_settings["provider"] != selected["provider"]
            or not valid_setting(effective_settings["model"])
            or (
                effective_settings["reasoningEffort"] is not None
                and not valid_setting(effective_settings["reasoningEffort"])
            )
        ):
            raise ServiceError("INVALID_CODEX_SETTINGS", 502)
        if "effectiveSettings" in record and effective_settings != record["effectiveSettings"]:
            raise ServiceError("CODEX_SETTINGS_CHANGED", 409)
        if selected["model"] is not None and effective_settings["model"] != selected["model"]:
            raise ServiceError("CODEX_MODEL_NOT_APPLIED", 502)
        if (
            selected["reasoningEffort"] is not None
            and effective_settings["reasoningEffort"] != selected["reasoningEffort"]
        ):
            raise ServiceError("CODEX_REASONING_EFFORT_NOT_APPLIED", 502)
        record["effectiveSettings"] = effective_settings
        model_record["threadId"] = thread_id
        self.known_threads.add(thread_id)
        assert self.match is not None
        self.match[
            job.purpose + "EffectiveSettings" if job.purpose in {"economy", "military"} else "effectiveSettings"
        ] = base_settings(record)
        await self.io.call(write_private_json, self.match_file, copy.deepcopy(self.match))
        await self._save(record)
        if self.failure:
            raise ServiceError(self.failure)
        self.accept_usage = True
        self.gateway.commit_startup_coordination(job)
        message = prompt.user
        while True:
            if record.get("roleState") in {"finished", "waiting_negotiation", "waiting_economy"} and self.chat.running:
                while not (await self.chat.social("job_claim", job=job.id, purpose=job.purpose))["granted"]:
                    if self.failure:
                        raise ServiceError(self.failure)
                    await asyncio.sleep(0.25)
            await self._start_model_turn(record, process, message)
            stopped = asyncio.create_task(self.stop_event.wait())
            negotiating = asyncio.create_task(job.negotiation_pending.wait())
            handed_off = asyncio.create_task(job.gameplay_ended.wait())
            role_finished = asyncio.create_task(job.role_finished.wait())
            role_wait = asyncio.create_task(job.role_wait_pending.wait())
            try:
                await asyncio.wait(
                    {stopped, negotiating, handed_off, role_finished, role_wait}, return_when=asyncio.FIRST_COMPLETED
                )
                # A model may finish before an accepted offer reaches the host.
                # Let that call establish its outcome (or negotiation hold)
                # before treating the conversation as terminal.
                while (
                    self.completion == "completed"
                    and job.pending_calls
                    and not job.negotiation_pending.is_set()
                    and not job.role_wait_pending.is_set()
                ):
                    await asyncio.wait({*job.pending_calls, negotiating}, return_when=asyncio.FIRST_COMPLETED)
            finally:
                stopped.cancel()
                negotiating.cancel()
                handed_off.cancel()
                role_finished.cancel()
                role_wait.cancel()
                await asyncio.gather(stopped, negotiating, handed_off, role_finished, role_wait, return_exceptions=True)
            if self.failure:
                raise ServiceError(self.failure)
            if job.gameplay_ended.is_set():
                # The gateway has saved the native receipt and fenced gameplay.
                # Stop inference before a closing message reprocesses the context.
                await self._interrupt_model_turn(record, process, "COMPLETION_TIMEOUT")
                return
            if job.role_finished.is_set() and self.gateway.role_coordinator is not None:
                await self._interrupt_model_turn(record, process, "COMPLETION_TIMEOUT")
                record["roleState"] = "finished"
                await self._save(record)
                await self.flush_telemetry()
                message = await self.gateway.role_coordinator.wait_for_resume(job)
                if job.gameplay_ended.is_set():
                    return
                arguments = self.gateway.role_coordinator.economy_arguments() if job.purpose == "economy" else {}
                arguments = {**arguments, "delivery": "full"}
                fresh = await self.gateway.call(
                    job, "player_turn", arguments, f"resume:{record.get('modelRequests', 0)}"
                )
                message += "\n" + json.dumps(fresh, ensure_ascii=False)
                continue
            if job.role_wait_pending.is_set() and self.gateway.role_coordinator is not None:
                message = await self._settle_economy_wait(record, job, process)
                if job.gameplay_ended.is_set():
                    return
                await self.io.call(self.history.next_conversation, record["activationId"], record["modelPhase"])
                await self.io.call(self.history.role_wait_result, record["activationId"], message)
                continue
            if not job.negotiation_pending.is_set():
                if self.completion == "completed" and self.gateway.role_coordinator is not None:
                    # Explicit completion is required; a closing message is not a turn handoff.
                    record["finishReminders"] = record.get("finishReminders", 0) + 1
                    if record["finishReminders"] > 2:
                        raise ServiceError("ROLE_NOT_FINISHED")
                    message = "Use finish_role after completing your role's work. The host advances the native turn."
                    continue
                return
            message = await self._settle_negotiation(record, job, process)
            await self.io.call(self.history.next_conversation, record["activationId"], record["modelPhase"])
            await self.io.call(self.history.negotiation_result, record["activationId"], message)

    async def _start_model_turn(self, record, process, message):
        """Continue the same thread; usage notifications contain cumulative totals."""
        self.stop_event.clear()
        self.turn_id = self.completion = None
        self.early_usage.clear()
        self.early_completions.clear()
        record.pop("turnId", None)
        record.pop("modelStatus", None)
        effective_settings = record["effectiveSettings"]
        turn_options = {
            "threadId": record["threadId"],
            "input": [{"type": "text", "text": message}],
            "approvalPolicy": "never",
            "summary": "auto",
            "model": effective_settings["model"],
            **({"serviceTier": "fast"} if self.fast else {}),
        }
        if effective_settings["reasoningEffort"] is not None:
            turn_options["effort"] = effective_settings["reasoningEffort"]
        result = await process.request("turn/start", turn_options)
        # Only the response to our request binds this activation's turn.
        turn_id = result.get("turn", {}).get("id")
        if not identifier(turn_id):
            raise ServiceError("INVALID_CODEX_TURN", 502)
        record["turnId"] = turn_id
        await self.io.call(self.history.bind_turn, self.active_id, turn_id)
        # Buffer notifications until the acknowledged model turn is bound.
        self.turn_id = turn_id
        if self.turn_id in self.early_usage:
            await self._record_usage(record, self.early_usage[self.turn_id])
        self.early_usage.clear()
        if self.turn_id in self.early_completions:
            self.completion = self.early_completions[self.turn_id]
            self.stop_event.set()
        record["phase"] = "RUNNING"
        record.pop("roleState", None)
        await self._save(record)

    async def _interrupt_model_turn(self, record, process, timeout_error):
        """Stop inference while still accepting completion and usage notifications."""
        if self.completion is None:
            try:
                await process.request("turn/interrupt", {"threadId": record["threadId"], "turnId": self.turn_id})
            except ServiceError:
                # The turn can finish between checking it and requesting the
                # interruption. A confirmed completion is already a safe pause.
                if self.completion not in {"completed", "interrupted"}:
                    raise
            try:
                await asyncio.wait_for(self.stop_event.wait(), MODEL_INTERRUPT_TIMEOUT)
            except TimeoutError:
                raise ServiceError(timeout_error, 502) from None
        if self.failure:
            raise ServiceError(self.failure)
        if self.completion not in {"completed", "interrupted"}:
            raise ServiceError("CODEX_TURN_FAILED", 502)

    async def _settle_negotiation(self, record, job, process):
        """Stop inference, finish accepted tools, then supply their actual results."""
        record["negotiationWait"] = {"state": "suspending"}
        await self._save(record)
        await self._interrupt_model_turn(record, process, "NEGOTIATION_SUSPEND_TIMEOUT")
        record["negotiationWait"] = {"state": "waiting"}
        record["roleState"] = "waiting_negotiation"
        await self._save(record)
        await self.flush_telemetry()
        self.diagnostics.emit("gameplay_negotiation_suspended", activationId=job.id, turnId=self.turn_id)
        calls = list(job.negotiation_calls.items())
        # Shield individual calls from a model disconnect. A real job stop cancels
        # and drains them in gateway.close(), retaining uncertain-action handling.
        results = await asyncio.gather(*(asyncio.shield(task) for task, _ in calls))
        if not calls:
            await self.gateway.wait_for_gameplay(job)
        if self.failure:
            raise ServiceError(self.failure)
        self.gateway.check(job, ended_allowed=True)
        # Include native receipts from earlier in this model turn: a completed
        # sibling call can still have been waiting inside the interrupted batch.
        # These omit overview deltas; the settled response rebases that state.
        completed = [value for task, value in job.completed_actions.items() if task not in job.negotiation_calls]
        job.completed_actions.clear()
        job.negotiation_calls.clear()
        job.negotiation_pending.clear()
        record.pop("negotiationWait", None)
        self.diagnostics.emit("gameplay_negotiation_resumed", activationId=job.id, turnId=self.turn_id)
        return (
            "The negotiation has finished. Continue this gameplay turn using the completed tool results below. "
            "Do not repeat successful actions.\n"
            + json.dumps(
                {
                    **({"completedActions": completed} if completed else {}),
                    "toolResults": [
                        {**info, "result": result} for (_, info), result in zip(calls, results, strict=True)
                    ],
                },
                ensure_ascii=False,
                separators=(",", ":"),
            )
        )

    async def _settle_economy_wait(self, record, job, process):
        """Suspend an unfinished Main without holding tool admission or native dispatch."""
        coordinator = self.gateway.role_coordinator
        assert coordinator is not None
        started = None
        outcome = "cancelled"
        try:
            record["economyWait"] = {"state": "suspending"}
            await self._save(record)
            await self._interrupt_model_turn(record, process, "ROLE_WAIT_SUSPEND_TIMEOUT")
            started = time.monotonic()
            record["roleWaitStartedAtMs"] = int(time.time() * 1000)
            record["economyWait"] = {"state": "waiting"}
            record["roleState"] = "waiting_economy"
            await self._save(record)
            await self.flush_telemetry()
            calls = list(job.role_wait_calls.items())
            results = await asyncio.gather(*(asyncio.shield(task) for task, _ in calls))
            outcome = await coordinator.wait_for_economy(job)
            wait_result = await coordinator.finish_economy_wait(job, outcome)
            if outcome == "turn_ended":
                return ""
            negotiation = None
            if outcome == "negotiation" or job.negotiation_pending.is_set():
                record["roleWaitSeconds"] = record.get("roleWaitSeconds", 0) + time.monotonic() - started
                record.pop("roleWaitStartedAtMs", None)
                started = None
                negotiation = await self._settle_negotiation(record, job, process)
            completed = [value for task, value in job.completed_actions.items() if task not in job.role_wait_calls]
            job.completed_actions.clear()
            self.gateway.check(job)
            # The interrupted code cell may not have printed an accepted result.
            # Rebase all delivery cursors before supplying one authoritative view.
            self.gateway.forget_observation(job)
            self.gateway.reset_coordination(job)
            fresh = await self.gateway.call(
                job,
                "player_turn",
                {"view": "current", "delivery": "full"},
                "role-wait-resume:" + str(wait_result["id"]),
                source="host",
            )
            if "error" in fresh:
                raise ServiceError("ROLE_WAIT_REFRESH_FAILED")
            return (
                "Your economy wait ended; your role is still unfinished. Reconsider using the currentGame below, "
                "which supersedes earlier observations and the historical tool results. Do not repeat successful actions.\n"
                + json.dumps(
                    {
                        "economyWait": wait_result,
                        **({"completedActions": completed} if completed else {}),
                        "toolResults": [
                            {**info, "result": result} for (_, info), result in zip(calls, results, strict=True)
                        ],
                        **({"negotiation": negotiation} if negotiation else {}),
                        "currentGame": fresh,
                    },
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
            )
        finally:
            if started is not None:
                record["roleWaitSeconds"] = record.get("roleWaitSeconds", 0) + time.monotonic() - started
            record.pop("roleWaitStartedAtMs", None)
            record.pop("economyWait", None)
            job.role_wait_pending.clear()
            job.role_wait_calls.clear()
            if coordinator.economy_wait_record is not None:
                record["lastEconomyWait"] = {**coordinator.economy_wait_record, "state": outcome}
                coordinator.economy_wait_record = None
                record["roles"] = copy.deepcopy(coordinator.state())
            await self._save(record)

    async def _run(self, payload: JsonObject) -> None:
        try:
            await self._run_job(payload)
        except (OSError, ValueError, SQLAlchemyError, ServiceError, aiohttp.ClientError) as error:
            # Startup can fail before a child process or its shutdown handler
            # exists. Publish a terminal result and release interruption waiters.
            self.diagnostics.emit(
                "gameplay_job_failed", error=error, activationId=payload["activationId"], gameTurn=payload["gameTurn"]
            )
            record = self.records[payload["activationId"]]
            if self.process is None and record["phase"] not in TERMINAL:
                record.update(
                    phase="FAILED",
                    error=error.code
                    if isinstance(error, ServiceError)
                    else "STATE_STORE_FAILED"
                    if isinstance(error, SQLAlchemyError)
                    else "JOB_START_FAILED",
                )
                await self._save(record)
            else:
                raise
        finally:
            self.run_started.set()

    async def _run_job(self, payload: JsonObject) -> None:
        context = {
            "purpose": "gameplay",
            "activationId": payload["activationId"],
            "gameTurn": payload["gameTurn"],
            "epoch": payload["epoch"],
        }
        self.diagnostics.emit("gameplay_job_started", **context)
        try:
            async with self.connection_factory(
                self.mcp_url, payload["playerToken"], self.ca_file, diagnostics=self.diagnostics, context=context
            ) as connection:
                await self._run_connected_job(payload, connection)
        except StateConflict:
            if not self.gateway.run_failure:
                raise
            # A concurrent chat failure can fence startup before its watchdog exists.
            record = self.records[payload["activationId"]]
            record.update(
                phase="FAILED", error=self.gateway.run_failure["error"], runFailure=self.health()["runFailure"]
            )
            await self._save(record)
        self.diagnostics.emit("gameplay_job_finished", **context, phase=self.records[payload["activationId"]]["phase"])

    async def _run_connected_job(self, payload: JsonObject, connection: HostConnection) -> None:
        record = self.records[payload["activationId"]]
        started = time.monotonic()
        workspace = private_directory(
            self.workspace / "jobs" / hashlib.sha256(record["activationId"].encode()).hexdigest()[:12]
        )
        job = await self.gateway.open(
            record["activationId"],
            "gameplay",
            payload["epoch"],
            connection,
            record["settings"],
            record["gameTurn"],
            memory_snapshot=payload.get("_memorySnapshot"),
        )
        owner = asyncio.current_task()
        assert owner is not None
        economy_task = advance_task = negotiation_task = None

        async def watchdog():
            while True:
                await asyncio.sleep(0.1)
                if self.stopping:
                    return
                now = time.monotonic()
                if (
                    advance_task is not None
                    and advance_task.done()
                    and not advance_task.cancelled()
                    and advance_task.exception()
                    and record.get("confirmedHandoff") != record["handoff"] + 1
                ):
                    self.failure = "TURN_COMPLETION_FAILED"
                if (
                    negotiation_task is not None
                    and negotiation_task.done()
                    and not negotiation_task.cancelled()
                    and negotiation_task.exception()
                ):
                    self.failure = "NEGOTIATION_MONITOR_FAILED"
                if self.turn_roles is not None:
                    for military_task in self.turn_roles.military_tasks:
                        if military_task.done() and not military_task.cancelled() and military_task.exception():
                            self.failure = self.failure or "MILITARY_ROLE_FAILED"
                if self.gateway.run_failure or self.failure:
                    self.failure = self.gateway.run_failure["error"] if self.gateway.run_failure else self.failure
                elif now - self.last_heartbeat >= payload["leaseSeconds"]:
                    self.failure = "HEARTBEAT_EXPIRED"
                elif job.handoff_at is not None and now - job.handoff_at >= 60:
                    self.failure = "COMPLETION_TIMEOUT"
                elif job.handoff_at is None and now - started - job.waiting_seconds() >= payload["timeoutSeconds"]:
                    self.failure = "ACTIVATION_TIMEOUT"
                else:
                    continue
                self.stop_event.set()
                owner.cancel()
                return

        monitor = asyncio.create_task(watchdog())

        async def handoff_watcher():
            await job.gameplay_ended.wait()
            try:
                record["gameplayEndedAtMs"] = int(time.time() * 1000)
                record["gameplayElapsedMs"] = max(0, int(((job.handoff_at or time.monotonic()) - started) * 1000))
                record["completionReason"] = "native_handoff"
                await self._save(record)
            except (OSError, ValueError, SQLAlchemyError):
                self.failure = "HANDOFF_RECORD_FAILED"
                self.stop_event.set()
                owner.cancel()

        handoff_monitor = asyncio.create_task(handoff_watcher())

        async def report_accounting():
            try:
                while True:
                    await asyncio.sleep(TELEMETRY_SAMPLE_INTERVAL)
                    await self.io.call(self.accounting.sample, copy.deepcopy(record), job.waiting_seconds())
                    if self.chat.running:
                        try:
                            await self.flush_telemetry()
                        except (OSError, ValueError, TimeoutError, aiohttp.ClientError) as error:
                            # Reports remain in the durable outbox for the next flush.
                            self.diagnostics.emit("periodic_telemetry_failed", error=error, activationId=job.id)
            except (OSError, ValueError, SQLAlchemyError, ServiceError) as error:
                self.failure = error.code if isinstance(error, ServiceError) else "DURABLE_RECORD_FAILED"
                self.diagnostics.emit("background_io_failed", error=error, activationId=job.id)
                self.stop_event.set()
                owner.cancel()

        reporter = asyncio.create_task(report_accounting())
        permit_renewer = asyncio.create_task(self.renew_gameplay_permit(record["activationId"]))
        self.run_started.set()
        try:
            # Preserve resolved settings across new app-server processes even when
            # service defaults or the local Codex configuration have changed.
            selected = record.get("effectiveSettings", record["settings"])
            if self.chat.running:
                while True:
                    admission = await self.chat.social("job_claim", job=record["activationId"], purpose="gameplay")
                    if admission["granted"]:
                        break
                    if admission.get("reason") in {"MODEL_INVOCATION_LIMIT", "ROLE_CAPACITY_TOO_SMALL"}:
                        raise ServiceError(admission["reason"], 503)
                    await asyncio.sleep(1)
                self.gameplay_permit = record["activationId"]
            await self.io.call(
                self.accounting.start,
                record["activationId"],
                record["epoch"],
                self.gameplay_permit == record["activationId"],
            )
            from civ_agent.economy_roles import TurnRoles
            from civ_agent.economy_runtime import RoleRuntime, run_role, run_scheduled_economy

            self.turn_roles = TurnRoles(self, record)
            self.gateway.role_coordinator = self.turn_roles
            self.turn_roles.register(job)
            military_generation = 0

            def start_military():
                nonlocal military_generation
                military_generation += 1
                military_payload = {
                    **payload,
                    "activationId": payload["activationId"] + f":military{military_generation}",
                }
                self.military_runtime = RoleRuntime(self, self.turn_roles, military_payload, role="military")
                return asyncio.create_task(run_role(self, self.turn_roles, military_payload, record, "military"))

            initial_view = await job.connection.tool("player_turn", {})
            if "error" in initial_view:
                raise ServiceError("WAR_STATE_UNAVAILABLE", 503)
            await self.turn_roles.initialize_military(initial_view)
            self.turn_roles.start_military = start_military
            self.turn_roles.start_military_if_needed()
            self.economy_runtime = None
            await self.turn_roles.initialize_economy(initial_view)
            economy_task = asyncio.create_task(run_scheduled_economy(self, self.turn_roles, payload, record))
            advance_task = asyncio.create_task(self.turn_roles.advance())
            self.turn_roles.advance_task = advance_task
            negotiation_task = asyncio.create_task(self.turn_roles.watch_negotiation())
            prepared = await prepare_startup(self.gateway, job)
            prompt = startup_prompt(
                prepared,
                "gameplay",
                payload["prompt"],
                strategy_guide=self.strategy_guide,
                civilization_guide=self.civilization_guide,
                civilization=self.civilization,
            )
            record["startup"] = {
                "characters": len(prompt.user),
                "coreTools": [tool["name"] for tool in prepared["coreTools"]],
                "reads": list(prepared["reads"]),
                "decisionDetails": len(prepared.get("decisionDetails", [])),
                **prompt.metadata(),
            }
            record["modelPhase"] = "gameplay"
            await self._model_turn(record, job, prompt, workspace, selected, prepared)
            if job.gameplay_ended.is_set() and advance_task is not None:
                try:
                    await asyncio.shield(advance_task)
                except (OSError, SQLAlchemyError):
                    if record.get("confirmedHandoff") != record["handoff"] + 1:
                        raise
            if economy_task is not None and self.turn_roles is not None and self.turn_roles.committed:
                await asyncio.wait_for(asyncio.shield(economy_task), 60)
            if job.gameplay_ended.is_set() and self.failure is None:
                record["completionReason"] = "native_handoff"
            if self.completion not in {"completed", "interrupted"} and self.failure is None:
                self.failure = "CODEX_TURN_FAILED"
        except asyncio.CancelledError:
            self.failure = self.failure or "INTERRUPTED"
        except (
            TimeoutError,
            ServiceError,
            OSError,
            ValueError,
            KeyError,
            SQLAlchemyError,
            aiohttp.ClientError,
        ) as error:
            self.failure = self.failure or (
                error.code
                if isinstance(error, ServiceError)
                else "STATE_STORE_FAILED"
                if isinstance(error, SQLAlchemyError)
                else "CODEX_CONNECTION_FAILED"
            )
        finally:
            self.stopping = True
            # Model shutdown can fail after both roles authorized advancement. Drain an
            # already issued native handoff before cancelling its controller.
            if (
                advance_task is not None
                and self.turn_roles is not None
                and self.turn_roles.committing
                and not advance_task.done()
            ):
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(asyncio.shield(advance_task), 30)
            if (
                economy_task is not None
                and self.turn_roles is not None
                and self.turn_roles.committed
                and not economy_task.done()
            ):
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(asyncio.shield(economy_task), 60)
            if economy_task is not None:
                if not economy_task.done():
                    economy_task.cancel()
                economy_result = await asyncio.gather(economy_task, return_exceptions=True)
                if isinstance(economy_result[0], Exception) and self.failure is None:
                    self.failure = "ECONOMY_ROLE_FAILED"
            if negotiation_task is not None:
                negotiation_task.cancel()
                await asyncio.gather(negotiation_task, return_exceptions=True)
            if advance_task is not None:
                advance_task.cancel()
                await asyncio.gather(advance_task, return_exceptions=True)
            if self.turn_roles is not None:
                for military_task in self.turn_roles.military_tasks:
                    if self.turn_roles.committed and not military_task.done():
                        with contextlib.suppress(Exception):
                            await asyncio.wait_for(asyncio.shield(military_task), 60)
                    if not military_task.done():
                        military_task.cancel()
                    military_result = await asyncio.gather(military_task, return_exceptions=True)
                    if isinstance(military_result[0], Exception) and self.failure is None:
                        self.failure = "MILITARY_ROLE_FAILED"
            self.gateway.role_coordinator = None
            if self.gateway.run_failure:
                self.failure = self.gateway.run_failure["error"]
                record["runFailure"] = self.health()["runFailure"]
            record["negotiationWaitSeconds"] = job.waiting_seconds()
            record.pop("negotiationWait", None)
            self.stopping = True
            if not job.gameplay_ended.is_set():
                handoff_monitor.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await handoff_monitor
            monitor.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await monitor
            reporter.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await reporter
            permit_renewer.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await permit_renewer
            try:
                if self.process is not None:
                    await self.process.close()
            except ProcessDrainError:
                # Inference has exited; retain its worker and I/O until the
                # accepted history/accounting writes can finish safely.
                record["error"] = "HISTORY_DRAIN_FAILED"
                await self._save(record)
                raise
            except BaseException:
                # Never advertise terminal without confirmed process shutdown.
                record["error"] = "STOP_UNCONFIRMED"
                await self._save(record)
                raise
            if self.economy_runtime is not None and self.economy_runtime.process is not None:
                record["error"] = "STOP_UNCONFIRMED"
                await self._save(record)
                raise ServiceError("STOP_UNCONFIRMED", 503)
            trace_complete = await self.io.call(reconcile_provider_usage, record, self.provider_usage)
            job.model_stopped.set()
            finished = copy.deepcopy(record)
            finished["phase"] = (
                "FAILED"
                if self.gateway.run_failure
                else "COMPLETED"
                if self.failure is None
                and (self.completion == "completed" or self.completion == "interrupted" and job.gameplay_ended.is_set())
                else "INTERRUPTED"
                if self.failure == "INTERRUPTED" or self.completion == "interrupted" and self.failure is None
                else "FAILED"
            )
            finished["usageComplete"] = trace_complete or (
                self.completion == "completed"
                and finished.get("usageReported", False)
                and "accountingIncompleteReason" not in record
            )
            if "gameplayEndedAtMs" in finished:
                finished["finalizationElapsedMs"] = max(0, int(time.time() * 1000) - finished["gameplayEndedAtMs"])
            if self.failure:
                finished["error"] = self.failure
            # Completion requires durable receipts and accounting, without model notes.
            try:
                await self.gateway.close(job, finished["phase"] == "COMPLETED")
                await self._save(finished)
            except (OSError, ValueError, SQLAlchemyError):
                record["error"] = "DURABLE_RECORD_FAILED"
                with contextlib.suppress(OSError, ValueError, SQLAlchemyError):
                    await self._save(record)
                raise ServiceError("DURABLE_RECORD_FAILED", 503) from None
            record.update(finished)
            if self.chat.running:
                with contextlib.suppress(OSError, ValueError, TimeoutError, aiohttp.ClientError, ServiceError):
                    await self.flush_telemetry()
            self.process = None
            self.gameplay_permit = None

    async def close(self):
        if self.active_task is not None and not self.active_task.done():
            await self.interrupt(self.active_id)
        if self.economy_runtime is not None and self.economy_runtime.process is not None:
            await self.economy_runtime.process.close()
            self.economy_runtime.process = None
            if self.economy_runtime.job is not None:
                self.economy_runtime.job.model_stopped.set()
                await self.gateway.close(self.economy_runtime.job, False)
        if self.process is not None:
            # A failed activation can retain a stopped process whose accepted
            # notifications have not flushed yet. Never close their I/O below.
            await self.process.close()
            self.process = None
        for task in self.finalization_tasks.values():
            await asyncio.shield(task)
        if self.military_runtime is not None and self.military_runtime.process is not None:
            await self.military_runtime.process.close()
            self.military_runtime.process = None
        if self.peace_reviews.unconfirmed_process is not None:
            await self.peace_reviews.unconfirmed_process.close()
            self.peace_reviews.unconfirmed_process = None
        await self.chat.close()
        for job in list(self.gateway.jobs.values()):
            await self.gateway.close(job, False)
        await asyncio.to_thread(self.diagnostics.close)
        await self.io.close()
        if self.lock is not None:
            os.close(self.lock)
            self.lock = None

    def application(self):
        application = web.Application(client_max_size=33 * 1024 * 1024)

        async def control(request):
            if not request.secure:
                raise web.HTTPForbidden(text="HTTPS_REQUIRED")
            if "Origin" in request.headers or len(request.headers.getall("Authorization", [])) != 1:
                raise web.HTTPForbidden(text="INVALID_REQUEST_HEADERS")
            supplied = request.headers.get("Authorization", "")
            if not hmac.compare_digest(supplied.encode(), ("Bearer " + self.token).encode()):
                raise web.HTTPUnauthorized(text="AUTHENTICATION_REQUIRED")
            try:
                payload = await request.json()
            except (ValueError, UnicodeError):
                return web.json_response({"error": "INVALID_JSON"}, status=400)
            try:
                if not isinstance(payload, dict):
                    raise ServiceError("INVALID_ARGUMENT")
                operation = payload.pop("op", None)
                if operation == "start":
                    result = await self.start(payload)
                elif operation == "review_phase":
                    result = await self.review_phase(payload)
                elif operation == "review_triage":
                    result = await self.review_triage(payload)
                elif operation in {"open_interactive", "close_interactive"}:
                    from civ_agent import interactive_session

                    if operation == "open_interactive":
                        result = await interactive_session.open_session(self, payload)
                    else:
                        result = await interactive_session.close_session(self, payload)
                elif operation == "connect":
                    result = await self.chat.connect(payload)
                elif operation == "disconnect":
                    await self.chat.close()
                    result = {"connected": False}
                elif operation == "cancel_jobs":
                    if not self.gateway.paused:
                        raise ServiceError("BARRIER_REQUIRED", 409)
                    if self.active_task is not None and not self.active_task.done():
                        await self.interrupt(self.active_id)
                    await self.chat.close()
                    for job in list(self.gateway.jobs.values()):
                        await self.gateway.close(job, False)
                    result = {"stopped": True}
                elif operation == "barrier":
                    if set(payload) != {"paused"} or type(payload["paused"]) is not bool:
                        raise ServiceError("INVALID_ARGUMENT")
                    result = await service_recovery.barrier(self, payload["paused"])
                elif operation == "snapshot":
                    result = await service_recovery.capture(self, payload["id"])
                elif operation == "pending_requests":
                    if payload:
                        raise ServiceError("INVALID_ARGUMENT")
                    result = await service_recovery.pending_requests(self)
                elif operation == "settle_request":
                    result = await service_recovery.settle_request(self, payload)
                elif operation == "retain_snapshot":
                    result = await service_recovery.retain(self, payload["id"])
                elif operation == "verify_snapshot":
                    path = service_recovery.checkpoint_path(self, payload["id"])
                    await self.io.call(self.memory.verify, path, payload["sha256"])
                    result = {"valid": payload["civilization"] == self.civilization}
                elif operation == "restore":
                    result = await service_recovery.restore(self, payload)
                elif operation == "finish_handoff":
                    result = await self.finish_handoff(payload)
                elif operation == "begin_finalize":
                    result = self.begin_finalize(payload)
                elif operation == "flush_accounting":
                    result = await self.flush_accounting(payload)
                elif operation == "finalize_terminal":
                    result = await self.finalize_terminal(payload)
                elif operation == "result":
                    result = await self.result(payload)
                elif operation == "archive":
                    if set(payload) != {"activationId"} or not identifier(payload["activationId"]):
                        raise ServiceError("INVALID_ARGUMENT")
                    return web.json_response(await self.archived(payload["activationId"]))
                elif operation == "events":
                    if set(payload) - {"activationId", "cursor", "limit"} or not identifier(
                        payload.get("activationId")
                    ):
                        raise ServiceError("INVALID_ARGUMENT")
                    self.status(payload["activationId"])
                    try:
                        result = await self.io.call(
                            self.history.page,
                            payload["activationId"],
                            payload.get("cursor", 0),
                            payload.get("limit", 100),
                        )
                    except ValueError:
                        raise ServiceError("INVALID_EVENT_CURSOR") from None
                    return web.json_response(
                        result, dumps=lambda value: json.dumps(value, ensure_ascii=False, separators=(",", ":"))
                    )
                elif operation == "health":
                    if payload:
                        raise ServiceError("INVALID_ARGUMENT")
                    result = self.health()
                elif operation in {"status", "heartbeat", "interrupt"}:
                    if set(payload) - {"activationId"} or (
                        operation != "status" and not identifier(payload.get("activationId"))
                    ):
                        raise ServiceError("INVALID_ARGUMENT")
                    if operation == "status":
                        result = self.status(payload.get("activationId"))
                    else:
                        result = await getattr(self, operation)(payload["activationId"])
                else:
                    raise ServiceError("INVALID_OPERATION")
                return web.json_response({"status": result})
            except ServiceError as error:
                return web.json_response({"error": error.code}, status=error.status)
            except StateConflict as error:
                return web.json_response({"error": str(error)}, status=409)

        application.router.add_post("/agent", control)
        application.router.add_post("/mcp", self.gateway.handle)

        async def cleanup(_):
            await self.close()

        application.on_cleanup.append(cleanup)
        return application


def server_ssl_context(cert_file, key_file):
    regular_file(cert_file)
    regular_file(key_file, private=True)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.load_cert_chain(cert_file, key_file)
    return context
