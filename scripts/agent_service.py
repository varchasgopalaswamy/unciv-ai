"""One civilization's HTTPS control service and private Unix-socket Codex client.

Codex app-server uses WebSocket frames over a private Unix socket. Standard streams
are never used as a control protocol, and no unencrypted network listener is opened.
"""

import asyncio
import contextlib
import ctypes
import fcntl
import functools
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import signal
import ssl
import tempfile
import time

import aiohttp
from aiohttp import web

from agent_history import HistoryStore
from unciv_tls import codex_trust_environment, https_url, private_directory, regular_file, write_private_json


TERMINAL = {"COMPLETED", "INTERRUPTED", "FAILED"}
USAGE_KEYS = {
    "input_tokens": "inputTokens", "cached_input_tokens": "cachedInputTokens",
    "output_tokens": "outputTokens", "reasoning_output_tokens": "reasoningOutputTokens",
}


class ServiceError(Exception):
    def __init__(self, code, status=400):
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

    def __init__(self, executable, workspace, mcp_url, ca_file, notification):
        self.executable, self.workspace, self.mcp_url = executable, Path(workspace), mcp_url
        self.ca_file, self.notification = ca_file, notification
        self.process = self.session = self.socket = self.reader_task = self.temporary = None
        self.pending = {}
        self.sequence = 0

    async def start(self, token, model):
        self.temporary = tempfile.TemporaryDirectory(prefix="unciv-agent-ipc-")
        socket_path = Path(self.temporary.name) / "codex.sock"
        environment = codex_trust_environment(self.ca_file, os.environ, self.workspace)
        for name in list(environment):
            if name.startswith("UNCIV_"):
                del environment[name]
        environment["UNCIV_PLAYER_TOKEN"] = token
        config = 'mcp_servers.civ={url=' + json.dumps(self.mcp_url) + ',bearer_token_env_var="UNCIV_PLAYER_TOKEN",required=true}'
        arguments = [str(self.executable), "-c", config, "app-server", "--listen", "unix://" + str(socket_path)]
        self.process = await asyncio.create_subprocess_exec(
            *arguments, cwd=self.workspace, env=environment,
            stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL, start_new_session=True,
            preexec_fn=functools.partial(parent_death_signal, os.getpid()),
        )
        deadline = time.monotonic() + 15
        while not socket_path.exists():
            if self.process.returncode is not None or time.monotonic() >= deadline:
                raise ServiceError("CODEX_START_FAILED", 503)
            await asyncio.sleep(0.05)
        self.session = aiohttp.ClientSession(connector=aiohttp.UnixConnector(path=str(socket_path)))
        # The HTTP-shaped URL supplies WebSocket framing only. UnixConnector never opens TCP.
        self.socket = await self.session.ws_connect("http://localhost/", max_msg_size=8 * 1024 * 1024)
        self.reader_task = asyncio.create_task(self._read())
        await self.request("initialize", {"clientInfo": {"name": "civ-agent-service", "version": "1"}, "capabilities": {"experimentalApi": True}})
        await self.socket.send_json({"method": "initialized", "params": {}})

    async def request(self, method, parameters):
        self.sequence += 1
        request_id = self.sequence
        future = asyncio.get_running_loop().create_future()
        self.pending[request_id] = future
        try:
            await self.socket.send_json({"id": request_id, "method": method, "params": parameters})
            return await asyncio.wait_for(future, 30)
        finally:
            self.pending.pop(request_id, None)

    async def _read(self):
        try:
            async for frame in self.socket:
                if frame.type != aiohttp.WSMsgType.TEXT:
                    continue
                message = json.loads(frame.data)
                if "method" in message:
                    if "id" in message:
                        # No unattended approvals, user input, or external token requests.
                        await self.socket.send_json({"id": message["id"], "error": {"code": -32000, "message": "Interactive decision requires operator attention"}})
                        await self.notification("service/approvalRequired", {})
                    else:
                        await self.notification(message["method"], message.get("params", {}))
                else:
                    future = self.pending.get(message.get("id"))
                    if future is not None and not future.done():
                        if "error" in message:
                            future.set_exception(ServiceError("CODEX_REQUEST_FAILED", 502))
                        else:
                            future.set_result(message.get("result", {}))
        except (ValueError, OSError, aiohttp.ClientError):
            pass
        finally:
            for future in list(self.pending.values()):
                if not future.done():
                    future.set_exception(ServiceError("CODEX_DISCONNECTED", 502))
            await self.notification("service/disconnected", {})

    async def close(self):
        if self.process is not None:
            # The process group is dedicated to this activation. Wait for actual exit before
            # publishing a terminal phase, even when turn/interrupt did not answer.
            with contextlib.suppress(ProcessLookupError):
                os.killpg(self.process.pid, signal.SIGTERM)
            try:
                await asyncio.wait_for(self.process.wait(), 2)
            except asyncio.TimeoutError:
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(self.process.pid, signal.SIGKILL)
                await self.process.wait()
            with contextlib.suppress(ProcessLookupError):
                os.killpg(self.process.pid, signal.SIGKILL)
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
    def __init__(self, civilization, workspace, state_dir, token, mcp_url, ca_file, executable="codex", process_factory=CodexProcess):
        self.civilization = civilization
        self.workspace, self.state_dir = private_directory(workspace), private_directory(state_dir)
        self.token, self.mcp_url, self.ca_file = token, https_url(mcp_url, ("/mcp",)), ca_file
        self.executable, self.process_factory = executable, process_factory
        self.history = HistoryStore(self.state_dir / "history", token)
        self.records_dir = private_directory(self.state_dir / "activations")
        self.lock = os.open(self.state_dir / ".lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        try:
            fcntl.flock(self.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            os.close(self.lock)
            raise ServiceError("SERVICE_ALREADY_RUNNING", 409) from None
        self.records = {}
        self.known_threads = {}
        self.thread_totals = {}
        self.active_task = None
        self.active_id = None
        self.process = None
        self.stop_event = asyncio.Event()
        self.last_heartbeat = 0
        self.turn_id = None
        self.completion = None
        self.early_completions = {}
        self.early_usage = {}
        self.failure = None
        self.usage_baseline = None
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
        for file in sorted(self.records_dir.glob("*.json")):
            regular_file(file, private=True)
            record = json.loads(file.read_text())
            self.records[record["activationId"]] = record
            if record.get("threadId"):
                self.known_threads[record["threadId"]] = record["epoch"]
                previous = self.thread_totals.setdefault(record["threadId"], dict.fromkeys(USAGE_KEYS, 0))
                for key in USAGE_KEYS:
                    previous[key] = max(previous[key], record.get("threadTotals", {}).get(key, 0))
            if record["phase"] not in TERMINAL:
                record["phase"], record["error"] = "FAILED", "SERVICE_RESTARTED"
                self._save(record)

    def _save(self, record):
        write_private_json(self.records_dir / (hashlib.sha256(record["activationId"].encode()).hexdigest() + ".json"), record)
        self.history.lifecycle(record)
        # Persist the directory entry before a newly created thread can be used by turn/start.
        descriptor = os.open(self.records_dir, os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    def status(self, activation_id=None):
        if activation_id is None:
            activation_id = self.active_id
        if activation_id is None:
            return {"civilization": self.civilization, "activationId": None, "epoch": None, "threadId": None, "phase": "IDLE", "usage": dict.fromkeys(USAGE_KEYS, 0)}
        record = self.records.get(activation_id)
        if record is None:
            raise ServiceError("ACTIVATION_NOT_FOUND", 404)
        return {key: (dict(value) if isinstance(value, dict) else value) for key, value in record.items() if key not in {"fingerprint", "threadTotals", "baseline"}}

    def _validate_start(self, payload):
        required = {"activationId", "epoch", "civilization", "threadId", "mcpUrl", "playerToken", "prompt", "model", "timeoutSeconds", "leaseSeconds"}
        if set(payload) != required:
            raise ServiceError("INVALID_ARGUMENT")
        if not identifier(payload["activationId"]) or not identifier(payload["epoch"]) or payload["civilization"] != self.civilization:
            raise ServiceError("INVALID_BINDING")
        if payload["mcpUrl"] != self.mcp_url:
            raise ServiceError("INVALID_MCP_ENDPOINT")
        if not isinstance(payload["playerToken"], str) or not re.fullmatch(r"[A-Za-z0-9_-]{43}", payload["playerToken"]):
            raise ServiceError("INVALID_PLAYER_TOKEN")
        if payload["threadId"] is not None and not identifier(payload["threadId"]):
            raise ServiceError("INVALID_THREAD")
        if not isinstance(payload["prompt"], str) or not 1 <= len(payload["prompt"]) <= 1_000_000:
            raise ServiceError("INVALID_PROMPT")
        if payload["model"] is not None and (not isinstance(payload["model"], str) or not 1 <= len(payload["model"]) <= 128):
            raise ServiceError("INVALID_MODEL")
        for key, maximum in (("timeoutSeconds", 86400), ("leaseSeconds", 300)):
            if type(payload[key]) is not int or not 1 <= payload[key] <= maximum:
                raise ServiceError("INVALID_TIMEOUT")

    async def start(self, payload):
        self._validate_start(payload)
        fingerprint = hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        async with self.guard:
            previous = self.records.get(payload["activationId"])
            if previous is not None:
                if previous["fingerprint"] != fingerprint:
                    raise ServiceError("ACTIVATION_CONFLICT", 409)
                return self.status(payload["activationId"])
            if self.active_id is not None and self.records[self.active_id]["phase"] not in TERMINAL:
                raise ServiceError("AGENT_BUSY", 409)
            if payload["threadId"] is not None and self.known_threads.get(payload["threadId"]) != payload["epoch"]:
                raise ServiceError("THREAD_NOT_OWNED", 409)
            record = {
                "activationId": payload["activationId"], "epoch": payload["epoch"], "civilization": self.civilization,
                "threadId": payload["threadId"], "phase": "STARTING", "usage": dict.fromkeys(USAGE_KEYS, 0), "fingerprint": fingerprint,
            }
            self.records[record["activationId"]] = record
            self._save(record)
            self.history.begin(record, payload["prompt"], payload["playerToken"])
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

    async def heartbeat(self, activation_id):
        status = self.status(activation_id)
        if activation_id == self.active_id and status["phase"] not in TERMINAL:
            self.last_heartbeat = time.monotonic()
        return status

    async def interrupt(self, activation_id):
        status = self.status(activation_id)
        if activation_id == self.active_id and status["phase"] not in TERMINAL:
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

    async def _notification(self, method, params):
        record = self.records[self.active_id]
        self.history.notify(self.active_id, record["threadId"], method, params)
        if method == "service/approvalRequired":
            self.failure = "INTERACTIVE_DECISION_REQUIRED"
            self.stop_event.set()
        elif method == "service/disconnected":
            if self.completion is None and not self.stopping and self.failure is None:
                self.failure = "CODEX_DISCONNECTED"
                self.stop_event.set()
        elif method == "thread/tokenUsage/updated" and self.accept_usage and params.get("threadId") == record["threadId"]:
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
                self._record_usage(record, usage)
        elif method == "turn/completed" and self.accept_usage and params.get("threadId") == record["threadId"]:
            turn = params.get("turn", {})
            if not identifier(turn.get("id")):
                return
            if self.turn_id is None:
                self.early_completions[turn.get("id")] = turn.get("status")
            elif turn.get("id") == self.turn_id:
                self.completion = turn.get("status")
                self.stop_event.set()

    def _record_usage(self, record, token_usage):
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
                self.usage_baseline[key] = values[key] - previous if type(previous) is int and 0 <= previous <= values[key] else 0
        for key in USAGE_KEYS:
            record["usage"][key] = max(record["usage"][key], values[key] - self.usage_baseline[key])
        prior = self.thread_totals.setdefault(record["threadId"], dict.fromkeys(USAGE_KEYS, 0))
        for key in USAGE_KEYS:
            prior[key] = max(prior[key], record["baseline"][key] + record["usage"][key])
        record["threadTotals"] = dict(prior)
        self._save(record)

    async def _run(self, payload):
        self.run_started.set()
        record = self.records[payload["activationId"]]
        started = time.monotonic()
        workspace = private_directory(self.workspace / hashlib.sha256(record["epoch"].encode()).hexdigest())
        process = self.process_factory(self.executable, workspace, self.mcp_url, self.ca_file, self._notification)
        self.process = process
        owner = asyncio.current_task()

        async def watchdog():
            while True:
                await asyncio.sleep(0.1)
                if self.stopping:
                    return
                now = time.monotonic()
                if now - self.last_heartbeat >= payload["leaseSeconds"]:
                    self.failure = "HEARTBEAT_EXPIRED"
                elif now - started >= payload["timeoutSeconds"]:
                    self.failure = "ACTIVATION_TIMEOUT"
                else:
                    continue
                self.stop_event.set()
                owner.cancel()
                return

        monitor = asyncio.create_task(watchdog())
        try:
            await process.start(payload["playerToken"], payload["model"])
            options = {"cwd": str(workspace), "approvalPolicy": "never", "sandbox": "workspace-write"}
            if payload["model"] is not None:
                options["model"] = payload["model"]
            if record["threadId"] is None:
                result = await process.request("thread/start", options)
            else:
                result = await process.request("thread/resume", {**options, "threadId": record["threadId"], "excludeTurns": True})
            thread_id = result.get("thread", {}).get("id")
            if not identifier(thread_id) or (record["threadId"] is not None and thread_id != record["threadId"]):
                raise ServiceError("INVALID_CODEX_THREAD", 502)
            record["threadId"] = thread_id
            self.known_threads[thread_id] = record["epoch"]
            record["baseline"] = dict(self.thread_totals.get(thread_id, dict.fromkeys(USAGE_KEYS, 0)))
            record["threadTotals"] = dict(record["baseline"])
            self._save(record)
            if self.failure:
                raise ServiceError(self.failure)
            self.accept_usage = True
            result = await process.request("turn/start", {"threadId": thread_id, "input": [{"type": "text", "text": payload["prompt"]}], "approvalPolicy": "never", "summary": "auto"})
            # Only the response to our own request can bind this activation's turn.
            # A turn/started replay during resume must never replace this identity.
            self.turn_id = result.get("turn", {}).get("id")
            if not identifier(self.turn_id):
                raise ServiceError("INVALID_CODEX_TURN", 502)
            record["turnId"] = self.turn_id
            self.history.bind_turn(self.active_id, self.turn_id)
            for usage in self.early_usage.get(self.turn_id, []):
                self._record_usage(record, usage)
            self.early_usage.clear()
            if self.turn_id in self.early_completions:
                self.completion = self.early_completions[self.turn_id]
                self.stop_event.set()
            record["phase"] = "RUNNING"
            self._save(record)
            while not self.stop_event.is_set():
                now = time.monotonic()
                if now - self.last_heartbeat >= payload["leaseSeconds"]:
                    self.failure = "HEARTBEAT_EXPIRED"
                    break
                if now - started >= payload["timeoutSeconds"]:
                    self.failure = "ACTIVATION_TIMEOUT"
                    break
                with contextlib.suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(self.stop_event.wait(), 0.25)
            if self.completion not in {"completed", "interrupted"} and self.failure is None:
                self.failure = "CODEX_TURN_FAILED"
        except asyncio.CancelledError:
            self.failure = self.failure or "INTERRUPTED"
        except (ServiceError, OSError, ValueError, KeyError, asyncio.TimeoutError, aiohttp.ClientError) as error:
            self.failure = error.code if isinstance(error, ServiceError) else "CODEX_CONNECTION_FAILED"
        finally:
            self.stopping = True
            monitor.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await monitor
            try:
                await process.close()
            except BaseException:
                # Never advertise terminal without confirmed process shutdown.
                record["error"] = "STOP_UNCONFIRMED"
                self._save(record)
                raise
            record["phase"] = "COMPLETED" if self.completion == "completed" and self.failure is None else "INTERRUPTED" if self.failure == "INTERRUPTED" or self.completion == "interrupted" else "FAILED"
            if self.failure:
                record["error"] = self.failure
            self._save(record)
            self.process = None

    async def close(self):
        if self.active_task is not None and not self.active_task.done():
            await self.interrupt(self.active_id)
        if self.lock is not None:
            os.close(self.lock)
            self.lock = None

    def application(self):
        application = web.Application(client_max_size=1_100_000)

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
                elif operation == "events":
                    if set(payload) - {"activationId", "cursor", "limit"} or not identifier(payload.get("activationId")):
                        raise ServiceError("INVALID_ARGUMENT")
                    self.status(payload["activationId"])
                    try:
                        result = self.history.page(payload["activationId"], payload.get("cursor", 0), payload.get("limit", 100))
                    except ValueError:
                        raise ServiceError("INVALID_EVENT_CURSOR") from None
                    return web.json_response(result, dumps=lambda value: json.dumps(value, ensure_ascii=False, separators=(",", ":")))
                elif operation in {"status", "heartbeat", "interrupt"}:
                    if set(payload) - {"activationId"} or (operation != "status" and not identifier(payload.get("activationId"))):
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
