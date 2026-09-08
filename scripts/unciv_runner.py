"""Trusted local turn supervisor. Operator credentials never enter a model process."""

import argparse
import fcntl
import json
import os
from pathlib import Path
import re
import signal
import ssl
import stat
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

from runner_history import EventHistory, HistoryError
from agent_results import ResultError, validate_result
from agent_settings import CLOSE_START_PROMPT, TURN_REVIEW_PROMPT, add_settings_arguments, valid_setting


class RunnerError(Exception):
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
                raise RunnerError("Operator credentials must be a regular file with owner-only permissions (chmod 600).")
            content = source.read(65537)
        value = json.loads(content)
        if len(content) > 65536 or not isinstance(value, dict) or set(value) != {"token"}:
            raise ValueError()
        token = value["token"]
        if not isinstance(token, str) or not re.fullmatch(r"[A-Za-z0-9_-]{43}", token):
            raise ValueError()
        return token
    except FileNotFoundError:
        raise RunnerError("Operator credentials are missing. Restart scripts/launch_host with operator support first.") from None
    except (ValueError, UnicodeError, TypeError):
        raise RunnerError("Invalid operator credentials file.") from None


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, _request, _response, _code, _message, _headers, _url):
        raise OperatorError("Authenticated control endpoints must not redirect.")


def secure_url(value):
    if not isinstance(value, str):
        raise RunnerError("Control endpoints must be HTTPS URL strings.")
    parsed = urllib.parse.urlsplit(value)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise RunnerError("Control endpoints must be HTTPS URLs without credentials, queries or fragments.")
    return value.rstrip("/")


class JsonEndpoint:
    endpoint_role = "control endpoint"

    def __init__(self, url, token, ca_file, timeout=5):
        self.url, self.token, self.timeout = secure_url(url), token, timeout
        self.context = ssl.create_default_context(cafile=str(ca_file) if ca_file else None)
        # Direct connections keep operator/service credentials out of HTTP proxies.
        self.opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}), urllib.request.HTTPSHandler(context=self.context), NoRedirect(),
        )

    def call(self, operation, **fields):
        operation_name = operation if isinstance(operation, str) and re.fullmatch(r"[a-z_]{1,40}", operation) else "unknown operation"
        diagnostic = f"{self.endpoint_role} {operation_name}"
        request = urllib.request.Request(
            self.url,
            data=json.dumps(dict(op=operation, **fields)).encode(),
            headers={"Authorization": "Bearer " + self.token, "Content-Type": "application/json"},
            method="POST",
        )
        try:
            maximum = 32 * 1024 * 1024 if operation in {"archive", "results"} else 300 * 1024 if operation == "events" else 65536
            with self.opener.open(request, timeout=self.timeout) as response:
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
            raise OperatorError(f"HTTPS {diagnostic} rejected: HTTP {failure.code} {code}.", code, failure.code) from None
        except (urllib.error.URLError, TimeoutError, OSError) as failure:
            cause = failure.reason if isinstance(failure, urllib.error.URLError) and isinstance(failure.reason, BaseException) else failure
            raise OperatorError(f"Cannot reach or verify HTTPS {diagnostic} ({type(cause).__name__}); stopped for inspection.") from None
        except (ValueError, KeyError, TypeError) as failure:
            raise OperatorError(f"Invalid HTTPS {diagnostic} response ({type(failure).__name__}); stopped for inspection.") from None


