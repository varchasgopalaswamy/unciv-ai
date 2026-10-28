"""Trusted local turn supervisor. Operator credentials never enter a model process."""

import argparse
import fcntl
import json
import os
import re
import signal
import ssl
import stat
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from sqlalchemy.exc import SQLAlchemyError

from civ_agent.agent_results import ResultError, validate_result
from civ_agent.agent_settings import add_settings_arguments, valid_setting
from civ_agent.decisions_policy import MAX_DECISIONS_TIMEOUT_SECONDS
from civ_agent.match_recovery import TurnCoordinator
from civ_agent.monitoring import MonitorStore, read_record
from civ_agent.round_review import review_round
from civ_agent.runner_history import EventHistory, HistoryError
from civ_agent.terminal_output import TerminalOutput
from civ_agent.turn_display import display_turn
from civ_agent.turn_finalization import finish_turn

RUN_STATE_VERSION = 5


class RunnerError(Exception):
    pass


class QuotaExhausted(RunnerError):
    pass


class OperatorError(RunnerError):
    def __init__(self, message, code=None, status=None):
        super().__init__(message)
        self.code, self.status = code, status


def positive(value):
    try:
        number = int(value)
        if number <= 0:
            raise ValueError()
        return number
    except ValueError:
        raise argparse.ArgumentTypeError("must be a positive integer") from None


def atomic_json(path, value):
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=".runner-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        parent = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(parent)
        finally:
            os.close(parent)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def read_json(path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, UnicodeError):
        raise RunnerError(f"Invalid JSON in {path}; inspect it before resuming.") from None


def operator_token(path):
    try:
        with path.open("rb") as source:
            mode = os.fstat(source.fileno()).st_mode
            if not stat.S_ISREG(mode) or stat.S_IMODE(mode) & ~0o600:
                raise RunnerError(
                    "Operator credentials must be a regular file with owner-only permissions (chmod 600)."
                )
            content = source.read(65537)
        value = json.loads(content)
        if len(content) > 65536 or not isinstance(value, dict) or set(value) != {"token"}:
            raise ValueError()
        token = value["token"]
        if not isinstance(token, str) or not re.fullmatch(r"[A-Za-z0-9_-]{43}", token):
            raise ValueError()
        return token
    except FileNotFoundError:
        raise RunnerError(
            "Operator credentials are missing. Restart civ-agent host start with operator support first."
        ) from None
    except (ValueError, UnicodeError, TypeError):
        raise RunnerError("Invalid operator credentials file.") from None


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise OperatorError("Authenticated control endpoints must not redirect.")


def secure_url(value):
    if not isinstance(value, str):
        raise RunnerError("Control endpoints must be HTTPS URL strings.")
    parsed = urllib.parse.urlsplit(value)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise RunnerError("Control endpoints must be HTTPS URLs without credentials, queries or fragments.")
    return value.rstrip("/")


class JsonEndpoint:
    endpoint_role = "control endpoint"

    def __init__(self, url, token, ca_file, timeout=15):
        self.url, self.token, self.timeout = secure_url(url), token, timeout
        self.context = ssl.create_default_context(cafile=str(ca_file) if ca_file else None)
        # Direct connections keep operator/service credentials out of HTTP proxies.
        self.opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}),
            urllib.request.HTTPSHandler(context=self.context),
            NoRedirect(),
        )

    def call(self, operation, *, _timeout=None, **fields):
        operation_name = (
            operation
            if isinstance(operation, str) and re.fullmatch(r"[a-z_]{1,40}", operation)
            else "unknown operation"
        )
        diagnostic = f"{self.endpoint_role} {operation_name}"
        request = urllib.request.Request(
            self.url,
            data=json.dumps(dict(op=operation, **fields)).encode(),
            headers={"Authorization": "Bearer " + self.token, "Content-Type": "application/json"},
            method="POST",
        )
        try:
            maximum = (
                32 * 1024 * 1024
                if operation in {"archive", "results", "snapshot", "pending_requests", "resolve_request"}
                else 300 * 1024
                if operation == "events"
                else 65536
            )
            timeout = (
                max(self.timeout, 60)
                if operation
                in {
                    "connect",
                    "finalize_terminal",
                    "interrupt",
                    "archive",
                    "snapshot",
                    "pending_requests",
                    "verify_snapshot",
                    "restore",
                    "cancel_jobs",
                }
                or operation == "review_phase"
                and fields.get("stage") == "start"
                else self.timeout
            )
            if operation == "review_triage":
                # Triage may reconcile an earlier broker receipt, then submit
                # when it was never received. Each has its own Decisions bound.
                # TurnCoordinator renews the host lease throughout this wait.
                timeout = max(timeout, 2 * MAX_DECISIONS_TIMEOUT_SECONDS + 30)
            if _timeout is not None:
                timeout = min(timeout, _timeout)
            with self.opener.open(request, timeout=timeout) as response:
                body = response.read(maximum + 1)
            value = json.loads(body)
            if len(body) > maximum or not isinstance(value, dict):
                raise ValueError()
            return value
        except urllib.error.HTTPError as failure:
            code = "REQUEST_REJECTED"
            try:
                detail = json.loads(failure.read(4096)).get("error")
                if isinstance(detail, str) and re.fullmatch(r"[A-Z_]{1,80}", detail):
                    code = detail
            except (ValueError, AttributeError):
                pass
            finally:
                failure.close()
            raise OperatorError(
                f"HTTPS {diagnostic} rejected: HTTP {failure.code} {code}.", code, failure.code
            ) from None
        except (urllib.error.URLError, TimeoutError, OSError) as failure:
            cause = (
                failure.reason
                if isinstance(failure, urllib.error.URLError) and isinstance(failure.reason, BaseException)
                else failure
            )
            raise OperatorError(
                f"Cannot reach or verify HTTPS {diagnostic} ({type(cause).__name__}); stopped for inspection.",
                "TRANSPORT_TIMEOUT" if isinstance(cause, TimeoutError) else None,
            ) from None
        except (ValueError, KeyError, TypeError) as failure:
            raise OperatorError(
                f"Invalid HTTPS {diagnostic} response ({type(failure).__name__}); stopped for inspection."
            ) from None


