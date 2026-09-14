"""Exercise the supervisor through verified HTTPS fake host and agent services."""

import json
import os
import secrets
import ssl
import subprocess
import tempfile
import threading
import time
import unittest
import uuid
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from civ_agent.protocol import JsonObject
from civ_agent.scheduler import Operator, OperatorError, RunnerError, atomic_json, operator_token, player_prompt
from tests.support import match_command


class FakeHost:
    def __init__(self, certificate, private_key, behaviors=()):
        self.epoch, self.instance = str(uuid.uuid4()), str(uuid.uuid4())
        self.operator_token = secrets.token_urlsafe(32)
        self.player_token: str | None = None
        self.lease: str | None = None
        self.activation: str | None = None
        self.phase: str = "ACTIVE"
        self.scheduled = False
        self.handoff = 0
        self.behaviors = list(behaviors)
        self.calls, self.plays = [], []
        self.agent_tokens = {name: secrets.token_urlsafe(32) for name in ("Rome", "Greece")}
        self.activations = {}
        self.interruptions = []
        self.archive_failures = 0
        self.finalizations = []
        self.terminal_finalizations, self.result_deliveries = [], []
        self.result_failures = {}
        self.rollbacks, self.abortions = [], []
        self.effects, self.turn_start_effects = 0, 0
        self.turn_start_handoff: int | None = None
        self.rollback_failures, self.abort_failures = 0, 0
        self.drop_start_calls = 0
        self.rollback_bad_response = False
        self.rollback_error: str | None = None
        self.changed = threading.Condition()
        self.mcp_override: Callable[[str | None, JsonObject, str], JsonObject | None] | None = None
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def setup(self):
                # Handshake inside the request worker, not the accept loop. A
                # cancelled client may leave a TCP connection without TLS data.
                self.request.settimeout(10)
                super().setup()

            def log_message(self, format: str, *args: object) -> None:
                pass

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                with owner.changed:
                    if self.path == "/operator":
                        if self.headers.get("Authorization") != "Bearer " + owner.operator_token:
                            code, value = 401, {"error": "UNAUTHORIZED"}
                        else:
                            code, value = owner.request(body)
                    elif self.path.startswith("/agent/"):
                        civilization = self.path.removeprefix("/agent/")
                        if self.headers.get("Authorization") != "Bearer " + owner.agent_tokens.get(civilization, ""):
                            code, value = 401, {"error": "UNAUTHORIZED"}
                        else:
                            code, value = owner.agent_request(civilization, body)
                    elif self.path == "/mcp":
                        supplied = self.headers.get("Authorization", "").removeprefix("Bearer ")
                        if supplied != owner.player_token and supplied not in owner.agent_tokens.values():
                            code, value = 401, {"error": "UNAUTHORIZED"}
                        elif body.get("method", "").startswith("notifications/"):
                            code, value = 202, {}
                        elif body.get("method") == "initialize":
                            code, value = (
                                200,
                                {
                                    "jsonrpc": "2.0",
                                    "id": body["id"],
                                    "result": {
                                        "protocolVersion": "2025-06-18",
                                        "serverInfo": {"name": "test-host", "version": "1"},
                                        "capabilities": {"tools": {}},
                                    },
                                },
                            )
                        elif body.get("method") == "tools/list":
                            code, value = (
                                200,
                                {
                                    "jsonrpc": "2.0",
                                    "id": body["id"],
                                    "result": {
                                        "tools": [
                                            {"name": name, "inputSchema": {"type": "object"}}
                                            for name in (
                                                "player_turn",
                                                "player_communication",
                                                "player_command",
                                                "player_receipt",
                                            )
                                        ]
                                    },
                                },
                            )
                        else:
                            arguments = body.get("params", {}).get("arguments", {})
                            name = body.get("params", {}).get("name")
                            if name == "player_communication":
                                arguments = arguments["request"]
                            override = owner.mcp_override(name, arguments, supplied) if owner.mcp_override else None
                            if override is not None:
                                result = override
                            elif name == "player_communication":
                                op = arguments["op"]
                                if op == "state":
                                    result = {
                                        "epoch": owner.epoch,
                                        "civilization": next(
                                            (civ for civ, token in owner.agent_tokens.items() if token == supplied),
                                            owner.status()["activeCivilization"],
                                        ),
                                        "peaceVersion": 0,
                                        "offers": [],
                                        "consents": [],
                                        "outcomes": [],
                                    }
                                elif op == "read":
                                    result = {"events": [], "nextCursor": 0, "hasMore": False}
                                elif op == "negotiation":
                                    result = {"negotiation": {"active": False}}
                                elif op == "receipt":
                                    result = {"error": "NOT_ENACTED"}
                                else:
                                    result = {"granted": True}
                            elif name == "player_turn":
                                result = {
                                    "epoch": owner.epoch,
                                    "revision": "rev1",
                                    "turnReview": {"id": "review1"},
                                    "phase": owner.phase,
                                }
                            elif name == "player_command":
                                behavior = owner.behaviors.pop(0) if owner.behaviors else "complete"
                                owner.plays.append(
                                    dict(
                                        civ=owner.status()["activeCivilization"],
                                        thread=arguments.get("deferReason"),
                                        token=owner.player_token,
                                        effectsBefore=owner.effects,
                                    )
                                )
                                owner.effects += 1
                                if behavior == "victory":
                                    owner.phase = "FINISHED"
                                if behavior in {
                                    "complete",
                                    "lost_response",
                                    "after_handoff_hang",
                                    "slow_report",
                                    "many_events",
                                }:
                                    owner.handoff += 1
                                result = {"behavior": behavior, "requestId": arguments["requestId"], "kind": "END_TURN"}
                            elif name == "player_receipt":
                                result = {"error": "NOT_ENACTED"}
                            else:
                                result = {}
                            code, value = (
                                200,
                                {
                                    "jsonrpc": "2.0",
                                    "id": body["id"],
                                    "result": {
                                        "structuredContent": result,
                                        "content": [{"type": "text", "text": json.dumps(result)}],
                                    },
                                },
                            )
                    elif self.path == "/simulate":
                        if (
                            self.headers.get("Authorization") != "Bearer " + str(owner.player_token)
                            or owner.phase != "ACTIVE"
                        ):
                            code, value = 403, {"error": "WRONG_PLAYER"}
                        elif body["civilization"] != owner.status()["activeCivilization"]:
                            code, value = 403, {"error": "WRONG_PLAYER"}
                        else:
                            behavior = owner.behaviors.pop(0) if owner.behaviors else "complete"
                            owner.plays.append(
                                dict(
                                    civ=body["civilization"],
                                    thread=body["threadId"],
                                    token=owner.player_token,
                                    effectsBefore=owner.effects,
                                )
                            )
                            owner.effects += 1
                            if behavior == "victory":
                                owner.phase = "FINISHED"
                            if behavior in {
                                "complete",
                                "lost_response",
                                "after_handoff_hang",
                                "slow_report",
                                "many_events",
                            }:
                                owner.handoff += 1
                            code, value = 200, {"behavior": behavior}
                    else:
                        code, value = 404, {}
                    owner.changed.notify_all()
                encoded = json.dumps(value).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(encoded)))
                self.end_headers()
                self.wfile.write(encoded)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(certificate, private_key)
        self.server.socket = context.wrap_socket(self.server.socket, server_side=True, do_handshake_on_connect=False)
        self.port = self.server.server_address[1]
        self.url = f"https://localhost:{self.port}"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def status(self):
        civilizations = tuple(self.agent_tokens)
        return dict(
            epoch=self.epoch,
            serverInstance=self.instance,
            turn=self.handoff // len(civilizations),
            handoff=self.handoff,
            phase=self.phase,
            activeCivilization=civilizations[self.handoff % len(civilizations)],
            scheduled=self.scheduled,
            leaseId=self.lease,
            leaseExpiresInMs=30000 if self.lease else 0,
        )

    def request(self, body):
        self.calls.append(dict(body))
        operation = body["op"]
        extra: JsonObject = {}
        if operation in {"job_limits", "unbarrier"}:
            pass
        elif operation == "connect":
            extra["playerToken"] = self.agent_tokens[body["civilization"]]
        elif operation == "acquire":
            if self.lease and self.lease != body["leaseId"]:
                return 409, {"error": "LEASE_BUSY"}
            self.lease, self.phase, self.scheduled = body["leaseId"], "PAUSED", True
            if self.turn_start_handoff != self.handoff:
                self.turn_start_handoff, self.turn_start_effects = self.handoff, self.effects
            extra["leaseId"] = self.lease
        elif operation in {"activate", "rollback", "heartbeat", "release"} or (
            operation == "pause" and "leaseId" in body
        ):
            if body["leaseId"] != self.lease or self.lease is None:
                return 409, {"error": "LEASE_LOST"}
            if operation == "activate":
                if any(body[key] != self.status()[key] for key in ("epoch", "serverInstance", "handoff")):
                    return 409, {"error": "STALE_STATUS"}
                if body["civilization"] != self.status()["activeCivilization"]:
                    return 409, {"error": "WRONG_PLAYER"}
                if self.turn_start_handoff != self.handoff:
                    self.turn_start_handoff, self.turn_start_effects = self.handoff, self.effects
                self.phase, self.activation, self.player_token = (
                    "ACTIVE",
                    body["activationId"],
                    secrets.token_urlsafe(32),
                )
                extra.update(playerToken=self.player_token, activationId=self.activation)
            elif operation == "rollback":
                if self.rollback_error:
                    return 409, {"error": self.rollback_error}
                if any(body[key] != self.status()[key] for key in ("epoch", "serverInstance", "handoff")):
                    return 409, {"error": "STALE_STATUS"}
                if body["civilization"] != self.status()["activeCivilization"]:
                    return 409, {"error": "WRONG_PLAYER"}
                if self.turn_start_handoff != self.handoff:
                    return 409, {"error": "TURN_START_UNAVAILABLE"}
                if body.get("activationId") != self.activation:
                    return 409, {"error": "STALE_ACTIVATION"}
                self.effects = self.turn_start_effects
                self.phase, self.player_token = "PAUSED", None
                self.rollbacks.append(dict(body))
                if self.rollback_failures:
                    self.rollback_failures -= 1
                    return 503, {"error": "LOST_ROLLBACK_RESPONSE"}
                if self.rollback_bad_response:
                    return 200, {"status": {**self.status(), "handoff": self.handoff + 1}}
            elif operation in {"pause", "release"}:
                self.phase, self.player_token = "FINISHED" if self.phase == "FINISHED" else "PAUSED", None
                if operation == "release":
                    self.lease = None
        elif operation == "pause":
            self.phase, self.player_token, self.lease = "FINISHED" if self.phase == "FINISHED" else "PAUSED", None, None
        elif operation == "manual":
            if self.lease:
                return 409, {"error": "LEASE_BUSY"}
            self.phase, self.scheduled = "ACTIVE", False
        elif operation == "results":
            from tests.test_agent_results import result_payload

            extra["results"] = (
                {civ: result_payload(civ, self.epoch) for civ in self.agent_tokens} if self.phase == "FINISHED" else {}
            )
        elif operation != "status":
            return 400, {"error": "BAD_REQUEST"}
        return 200, dict(status=self.status(), **extra)

    def agent_request(self, civilization, body):
        if body["op"] in {"connect", "disconnect", "barrier"}:
            return 200, {"status": {"connected": True, "ready": True, "paused": body.get("paused", False)}}
        if body["op"] == "result":
            self.result_deliveries.append({key: value for key, value in body.items() if key != "op"})
            if self.result_failures.get(civilization, 0):
                self.result_failures[civilization] -= 1
                return 503, {"error": "LOST_RESULT_RESPONSE"}
            return 200, {"status": {"civilization": civilization, "epoch": body["epoch"], "received": True}}
        if body["op"] == "events":
            return 400, {"error": "INVALID_OPERATION"}  # Compatibility with an older service.
        key = (civilization, body["activationId"])
        if body["op"] == "start":
            if self.drop_start_calls:
                self.drop_start_calls -= 1
                return 503, {"error": "START_NOT_RECEIVED"}
            if key not in self.activations:
                if body["playerToken"] != self.player_token or civilization != self.status()["activeCivilization"]:
                    return 403, {"error": "WRONG_PLAYER"}
                if any(status["phase"] == "RUNNING" for status in self.activations.values()):
                    return 409, {"error": "ANOTHER_AGENT_RUNNING"}
                behavior = self.behaviors.pop(0) if self.behaviors else "complete"
                thread = body.get("threadId") or str(uuid.uuid4())
                self.plays.append(
                    dict(
                        civ=civilization,
                        thread=thread,
                        token=body["playerToken"],
                        request=body,
                        effectsBefore=self.effects,
                    )
                )
                self.effects += 1
                if behavior in {"victory", "victory_hang", "victory_after_handoff"}:
                    self.phase = "FINISHED"
                if behavior in {
                    "complete",
                    "lost_response",
                    "after_handoff_hang",
                    "refuse_interrupt",
                    "victory_after_handoff",
                }:
                    self.handoff += 1
                phase = (
                    "RUNNING"
                    if behavior in {"hang", "after_handoff_hang", "refuse_interrupt", "victory_hang"}
                    else "COMPLETED"
                )
                if behavior in {"crash", "lost_response", "missing_thread"}:
                    phase = "FAILED"
                self.activations[key] = dict(
                    activationId=body["activationId"],
                    epoch=body["epoch"],
                    civilization=civilization,
                    threadId=None if behavior == "missing_thread" else thread,
                    phase=phase,
                    gameTurn=body["gameTurn"],
                    handoff=body["handoff"],
                    usage={"input_tokens": 10, "cached_input_tokens": 2, "output_tokens": 3}
                    if phase == "COMPLETED"
                    else {},
                    _behavior=behavior,
                )
        if key not in self.activations and body["op"] == "abort":
            self.activations[key] = dict(
                activationId=body["activationId"],
                epoch=body["epoch"],
                civilization=civilization,
                threadId=None,
                phase="INTERRUPTED",
                gameTurn=body["gameTurn"],
                handoff=body["handoff"],
                usage={},
                _behavior="missing_start",
            )
        if key not in self.activations:
            return 404, {"error": "UNKNOWN_ACTIVATION"}
        status = self.activations[key]
        if body["op"] == "finalize":
            if status["phase"] == "RUNNING":
                return 409, {"error": "AGENT_BUSY"}
            if (
                body["epoch"] != status["epoch"]
                or body["gameTurn"] != status["gameTurn"]
                or body["committedHandoff"] <= status["handoff"]
            ):
                return 409, {"error": "INVALID_HANDOFF"}
            self.finalizations.append(dict(body))
            status["committedHandoff"] = body["committedHandoff"]
        if body["op"] == "finalize_terminal":
            if status["phase"] == "RUNNING" or any(
                body[field] != status[field] for field in ("epoch", "gameTurn", "handoff")
            ):
                return 409, {"error": "INVALID_BINDING"}
            self.terminal_finalizations.append(dict(body))
            status["terminalOutcome"] = True
        if body["op"] == "abort":
            if status["phase"] == "RUNNING":
                return 409, {"error": "AGENT_BUSY"}
            if (
                any(body[field] != status[field] for field in ("epoch", "gameTurn", "handoff"))
                or "committedHandoff" in status
            ):
                return 409, {"error": "INVALID_BINDING"}
            self.abortions.append(dict(body))
            status["aborted"] = True
            if self.abort_failures:
                self.abort_failures -= 1
                return 503, {"error": "LOST_ABORT_RESPONSE"}
        if body["op"] == "archive":
            if self.archive_failures:
                self.archive_failures -= 1
                return 503, {"error": "ARCHIVE_UNAVAILABLE"}
            if status["phase"] == "RUNNING":
                return 409, {"error": "TURN_NOT_FINALIZED"}
            return 200, dict(
                civilization=civilization,
                gameTurn=status["gameTurn"],
                commands=json.dumps(
                    {
                        "kind": "tool_result",
                        "command": "END_TURN",
                        "turn": status["gameTurn"],
                        "aborted": status.get("aborted", False),
                    }
                )
                + "\n",
                summaryData={"turn": status["gameTurn"]},
                summary=f"# {civilization} turn {status['gameTurn']}\n"
                + (
                    "Rolled back the unfinished attempt.\n"
                    if status.get("aborted")
                    else "Ended the turn to let the opponent play.\n"
                ),
            )
        if body["op"] == "interrupt":
            self.interruptions.append(key)
            if status["phase"] == "RUNNING" and status["_behavior"] != "refuse_interrupt":
                status["phase"] = "INTERRUPTED"
        return 200, {"status": {key: value for key, value in status.items() if not key.startswith("_")}}

    def wait_for_plays(self, count, timeout=5):
        deadline = time.monotonic() + timeout
        with self.changed:
            while len(self.plays) < count and time.monotonic() < deadline:
                self.changed.wait(timeout=0.1)
        return len(self.plays) >= count


class SchedulerTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tls = tempfile.TemporaryDirectory(prefix="unciv scheduler tls ")
        cls.addClassCleanup(cls.tls.cleanup)
        cls.certificate = Path(cls.tls.name) / "certificate.pem"
        cls.private_key = Path(cls.tls.name) / "key.pem"
        subprocess.run(
            [
                "openssl",
                "req",
                "-x509",
                "-newkey",
                "rsa:2048",
                "-nodes",
                "-days",
                "1",
                "-keyout",
                str(cls.private_key),
                "-out",
                str(cls.certificate),
                "-subj",
                "/CN=localhost",
                "-addext",
                "subjectAltName=DNS:localhost",
            ],
            check=True,
            capture_output=True,
        )

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="unciv scheduler tests ")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.host = FakeHost(self.certificate, self.private_key)
        self.addCleanup(self.host.close)
        self.state = self.root / "match"
        self.state.mkdir(mode=0o700)
        atomic_json(self.state / "operator.json", {"token": self.host.operator_token})
        atomic_json(self.state / "endpoints.json", {"hostUrl": self.host.url, "caFile": str(self.certificate)})
        for civilization, token in self.host.agent_tokens.items():
            service = self.state / "agent-services" / civilization
            atomic_json(service / "token.json", {"token": token})
            atomic_json(
                service / "endpoint.json",
                dict(
                    civilizationId=civilization,
                    url=self.host.url + "/agent/" + civilization,
                    tokenFile=str(service / "token.json"),
                    caFile=str(self.certificate),
                ),
            )
        self.environment = dict(os.environ, UNCIV_OPERATOR_TOKEN="must-not-reach-model")

    def command(self, *args):
        return match_command(*args, "--state-dir", str(self.state))

    def run_cli(self, *args, timeout=15):
        return subprocess.run(
            self.command(*args), env=self.environment, text=True, capture_output=True, timeout=timeout
        )

    def background(self, *args):
        process = subprocess.Popen(
            self.command(*args), env=self.environment, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
        )

        def cleanup():
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
            assert process.stdout is not None and process.stderr is not None
            process.stdout.close()
            process.stderr.close()

        self.addCleanup(cleanup)
        return process

    def saved(self):
        return json.loads((self.state / "runner/run.json").read_text())

    def test_three_players_cycle_and_resume_without_skipping_france(self):
        token = secrets.token_urlsafe(32)
        self.host.agent_tokens["France"] = token
        service = self.state / "agent-services" / "France"
        atomic_json(service / "token.json", {"token": token})
        atomic_json(
            service / "endpoint.json",
            dict(
                civilizationId="France",
                url=self.host.url + "/agent/France",
                tokenFile=str(service / "token.json"),
                caFile=str(self.certificate),
            ),
        )
        first = self.run_cli("--max-turns", "1", "--max-seconds", "60")
        self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
        self.assertEqual([play["civ"] for play in self.host.plays], ["Rome", "Greece", "France"])
        resumed = self.run_cli("resume", "--max-turns", "2")
        self.assertEqual(resumed.returncode, 0, resumed.stdout + resumed.stderr)
        self.assertEqual([play["civ"] for play in self.host.plays], ["Rome", "Greece", "France"] * 2)
        self.assertEqual(self.saved()["handoffs"], 6)
        for turn in (0, 1):
            self.assertTrue((self.state / f"runner/history/France/turn_{turn}/summary.md").is_file())

    def test_consecutive_rounds_use_fresh_threads_tokens_and_authoritative_handoffs(self):
        result = self.run_cli("--max-turns", "2", "--max-invocations", "8", "--max-seconds", "30")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual([play["civ"] for play in self.host.plays], ["Rome", "Greece", "Rome", "Greece"])
        self.assertNotEqual(self.host.plays[0]["thread"], self.host.plays[2]["thread"])
        self.assertNotEqual(self.host.plays[0]["thread"], self.host.plays[1]["thread"])
        self.assertEqual(len({play["token"] for play in self.host.plays}), 4)
        self.assertEqual([play["request"]["threadId"] for play in self.host.plays], [None] * 4)
        self.assertEqual([play["request"]["gameTurn"] for play in self.host.plays], [0, 0, 1, 1])
        self.assertEqual([play["request"]["handoff"] for play in self.host.plays], [0, 1, 2, 3])
        self.assertEqual(len(self.host.finalizations), 4)
        for civilization in ("Rome", "Greece"):
            for turn in (0, 1):
                directory = self.state / "runner/history" / civilization / f"turn_{turn}"
                self.assertIn("let the opponent play", (directory / "summary.md").read_text())
                self.assertEqual(json.loads((directory / "commands.jsonl").read_text())["turn"], turn)
        for play in self.host.plays:
            self.assertEqual(play["request"]["civilization"], play["civ"])
            self.assertNotIn(self.host.operator_token, json.dumps(play["request"]))
            self.assertEqual(play["request"]["mcpUrl"], self.host.url + "/mcp")
        state = self.saved()
        self.assertEqual((state["invocations"], state["handoffs"]), (4, 4))
        self.assertEqual(state["usage"]["input_tokens"], 40)
        self.assertEqual(state["status"], "budget_exhausted")
        self.assertEqual(self.host.phase, "PAUSED")
        self.assertIsNone(self.host.lease)

    def test_manual_takeover_does_not_recapture_and_claim_to_undo_the_old_attempt(self):
        self.host.behaviors = ["incomplete"]
        result = self.run_cli("--max-turns", "1", "--max-seconds", "30")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.run_cli("manual").returncode, 0)
        effects = self.host.effects
        result = self.run_cli("resume")
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn("Finish the turn manually", result.stdout)
        self.assertEqual(self.host.effects, effects)
        self.assertEqual(len(self.host.plays), 1)
        self.assertEqual(self.host.rollbacks, [])
        self.assertIsNotNone(self.saved()["inFlight"])

    def test_incomplete_turn_pauses_instead_of_spawning_repeated_models(self):
        self.host.behaviors = ["incomplete"]
        result = self.run_cli("--max-turns", "1", "--max-seconds", "30")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(len(self.host.plays), 1)
        self.assertEqual(self.saved()["status"], "paused")
        self.assertEqual(self.host.handoff, 0)
        result = self.run_cli("resume")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual([play["civ"] for play in self.host.plays], ["Rome", "Rome", "Greece"])
        self.assertNotEqual(self.host.plays[0]["thread"], self.host.plays[1]["thread"])
        self.assertIsNone(self.host.plays[1]["request"]["threadId"])
        self.assertEqual(0, len(self.host.rollbacks))
        self.assertEqual(0, len(self.host.abortions))
        self.assertEqual(1, self.host.plays[1]["effectsBefore"])
        self.assertIn("fresh conversation", self.host.plays[1]["request"]["prompt"])

    def test_lost_end_turn_response_uses_committed_handoff_without_replay(self):
        self.host.behaviors = ["lost_response", "complete"]
        result = self.run_cli("--max-turns", "1", "--max-seconds", "30")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual([play["civ"] for play in self.host.plays], ["Rome", "Greece"])
        self.assertEqual(self.saved()["handoffs"], 2)
        self.assertEqual([], self.host.rollbacks)

    def test_host_restart_uses_current_instance_when_restoring_durable_turn_start(self):
        self.host.behaviors = ["incomplete"]
        first = self.run_cli("--max-turns", "1", "--max-seconds", "30")
        self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
        original_instance = self.host.instance
        self.host.instance = str(uuid.uuid4())
        resumed = self.run_cli("resume")
        self.assertEqual(resumed.returncode, 0, resumed.stdout + resumed.stderr)
        self.assertNotEqual(original_instance, self.host.instance)
        self.assertEqual([], self.host.rollbacks)
        self.assertEqual(1, self.host.plays[1]["effectsBefore"])

    def test_delayed_rollback_from_an_older_activation_cannot_erase_a_fresh_attempt(self):
        operator = Operator(self.host.url, self.host.operator_token, self.certificate)
        lease = str(uuid.uuid4())
        status = operator.call("acquire", leaseId=lease)["status"]
        binding = dict(
            leaseId=lease,
            epoch=status["epoch"],
            serverInstance=status["serverInstance"],
            handoff=status["handoff"],
            civilization="Rome",
        )
        operator.call("activate", **binding, activationId="old-attempt")
        self.host.effects = 7
        operator.call("pause", leaseId=lease)
        operator.call("rollback", **binding, activationId="old-attempt")
        operator.call("rollback", **binding, activationId="old-attempt")
        self.assertEqual(0, self.host.effects)
        operator.call("activate", **binding, activationId="fresh-attempt")
        self.host.effects = 11
        operator.call("pause", leaseId=lease)
        with self.assertRaises(OperatorError) as rejected:
            operator.call("rollback", **binding, activationId="old-attempt")
        self.assertEqual("STALE_ACTIVATION", rejected.exception.code)
        self.assertEqual(11, self.host.effects)
        operator.call("rollback", **binding, activationId="fresh-attempt")
        self.assertEqual(0, self.host.effects)

    def test_missing_agent_activation_is_fenced_before_a_fresh_start(self):
        self.host.drop_start_calls = 1
        failed = self.run_cli("--max-turns", "1", "--max-seconds", "30")
        self.assertEqual(failed.returncode, 2, failed.stdout + failed.stderr)
        self.assertEqual([], self.host.plays)
        original_activation = self.saved()["inFlight"]["activationId"]
        resumed = self.run_cli("resume")
        self.assertEqual(resumed.returncode, 0, resumed.stdout + resumed.stderr)
        self.assertEqual([play["civ"] for play in self.host.plays], ["Rome", "Greece"])
        self.assertEqual([], self.host.abortions)
        self.assertNotEqual(original_activation, self.host.plays[-1]["request"]["activationId"])
        self.assertEqual(3, self.saved()["invocations"])

    def test_hung_process_is_killed_and_not_automatically_retried(self):
        self.host.behaviors = ["hang"]
        result = self.run_cli("--turn-timeout", "1", "--max-turns", "1", "--max-seconds", "30")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(len(self.host.plays), 1)
        self.assertEqual(self.saved()["status"], "paused")
        self.assertEqual(self.host.phase, "PAUSED")
        state = self.saved()
        self.assertEqual(state["status"], "paused")
        self.assertEqual(state["inFlight"]["agentPhase"], "INTERRUPTED")
        self.assertEqual(self.host.phase, "PAUSED")
        resumed = self.run_cli("resume")
        self.assertEqual(resumed.returncode, 0, resumed.stdout + resumed.stderr)
        self.assertEqual(len(self.host.plays), 3)
        self.assertEqual(0, len(self.host.rollbacks))

    def test_hung_report_after_handoff_does_not_prevent_next_player(self):
        self.host.behaviors = ["after_handoff_hang", "complete"]
        result = self.run_cli("--max-turns", "1", "--max-seconds", "30", "--turn-timeout", "10")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual([play["civ"] for play in self.host.plays], ["Rome", "Greece"])
        self.assertEqual(self.saved()["handoffs"], 2)

    def test_operator_pause_stops_child_and_resume_restores_turn_with_same_budgets(self):
        self.host.behaviors = ["hang", "complete", "complete"]
        process = self.background("--max-turns", "1", "--max-seconds", "30")
        self.assertTrue(self.host.wait_for_plays(1))
        deadline = self.saved()["deadline"]
        paused = self.run_cli("pause")
        self.assertEqual(paused.returncode, 0, paused.stdout + paused.stderr)
        output, error = process.communicate(timeout=8)
        self.assertEqual(process.returncode, 0, output + error)
        self.assertEqual(len(self.host.plays), 1)
        resumed = self.run_cli("resume")
        self.assertEqual(resumed.returncode, 0, resumed.stdout + resumed.stderr)
        self.assertEqual(self.saved()["deadline"], deadline)
        self.assertNotEqual(self.host.plays[0]["thread"], self.host.plays[1]["thread"])
        self.assertEqual(1, self.host.plays[1]["effectsBefore"])
        self.assertEqual(self.saved()["invocations"], 3)

    def test_duplicate_supervisor_does_not_pause_owner(self):
        self.host.behaviors = ["hang"]
        process = self.background("--turn-timeout", "2", "--max-seconds", "30")
        self.assertTrue(self.host.wait_for_plays(1))
        calls_before = len(self.host.calls)
        duplicate = self.run_cli("resume")
        self.assertEqual(duplicate.returncode, 2)
        self.assertIn("Another scheduler", duplicate.stderr)
        self.assertFalse(any(call["op"] == "pause" for call in self.host.calls[calls_before:]))
        self.assertEqual(self.host.phase, "ACTIVE")
        process.communicate(timeout=8)

    def test_crash_recovery_restores_turn_before_a_fresh_conversation(self):
        self.host.behaviors = ["hang"]
        process = self.background("--max-turns", "1", "--max-seconds", "30")
        self.assertTrue(self.host.wait_for_plays(1))
        time.sleep(0.15)  # Allow the thread.started event to be persisted.
        process.kill()
        process.communicate(timeout=5)
        self.host.lease = None  # Simulate host lease expiry after the supervisor crash.
        self.host.phase = "PAUSED"
        resumed = self.run_cli("resume")
        self.assertEqual(resumed.returncode, 0, resumed.stdout + resumed.stderr)
        self.assertEqual(len(self.host.plays), 3)
        self.assertEqual(1, self.host.plays[1]["effectsBefore"])
        self.assertNotEqual(self.host.plays[0]["thread"], self.host.plays[1]["thread"])

    def test_crash_after_committed_handoff_reconciles_without_second_end_turn(self):
        self.host.behaviors = ["after_handoff_hang", "complete"]
        process = self.background("--max-turns", "1", "--max-seconds", "30")
        self.assertTrue(self.host.wait_for_plays(1))
        time.sleep(0.15)
        process.kill()
        process.communicate(timeout=5)
        self.host.lease = None
        self.host.phase = "PAUSED"
        resumed = self.run_cli("resume")
        self.assertEqual(resumed.returncode, 0, resumed.stdout + resumed.stderr)
        self.assertEqual([play["civ"] for play in self.host.plays], ["Rome", "Greece"])
        self.assertEqual(self.saved()["handoffs"], 2)

    def test_invocation_and_wall_time_budgets_remain_exhausted_on_resume(self):
        result = self.run_cli("--max-invocations", "1", "--max-seconds", "30")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(len(self.host.plays), 1)
        resumed = self.run_cli("resume")
        self.assertEqual(resumed.returncode, 0, resumed.stdout + resumed.stderr)
        self.assertEqual(len(self.host.plays), 1)
        state = self.saved()
        state["startedAt"] = time.time() - state["limits"]["maxSeconds"] - 1
        state["deadline"] = state["startedAt"] + state["limits"]["maxSeconds"]
        state["limits"]["maxInvocations"] = 10
        atomic_json(self.state / "runner/run.json", state)
        resumed = self.run_cli("resume")
        self.assertEqual(resumed.returncode, 0)
        self.assertIn("Wall-time", resumed.stdout)
        self.assertEqual(len(self.host.plays), 1)

    def test_explicit_limit_increases_keep_usage_settings_and_time_origin(self):
        first = self.run_cli("--max-invocations", "1", "--max-seconds", "30")
        self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
        origin = self.saved()["startedAt"]
        resumed = self.run_cli("resume", "--max-invocations", "3", "--max-seconds", "60")
        self.assertEqual(resumed.returncode, 0, resumed.stdout + resumed.stderr)
        self.assertEqual(self.saved()["invocations"], 3)
        self.assertEqual(self.saved()["deadline"], origin + 60)
        self.assertNotEqual(self.host.plays[0]["thread"], self.host.plays[2]["thread"])
        refused = self.run_cli("resume", "--max-invocations", "2")
        self.assertEqual(refused.returncode, 2)
        self.assertEqual(len(self.host.plays), 3)

    def test_model_and_reasoning_are_forwarded_persisted_and_fixed_on_resume(self):
        result = self.run_cli(
            "--model", "chosen-model", "--reasoning-effort", "high", "--max-invocations", "1", "--max-seconds", "30"
        )
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        state = self.saved()
        self.assertEqual(("chosen-model", "high"), (state["model"], state["reasoningEffort"]))
        first = self.host.plays[0]["request"]
        self.assertEqual(("chosen-model", "high"), (first["model"], first["reasoningEffort"]))
        for arguments in (("--model", "other-model"), ("--reasoning", "medium")):
            rejected = self.run_cli("resume", *arguments, "--max-invocations", "2")
            self.assertEqual(2, rejected.returncode, rejected.stdout + rejected.stderr)
            self.assertIn("Resume retains", rejected.stderr)
            self.assertEqual(1, len(self.host.plays))
        resumed = self.run_cli("resume", "--max-invocations", "2")
        self.assertEqual(0, resumed.returncode, resumed.stdout + resumed.stderr)
        last = self.host.plays[-1]["request"]
        self.assertEqual(("chosen-model", "high"), (last["model"], last["reasoningEffort"]))

    def test_archive_failure_stops_next_dispatch_then_resume_retries_without_model_replay(self):
        self.host.archive_failures = 1
        failed = self.run_cli("--max-turns", "1", "--max-seconds", "30")
        self.assertEqual(failed.returncode, 2, failed.stdout + failed.stderr)
        self.assertEqual([play["civ"] for play in self.host.plays], ["Rome"])
        self.assertEqual(self.host.handoff, 1)
        self.assertEqual(self.saved()["handoffs"], 0)
        self.assertIsNotNone(self.saved()["inFlight"])
        resumed = self.run_cli("resume")
        self.assertEqual(resumed.returncode, 0, resumed.stdout + resumed.stderr)
        self.assertEqual([play["civ"] for play in self.host.plays], ["Rome", "Greece"])
        self.assertEqual(self.saved()["handoffs"], 2)
        roman = [item for item in self.host.finalizations if item["gameTurn"] == 0 and item["committedHandoff"] == 1]
        self.assertEqual(len(roman), 2)
        self.assertEqual(roman[0], roman[1])

    def test_old_run_layout_is_rejected_without_replaying_or_moving_existing_history(self):
        result = self.run_cli("--max-invocations", "1", "--max-seconds", "30")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        state = self.saved()
        state["version"] = 1
        atomic_json(self.state / "runner/run.json", state)
        refused = self.run_cli("resume")
        self.assertEqual(refused.returncode, 2)
        self.assertIn("Start a new match directory", refused.stderr)
        self.assertEqual(len(self.host.plays), 1)

    def test_operator_credentials_are_private_and_not_accepted_as_player_tokens(self):
        credentials = self.state / "operator.json"
        credentials.chmod(0o644)
        with self.assertRaises(RunnerError):
            operator_token(credentials)
        with self.assertRaises(OperatorError):
            Operator(self.host.url, "A" * 43, self.certificate).call("status")

    def test_tls_verification_and_https_are_required(self):
        with self.assertRaises(RunnerError):
            Operator("http://localhost:8765", self.host.operator_token, self.certificate)
        with self.assertRaises(OperatorError):
            Operator(self.host.url, self.host.operator_token, None).call("status")
        with self.assertRaises(OperatorError):
            Operator(self.host.url.replace("localhost", "127.0.0.1"), self.host.operator_token, self.certificate).call(
                "status"
            )

    def test_unconfirmed_agent_cancellation_prevents_next_dispatch(self):
        self.host.behaviors = ["refuse_interrupt"]
        result = self.run_cli("--max-turns", "1", "--max-seconds", "30", "--turn-timeout", "1")
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertEqual(len(self.host.plays), 1)

    def test_new_activation_names_the_current_turn_without_replaying_historical_end_turn(self):
        prompt = player_prompt("Greece", game_turn=2)
        self.assertIn("numbered 2 in player_turn", prompt)
        self.assertIn("fresh conversation", prompt)
        self.assertIn("historical evidence and plans, not current state", prompt)
        self.assertIn("Follow scenario.instructions", prompt)
        self.assertIn("otherwise your objective is to win the game", prompt)
        self.assertIn("player_turn.rules", prompt)
        self.assertNotIn("CLOSE_START_DIPLOMACY", prompt)
        self.assertNotIn("requestId", prompt)
        self.assertNotIn("t11a1", player_prompt("Rome", game_turn=11))
        self.assertNotIn("reviewId=", prompt)
        self.assertIn("Read turnReview", prompt)
        self.assertIn("deferReason", prompt)
        self.assertLess(len(prompt.split()), 250)

    def test_old_service_gets_restart_guidance_without_changing_turn_scheduling(self):
        result = self.run_cli("--max-turns", "1", "--max-seconds", "30")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("Restart civ-agent agent-server start", result.stdout)
        self.assertEqual(self.host.handoff, 2)
        self.assertEqual(self.host.phase, "PAUSED")

    def test_missing_conversation_id_recovers_from_starting_save_in_a_new_thread(self):
        self.host.behaviors = ["missing_thread"]
        failed = self.run_cli("--max-turns", "1", "--max-seconds", "30")
        self.assertEqual(failed.returncode, 0, failed.stdout + failed.stderr)
        resumed = self.run_cli("resume")
        self.assertEqual(resumed.returncode, 0, resumed.stdout + resumed.stderr)
        self.assertEqual(len(self.host.plays), 3)
        self.assertEqual(1, self.host.plays[1]["effectsBefore"])

    def test_victory_during_activation_retains_commands_without_inventing_a_handoff(self):
        self.host.behaviors = ["victory"]
        result = self.run_cli("--max-turns", "2", "--max-seconds", "60")
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertEqual("complete", self.saved()["status"])
        self.assertEqual(0, self.saved()["handoffs"])
        self.assertEqual(1, self.host.effects)
        self.assertEqual(1, len(self.host.plays))
        self.assertEqual(1, len(self.host.terminal_finalizations))
        self.assertEqual([], self.host.finalizations)
        self.assertEqual([], self.host.rollbacks)
        self.assertIsNone(self.saved()["inFlight"])
        self.assertEqual({"Rome", "Greece"}, set(self.saved()["resultsDelivered"]))
        self.assertEqual({"Rome", "Greece"}, {delivery["civilization"] for delivery in self.host.result_deliveries})
        for delivery in self.host.result_deliveries:
            other = "Greece" if delivery["civilization"] == "Rome" else "Rome"
            self.assertNotIn(other + " permitted report", json.dumps(delivery))

    def test_final_result_delivery_failure_retries_after_restart_without_more_gameplay(self):
        self.host.behaviors = ["victory"]
        self.host.result_failures["Rome"] = 1
        initial = self.run_cli("--max-turns", "2", "--max-seconds", "60")
        self.assertEqual(2, initial.returncode, initial.stdout + initial.stderr)
        self.assertEqual("needs_inspection", self.saved()["status"])
        self.assertEqual(["Greece"], self.saved()["resultsDelivered"])
        self.assertIsNone(self.saved()["inFlight"])
        self.host.instance = str(uuid.uuid4())
        resumed = self.run_cli("resume")
        self.assertEqual(0, resumed.returncode, resumed.stdout + resumed.stderr)
        self.assertEqual("complete", self.saved()["status"])
        self.assertEqual(["Greece", "Rome", "Rome"], [item["civilization"] for item in self.host.result_deliveries])
        self.assertEqual(1, len(self.host.plays))
        self.assertEqual(1, self.host.effects)
        self.assertEqual([], self.host.rollbacks)
        self.assertEqual(0, self.saved()["handoffs"])

    def test_terminal_archive_failure_resumes_without_rolling_back_the_winning_action(self):
        self.host.behaviors = ["victory"]
        self.host.archive_failures = 1
        initial = self.run_cli("--max-turns", "2", "--max-seconds", "60")
        self.assertEqual(2, initial.returncode, initial.stdout + initial.stderr)
        self.assertIsNotNone(self.saved()["inFlight"])
        self.assertEqual([], self.host.result_deliveries)
        resumed = self.run_cli("resume")
        self.assertEqual(0, resumed.returncode, resumed.stdout + resumed.stderr)
        self.assertEqual("complete", self.saved()["status"])
        self.assertEqual(1, self.host.effects)
        self.assertEqual([], self.host.rollbacks)
        self.assertEqual(1, len(self.host.plays))

    def test_already_finished_game_delivers_results_without_acquiring_a_play_lease(self):
        self.host.phase = "FINISHED"
        result = self.run_cli("--max-turns", "1", "--max-seconds", "30")
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertEqual("complete", self.saved()["status"])
        self.assertEqual([], self.host.plays)
        self.assertFalse(any(call["op"] in {"acquire", "activate", "rollback"} for call in self.host.calls))
        self.assertEqual(2, len(self.host.result_deliveries))

    def test_final_outcome_interrupts_live_agent_before_delivering_results(self):
        self.host.behaviors = ["victory_hang"]
        result = self.run_cli("--max-turns", "1", "--max-seconds", "30")
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertEqual("complete", self.saved()["status"])
        self.assertTrue(self.host.interruptions)
        self.assertEqual(0, self.saved()["handoffs"])
        self.assertEqual(1, len(self.host.plays))
        self.assertTrue(all(record["phase"] != "RUNNING" for record in self.host.activations.values()))

    def test_game_finished_by_end_turn_keeps_the_real_handoff_archive(self):
        self.host.behaviors = ["victory_after_handoff"]
        result = self.run_cli("--max-turns", "1", "--max-seconds", "30")
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertEqual("complete", self.saved()["status"])
        self.assertEqual(1, self.saved()["handoffs"])
        self.assertEqual(1, len(self.host.finalizations))
        self.assertEqual([], self.host.terminal_finalizations)
        self.assertEqual(2, len(self.host.result_deliveries))
        self.assertEqual([], self.host.rollbacks)