class Operator(JsonEndpoint):
    endpoint_role = "host operator"

    def __init__(self, host_url, token, ca_file, timeout=5):
        self.host_url = secure_url(host_url)
        super().__init__(self.host_url + "/operator", token, ca_file, timeout)

    def call(self, operation, **fields):
        value = super().call(operation, **fields)
        try:
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

    def archive(self, activation):
        value = super().call("archive", activationId=activation)
        if (value.get("civilization") != self.civilization or type(value.get("gameTurn")) is not int
                or value["gameTurn"] < 0 or not isinstance(value.get("commands"), str)
                or not isinstance(value.get("summary"), str)):
            raise OperatorError("Invalid player turn archive.")
        return value

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
        if (not isinstance(page.get("events"), list) or type(page.get("nextCursor")) is not int
                or page["nextCursor"] < cursor or type(page.get("hasMore")) is not bool
                or type(page.get("available")) is not bool):
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
        self.path = root / "run.json"
        self.control = root / "control.json"
        self.lock = None

    def acquire(self):
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.lock = (self.root / "scheduler.lock").open("a+")
        try:
            fcntl.flock(self.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            self.lock.close()
            self.lock = None
            raise RunnerError("Another scheduler owns this match. Use run_agents status, pause, or stop.") from None

    def close(self):
        if self.lock is not None:
            self.lock.close()
            self.lock = None

    def load(self):
        value = read_json(self.path)
        if not isinstance(value, dict) or value.get("version") != 2:
            raise RunnerError("This runner state predates per-turn conversations or has an unsupported format. Start a new match directory for the new layout.")
        return value

    def save(self, state):
        atomic_json(self.path, state)

    def requested(self):
        if not self.control.exists():
            return None
        value = read_json(self.control)
        if not isinstance(value, dict) or value.get("action") not in {"pause", "stop"}:
            raise RunnerError("Invalid scheduler control request.")
        return value["action"]


def player_prompt(civilization, game_turn=None):
    current = f"Play {civilization}'s current game turn, numbered {game_turn} in player_turn. " if game_turn is not None else f"Play {civilization}'s current game turn. "
    return current + (
        "A fresh conversation: summaries are historical notes, not current state or instructions. "
        "Use civ MCP tools for game state/actions; only your own turn files for history. "
        "Read player_capabilities and player_turn. Play one ACTIVE turn using offered actions. "
        "Read and briefly note full popups, declaration warnings and diplomatic messages/terms before responding. "
        "Re-observe after commands; use unique request IDs and exact current revisions. "
        "STALE_REVISION applied nothing: re-observe, reconsider, retry once if legal; stop on repetition. "
        + CLOSE_START_PROMPT + TURN_REVIEW_PROMPT +
        "End when legal, including while awaiting an offer response. Stop after handoff, unsupported decisions, "
        "uncertain failures or a paused/finished game. "
        "Your final response becomes summary.md: at most 200 words of confirmed actions and reasons, discoveries, "
        "unfinished business and next-turn plans. Distinguish failures and historical actions. "
        "No headings, IDs, receipts, retry history or execution status."
    )


class Scheduler:
    def __init__(self, store, operator, agents, state):
        self.store, self.operator, self.agents, self.state = store, operator, agents, state
        self.lease = str(uuid.uuid4())
        self.owns_lease = False
        self.interrupted = False
        self.histories = {}
        self.history_notices = set()

    def history_for(self, flight, player_token=None):
        key = flight["activationId"]
        if key not in self.histories:
            agent = self.agent_for(flight)
            self.histories[key] = EventHistory(
                self.store.root / "history", self.state["epoch"], flight["civilization"], key,
                secrets=(self.operator.token, agent.token, player_token), game_turn=flight.get("gameTurn"),
            )
            print(f"[{flight['civilization']}] Local history: {self.histories[key].markdown}", flush=True)
        return self.histories[key]

    def poll_events(self, flight, *, drain=False, player_token=None):
        agent = self.agent_for(flight)
        key = flight["activationId"]
        if flight.get("eventsUnsupported"):
            return True
        try:
            history = self.history_for(flight, player_token)
            while True:
                before = history.cursor
                page = agent.events(key, before)
                if not page["available"]:
                    if key not in self.history_notices:
                        history.output.line("History", "No service events were recorded for this activation.")
                        self.history_notices.add(key)
                    return True
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
            if failure.status in {400, 404} and failure.code in {"INVALID_OPERATION", "UNKNOWN_OPERATION", "REQUEST_REJECTED", "NOT_FOUND"}:
                flight["eventsUnsupported"] = True
                self.save()
                print(f"[{flight['civilization']}] Live history is unavailable in this older service. Restart launch_agent_service to enable it; turn scheduling continues.", flush=True)
                return True
            elif key not in self.history_notices:
                print(f"[{flight['civilization']}] Could not fetch live history; it remains on the service and will be retried.", flush=True)
                self.history_notices.add(key)
        except (HistoryError, OSError) as failure:
            if key not in self.history_notices:
                print(f"[{flight['civilization']}] History mirror needs attention: {failure}", flush=True)
                self.history_notices.add(key)
        return False

    def save(self):
        self.store.save(self.state)

    def note(self, status, reason):
        self.state.update(status=status, reason=reason)
        self.save()
        print(reason, flush=True)

    def check_status(self, status):
        if status["epoch"] != self.state["epoch"]:
            raise RunnerError("Host epoch changed. Existing conversations cannot be applied to this game.")
        flight = self.state.get("inFlight")
        if flight and status["handoff"] < flight["handoff"]:
            raise RunnerError("Host handoff moved backwards; inspect checkpoint recovery before resuming.")

    def agent_for(self, flight):
        agent = self.agents.get(flight["civilization"])
        if agent is None or agent.url != flight["agentUrl"]:
            raise RunnerError("The previous activation's agent endpoint is missing or changed. Restore it before resuming.")
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
        if self.state["invocations"] >= limits["maxInvocations"]:
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
        settings = status.get("effectiveSettings")
        if settings is not None and (
            not isinstance(settings, dict) or set(settings) != {"model", "reasoningEffort"}
            or not valid_setting(settings["model"])
            or (settings["reasoningEffort"] is not None and not valid_setting(settings["reasoningEffort"]))
        ):
            raise RunnerError("Agent service returned invalid model or reasoning settings.")
        if settings is not None and session.get("effectiveSettings") != settings:
            if "effectiveSettings" in session:
                raise RunnerError("Agent service changed the conversation's model or reasoning effort.")
            session["effectiveSettings"] = settings
            changed = True
            print(f"[{flight['civilization']}] Model: {settings.get('model') or 'Codex config'}; "
                  f"reasoning: {settings.get('reasoningEffort') or 'Codex default'}.", flush=True)
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
        if changed:
            self.save()

    def interrupt_agent(self, flight):
        agent = self.agent_for(flight)
        status = agent.call("interrupt", activationId=flight["activationId"])
        self.record_agent(status, flight)
        if status["phase"] not in Agent.TERMINAL:
            raise RunnerError("Agent service has not confirmed cancellation; no next player will be activated.")
        return status

    def complete_turn(self, flight, host_status):
        """Promote memory only after a committed host handoff, before clearing recovery state."""
        if host_status["handoff"] != flight["handoff"] + 1:
            raise RunnerError("Host advanced beyond this activation's handoff; its turn archive cannot be confirmed.")
        agent = self.agent_for(flight)
        finalized = agent.call("finalize", activationId=flight["activationId"], epoch=self.state["epoch"],
                               gameTurn=flight["gameTurn"], committedHandoff=host_status["handoff"])
        self.record_agent(finalized, flight)
        if finalized.get("committedHandoff") != host_status["handoff"]:
            raise RunnerError("Agent service did not confirm the committed turn archive.")
        archive = agent.archive(flight["activationId"])
        if archive["gameTurn"] != flight["gameTurn"]:
            raise RunnerError("Agent service returned another turn's archive.")
        try:
            history = self.history_for(flight)
            history.publish_archive(archive)
        except HistoryError as failure:
            raise RunnerError(f"Turn archive could not be mirrored: {failure}") from None
        print(f"[{flight['civilization']}] Turn summary: {history.turn_directory / 'summary.md'}", flush=True)
        session = self.state["sessions"][flight["civilization"]]
        session["completedTurn"] = flight["gameTurn"]
        self.state["handoffs"] += 1
        self.state["inFlight"] = None
        self.save()

    def complete_terminal_turn(self, flight, host_status):
        """Retain a winning command that ended the game without an END_TURN handoff."""
        if host_status["phase"] != "FINISHED" or host_status["handoff"] != flight["handoff"]:
            raise RunnerError("Host did not confirm this activation's terminal outcome.")
        agent = self.agent_for(flight)
        finalized = agent.call("finalize_terminal", activationId=flight["activationId"], epoch=self.state["epoch"],
                               gameTurn=flight["gameTurn"], handoff=flight["handoff"])
        self.record_agent(finalized, flight)
        if finalized.get("terminalOutcome") is not True:
            raise RunnerError("Agent service did not confirm the terminal turn archive.")
        archive = agent.archive(flight["activationId"])
        if archive["gameTurn"] != flight["gameTurn"]:
            raise RunnerError("Agent service returned another turn's archive.")
        try:
            history = self.history_for(flight)
            history.publish_archive(archive)
        except HistoryError as failure:
            raise RunnerError(f"Terminal turn archive could not be mirrored: {failure}") from None
        self.state["sessions"][flight["civilization"]]["completedTurn"] = flight["gameTurn"]
        self.state["inFlight"] = None
        self.save()
        print(f"[{flight['civilization']}] Game ended during the turn; confirmed actions are retained in {history.turn_directory}.", flush=True)

    def complete_game(self, status):
        self.check_status(status)
        if status["phase"] != "FINISHED":
            raise RunnerError("Host has not finished the game.")
        flight = self.state.get("inFlight")
        if flight:
            self.interrupt_agent(flight)
            if not self.poll_events(flight, drain=True):
                raise RunnerError("Final agent history could not be mirrored; resume to retry.")
            if status["handoff"] > flight["handoff"]:
                self.complete_turn(flight, status)
            else:
                self.complete_terminal_turn(flight, status)
        response = self.operator.call("results")
        self.check_status(response["status"])
        results = response.get("results")
        if response["status"]["phase"] != "FINISHED" or not isinstance(results, dict):
            raise RunnerError("Host did not return completed player results.")
        delivered = self.state.setdefault("resultsDelivered", [])
        for civilization, agent in self.agents.items():
            payload = results.get(civilization)
            if not isinstance(payload, dict) or payload.get("epoch") != self.state["epoch"] or payload.get("civilization") != civilization:
                raise RunnerError("Host omitted a bound player's final result.")
            if civilization in delivered:
                continue
            agent.result(payload)
            delivered.append(civilization)
            self.save()
            print(f"[{civilization}] Final result delivered to result.json and result.md.", flush=True)
        self.note("complete", "Game finished. Every player received its final result and permitted combat reports.")

    def reconcile(self, status):
        self.check_status(status)
        if status["phase"] == "FINISHED":
            self.complete_game(status)
            return False
        flight = self.state.get("inFlight")
        if not flight:
            return True
        agent = self.agent_for(flight)
        agent_missing = False
        try:
            agent_status = agent.call("status", activationId=flight["activationId"])
        except OperatorError as failure:
            if failure.status != 404 or failure.code not in {"ACTIVATION_NOT_FOUND", "UNKNOWN_ACTIVATION"}:
                raise
            agent_missing = True
        if not agent_missing:
            self.record_agent(agent_status, flight)
            if agent_status["phase"] not in Agent.TERMINAL:
                self.interrupt_agent(flight)
        if not agent_missing and not self.poll_events(flight, drain=True):
            self.note("paused", "Final agent history is still on the service. Resume to retry mirroring before the next activation.")
            return False
        if status["handoff"] > flight["handoff"]:
            if agent_missing:
                raise RunnerError("Host advanced but the agent activation record is missing; no turn summary was promoted.")
            self.complete_turn(flight, status)
            print("Confirmed the previous activation's committed handoff; continuing from host state.", flush=True)
            return True
        restored = self.operator.call("rollback", leaseId=self.lease, epoch=self.state["epoch"],
                                      serverInstance=status["serverInstance"], handoff=flight["handoff"],
                                      civilization=flight["civilization"], activationId=flight["activationId"])["status"]
        self.check_status(restored)
        if (restored["handoff"] != flight["handoff"] or restored["turn"] != flight["gameTurn"]
                or restored.get("activeCivilization") != flight["civilization"] or restored["phase"] != "PAUSED"
                or restored["serverInstance"] != status["serverInstance"] or restored.get("leaseId") != self.lease
                or restored.get("scheduled") is not True):
            raise RunnerError("Host did not confirm restoration of the interrupted turn; no fresh agent was started.")
        aborted = agent.call("abort", activationId=flight["activationId"], epoch=self.state["epoch"],
                             gameTurn=flight["gameTurn"], handoff=flight["handoff"])
        self.record_agent(aborted, flight)
        if aborted.get("aborted") is not True:
            raise RunnerError("Agent service did not confirm archiving the rolled-back attempt.")
        if not self.poll_events(flight, drain=True):
            raise RunnerError("Rolled-back attempt history could not be mirrored; resume to retry before starting a fresh agent.")
        archive = agent.archive(flight["activationId"])
        if archive["gameTurn"] != flight["gameTurn"]:
            raise RunnerError("Agent service returned another turn's archive.")
        try:
            self.history_for(flight).publish_archive(archive)
        except HistoryError as failure:
            raise RunnerError(f"Rolled-back turn archive could not be mirrored: {failure}") from None
        self.state["sessions"][flight["civilization"]]["threadId"] = None
        self.state["inFlight"] = None
        self.save()
        print(f"Restored {flight['civilization']}'s turn to its starting save; the next attempt uses a fresh conversation.", flush=True)
        return True

    def run(self):
        try:
            initial = self.operator.call("status")["status"]
            self.check_status(initial)
            flight = self.state.get("inFlight")
            if flight and initial["phase"] != "FINISHED" and not initial.get("scheduled") and initial["handoff"] == flight["handoff"]:
                raise RunnerError("Host returned to manual control during this unfinished turn. Finish the turn manually before resuming the scheduler.")
            if initial["phase"] == "FINISHED":
                self.complete_game(initial)
                return 0
            if initial["phase"] == "HALTED":
                if self.state.get("inFlight"):
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
            if not self.reconcile(acquired["status"]):
                return 0 if self.state["status"] == "complete" else 2
            self.state.update(status="running", reason=None)
            self.save()
            while True:
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
                if not self.invoke(status):
                    return 0 if self.state["status"] in {"paused", "stopped", "budget_exhausted", "complete"} else 2
        except (RunnerError, OSError) as failure:
            self.note("needs_inspection", str(failure))
            return 2
        finally:
            if self.owns_lease:
                try:
                    self.pause_owned()
                except RunnerError:
                    pass
                flight = self.state.get("inFlight")
                if flight:
                    try:
                        self.interrupt_agent(flight)
                        self.poll_events(flight, drain=True)
                    except RunnerError:
                        # No handoff follows an unconfirmed cancellation. The agent's
                        # independent heartbeat lease/timeout also stops model work.
                        pass
                try:
                    self.operator.call("release", leaseId=self.lease)
                except RunnerError:
                    pass

    def invoke(self, status):
        civilization = status.get("activeCivilization")
        agent = self.agents.get(civilization)
        if agent is None:
            raise RunnerError("No HTTPS agent service is configured for the active civilization.")
        activation = str(uuid.uuid4())
        session = self.state["sessions"].setdefault(civilization, {"threadId": None})
        if session.get("gameTurn") != status["turn"]:
            if session.get("gameTurn") is not None and session.get("completedTurn") != session["gameTurn"]:
                raise RunnerError("The previous game turn has no confirmed archive; reconcile it before starting a fresh conversation.")
            session.update(gameTurn=status["turn"], threadId=None)
        elif session.get("completedTurn") == status["turn"]:
            raise RunnerError("Host requested a turn already recorded as complete.")
        flight = dict(activationId=activation, civilization=civilization, handoff=status["handoff"], gameTurn=status["turn"],
                      serverInstance=status["serverInstance"], agentUrl=agent.url, usage={})
        self.state["invocations"] += 1
        self.state["inFlight"] = flight
        self.save()
        try:
            activated = self.operator.call("activate", leaseId=self.lease, epoch=status["epoch"],
                                           serverInstance=status["serverInstance"], handoff=status["handoff"],
                                           civilization=civilization, activationId=activation)
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
        print(f"Activating {civilization}: activation {self.state['invocations']}, game turn {status['turn']}.", flush=True)
        selected = session.get("effectiveSettings", {"model": self.state.get("model"), "reasoningEffort": self.state.get("reasoningEffort")})
        agent_status = agent.call("start", activationId=activation, epoch=status["epoch"], civilization=civilization,
                                  gameTurn=status["turn"], handoff=status["handoff"],
                                  threadId=session["threadId"], mcpUrl=self.operator.host_url + "/mcp", playerToken=token,
                                  prompt=prompt, model=selected["model"], reasoningEffort=selected["reasoningEffort"],
                                  timeoutSeconds=timeout, leaseSeconds=30)
        self.record_agent(agent_status, flight)
        self.poll_events(flight, player_token=token)
        interrupted = None
        began = last_heartbeat = last_observation = time.monotonic()
        handoff_seen = None
        try:
            while agent_status["phase"] not in Agent.TERMINAL:
                now = time.monotonic()
                requested = self.store.requested() or ("pause" if self.interrupted else None)
                if requested:
                    interrupted = requested
                    break
                if time.time() >= self.state["deadline"]:
                    interrupted = "budget"
                    break
                if now - began >= self.state["limits"]["activationSeconds"]:
                    interrupted = "timeout"
                    break
                if now - last_heartbeat >= 5:
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
                        handoff_seen = handoff_seen or now
                    elif current["serverInstance"] != flight["serverInstance"]:
                        interrupted = "host_restarted"
                        break
                    elif current["phase"] != "ACTIVE":
                        interrupted = "host_paused"
                        break
                    if handoff_seen and now - handoff_seen >= 30:
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
                if agent_status["phase"] not in Agent.TERMINAL:
                    agent_status = self.interrupt_agent(flight)
        if not self.poll_events(flight, drain=True):
            raise RunnerError("Final agent history could not be mirrored. Resume to retry before the next activation.")
        final_status = self.operator.call("status")["status"]
        self.check_status(final_status)
        if final_status["phase"] == "FINISHED":
            self.complete_game(final_status)
            return False
        if interrupted == "host_paused":
            interrupted = self.store.requested() or ("pause" if self.interrupted else "host_paused")
        if final_status["handoff"] > flight["handoff"]:
            self.complete_turn(flight, final_status)
            print(f"Host confirmed {civilization}'s handoff.", flush=True)
            if interrupted in {"pause", "stop"}:
                self.note("paused" if interrupted == "pause" else "stopped", "Operator requested " + interrupted + ".")
                return False
            return True
        if interrupted in {"pause", "stop"}:
            self.note("stopped" if interrupted == "stop" else "paused", "Activation paused. Resume restores the turn's starting save and starts a fresh conversation.")
        elif interrupted in {"host_paused", "host_restarted"}:
            self.note("paused", "Host interrupted the activation. Resume restores the turn's starting save before a fresh attempt.")
        elif interrupted == "budget":
            self.note("budget_exhausted", "Wall-time budget exhausted during an activation.")
        elif interrupted == "timeout":
            self.note("paused", "Activation timed out without a confirmed handoff. Resume restores the turn's starting save before a fresh attempt.")
        elif agent_status["phase"] != "COMPLETED":
            self.note("paused", "Agent service failed without a confirmed handoff. Resume restores the turn's starting save before a fresh attempt.")
        else:
            self.note("paused", "Agent returned without a committed handoff. Resume restores the turn's starting save before a fresh attempt.")
        return False


def parser():
    result = argparse.ArgumentParser(description="Automatically schedule trusted local Codex players with persisted finite budgets.")
    result.add_argument("action", nargs="?", choices=("start", "resume", "status", "pause", "stop", "manual"), default="start")
    result.add_argument("--state-dir", type=Path, default=Path(os.environ.get("UNCIV_HOST_DIR", Path.home() / ".local/state/unciv-host/demo")))
    result.add_argument("--operator-config", type=Path, help="Default: STATE_DIR/operator.json")
    result.add_argument("--url", help="Host HTTPS base URL (default: STATE_DIR/endpoints.json)")
    result.add_argument("--ca-file", type=Path, help="CA certificate for the host (default: endpoints.json or system trust)")
    result.add_argument("--agents-config", type=Path, help="Agent endpoint list; defaults to agent-services/*/endpoint.json")
    result.add_argument("--max-turns", type=positive, help="Game rounds from start (default: 20; persisted on resume)")
    result.add_argument("--max-invocations", type=positive, help="Agent activations including interrupted attempts (default: 48)")
    result.add_argument("--max-seconds", type=positive, help="Wall-time budget including pauses (default: 3600)")
    result.add_argument("--turn-timeout", type=positive, help="Timeout per activation (default: 180 seconds)")
    add_settings_arguments(result, model_help="Optional model override for all players; fixed for this run",
                           reasoning_help="Optional reasoning effort override for all players; fixed for this run")
    return result


def new_state(options, status):
    began = time.time()
    limits = dict(maxTurns=options.max_turns or 20, maxInvocations=options.max_invocations or 48,
                  maxSeconds=options.max_seconds or 3600, activationSeconds=options.turn_timeout or 180)
    return dict(version=2, runId=str(uuid.uuid4()), epoch=status["epoch"], initialTurn=status["turn"],
                startedAt=began, deadline=began + limits["maxSeconds"], limits=limits, model=options.model,
                reasoningEffort=options.reasoning_effort,
                invocations=0, handoffs=0, usage={}, sessions={}, inFlight=None, status="starting", reason=None)


def extend_limits(state, options):
    """Explicit increases retain all previous usage and the original wall-time origin."""
    if options.model is not None and options.model != state.get("model"):
        raise RunnerError("Resume retains the run's model and conversations.")
    if options.reasoning_effort is not None and options.reasoning_effort != state.get("reasoningEffort"):
        raise RunnerError("Resume retains the run's reasoning effort and conversations.")
    changes = (("maxTurns", options.max_turns), ("maxInvocations", options.max_invocations),
               ("maxSeconds", options.max_seconds), ("activationSeconds", options.turn_timeout))
    for key, value in changes:
        if value is not None and value < state["limits"][key]:
            raise RunnerError("Resume limits may only increase; existing usage and elapsed time are retained.")
    for key, value in changes:
        if value is not None:
            state["limits"][key] = value
    state["deadline"] = state["startedAt"] + state["limits"]["maxSeconds"]


def main(argv=None):
    options = parser().parse_args(argv)
    store = None
    old_signals = {}
    try:
        os.umask(0o077)
        state_dir = options.state_dir.expanduser().resolve()
        credentials = (options.operator_config.expanduser() if options.operator_config else state_dir / "operator.json").resolve()
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
            state = store.load() if store.path.exists() else None
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
                if store.path.exists():
                    state = store.load()
                    state.update(status="paused" if options.action == "pause" else "stopped", reason="Operator requested " + options.action + ".")
                    store.save(state)
            print("Host mutations paused; requested scheduler " + options.action + ".")
            return 0
        if options.action == "manual":
            operator.call("manual")
            print("Host returned to manual player control.")
            return 0
        store.acquire()
        agents = agent_services(state_dir, options.agents_config.expanduser() if options.agents_config else None)
        if options.action == "start":
            if store.path.exists():
                raise RunnerError("A persisted run already exists. Use resume; budgets and conversations are never reset silently.")
            state = new_state(options, operator.call("status")["status"])
            store.save(state)
        else:
            if not store.path.exists():
                raise RunnerError("No persisted run exists; use start first.")
            state = store.load()
            extend_limits(state, options)
            store.save(state)
        if store.control.exists():
            store.control.unlink()
        scheduler = Scheduler(store, operator, agents, state)
        for signum in (signal.SIGINT, signal.SIGTERM):
            old_signals[signum] = signal.signal(signum, lambda _signum, _frame: setattr(scheduler, "interrupted", True))
        return scheduler.run()
    except (RunnerError, OSError) as failure:
        print(f"run_agents: {failure}", file=sys.stderr)
        return 2
    finally:
        for signum, handler in old_signals.items():
            signal.signal(signum, handler)
        if store is not None:
            store.close()