class Operator(JsonEndpoint):
    endpoint_role = "host operator"

    def __init__(self, host_url, token, ca_file, timeout=15):
        self.host_url = secure_url(host_url)
        super().__init__(self.host_url + "/operator", token, ca_file, timeout)
        self._lease_lock = threading.Lock()
        self._lease_deadline = None
        self._server_instance = None

    def call(self, operation, **fields):
        began = time.monotonic()
        with self._lease_lock:
            lease_deadline = self._lease_deadline
        deadline = min(began + 2 * self.timeout, lease_deadline - 5) if lease_deadline else began + 2 * self.timeout
        value = None
        for attempt in range(2):
            try:
                value = super().call(
                    operation, _timeout=None if attempt == 0 else deadline - time.monotonic(), **fields
                )
                break
            except OperatorError as error:
                if (
                    attempt
                    or operation not in {"status", "heartbeat"}
                    or error.code != "TRANSPORT_TIMEOUT"
                    or deadline - time.monotonic() < 0.1
                ):
                    raise
        try:
            if not isinstance(value, dict):
                raise ValueError()
            status = value["status"]
            if not isinstance(status, dict):
                raise ValueError()
            for key in ("epoch", "serverInstance", "phase"):
                if not isinstance(status.get(key), str):
                    raise ValueError()
            if status["phase"] not in {"ACTIVE", "PAUSED", "FINISHED", "HALTED"}:
                raise ValueError()
            if any(type(status.get(key)) is not int or status[key] < 0 for key in ("turn", "handoff")):
                raise ValueError()
            war_turn = status.get("warDeclarationTurn")
            if war_turn is not None and (type(war_turn) is not int or war_turn < 0):
                raise ValueError()
            with self._lease_lock:
                if self._server_instance is not None and self._server_instance != status["serverInstance"]:
                    self._lease_deadline = None
                self._server_instance = status["serverInstance"]
                remaining = status.get("leaseExpiresInMs")
                if operation in {"acquire", "heartbeat"} and status.get("leaseId") == fields.get("leaseId"):
                    if type(remaining) is int and remaining > 0:
                        self._lease_deadline = began + remaining / 1000
                elif not status.get("leaseId"):
                    self._lease_deadline = None
            return value
        except (ValueError, KeyError, TypeError):
            raise OperatorError("Invalid operator response; stopped for inspection.") from None


class Agent(JsonEndpoint):
    endpoint_role = "agent service"
    TERMINAL = {"COMPLETED", "INTERRUPTED", "FAILED"}

    def __init__(self, configuration):
        self.civilization = configuration["civilizationId"]
        token = operator_token(Path(configuration["tokenFile"]).expanduser())
        ca_file = configuration.get("caFile")
        super().__init__(configuration["url"], token, Path(ca_file).expanduser() if ca_file else None)

    def call(self, operation, **fields):
        result = super().call(operation, **fields)
        if operation in {
            "connect",
            "disconnect",
            "barrier",
            "snapshot",
            "pending_requests",
            "settle_request",
            "verify_snapshot",
            "retain_snapshot",
            "restore",
            "cancel_jobs",
            "open_interactive",
            "close_interactive",
            "flush_accounting",
            "health",
            "review_phase",
            "review_triage",
        }:
            status = result.get("status")
            if not isinstance(status, dict):
                raise OperatorError("Invalid agent coordination response")
            return status
        try:
            status = result["status"]
            if not isinstance(status, dict) or status.get("civilization") != self.civilization:
                raise ValueError()
            if status.get("activationId") != fields["activationId"]:
                raise ValueError()
            if status.get("phase") not in self.TERMINAL | {"STARTING", "RUNNING"}:
                raise ValueError()
            if not isinstance(status.get("epoch"), str):
                raise ValueError()
            if any(type(status.get(key)) is not int or status[key] < 0 for key in ("gameTurn", "handoff")):
                raise ValueError()
            thread = status.get("threadId")
            if thread is not None and (not isinstance(thread, str) or not thread.strip()):
                raise ValueError()
            return status
        except (ValueError, TypeError, KeyError):
            raise OperatorError("Invalid agent-service response; stopped for inspection.") from None

    def result(self, payload):
        try:
            validate_result(payload)
        except ResultError:
            raise OperatorError("Invalid player result from host; no result was delivered.") from None
        if payload["civilization"] != self.civilization:
            raise OperatorError("Result belongs to a different civilization.")
        value = super().call("result", **payload)
        expected = {"civilization": self.civilization, "epoch": payload["epoch"], "received": True}
        if value.get("status") != expected or value["status"].get("received") is not True:
            raise OperatorError("Agent service did not confirm final result delivery.")
        return expected

    def events(self, activation, cursor):
        page = super().call("events", activationId=activation, cursor=cursor, limit=100)
        if (
            not isinstance(page.get("events"), list)
            or type(page.get("nextCursor")) is not int
            or page["nextCursor"] < cursor
            or type(page.get("hasMore")) is not bool
            or type(page.get("available")) is not bool
        ):
            raise OperatorError("Invalid agent event page.")
        return page


def agent_services(state_dir, config_file=None):
    if config_file:
        configuration = read_json(config_file)
        entries = configuration.get("agents") if isinstance(configuration, dict) else None
        if not isinstance(entries, list):
            raise RunnerError("Agent configuration must contain an agents list.")
    else:
        entries = [read_json(path) for path in sorted((state_dir / "agent-services").glob("*/endpoint.json"))]
    services = {}
    for entry in entries:
        if not isinstance(entry, dict) or not {"civilizationId", "url", "tokenFile"} <= set(entry):
            raise RunnerError("Invalid agent-service configuration.")
        name = entry["civilizationId"]
        if not isinstance(name, str) or not name.strip() or name in services:
            raise RunnerError("Agent-service civilization is missing or duplicated.")
        services[name] = Agent(entry)
    if not services:
        raise RunnerError("No HTTPS agent services found. Launch one service per civilization first.")
    return services


