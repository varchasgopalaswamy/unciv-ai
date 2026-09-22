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
from civ_agent.civilization_state import CivilizationState, StateConflict
from civ_agent.diagnostics import Diagnostics
from civ_agent.host_connection import HostConnection
from civ_agent.model_status import LogLines, ModelStatus
from civ_agent.proposal_gateway import ProposalGateway
from civ_agent.protocol import AgentProcess, JsonObject, Notification
from civ_agent.schemas import StartRequest, start_error
from civ_agent.service_io import ServiceIO
from civ_agent.startup_context import prepare_startup, startup_prompt, startup_text
from civ_agent.telemetry import JobAccounting
from civ_agent.tls import codex_trust_environment, https_url, private_directory, regular_file, write_private_json
from civ_agent.turn_archive import TurnArchive

TERMINAL = {"COMPLETED", "INTERRUPTED", "FAILED"}
HISTORY_RENDER_INTERVAL = 2.0
MEMORY_PHASE_SECONDS = 300
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
        self.model_status = ModelStatus()
        self.closing = False
        self.temporary: tempfile.TemporaryDirectory[str] | None = None
        self.pending: dict[int, asyncio.Future[JsonObject]] = {}
        self.requests: dict[int, tuple[str, JsonObject]] = {}
        self.sequence = 0

    async def start(self, token: str, model: str | None) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="unciv-agent-ipc-")
        socket_path = Path(self.temporary.name) / "codex.sock"
        environment = codex_trust_environment(self.ca_file, os.environ, self.workspace)
        for name in list(environment):
            if name.startswith("UNCIV_"):
                del environment[name]
        environment["UNCIV_PLAYER_TOKEN"] = token
        # Some automatic provider retries have no app-server notification. Only
        # enable their warning target; never copy arbitrary child logs to history.
        environment["RUST_LOG"] = "off,codex_core::responses_retry=warn"
        config = player_mcp_configuration(self.mcp_url, tool_timeout_seconds=86400)
        # Models use player tools and private memory tools. Disable general host
        # inspection/execution surfaces in these narrowly scoped jobs.
        arguments = [
            self.executable,
            "-c",
            config,
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

    async def request(self, method: str, parameters: JsonObject) -> JsonObject:
        assert self.socket is not None
        self.sequence += 1
        request_id = self.sequence
        future: asyncio.Future[JsonObject] = asyncio.get_running_loop().create_future()
        self.pending[request_id] = future
        self.requests[request_id] = method, parameters
        try:
            await self.socket.send_json({"id": request_id, "method": method, "params": parameters})
            return await asyncio.wait_for(future, 30)
        finally:
            self.pending.pop(request_id, None)
            self.requests.pop(request_id, None)

    async def _model_notice(self, notice: JsonObject | None) -> None:
        if notice is not None and not self.closing:
            await self.notification("service/modelStatus", notice)

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
                if "method" in message:
                    if "id" in message:
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
                        await self.notification("service/approvalRequired", {})
                    else:
                        notice = self.model_status.observe(message["method"], message.get("params", {}))
                        await self._model_notice(notice)
                        if message["method"] in {"error", "warning"}:
                            # The normalized notice replaces arbitrary provider text.
                            continue
                        await self.notification(message["method"], message.get("params", {}))
                else:
                    future = self.pending.get(message.get("id"))
                    if future is not None and not future.done():
                        if "error" in message:
                            future.set_exception(ServiceError("CODEX_REQUEST_FAILED", 502))
                        else:
                            result = message.get("result", {})
                            method, params = self.requests.get(message.get("id"), (None, {}))
                            if method == "turn/start" and identifier(result.get("turn", {}).get("id")):
                                # Bind before consuming following frames, which can
                                # arrive before request()'s caller resumes.
                                self.model_status.begin(params["threadId"], result["turn"]["id"])
                            future.set_result(result)
        except (ValueError, OSError, aiohttp.ClientError):
            pass
        finally:
            for future in list(self.pending.values()):
                if not future.done():
                    future.set_exception(ServiceError("CODEX_DISCONNECTED", 502))
            await self.notification("service/disconnected", {})

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
        if self.reader_task is not None:
            self.reader_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self.reader_task
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
        reasoning_effort: str | None = None,
        memory_context_chars: int = 12000,
        strategy_guide: bool = False,
        fast: bool = False,
        gateway_url: str = "https://localhost:8865/mcp",
        gateway_ca_file: Path | None = None,
        connection_factory=HostConnection,
    ) -> None:
        if model is not None and not valid_setting(model):
            raise ServiceError("INVALID_MODEL")
        if reasoning_effort is not None and not valid_setting(reasoning_effort):
            raise ServiceError("INVALID_REASONING_EFFORT")
        self.default_settings = {"model": model, "reasoningEffort": reasoning_effort}
        self.strategy_guide = strategy_guide
        self.fast = fast
        self.civilization = civilization
        self.workspace, self.state_dir = private_directory(workspace), private_directory(state_dir)
        if self.state_dir.resolve().is_relative_to(self.workspace.resolve()):
            raise ServiceError("PRIVATE_STATE_INSIDE_WORKSPACE")
        self.token, self.mcp_url, self.ca_file = token, https_url(mcp_url, ("/mcp",)), ca_file
        self.executable, self.process_factory = executable, process_factory
        self.connection_factory = connection_factory
        self.history = HistoryStore(self.state_dir / "history", token, auto_render=False)
        self.io = ServiceIO()
        self.archive_cache = {}
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
        self.known_threads: dict[str, tuple[str, int]] = {}
        self.thread_settings = {}
        self.thread_totals = {}
        self.archive: TurnArchive | None = None
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
        self.usage_baseline: dict[str, int] | None = None
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
        self.memory.export(self.workspace)
        self.gateway = ProposalGateway(self.memory, self.io, self.workspace)
        self.gateway.history = self.history
        self.accounting = JobAccounting(self.state_dir / "telemetry", self.memory.memory_edit_counts)
        self.gateway.accounting = self.accounting
        self.telemetry_lock = asyncio.Lock()
        self.gateway.paused = self.memory.is_paused()
        self.gateway_url, self.gateway_ca_file = gateway_url, gateway_ca_file
        if (self.state_dir / "restore.json").exists():
            self.gateway.paused = True
        self.chat = ChatRuntime(self)
        if self.match_file.exists():
            regular_file(self.match_file, private=True)
            self.match = json.loads(self.match_file.read_text())
            self.archive = TurnArchive(
                self.state_dir / "turn-archive",
                self.workspace,
                self.civilization,
                self.match["epoch"],
                secrets=(self.token,),
            )
        for file in sorted(self.records_dir.glob("*.json")):
            regular_file(file, private=True)
            record = json.loads(file.read_text())
            self.records[record["activationId"]] = record
            if record.get("threadId"):
                self.known_threads[record["threadId"]] = (record["epoch"], record.get("gameTurn"))
                if "settings" in record and "effectiveSettings" in record:
                    self.thread_settings[record["threadId"]] = (record["settings"], record["effectiveSettings"])
                previous = self.thread_totals.setdefault(record["threadId"], dict.fromkeys(USAGE_KEYS, 0))
                for key in USAGE_KEYS:
                    previous[key] = max(previous[key], record.get("threadTotals", {}).get(key, 0))
            if record["phase"] not in TERMINAL:
                record["phase"], record["error"] = "FAILED", "SERVICE_RESTARTED"
                if record.get("memoryPhase"):
                    record["memoryPhaseComplete"] = False
                    record["frozenTurnSummary"] = self.memory.completed_summary(record["activationId"])
                self._persist(record, refresh=True)
        received = self.results.load()
        if received is not None:
            if self.match is None or self.match["epoch"] != received["epoch"]:
                raise ServiceError("RESULT_BINDING_CONFLICT", 409)
            self.results.receive(received)
        self.diagnostics = Diagnostics(self.state_dir, self.civilization)

    def _persist(self, record, *, refresh=False):
        self.accounting.sample(record, record.get("negotiationWaitSeconds", 0))
        self.history.lifecycle(record)
        if refresh:
            self._refresh(record)
        # Commit terminal metadata only after its history and archives. A crash
        # during rendering therefore still restores an unfinished activation.
        write_private_json(
            self.records_dir / (hashlib.sha256(record["activationId"].encode()).hexdigest() + ".json"), record
        )
        # Persist the directory entry before a newly created thread can be used by turn/start.
        descriptor = os.open(self.records_dir, os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    async def _save(self, record, *, refresh=False):
        # The worker owns all history access. Pass a detached record so another
        # HTTPS request cannot change an operation already queued for persistence.
        await self.io.call(self._persist, copy.deepcopy(record), refresh=refresh)

    def _refresh(self, record):
        log = self.history.logs.get(record["activationId"])
        if log is None:
            self.history.page(record["activationId"], 0, 1)
            log = self.history.logs.get(record["activationId"])
        if log is not None:
            log.render()
        if self.archive is None or "gameTurn" not in record:
            return None
        version = (
            log.sequence if log else 0,
            record["phase"],
            record.get("committedHandoff"),
            record.get("aborted", False),
            record.get("terminalOutcome", False),
            None if record.get("frozenTurnSummary") else self.memory.generation(),
        )
        previous = self.archive_cache.get(record["activationId"])
        if previous is None or previous[0] != version:
            previous = (version, self._export(record))
            # Completed transcripts can be large; retain only the most recently
            # rendered activation, not another in-memory copy of the whole match.
            self.archive_cache.clear()
            self.archive_cache[record["activationId"]] = previous
        return previous[1]

    def _export(self, record):
        assert self.archive is not None
        self.memory.export(self.workspace)
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
        return self.archive.update(
            record["gameTurn"],
            record["activationId"],
            events,
            phase=record["phase"],
            handoff=record.get("committedHandoff"),
            aborted=record.get("aborted", False),
            terminal=record.get("terminalOutcome", False),
            structured_summary=record.get("frozenTurnSummary") or self.memory.summary(record["gameTurn"]),
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
            if record.get("aborted") or "committedHandoff" in record:
                raise ServiceError("FINALIZATION_CONFLICT", 409)
            record["terminalOutcome"] = True
            await self._save(record, refresh=True)
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
            if record.get("aborted"):
                raise ServiceError("ACTIVATION_ABORTED", 409)
            if record.get("terminalOutcome"):
                raise ServiceError("FINALIZATION_CONFLICT", 409)
            if payload["committedHandoff"] != record["handoff"] + 1:
                raise ServiceError("INVALID_HANDOFF", 409)
            if record.get("committedHandoff", payload["committedHandoff"]) != payload["committedHandoff"]:
                raise ServiceError("FINALIZATION_CONFLICT", 409)
            record["committedHandoff"] = payload["committedHandoff"]
            if "finalizationFinishedAtMs" not in record:
                await self._save(record, refresh=True)
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
            # when the archive and its completion marker were already saved.
            if self.chat.running:
                await self.flush_telemetry()
            return self.status(payload["activationId"])

    def begin_finalize(self, payload):
        """Schedule idempotent finalization; HTTP never waits for file generation."""
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
        if record.get("aborted") or record.get("terminalOutcome"):
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
        """Accept operator proof and fence this job without waiting for rendering."""
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
            if record.get("aborted") or record.get("terminalOutcome"):
                raise ServiceError("FINALIZATION_CONFLICT", 409)
            if payload["committedHandoff"] != record["handoff"] + 1:
                raise ServiceError("INVALID_HANDOFF", 409)
            # Finalization workers repeat this proof after the scheduler has
            # accepted it. Memory may now be frozen for a round checkpoint;
            # the already-established memory phase needs no new database write.
            if record.get("confirmedHandoff") != payload["committedHandoff"]:
                for job in list(self.gateway.jobs.values()):
                    if job.id == record["activationId"]:
                        async with job.lock:
                            await self.io.call(self.memory.begin_memory_phase, job.id)
                            job.gameplay_fenced = True
                            if job.handoff_at is None:
                                job.handoff_at = time.monotonic()
                            job.gameplay_ended.set()
            record["confirmedHandoff"] = payload["committedHandoff"]
            await self._save(record)
            return self.status(record["activationId"])

    async def abort(self, payload: JsonObject) -> JsonObject:
        if (
            set(payload) != {"activationId", "epoch", "gameTurn", "handoff"}
            or not identifier(payload.get("activationId"))
            or not identifier(payload.get("epoch"))
        ):
            raise ServiceError("INVALID_ARGUMENT")
        if any(type(payload[key]) is not int or payload[key] < 0 for key in ("gameTurn", "handoff")):
            raise ServiceError("INVALID_ARGUMENT")
        async with self.guard:
            record: JsonObject | None = self.records.get(payload["activationId"])
            if record is None:
                if self.active_id is not None and self.records[self.active_id]["phase"] not in TERMINAL:
                    raise ServiceError("AGENT_BUSY", 409)
                if self.match is not None and self.match["epoch"] != payload["epoch"]:
                    raise ServiceError("WORKSPACE_EPOCH_CONFLICT", 409)
                if self.match is None:
                    self.archive = await self.io.call(
                        TurnArchive,
                        self.state_dir / "turn-archive",
                        self.workspace,
                        self.civilization,
                        payload["epoch"],
                        secrets=(self.token,),
                    )
                    self.match = {"epoch": payload["epoch"]}
                    await self.io.call(write_private_json, self.match_file, dict(self.match))
                record = {
                    **payload,
                    "civilization": self.civilization,
                    "threadId": None,
                    "phase": "INTERRUPTED",
                    "error": "NOT_STARTED",
                    "aborted": True,
                    "usage": dict.fromkeys(USAGE_KEYS, 0),
                    "turnDirectory": f"turn_{payload['gameTurn']}",
                }
                self.records[record["activationId"]] = record
            if (payload["epoch"], payload["gameTurn"], payload["handoff"]) != (
                record["epoch"],
                record["gameTurn"],
                record["handoff"],
            ):
                raise ServiceError("INVALID_BINDING", 409)
            if record["phase"] not in TERMINAL:
                raise ServiceError("AGENT_BUSY", 409)
            if "committedHandoff" in record or record.get("terminalOutcome"):
                raise ServiceError("GAME_TURN_ALREADY_COMMITTED", 409)
            record["aborted"] = True
            await self._save(record, refresh=True)
            return self.status(payload["activationId"])

    async def archived(self, activation_id: str) -> JsonObject:
        self.status(activation_id)
        record = self.records[activation_id]
        archive = await self.io.call(self._refresh, copy.deepcopy(record))
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
                "usage": dict.fromkeys(USAGE_KEYS, 0),
            }
        record = self.records.get(activation_id)
        if record is None:
            raise ServiceError("ACTIVATION_NOT_FOUND", 404)
        result = {
            key: (dict(value) if isinstance(value, dict) else value)
            for key, value in record.items()
            if key not in {"fingerprint", "threadTotals", "baseline", "frozenTurnSummary"}
        }
        result["memoryTimeoutSeconds"] = MEMORY_PHASE_SECONDS
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
        return result

    def health(self):
        failure = self.gateway.run_failure
        return {
            "civilization": self.civilization,
            "runFailure": {key: failure[key] for key in ("error", "diagnosticId", "job")} if failure else None,
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

    async def start(self, payload: JsonObject) -> JsonObject:
        self._validate_start(payload)
        if self.gateway.run_failure:
            raise ServiceError(self.gateway.run_failure["error"], 409)
        fingerprint = hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        async with self.guard:
            if await self.io.call(self.results.load) is not None or any(
                record.get("terminalOutcome") for record in self.records.values()
            ):
                raise ServiceError("GAME_FINISHED", 409)
            previous = self.records.get(payload["activationId"])
            if previous is not None:
                if previous.get("aborted"):
                    raise ServiceError("ACTIVATION_ABORTED", 409)
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
                if (record["epoch"], record.get("gameTurn")) == (payload["epoch"], payload["gameTurn"])
            ]
            if any("committedHandoff" in record for record in same_turn):
                raise ServiceError("GAME_TURN_ALREADY_COMMITTED", 409)
            game_turns = {
                record["gameTurn"]
                for record in self.records.values()
                if record["epoch"] == payload["epoch"] and "gameTurn" in record
            }
            if game_turns and payload["gameTurn"] < max(game_turns):
                raise ServiceError("GAME_TURN_STALE", 409)
            committed_turns = {record["gameTurn"] for record in self.records.values() if "committedHandoff" in record}
            if any(turn < payload["gameTurn"] and turn not in committed_turns for turn in game_turns):
                raise ServiceError("PREVIOUS_TURN_NOT_FINALIZED", 409)
            if payload["threadId"] is not None and self.known_threads.get(payload["threadId"]) != (
                payload["epoch"],
                payload["gameTurn"],
            ):
                raise ServiceError("THREAD_NOT_OWNED", 409)
            if payload["threadId"] is not None and any(
                record.get("threadId") == payload["threadId"] and record.get("aborted") for record in same_turn
            ):
                raise ServiceError("THREAD_ABORTED", 409)
            pinned = self.thread_settings.get(payload["threadId"])
            if pinned is not None:
                settings, effective_settings = pinned
                for key in self.default_settings:
                    if payload.get(key) is not None and payload[key] not in (settings[key], effective_settings[key]):
                        raise ServiceError("THREAD_SETTINGS_CONFLICT", 409)
                settings = dict(settings)
            else:
                defaults = (
                    self.match.get("effectiveSettings", self.default_settings) if self.match else self.default_settings
                )
                settings = {
                    key: payload.get(key) if payload.get(key) is not None else default
                    for key, default in defaults.items()
                }
            if self.match is None:
                self.archive = await self.io.call(
                    TurnArchive,
                    self.state_dir / "turn-archive",
                    self.workspace,
                    self.civilization,
                    payload["epoch"],
                    secrets=(self.token, payload["playerToken"]),
                )
                self.match = {"epoch": payload["epoch"]}
                await self.io.call(write_private_json, self.match_file, dict(self.match))
            prompt = payload["prompt"] + f"\n\nTurn {payload['gameTurn']}; files: turn_{payload['gameTurn']}/."
            if payload["threadId"] is None:
                assert self.archive is not None
                await self.io.call(self.memory.bind, payload["epoch"])
                snapshot = await self.io.call(self.memory.snapshot, "gameplay")
                payload = {**payload, "_memorySnapshot": snapshot}
            else:
                prompt += "\nThis is recovery of the same unfinished game turn. Reobserve current host state before retrying commands."
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
                "fingerprint": fingerprint,
                "settings": settings,
                "requestedServiceTier": "fast" if self.fast else None,
            }
            if pinned is not None:
                record["effectiveSettings"] = dict(pinned[1])
            self.records[record["activationId"]] = record
            await self._save(record)
            # Record model input after preparation, in its actual developer/user order.
            await self.io.call(self.history.begin, copy.deepcopy(record), None, payload["playerToken"])
            await self.io.call(self._refresh, copy.deepcopy(record))
            self.active_id = record["activationId"]
            self.stop_event = asyncio.Event()
            self.last_heartbeat = time.monotonic()
            self.turn_id = self.completion = self.failure = self.usage_baseline = None
            self.early_completions = {}
            self.early_usage = {}
            self.accept_usage = self.stopping = False
            self.run_started = asyncio.Event()
            self.active_task = asyncio.create_task(self._run(payload))
            return self.status()

    async def flush_telemetry(self, *, connection=None, epoch=None):
        if connection is None and self.chat.connection is None:
            return
        async with self.telemetry_lock:
            for report in await self.io.call(self.accounting.pending):
                fields = {"job": report["job"], "usage": report["usage"], "telemetry": report["telemetry"]}
                response = (
                    await connection.tool("player_communication", {"op": "job_report", "epoch": epoch, **fields})
                    if connection is not None
                    else await self.chat.social("job_report", **fields)
                )
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
            if self.chat.running and self.gameplay_permit == activation_id:
                await self.io.call(self.accounting.sample, self.records[activation_id])
                await self.flush_telemetry()
                grant = await self.chat.social("job_heartbeat", job=activation_id)
                if not grant["granted"]:
                    self.failure = "JOB_PERMIT_EXPIRED"
                    self.stop_event.set()
                    for job in self.gateway.jobs.values():
                        if job.id == activation_id:
                            job.closed = True
                    if self.active_task is not None:
                        self.active_task.cancel()
        return status

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
        try:
            if method in HISTORY_METHODS:
                await self.io.call(self.history.notify, self.active_id, record["threadId"], method, params)
            if (
                method == "service/modelStatus"
                and params.get("threadId") == record["threadId"]
                and params.get("turnId") == record.get("turnId")
            ):
                record["modelStatus"] = {
                    key: value for key, value in params.items() if key not in {"threadId", "turnId"}
                }
                self.diagnostics.emit("model_status", activationId=self.active_id, **params)
                await self._save(record)
        except (OSError, ValueError):
            self.failure = "HISTORY_WRITE_FAILED"
            self.stop_event.set()
            raise
        if method == "service/approvalRequired":
            self.failure = "INTERACTIVE_DECISION_REQUIRED"
            self.stop_event.set()
        elif method == "service/disconnected":
            if self.completion is None and not self.stopping and self.failure is None:
                self.failure = "CODEX_DISCONNECTED"
                self.stop_event.set()
        elif (
            method == "thread/tokenUsage/updated" and self.accept_usage and params.get("threadId") == record["threadId"]
        ):
            turn_id = params.get("turnId")
            usage = params.get("tokenUsage")
            if not identifier(turn_id) or not isinstance(usage, dict):
                return
            if self.turn_id is None:
                # Keep the first update as well as the latest: total-last in the first
                # update establishes this process's counter baseline before its work.
                pending = self.early_usage.setdefault(turn_id, [usage, usage])
                pending[1] = usage
            elif turn_id == self.turn_id:
                await self._record_usage(record, usage)
        elif method == "turn/completed" and self.accept_usage and params.get("threadId") == record["threadId"]:
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
        if not isinstance(total, dict):
            return
        values = {key: total.get(field, 0) for key, field in USAGE_KEYS.items()}
        if any(type(value) is not int or value < 0 for value in values.values()):
            return
        if self.usage_baseline is None:
            # A new native app-server resets total counters even when resuming a
            # durable thread. Other servers may hydrate cumulative totals instead.
            # The first matched update's total-last handles both; persisted lifetime
            # accounting is deliberately separate from these process-local counters.
            last = token_usage.get("last", {})
            if not isinstance(last, dict):
                last = {}
            self.usage_baseline = {}
            for key, field in USAGE_KEYS.items():
                previous = last.get(field)
                self.usage_baseline[key] = (
                    values[key] - previous if type(previous) is int and 0 <= previous <= values[key] else 0
                )
        for key in USAGE_KEYS:
            record["usage"][key] = max(record["usage"][key], values[key] - self.usage_baseline[key])
        prior = self.thread_totals.setdefault(record["threadId"], dict.fromkeys(USAGE_KEYS, 0))
        for key in USAGE_KEYS:
            prior[key] = max(prior[key], record["baseline"][key] + record["usage"][key])
        record["threadTotals"] = dict(prior)
        record["usageReported"] = all(field in total for field in USAGE_KEYS.values())
        await self._save(record)

    async def _run(self, payload: JsonObject) -> None:
        try:
            await self._run_job(payload)
        except (OSError, ValueError, ServiceError, aiohttp.ClientError) as error:
            # Startup can fail before a child process or its shutdown handler
            # exists. Publish a terminal result and release interruption waiters.
            self.diagnostics.emit(
                "gameplay_job_failed", error=error, activationId=payload["activationId"], gameTurn=payload["gameTurn"]
            )
            record = self.records[payload["activationId"]]
            if self.process is None and record["phase"] not in TERMINAL:
                record.update(phase="FAILED", error="JOB_START_FAILED")
                await self._save(record, refresh=True)
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
            await self._save(record, refresh=True)
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
        job.memory_after_handoff = True
        process = self.process_factory(
            self.executable, workspace, self.gateway_url, self.gateway_ca_file, self._notification
        )
        self.process = process
        owner = asyncio.current_task()
        assert owner is not None

        async def watchdog():
            while True:
                await asyncio.sleep(0.1)
                if self.stopping:
                    return
                now = time.monotonic()
                if self.gateway.run_failure:
                    self.failure = self.gateway.run_failure["error"]
                elif now - self.last_heartbeat >= payload["leaseSeconds"]:
                    self.failure = "HEARTBEAT_EXPIRED"
                elif job.handoff_at is not None and now - job.handoff_at >= MEMORY_PHASE_SECONDS:
                    self.failure = "MEMORY_PHASE_TIMEOUT"
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
                record["memoryPhase"] = True
                record["completionReason"] = "native_handoff"
                await self._save(record)
            except (OSError, ValueError, SQLAlchemyError):
                self.failure = "HANDOFF_RECORD_FAILED"
                self.stop_event.set()
                owner.cancel()

        handoff_monitor = asyncio.create_task(handoff_watcher())

        async def render_history():
            try:
                while True:
                    await asyncio.sleep(HISTORY_RENDER_INTERVAL)
                    await self.io.call(self._refresh, copy.deepcopy(record))
            except (OSError, ValueError):
                self.failure = "HISTORY_WRITE_FAILED"
                self.stop_event.set()
                owner.cancel()

        renderer = asyncio.create_task(render_history())
        self.run_started.set()
        try:
            # Preserve resolved settings across new app-server processes even when
            # service defaults or the local Codex configuration have changed.
            selected = record.get("effectiveSettings", record["settings"])
            if self.chat.running:
                while not (await self.chat.social("job_claim", job=record["activationId"], purpose="gameplay"))[
                    "granted"
                ]:
                    await asyncio.sleep(1)
                self.gameplay_permit = record["activationId"]
            await self.io.call(
                self.accounting.start,
                record["activationId"],
                record["epoch"],
                self.gameplay_permit == record["activationId"],
            )
            prepared = await prepare_startup(self.gateway, job)
            prompt = startup_prompt(prepared, "gameplay", payload["prompt"], strategy_guide=self.strategy_guide)
            record["startup"] = {
                "characters": len(startup_text(prepared, strategy_guide=self.strategy_guide)),
                "coreTools": [tool["name"] for tool in prepared["coreTools"]],
                "reads": list(prepared["reads"]),
                **prompt.metadata(),
            }
            await self.io.call(
                self.history.startup_context, record["activationId"], prompt.instructions, role="developer"
            )
            await self.io.call(self.history.startup_context, record["activationId"], prompt.user)
            await process.start(job.token, selected["model"])
            options: JsonObject = {
                "cwd": str(workspace),
                "approvalPolicy": "never",
                "sandbox": "workspace-write",
                "developerInstructions": prompt.instructions,
                **codex_thread_settings(selected, fast=self.fast),
            }
            if record["threadId"] is None:
                result = await process.request("thread/start", options)
            else:
                result = await process.request(
                    "thread/resume", {**options, "threadId": record["threadId"], "excludeTurns": True}
                )
            thread_id = result.get("thread", {}).get("id")
            if not identifier(thread_id) or (record["threadId"] is not None and thread_id != record["threadId"]):
                raise ServiceError("INVALID_CODEX_THREAD", 502)
            if record["threadId"] is None and thread_id in self.known_threads:
                raise ServiceError("INVALID_CODEX_THREAD", 502)
            if thread_id in self.known_threads and self.known_threads[thread_id] != (
                record["epoch"],
                record["gameTurn"],
            ):
                raise ServiceError("INVALID_CODEX_THREAD", 502)
            effective_settings = {"model": result.get("model"), "reasoningEffort": result.get("reasoningEffort")}
            record["serviceTier"] = result.get("serviceTier")
            if not valid_setting(effective_settings["model"]) or (
                effective_settings["reasoningEffort"] is not None
                and not valid_setting(effective_settings["reasoningEffort"])
            ):
                raise ServiceError("INVALID_CODEX_SETTINGS", 502)
            if "effectiveSettings" in record and effective_settings != record["effectiveSettings"]:
                raise ServiceError("CODEX_SETTINGS_CHANGED", 409)
            if (
                selected["reasoningEffort"] is not None
                and effective_settings["reasoningEffort"] != selected["reasoningEffort"]
            ):
                raise ServiceError("CODEX_REASONING_EFFORT_NOT_APPLIED", 502)
            record["effectiveSettings"] = effective_settings
            record["threadId"] = thread_id
            self.known_threads[thread_id] = (record["epoch"], record["gameTurn"])
            self.thread_settings[thread_id] = (dict(record["settings"]), dict(effective_settings))
            assert self.match is not None
            self.match["effectiveSettings"] = dict(effective_settings)
            await self.io.call(write_private_json, self.match_file, copy.deepcopy(self.match))
            record["baseline"] = dict(self.thread_totals.get(thread_id, dict.fromkeys(USAGE_KEYS, 0)))
            record["threadTotals"] = dict(record["baseline"])
            await self._save(record)
            if self.failure:
                raise ServiceError(self.failure)
            self.accept_usage = True
            turn_options = {
                "threadId": thread_id,
                "input": [{"type": "text", "text": prompt.user}],
                "approvalPolicy": "never",
                "summary": "auto",
                "model": effective_settings["model"],
                **({"serviceTier": "fast"} if self.fast else {}),
            }
            if effective_settings["reasoningEffort"] is not None:
                turn_options["effort"] = effective_settings["reasoningEffort"]
            result = await process.request("turn/start", turn_options)
            # Only the response to our own request can bind this activation's turn.
            # A turn/started replay during resume must never replace this identity.
            turn_id = result.get("turn", {}).get("id")
            if not identifier(turn_id):
                raise ServiceError("INVALID_CODEX_TURN", 502)
            record["turnId"] = turn_id
            await self.io.call(self.history.bind_turn, self.active_id, turn_id)
            # Keep buffering usage until binding finishes, so a concurrent native
            # update cannot establish the baseline ahead of the first update.
            self.turn_id = turn_id
            for usage in self.early_usage.get(self.turn_id, []):
                await self._record_usage(record, usage)
            self.early_usage.clear()
            if self.turn_id in self.early_completions:
                self.completion = self.early_completions[self.turn_id]
                self.stop_event.set()
            record["phase"] = "RUNNING"
            await self._save(record)
            while not self.stop_event.is_set():
                now = time.monotonic()
                if now - self.last_heartbeat >= payload["leaseSeconds"]:
                    self.failure = "HEARTBEAT_EXPIRED"
                    break
                with contextlib.suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(self.stop_event.wait(), 0.25)
            if job.gameplay_ended.is_set() and self.failure is None:
                record["completionReason"] = "native_handoff"
            if self.completion not in {"completed", "interrupted"} and self.failure is None:
                self.failure = "CODEX_TURN_FAILED"
        except asyncio.CancelledError:
            self.failure = self.failure or "INTERRUPTED"
        except (TimeoutError, ServiceError, OSError, ValueError, KeyError, aiohttp.ClientError) as error:
            self.failure = self.failure or (
                error.code if isinstance(error, ServiceError) else "CODEX_CONNECTION_FAILED"
            )
        finally:
            if self.gateway.run_failure:
                self.failure = self.gateway.run_failure["error"]
                record["runFailure"] = self.health()["runFailure"]
            record["negotiationWaitSeconds"] = job.waiting_seconds()
            self.stopping = True
            if not job.gameplay_ended.is_set():
                handoff_monitor.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await handoff_monitor
            monitor.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await monitor
            renderer.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await renderer
            try:
                await process.close()
            except BaseException:
                # Never advertise terminal without confirmed process shutdown.
                record["error"] = "STOP_UNCONFIRMED"
                await self._save(record)
                raise
            job.model_stopped.set()
            finished = copy.deepcopy(record)
            finished["phase"] = (
                "FAILED"
                if self.gateway.run_failure
                else "COMPLETED"
                if self.completion == "completed" and self.failure is None
                else "INTERRUPTED"
                if self.failure == "INTERRUPTED" or self.completion == "interrupted"
                else "FAILED"
            )
            finished["usageComplete"] = (
                self.completion == "completed"
                and finished.get("usageReported", False)
                and "accountingIncompleteReason" not in record
            )
            if "gameplayEndedAtMs" in finished:
                finished["finalizationElapsedMs"] = max(0, int(time.time() * 1000) - finished["gameplayEndedAtMs"])
            if self.failure:
                finished["error"] = self.failure
            # A committed handoff has durable receipts and stopped model work.
            # Its archive rendering is deferred to operator finalization; other
            # terminal paths retain their synchronous recovery archive flush.
            try:
                await self.gateway.close(job, finished["phase"] == "COMPLETED")
                if job.gameplay_ended.is_set():
                    finished["frozenTurnSummary"] = await self.io.call(self.memory.completed_summary, job.id)
                    finished["memoryPhaseComplete"] = finished["phase"] == "COMPLETED"
                await self._save(finished, refresh=not job.gameplay_ended.is_set())
            except (OSError, ValueError):
                record["error"] = "HISTORY_WRITE_FAILED"
                with contextlib.suppress(OSError, ValueError):
                    await self._save(record)
                raise ServiceError("HISTORY_WRITE_FAILED", 503) from None
            record.update(finished)
            if self.chat.running:
                with contextlib.suppress(OSError, ValueError, TimeoutError, aiohttp.ClientError):
                    await self.flush_telemetry()
                    await self.chat.social("job_release", job=record["activationId"], usage=finished["usage"])
            self.process = None
            self.gameplay_permit = None

    async def close(self):
        if self.active_task is not None and not self.active_task.done():
            await self.interrupt(self.active_id)
        for task in self.finalization_tasks.values():
            await asyncio.shield(task)
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
                if not isinstance(payload, dict):
                    raise ServiceError("INVALID_ARGUMENT")
                operation = payload.pop("op", None)
                if operation == "start":
                    result = await self.start(payload)
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
                elif operation == "abort":
                    result = await self.abort(payload)
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
            except (ValueError, UnicodeError):
                return web.json_response({"error": "INVALID_JSON"}, status=400)

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