class RunStore:
    def __init__(self, root):
        self.root = root
        self.path = root / "monitoring.sqlite3"
        self.control = root.parent / "control" / "request.json"
        self.lock = None

    def acquire(self):
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.lock = (self.root / "scheduler.lock").open("a+")
        try:
            fcntl.flock(self.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            self.lock.close()
            self.lock = None
            raise RunnerError(
                "Another scheduler owns this match. Use civ-agent match status, pause, or stop."
            ) from None

    def close(self):
        if self.lock is not None:
            self.lock.close()
            self.lock = None

    def load(self):
        value = read_record(self.path, "state", "run")
        if not isinstance(value, dict) or value.get("version") != RUN_STATE_VERSION:
            raise RunnerError(
                "Unsupported runner state format. Turn recovery requires a new match; use the old code to resume older matches."
            )
        return value

    def save(self, state):
        MonitorStore(self.path).put("state", "run", state)

    def exists(self):
        return read_record(self.path, "state", "run") is not None

    def requested(self):
        if not self.control.exists():
            return None
        value = read_json(self.control)
        if not isinstance(value, dict) or value.get("action") not in {"pause", "stop"}:
            raise RunnerError("Invalid scheduler control request.")
        return value["action"]


def player_prompt(civilization, game_turn=None):
    # The service supplies the reusable workflow as developerInstructions before
    # this changing request and private memory. Keep the scheduler payload small.
    return (
        f"Play {civilization}'s current game turn, numbered {game_turn} in player_turn. "
        if game_turn is not None
        else f"Play {civilization}'s current game turn. "
    )


class Scheduler:
    def __init__(self, store, operator, agents, state, *, verbose=False, color="auto", resume=False):
        self.store, self.operator, self.agents, self.state = store, operator, agents, state
        self.verbose = verbose
        self.output = TerminalOutput(color=color)
        self.lease = str(uuid.uuid4())
        self.owns_lease = False
        self.interrupted = False
        self.histories = {}
        self.history_notices = set()
        self.subscription_notices = {}
        self.finalizer = ThreadPoolExecutor(
            max_workers=max(1, min(4, len(agents))), thread_name_prefix="turn-finalization"
        )
        self.finalizations = {}
        self.finalization_cleanup_failed = False
        self.state.setdefault("pendingFinalizations", [])
        self.resume = resume
        self.services_connected = False
        self.round_open = False
        self.review_intervals = {}
        self.decisions_players = set()
        self.turns = TurnCoordinator(self.store.root.parent, self.operator, self.agents, self.lease)

    def history_for(self, flight, player_token=None):
        key = flight["activationId"]
        if key not in self.histories:
            agent = self.agent_for(flight)
            self.histories[key] = EventHistory(
                self.store.path,
                self.state["epoch"],
                flight["civilization"],
                key,
                secrets=(self.operator.token, agent.token, player_token),
                game_turn=flight.get("gameTurn"),
                verbose=self.verbose,
                renderer=self.output,
            )
            self.output.line("History", str(self.store.path), civilization=flight["civilization"])
        return self.histories[key]

    def poll_events(self, flight, *, drain=False, player_token=None):
        agent = self.agent_for(flight)
        key = flight["activationId"]
        try:
            history = self.history_for(flight, player_token)
            while True:
                before = history.cursor
                page = agent.events(key, before)
                if not page["available"]:
                    if key not in self.history_notices:
                        history.output.line("History", "No service events were recorded for this activation.")
                        self.history_notices.add(key)
                    return not drain
                history.append(page["events"])
                if page["nextCursor"] != history.cursor or (page["hasMore"] and history.cursor == before):
                    raise HistoryError("Agent event cursor did not match the durable mirror.")
                if not drain or not page["hasMore"]:
                    if drain:
                        history.output.finish_line()
                    return True
                # Event reads never call the model. Long histories still renew the
                # scheduler lease so draining pages cannot silently lose ownership.
                if self.owns_lease:
                    try:
                        self.operator.call("heartbeat", leaseId=self.lease)
                    except OperatorError as failure:
                        if failure.code != "LEASE_LOST":
                            raise
                        # An operator pause can revoke the lease while terminal
                        # history is draining; those reads need no mutation lease.
        except OperatorError as failure:
            if failure.status in {400, 404} and failure.code in {
                "INVALID_OPERATION",
                "UNKNOWN_OPERATION",
                "REQUEST_REJECTED",
                "NOT_FOUND",
            }:
                raise RunnerError(
                    "Agent history API is unavailable. Restart the agent services with the current code before resuming; turn scheduling stopped."
                ) from failure
            elif key not in self.history_notices:
                self.output.line(
                    "Warning",
                    "Could not fetch live history; it remains on the service and will be retried.",
                    civilization=flight["civilization"],
                )
                self.history_notices.add(key)
        except (HistoryError, OSError) as failure:
            if key not in self.history_notices:
                self.output.line(
                    "Warning", f"History mirror needs attention: {failure}", civilization=flight["civilization"]
                )
                self.history_notices.add(key)
        return False

    def save(self):
        self.store.save(self.state)

    def note(self, status, reason):
        self.state.update(status=status, reason=reason)
        self.save()
        self.output.notice(reason, level="Done" if status == "complete" else "Warning")

    def display_turn(self, flight, status):
        display_turn(self.output, self.store.root.parent, {**flight, "epoch": self.state["epoch"]}, status)

    def check_status(self, status):
        if status["epoch"] != self.state["epoch"]:
            raise RunnerError("Host epoch changed. Existing conversations cannot be applied to this game.")
        flight = self.state.get("inFlight")
        if flight and status["handoff"] < flight["handoff"]:
            raise RunnerError("Host handoff moved backwards outside turn recovery.")
        for pending in self.state["pendingFinalizations"]:
            if pending["epoch"] != status["epoch"] or pending["confirmedHandoff"] > status["handoff"]:
                raise RunnerError("Pending finalization does not belong to the current host history.")

    def agent_for(self, flight):
        agent = self.agents.get(flight["civilization"])
        if agent is None or agent.url != flight["agentUrl"]:
            raise RunnerError(
                "The previous activation's agent endpoint is missing or changed. Restore it before resuming."
            )
        return agent

    def pause_owned(self):
        try:
            return self.operator.call("pause", leaseId=self.lease)["status"]
        except OperatorError:
            status = self.operator.call("status")["status"]
            if status["phase"] in {"PAUSED", "HALTED", "FINISHED"}:
                return status
            raise

    def budget_reason(self, status):
        limits = self.state["limits"]
        if time.time() >= self.state["deadline"]:
            return "Wall-time budget exhausted (pause and downtime count)."
        if status["turn"] - self.state["initialTurn"] >= limits["maxTurns"]:
            return "Game-turn budget exhausted."
        if max(self.state["invocations"], status.get("modelInvocations", 0)) >= limits["maxInvocations"]:
            return "Agent activation budget exhausted."
        return None

    def record_agent(self, status, flight):
        if status["epoch"] != self.state["epoch"]:
            raise RunnerError("Agent service returned a different match epoch.")
        if status.get("gameTurn") != flight.get("gameTurn"):
            raise RunnerError("Agent service returned a different game turn.")
        if status.get("handoff") != flight.get("handoff"):
            raise RunnerError("Agent service returned a different activation handoff.")
        session = self.state["sessions"][flight["civilization"]]
        thread = status.get("threadId")
        changed = False
        if thread is not None:
            if session.get("threadId") and thread != session["threadId"]:
                raise RunnerError("Agent service resumed a different conversation.")
            if session.get("threadId") != thread:
                session["threadId"] = thread
                changed = True
        changed = self.record_settings(status, flight, session) or changed
        usage = status.get("usage", {})
        if not isinstance(usage, dict):
            raise RunnerError("Invalid agent usage response.")
        previous = flight.setdefault("usage", {})
        for key in ("input_tokens", "cached_input_tokens", "output_tokens", "reasoning_output_tokens"):
            value = usage.get(key, 0)
            if type(value) is not int or value < previous.get(key, 0):
                raise RunnerError("Agent activation usage moved backwards or is invalid.")
            changed = changed or key not in previous or value != previous[key]
            self.state["usage"][key] = self.state["usage"].get(key, 0) + value - previous.get(key, 0)
            previous[key] = value
        if flight.get("agentPhase") != status["phase"]:
            flight["agentPhase"] = status["phase"]
            changed = True
        if flight.get("modelStatus") != status.get("modelStatus"):
            flight["modelStatus"] = status.get("modelStatus")
            changed = True
        if flight.get("requestFailure") != status.get("requestFailure"):
            flight["requestFailure"] = status.get("requestFailure")
            changed = True
        if flight.get("negotiationWait") != status.get("negotiationWait"):
            flight["negotiationWait"] = status.get("negotiationWait")
            changed = True
        if flight.get("economyWait") != status.get("economyWait"):
            flight["economyWait"] = status.get("economyWait")
            changed = True
        if changed:
            self.save()

    def record_settings(self, status, flight, session):
        """Pin provider/model identity, recording effort separately from its default."""
        actual = status.get("effectiveSettings")
        if actual is None:
            return False
        configured = status.get("configuredSettings", actual)
        for value in (actual, configured):
            if (
                not isinstance(value, dict)
                or set(value) != {"provider", "model", "reasoningEffort"}
                or value["provider"] not in {"openai", "openrouter"}
                or not valid_setting(value["model"])
                or (value["reasoningEffort"] is not None and not valid_setting(value["reasoningEffort"]))
            ):
                raise RunnerError("Agent service returned invalid model or reasoning settings.")
        for previous in (configured, session.get("effectiveSettings"), flight.get("effectiveSettings")):
            if previous is not None and any(previous[key] != actual[key] for key in ("provider", "model")):
                raise RunnerError("Agent service changed the conversation's provider or model.")
        changed = session.get("configuredSettings") != configured or flight.get("effectiveSettings") != actual
        session["configuredSettings"] = dict(configured)
        session["effectiveSettings"] = dict(configured)
        if flight.get("effectiveSettings") != actual:
            flight["effectiveSettings"] = dict(actual)
            self.output.line(
                "Model",
                f"{actual['provider']}: {actual['model']}; reasoning: "
                f"{actual['reasoningEffort'] or 'Codex default'}"
                + (f" (configured default: {configured['reasoningEffort']})" if actual != configured else "")
                + ".",
                civilization=flight["civilization"],
            )
        return changed

    def interrupt_agent(self, flight):
        agent = self.agent_for(flight)
        status = agent.call("interrupt", activationId=flight["activationId"])
        self.record_agent(status, flight)
        if status["phase"] not in Agent.TERMINAL:
            raise RunnerError("Agent service has not confirmed cancellation; no next player will be activated.")
        return status

    def queue_finalization(self, flight, host_status):
        """Persist handoff proof before allowing another player to activate."""
        self.check_status(host_status)
        if host_status["handoff"] != flight["handoff"] + 1:
            raise RunnerError("Cannot bind a turn to an ambiguous host handoff.")
        status = self.agent_for(flight).call(
            "finish_handoff",
            activationId=flight["activationId"],
            epoch=self.state["epoch"],
            gameTurn=flight["gameTurn"],
            committedHandoff=host_status["handoff"],
        )
        self.record_agent(status, flight)
        if status.get("confirmedHandoff") != host_status["handoff"]:
            raise RunnerError("Agent did not durably accept the handoff or fence its tools.")
        pending = {
            **flight,
            "epoch": self.state["epoch"],
            "confirmedHandoff": host_status["handoff"],
        }
        self.state["pendingFinalizations"].append(pending)
        self.state["handoffs"] += 1
        self.state["inFlight"] = None
        self.save()
        self.start_finalizations()

    def start_finalizations(self):
        if not self.services_connected:
            return
        for pending in self.state["pendingFinalizations"]:
            key = pending["activationId"]
            if pending["epoch"] != self.state["epoch"]:
                raise RunnerError("Cannot finalize work from an abandoned epoch.")
            if key not in self.finalizations:
                self.finalizations[key] = self.finalizer.submit(
                    finish_turn,
                    self.agent_for(pending),
                    pending,
                    self.history_for(pending),
                )

    def collect_finalizations(self, *, wait=False, civilization=None):
        if not self.services_connected:
            return
        self.start_finalizations()
        last_heartbeat = time.monotonic()
        cancelled = set()
        while True:
            if self.store.requested() or self.interrupted or time.time() >= self.state["deadline"]:
                for pending in list(self.state["pendingFinalizations"]):
                    if pending["activationId"] not in cancelled:
                        self.interrupt_agent(pending)
                        cancelled.add(pending["activationId"])
            for pending in list(self.state["pendingFinalizations"]):
                future = self.finalizations[pending["activationId"]]
                if not future.done():
                    continue
                try:
                    status = future.result()
                except (RunnerError, OSError, ValueError, HistoryError) as error:
                    raise RunnerError(
                        f"Turn bookkeeping remains pending for {pending['civilization']}: {error}"
                    ) from None
                if status is None:
                    del self.finalizations[pending["activationId"]]
                    return
                self.record_agent(status, pending)
                self.state["sessions"][pending["civilization"]]["completedTurn"] = pending["gameTurn"]
                self.state["pendingFinalizations"].remove(pending)
                del self.finalizations[pending["activationId"]]
                self.save()
                self.display_turn(pending, status)
            outstanding = [
                p
                for p in self.state["pendingFinalizations"]
                if civilization is None or p["civilization"] == civilization
            ]
            if not wait or not outstanding:
                return
            if self.owns_lease and time.monotonic() - last_heartbeat >= 3:
                try:
                    self.operator.call("heartbeat", leaseId=self.lease)
                except OperatorError as failure:
                    if failure.code != "LEASE_LOST":
                        raise
                last_heartbeat = time.monotonic()
            time.sleep(0.1)

    def complete_terminal_turn(self, flight, host_status):
        """Retain a winning command that ended the game without an END_TURN handoff."""
        if host_status["phase"] != "FINISHED" or host_status["handoff"] != flight["handoff"]:
            raise RunnerError("Host did not confirm this activation's terminal outcome.")
        agent = self.agent_for(flight)
        finalized = agent.call(
            "finalize_terminal",
            activationId=flight["activationId"],
            epoch=self.state["epoch"],
            gameTurn=flight["gameTurn"],
            handoff=flight["handoff"],
        )
        self.record_agent(finalized, flight)
        if finalized.get("terminalOutcome") is not True:
            raise RunnerError("Agent service did not confirm the terminal turn archive.")
        self.state["sessions"][flight["civilization"]]["completedTurn"] = flight["gameTurn"]
        self.state["inFlight"] = None
        self.save()
        self.display_turn(flight, finalized)
        self.output.line(
            "Result",
            "Game ended during the turn; confirmed actions and events are retained.",
            civilization=flight["civilization"],
        )

    def complete_game(self, status):
        self.check_status(status)
        if status["phase"] != "FINISHED":
            raise RunnerError("Host has not finished the game.")
        flight = self.state.get("inFlight")
        if flight and flight.get("controller") == "HUMAN":
            if status["handoff"] > flight["handoff"]:
                self.finish_human_turn(flight, status)
            else:
                self.state["inFlight"] = None
                self.save()
        elif flight:
            self.interrupt_agent(flight)
            if not self.poll_events(flight, drain=True):
                raise RunnerError("Final agent history could not be mirrored; resume to retry.")
            if status["handoff"] > flight["handoff"]:
                self.queue_finalization(flight, status)
            else:
                self.complete_terminal_turn(flight, status)
        self.collect_finalizations(wait=True)
        accounting_agents = list(self.state["sessions"])
        if accounting_agents:
            if not self.owns_lease:
                self.operator.call("acquire", leaseId=self.lease)
                self.owns_lease = True
            for civilization in accounting_agents:
                connection = self.operator.call(
                    "connect", leaseId=self.lease, epoch=self.state["epoch"], civilization=civilization
                )
                self.agents[civilization].call(
                    "flush_accounting", epoch=self.state["epoch"], playerToken=connection["playerToken"]
                )
        response = self.operator.call("results")
        self.check_status(response["status"])
        results = response.get("results")
        if response["status"]["phase"] != "FINISHED" or not isinstance(results, dict):
            raise RunnerError("Host did not return completed player results.")
        delivered = self.state.setdefault("resultsDelivered", [])
        for civilization, agent in self.agents.items():
            payload = results.get(civilization)
            if (
                not isinstance(payload, dict)
                or payload.get("epoch") != self.state["epoch"]
                or payload.get("civilization") != civilization
            ):
                raise RunnerError("Host omitted a bound player's final result.")
            if civilization in delivered:
                continue
            agent.result(payload)
            delivered.append(civilization)
            self.save()
            self.output.line("Result", "Final result recorded by the player service.", civilization=civilization)
        self.note("complete", "Game finished. Every player received its final result and permitted combat reports.")

    def reconcile(self, status):
        self.check_status(status)
        if status["phase"] == "FINISHED":
            self.complete_game(status)
            return False
        flight = self.state.get("inFlight")
        if not flight:
            return True
        if flight.get("controller") == "HUMAN":
            self.finish_human_turn(flight, status)
            return True
        agent = self.agent_for(flight)
        agent_missing = False
        try:
            agent_status = agent.call("status", activationId=flight["activationId"])
        except OperatorError as failure:
            if failure.status != 404 or failure.code not in {"ACTIVATION_NOT_FOUND", "UNKNOWN_ACTIVATION"}:
                raise
            agent_missing = True
        else:
            self.record_agent(agent_status, flight)
            if agent_status["phase"] not in Agent.TERMINAL:
                self.interrupt_agent(flight)
        if not agent_missing and not self.poll_events(flight, drain=True):
            self.note(
                "paused",
                "Final agent history is still on the service. Resume to retry mirroring before the next activation.",
            )
            return False
        if status["handoff"] > flight["handoff"]:
            if agent_missing:
                raise RunnerError(
                    "Host advanced but the agent activation record is missing; no turn summary was promoted."
                )
            self.queue_finalization(flight, status)
            self.output.notice("Confirmed the previous activation's committed handoff; continuing from host state.")
            return True
        raise RunnerError("The interrupted activation was not restored from its turn checkpoint.")

    def run(self):
        result = self._run()
        return 2 if self.finalization_cleanup_failed else result

    def connect_services(self):
        self.operator.call("barrier", leaseId=self.lease)
        for civilization, agent in self.agents.items():
            connection = self.operator.call(
                "connect", leaseId=self.lease, epoch=self.state["epoch"], civilization=civilization
            )
            connected = self.turns.call_agent(
                agent,
                "connect",
                epoch=self.state["epoch"],
                playerToken=connection["playerToken"],
                model=self.state.get("model"),
                reasoningEffort=self.state.get("reasoningEffort"),
                timeoutSeconds=max(
                    self.state["limits"]["activationSeconds"],
                    connection["status"].get("negotiationTimeoutSeconds", 300),
                ),
            )
            interval = connected.get("reviewInterval")
            if type(interval) is not int or interval < 1:
                raise RunnerError("Invalid review interval from agent service")
            self.review_intervals[civilization] = interval
            if connected.get("decisionsEnabled") is True:
                self.decisions_players.add(civilization)
        self.services_connected = True
        self.state.update(status="running", reason=None)
        self.save()

    def _run(self):
        self.state.pop("quotaFailure", None)
        try:
            initial = self.operator.call("status")["status"]
            flight = self.state.get("inFlight")
            if (
                flight
                and initial["phase"] != "FINISHED"
                and not initial.get("scheduled")
                and initial["handoff"] == flight["handoff"]
            ):
                raise RunnerError(
                    "Host returned to manual control during this unfinished turn. Finish the turn manually before resuming the scheduler."
                )
            if initial["phase"] == "HALTED":
                if self.state.get("inFlight") and self.state["inFlight"].get("controller") != "HUMAN":
                    flight = self.state["inFlight"]
                    self.interrupt_agent(flight)
                    if not self.poll_events(flight, drain=True):
                        raise RunnerError("Final agent history could not be mirrored.")
                self.note("needs_inspection", "Host is HALTED.")
                return 0
            acquired = self.operator.call("acquire", leaseId=self.lease)
            self.owns_lease = True
            self.state["leaseId"] = self.lease
            self.save()
            if self.resume:
                acquired["status"] = self.turns.recover(acquired["status"], self.state)
                self.histories.clear()
            self.check_status(acquired["status"])
            self.operator.call(
                "job_limits",
                leaseId=self.lease,
                maxInvocations=self.state["limits"]["maxInvocations"],
                maxConcurrent=self.state["limits"].get("maxConcurrent", 4),
                maxPerCivilization=self.state["limits"].get("maxPerCivilization", 4),
            )
            self.connect_services()
            if not self.reconcile(acquired["status"]):
                return 0 if self.state["status"] == "complete" else 2
            while True:
                self.collect_finalizations()
                requested = self.store.requested() or ("pause" if self.interrupted else None)
                if requested:
                    self.note("stopped" if requested == "stop" else "paused", "Operator requested " + requested + ".")
                    return 0
                status = self.operator.call("status")["status"]
                self.check_status(status)
                if status["phase"] == "FINISHED":
                    self.complete_game(status)
                    return 0
                if status["phase"] == "HALTED":
                    self.note("needs_inspection", "Host is HALTED.")
                    return 0
                reason = self.budget_reason(status)
                if reason:
                    self.note("budget_exhausted", reason)
                    return 0
                if self.store.requested() or self.interrupted:
                    continue
                if not self.invoke(status):
                    return 0 if self.state["status"] in {"paused", "stopped", "budget_exhausted", "complete"} else 2
        except QuotaExhausted as failure:
            self.note("paused", str(failure))
            return 0
        except (RunnerError, OSError, ValueError, HistoryError, SQLAlchemyError) as failure:
            self.note("needs_inspection", str(failure))
            return 2
        finally:
            if self.owns_lease:
                try:
                    self.pause_owned()
                except RunnerError:
                    pass
                flight = self.state.get("inFlight")
                if flight and flight.get("controller") != "HUMAN":
                    try:
                        self.interrupt_agent(flight)
                        self.poll_events(flight, drain=True)
                    except RunnerError:
                        # No handoff follows an unconfirmed cancellation. The agent's
                        # independent heartbeat lease/timeout also stops model work.
                        pass
                if self.state["status"] in {"paused", "stopped", "needs_inspection"}:
                    for pending in list(self.state["pendingFinalizations"]):
                        try:
                            self.interrupt_agent(pending)
                        except RunnerError:
                            pass
                try:
                    self.collect_finalizations(wait=True)
                except (RunnerError, HistoryError) as failure:
                    self.finalization_cleanup_failed = True
                    self.note("needs_inspection", str(failure))
                for agent in self.agents.values():
                    try:
                        agent.call("disconnect")
                    except RunnerError:
                        pass
                try:
                    self.operator.call("release", leaseId=self.lease)
                except RunnerError:
                    pass
            self.finalizer.shutdown(wait=True)

    def finish_human_turn(self, flight, status):
        if status["handoff"] != flight["handoff"] + 1:
            raise RunnerError("The human turn has no confirmed handoff; resume must restore its starting save.")
        self.state["handoffs"] += 1
        self.state["inFlight"] = None
        self.save()
        self.output.line("Status", "Human turn completed.", civilization=flight["civilization"])

    def invoke_human(self, status):
        """Wait for desktop input while maintaining authority and background services."""
        flight = dict(
            controller="HUMAN",
            activationId=str(uuid.uuid4()),
            civilization=status["activeCivilization"],
            handoff=status["handoff"],
            gameTurn=status["turn"],
            serverInstance=status["serverInstance"],
            usage={},
        )
        self.state["inFlight"] = flight
        self.save()
        self.operator.call(
            "activate",
            leaseId=self.lease,
            epoch=status["epoch"],
            **{key: flight[key] for key in ("serverInstance", "handoff", "civilization", "activationId")},
        )
        self.output.turn(flight["civilization"], status["turn"])
        self.output.line("Status", "Your turn in the Unciv window.", civilization=flight["civilization"])
        heartbeat = time.monotonic()
        try:
            while True:
                self.collect_finalizations()
                requested = self.store.requested() or ("pause" if self.interrupted else None)
                if requested:
                    self.note("stopped" if requested == "stop" else "paused", "Operator requested " + requested + ".")
                    return False
                current = self.operator.call("status")["status"]
                self.check_status(current)
                if current["phase"] == "FINISHED":
                    self.complete_game(current)
                    return False
                if current["phase"] == "HALTED":
                    self.note(
                        "needs_inspection", "Host halted during the human turn; inspect its logs before resuming."
                    )
                    return False
                if current["handoff"] > flight["handoff"]:
                    self.finish_human_turn(flight, current)
                    return True
                reason = self.budget_reason(current)
                if reason:
                    self.note("budget_exhausted", reason)
                    return False
                if current["serverInstance"] != flight["serverInstance"] or current["phase"] != "ACTIVE":
                    self.note("paused", "Human turn interrupted; resume restores its starting save.")
                    return False
                if time.monotonic() - heartbeat >= 5:
                    self.check_agent_health()
                    self.operator.call("heartbeat", leaseId=self.lease)
                    heartbeat = time.monotonic()
                time.sleep(0.25)
        finally:
            self.pause_owned()

    def invoke(self, status):
        self.check_agent_health()
        civilization = status.get("activeCivilization")
        agent = self.agents.get(civilization)
        human = status.get("activeController") == "HUMAN"
        if agent is None and not human:
            raise RunnerError("No HTTPS agent service is configured for the active civilization.")
        if human and agent is not None:
            raise RunnerError("A human civilization must not have an agent service.")
        self.collect_finalizations(wait=True, civilization=civilization)
        if not review_round(self, status):
            return False
        self.turns.capture(status, self.state)
        requested = self.store.requested() or ("pause" if self.interrupted else None)
        if requested:
            self.note("stopped" if requested == "stop" else "paused", "Operator requested " + requested + ".")
            return False
        if time.time() >= self.state["deadline"]:
            self.note("budget_exhausted", "Wall-time budget exhausted (pause and downtime count).")
            return False
        if human:
            return self.invoke_human(status)
        assert agent is not None
        activation = str(uuid.uuid4())
        session = self.state["sessions"].setdefault(civilization, {"threadId": None})
        if session.get("gameTurn") != status["turn"]:
            if session.get("gameTurn") is not None and session.get("completedTurn") != session["gameTurn"]:
                raise RunnerError(
                    "The previous game turn has no confirmed archive; reconcile it before starting a fresh conversation."
                )
            session.update(gameTurn=status["turn"], threadId=None)
        elif session.get("completedTurn") == status["turn"]:
            raise RunnerError("Host requested a turn already recorded as complete.")
        flight = dict(
            activationId=activation,
            civilization=civilization,
            handoff=status["handoff"],
            gameTurn=status["turn"],
            serverInstance=status["serverInstance"],
            agentUrl=agent.url,
            usage={},
        )
        self.state["invocations"] += 1
        self.state["inFlight"] = flight
        self.save()
        try:
            activated = self.operator.call(
                "activate",
                leaseId=self.lease,
                epoch=status["epoch"],
                serverInstance=status["serverInstance"],
                handoff=status["handoff"],
                civilization=civilization,
                activationId=activation,
            )
        except RunnerError:
            # The agent request has not been attempted, so there is no uncertain
            # model mutation to reconcile. The reserved invocation still counts.
            self.state["inFlight"] = None
            self.save()
            raise
        token = activated.get("playerToken")
        if not isinstance(token, str) or not re.fullmatch(r"[A-Za-z0-9_-]{43}", token):
            raise RunnerError("Host did not issue a valid player activation token.")
        prompt = player_prompt(civilization, status["turn"])
        remaining = max(1, int(self.state["deadline"] - time.time()))
        timeout = min(remaining, self.state["limits"]["activationSeconds"])
        self.output.turn(civilization, status["turn"])
        self.output.line("Status", f"Starting activation {self.state['invocations']}.", civilization=civilization)
        selected = session.get(
            "effectiveSettings",
            {"model": self.state.get("model"), "reasoningEffort": self.state.get("reasoningEffort")},
        )
        # Validate the durable mirror before starting model work. A history
        # identity conflict must not stay hidden until after a committed turn.
        self.history_for(flight, token)
        agent_status = agent.call(
            "start",
            activationId=activation,
            epoch=status["epoch"],
            civilization=civilization,
            gameTurn=status["turn"],
            handoff=status["handoff"],
            threadId=session["threadId"],
            mcpUrl=self.operator.host_url + "/mcp",
            playerToken=token,
            prompt=prompt,
            model=selected["model"],
            # A legacy session may contain an adaptive choice rather than its
            # configured default. Let the service resolve its configured default
            # until the new configuredSettings contract has been observed.
            reasoningEffort=session.get("configuredSettings", {}).get(
                "reasoningEffort", self.state.get("reasoningEffort")
            ),
            timeoutSeconds=timeout,
            leaseSeconds=30,
        )
        self.record_agent(agent_status, flight)
        self.poll_events(flight, player_token=token)
        interrupted = None
        began = last_heartbeat = last_observation = time.monotonic()
        background_handoff = False
        try:
            while agent_status["phase"] not in Agent.TERMINAL:
                self.collect_finalizations()
                now = time.monotonic()
                requested = self.store.requested() or ("pause" if self.interrupted else None)
                if requested:
                    interrupted = requested
                    break
                if time.time() >= self.state["deadline"]:
                    interrupted = "budget"
                    break
                if (
                    now - began - agent_status.get("negotiationWaitSeconds", 0)
                    >= self.state["limits"]["activationSeconds"]
                ):
                    interrupted = "timeout"
                    break
                if now - last_heartbeat >= 5:
                    self.check_agent_health()
                    self.operator.call("heartbeat", leaseId=self.lease)
                    self.record_agent(agent.call("heartbeat", activationId=activation), flight)
                    last_heartbeat = now
                if now - last_observation >= 1:
                    current = self.operator.call("status")["status"]
                    self.check_status(current)
                    last_observation = now
                    if current["phase"] == "FINISHED":
                        break
                    elif current["handoff"] > flight["handoff"]:
                        background_handoff = True
                        break
                    elif current["serverInstance"] != flight["serverInstance"]:
                        interrupted = "host_restarted"
                        break
                    elif current["phase"] != "ACTIVE":
                        interrupted = "host_paused"
                        break
                time.sleep(0.25)
                agent_status = agent.call("status", activationId=activation)
                self.record_agent(agent_status, flight)
                self.poll_events(flight)
        finally:
            # Fence game writes before asking the HTTPS service to stop its model.
            try:
                self.pause_owned()
            finally:
                if agent_status["phase"] not in Agent.TERMINAL and not background_handoff:
                    agent_status = self.interrupt_agent(flight)
        final_status = self.operator.call("status")["status"]
        self.check_status(final_status)
        self.check_agent_health()
        if final_status["phase"] == "FINISHED":
            self.complete_game(final_status)
            return False
        if interrupted == "host_paused":
            interrupted = self.store.requested() or ("pause" if self.interrupted else "host_paused")
        if final_status["handoff"] > flight["handoff"]:
            self.queue_finalization(flight, final_status)
            self.output.line(
                "Status",
                "Handoff confirmed; finishing bookkeeping.",
                civilization=civilization,
            )
            if interrupted in {"pause", "stop"}:
                self.note("paused" if interrupted == "pause" else "stopped", "Operator requested " + interrupted + ".")
                return False
            return True
        if not self.poll_events(flight, drain=True):
            raise RunnerError("Final agent history could not be mirrored. Resume to retry before the next activation.")
        if interrupted in {"pause", "stop"}:
            self.note(
                "stopped" if interrupted == "stop" else "paused",
                "Activation paused. Resume restores the turn's starting save and starts a fresh conversation.",
            )
        elif interrupted in {"host_paused", "host_restarted"}:
            self.note(
                "paused",
                "Host interrupted the activation. Resume rewinds this turn and starts a fresh conversation.",
            )
        elif interrupted == "budget":
            self.note("budget_exhausted", "Wall-time budget exhausted during an activation.")
        elif interrupted == "timeout":
            self.note(
                "paused",
                "Activation timed out without a confirmed handoff. Resume rewinds this turn and starts a fresh conversation.",
            )
        elif agent_status["phase"] != "COMPLETED":
            self.note(
                "paused",
                "Agent service failed without a confirmed handoff. Resume rewinds this turn and starts a fresh conversation.",
            )
        else:
            self.note(
                "paused",
                "Agent returned without a committed handoff. Resume rewinds this turn and starts a fresh conversation.",
            )
        return False

    def check_agent_health(self):
        """Report subscription health and stop on fatal failures, including off-turn jobs."""
        pending_quota = None
        for civilization, agent in self.agents.items():
            health = agent.call("health")
            if health.get("civilization") != civilization or "runFailure" not in health:
                raise RunnerError("Invalid agent health response; stopped for inspection.")
            subscription = health.get("communicationSubscription")
            if isinstance(subscription, dict):
                previous = self.subscription_notices.get(civilization, ("CONNECTING", 0))
                current = (subscription.get("status"), subscription.get("failures", 0))
                self.subscription_notices[civilization] = current
                if current != previous:
                    if current[0] in {"RECONNECTING", "STOPPED"} or current[1] != previous[1]:
                        self.output.line(
                            "Warning",
                            f"Communication polling: {current[0]}; {current[1]} failures, "
                            f"{subscription.get('consecutiveFailures', 0)} consecutive. See service diagnostics.jsonl.",
                            civilization=civilization,
                        )
                    elif current[0] == "HEALTHY" and previous[0] in {"RECONNECTING", "STOPPED"}:
                        self.output.line("Communication", "Polling recovered.", civilization=civilization)
            if health["runFailure"] is not None:
                failure = health["runFailure"]
                if not isinstance(failure, dict) or not all(
                    isinstance(failure.get(key), str) and failure[key] for key in ("error", "diagnosticId")
                ):
                    raise RunnerError("Invalid agent failure report; stopped for inspection.")
                raise RunnerError(
                    f"{civilization}: {failure['error']}; diagnostic {failure['diagnosticId']}. Run stopped for inspection."
                )
            quota = health.get("quotaFailure")
            if isinstance(quota, dict) and quota.get("reason") == "usageLimitExceeded":
                if quota.get("epoch") != self.state.get("epoch"):
                    continue
                pending_quota = {**quota, "civilization": civilization}
        if pending_quota is not None:
            self.state["quotaFailure"] = pending_quota
            self.save()
            raise QuotaExhausted(
                f"{pending_quota['civilization']}: model quota exhausted; retrying automatically every five minutes."
            )


def parser(*, add_help: bool = True) -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(
        description="Automatically schedule trusted local Codex players with persisted finite budgets.",
        add_help=add_help,
    )
    result.add_argument(
        "--state-dir",
        type=Path,
        default=Path(os.environ.get("UNCIV_HOST_DIR", Path.home() / ".local/state/unciv-host/demo")),
    )
    result.add_argument("--operator-config", type=Path, help="Default: STATE_DIR/operator.json")
    result.add_argument("--url", help="Host HTTPS base URL (default: STATE_DIR/endpoints.json)")
    result.add_argument(
        "--ca-file", type=Path, help="CA certificate for the host (default: endpoints.json or system trust)"
    )
    result.add_argument(
        "--agents-config", type=Path, help="Agent endpoint list; defaults to agent-services/*/endpoint.json"
    )
    result.add_argument(
        "--verbose",
        action="store_true",
        help="Show startup context, tool activity and exposed reasoning summaries (always saved in history)",
    )
    result.add_argument(
        "--color",
        choices=("auto", "always", "never"),
        default="auto",
        help="Terminal colors (default: auto; respects NO_COLOR)",
    )
    result.add_argument("--max-turns", type=positive, help="Game rounds from start (default: 20; persisted on resume)")
    result.add_argument(
        "--max-invocations", type=positive, help="Agent activations including interrupted attempts (default: 48)"
    )
    result.add_argument("--max-seconds", type=positive, help="Wall-time budget including pauses (default: 3600)")
    result.add_argument("--turn-timeout", type=positive, help="Timeout per activation (default: 180 seconds)")
    result.add_argument("--max-concurrent-jobs", type=positive, default=4, help="Match-wide model concurrency")
    result.add_argument("--jobs-per-civilization", type=int, choices=(1, 2, 3, 4), default=4)
    add_settings_arguments(
        result,
        model_help="Optional model override for all players; fixed for this run",
        reasoning_help="Optional reasoning effort override for all players; fixed for this run",
    )
    return result


def new_state(options, status):
    began = time.time()
    limits = dict(
        maxTurns=options.max_turns or 20,
        maxInvocations=options.max_invocations or 48,
        maxSeconds=options.max_seconds or 3600,
        activationSeconds=options.turn_timeout or 180,
        maxConcurrent=options.max_concurrent_jobs,
        maxPerCivilization=options.jobs_per_civilization,
    )
    return dict(
        version=RUN_STATE_VERSION,
        runId=str(uuid.uuid4()),
        epoch=status["epoch"],
        initialTurn=status["turn"],
        reviewedBoundary=status["turn"] if status["turn"] else -1,
        startedAt=began,
        deadline=began + limits["maxSeconds"],
        limits=limits,
        model=options.model,
        reasoningEffort=options.reasoning_effort,
        invocations=0,
        handoffs=0,
        usage={},
        sessions={},
        inFlight=None,
        status="starting",
        reason=None,
    )


def extend_limits(state, options):
    """Explicit increases retain all previous usage and the original wall-time origin."""
    if options.model is not None and options.model != state.get("model"):
        raise RunnerError("Resume retains the run's model setting.")
    if options.reasoning_effort is not None and options.reasoning_effort != state.get("reasoningEffort"):
        raise RunnerError("Resume retains the run's reasoning effort setting.")
    changes = (
        ("maxTurns", options.max_turns),
        ("maxInvocations", options.max_invocations),
        ("maxSeconds", options.max_seconds),
        ("activationSeconds", options.turn_timeout),
    )
    for key, value in changes:
        if value is not None and value < state["limits"][key]:
            raise RunnerError("Resume limits may only increase; existing usage and elapsed time are retained.")
    for key, value in changes:
        if value is not None:
            state["limits"][key] = value
    state["deadline"] = state["startedAt"] + state["limits"]["maxSeconds"]


def run(options: argparse.Namespace) -> int:
    store = None
    old_signals = {}
    output = TerminalOutput(color=getattr(options, "color", "auto"))
    try:
        os.umask(0o077)
        state_dir = options.state_dir.expanduser().resolve()
        credentials = (
            options.operator_config.expanduser() if options.operator_config else state_dir / "operator.json"
        ).resolve()
        endpoints_path = state_dir / "endpoints.json"
        endpoints = read_json(endpoints_path) if endpoints_path.exists() else {}
        if not isinstance(endpoints, dict):
            raise RunnerError("Invalid host endpoint configuration.")
        host_url = options.url or endpoints.get("hostUrl")
        if not isinstance(host_url, str):
            raise RunnerError("Host HTTPS endpoint is missing. Launch the host or provide --url and --ca-file.")
        ca_file = options.ca_file or endpoints.get("caFile")
        operator = Operator(host_url, operator_token(credentials), Path(ca_file).expanduser() if ca_file else None)
        store = RunStore(state_dir / "runner")
        if options.action == "status":
            status = operator.call("status")["status"]
            state = store.load() if store.exists() else None
            print(json.dumps(dict(host=status, runner=state), indent=2))
            return 0
        if options.action in {"pause", "stop"}:
            atomic_json(store.control, dict(action=options.action))
            operator.call("pause")  # Explicit operator request may revoke a supervisor lease.
            try:
                store.acquire()
            except RunnerError:
                pass  # The running scheduler reads the control request and stops its child.
            else:
                if store.exists():
                    state = store.load()
                    state.update(
                        status="paused" if options.action == "pause" else "stopped",
                        reason="Operator requested " + options.action + ".",
                    )
                    store.save(state)
            output.notice("Host mutations paused; requested scheduler " + options.action + ".")
            return 0
        if options.action == "manual":
            operator.call("manual")
            output.notice("Host returned to manual player control.")
            return 0
        store.acquire()
        agents = agent_services(state_dir, options.agents_config.expanduser() if options.agents_config else None)
        if options.action == "start":
            if store.exists():
                raise RunnerError(
                    "A persisted run already exists. Use resume; budgets and conversations are never reset silently."
                )
            state = new_state(options, operator.call("status")["status"])
            store.save(state)
        else:
            if not store.exists():
                raise RunnerError("No persisted run exists; use start first.")
            state = store.load()
            extend_limits(state, options)
            store.save(state)
        if store.control.exists():
            store.control.unlink()
        scheduler = Scheduler(
            store,
            operator,
            agents,
            state,
            verbose=options.verbose,
            color=getattr(options, "color", "auto"),
            resume=options.action == "resume",
        )
        output = scheduler.output
        for signum in (signal.SIGINT, signal.SIGTERM):
            old_signals[signum] = signal.signal(signum, lambda _signum, _frame: setattr(scheduler, "interrupted", True))
        result = scheduler.run()
        return 130 if scheduler.interrupted else result
    except (RunnerError, OSError, SQLAlchemyError) as failure:
        output.finish_line()
        TerminalOutput(color=getattr(options, "color", "auto"), file=sys.stderr).line(
            "Error", f"civ-agent match: {failure}"
        )
        return 2
    finally:
        for signum, handler in old_signals.items():
            signal.signal(signum, handler)
        if store is not None:
            store.close()
