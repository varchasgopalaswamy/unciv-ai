"""Exercise the supervisor through verified HTTPS fake host and agent services."""

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
import ssl
from pathlib import Path
import secrets
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import uuid


SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
from unciv_runner import Operator, OperatorError, RunnerError, atomic_json, operator_token, player_prompt




class FakeHost:
    def __init__(self, certificate, private_key, behaviors=()):
        self.epoch, self.instance = str(uuid.uuid4()), str(uuid.uuid4())
        self.operator_token = secrets.token_urlsafe(32)
        self.player_token = None
        self.lease, self.activation = None, None
        self.phase, self.scheduled = "ACTIVE", False
        self.handoff = 0
        self.behaviors = list(behaviors)
        self.calls, self.plays = [], []
        self.agent_tokens = {name: secrets.token_urlsafe(32) for name in ("Rome", "Greece")}
        self.activations = {}
        self.interruptions = []
        self.changed = threading.Condition()
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
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
                    elif self.path == "/simulate":
                        if self.headers.get("Authorization") != "Bearer " + str(owner.player_token) or owner.phase != "ACTIVE":
                            code, value = 403, {"error": "WRONG_PLAYER"}
                        elif body["civilization"] != owner.status()["activeCivilization"]:
                            code, value = 403, {"error": "WRONG_PLAYER"}
                        else:
                            behavior = owner.behaviors.pop(0) if owner.behaviors else "complete"
                            owner.plays.append(dict(civ=body["civilization"], thread=body["threadId"], token=owner.player_token))
                            if behavior in {"complete", "lost_response", "after_handoff_hang", "slow_report", "many_events"}:
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
        self.server.socket = context.wrap_socket(self.server.socket, server_side=True)
        self.port = self.server.server_address[1]
        self.url = f"https://localhost:{self.port}"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def status(self):
        return dict(epoch=self.epoch, serverInstance=self.instance, turn=self.handoff // 2, handoff=self.handoff,
                    phase=self.phase, activeCivilization=("Rome", "Greece")[self.handoff % 2],
                    scheduled=self.scheduled, leaseId=self.lease, leaseExpiresInMs=30000 if self.lease else 0)

    def request(self, body):
        self.calls.append(dict(body))
        operation = body["op"]
        extra = {}
        if operation == "acquire":
            if self.lease and self.lease != body["leaseId"]:
                return 409, {"error": "LEASE_BUSY"}
            self.lease, self.phase, self.scheduled = body["leaseId"], "PAUSED", True
            extra["leaseId"] = self.lease
        elif operation in {"activate", "heartbeat", "release"} or (operation == "pause" and "leaseId" in body):
            if body["leaseId"] != self.lease or self.lease is None:
                return 409, {"error": "LEASE_LOST"}
            if operation == "activate":
                if any(body[key] != self.status()[key] for key in ("epoch", "serverInstance", "handoff")):
                    return 409, {"error": "STALE_STATUS"}
                if body["civilization"] != self.status()["activeCivilization"]:
                    return 409, {"error": "WRONG_PLAYER"}
                self.phase, self.activation, self.player_token = "ACTIVE", body["activationId"], secrets.token_urlsafe(32)
                extra.update(playerToken=self.player_token, activationId=self.activation)
            elif operation in {"pause", "release"}:
                self.phase, self.player_token = "PAUSED", None
                if operation == "release":
                    self.lease = None
        elif operation == "pause":
            self.phase, self.player_token, self.lease = "PAUSED", None, None
        elif operation == "manual":
            if self.lease:
                return 409, {"error": "LEASE_BUSY"}
            self.phase, self.scheduled = "ACTIVE", False
        elif operation != "status":
            return 400, {"error": "BAD_REQUEST"}
        return 200, dict(status=self.status(), **extra)

    def agent_request(self, civilization, body):
        if body["op"] == "events":
            return 400, {"error": "INVALID_OPERATION"}  # Compatibility with an older service.
        key = (civilization, body["activationId"])
        if body["op"] == "start":
            if key not in self.activations:
                if body["playerToken"] != self.player_token or civilization != self.status()["activeCivilization"]:
                    return 403, {"error": "WRONG_PLAYER"}
                if any(status["phase"] == "RUNNING" for status in self.activations.values()):
                    return 409, {"error": "ANOTHER_AGENT_RUNNING"}
                behavior = self.behaviors.pop(0) if self.behaviors else "complete"
                thread = body.get("threadId") or str(uuid.uuid4())
                self.plays.append(dict(civ=civilization, thread=thread, token=body["playerToken"], request=body))
                if behavior in {"complete", "lost_response", "after_handoff_hang", "refuse_interrupt"}:
                    self.handoff += 1
                phase = "RUNNING" if behavior in {"hang", "after_handoff_hang", "refuse_interrupt"} else "COMPLETED"
                if behavior in {"crash", "lost_response", "missing_thread"}:
                    phase = "FAILED"
                self.activations[key] = dict(activationId=body["activationId"], epoch=body["epoch"], civilization=civilization,
                                             threadId=None if behavior == "missing_thread" else thread, phase=phase,
                                             usage={"input_tokens": 10, "cached_input_tokens": 2, "output_tokens": 3} if phase == "COMPLETED" else {},
                                             _behavior=behavior)
        if key not in self.activations:
            return 404, {"error": "UNKNOWN_ACTIVATION"}
        status = self.activations[key]
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
        subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1",
                        "-keyout", str(cls.private_key), "-out", str(cls.certificate), "-subj", "/CN=localhost",
                        "-addext", "subjectAltName=DNS:localhost"], check=True, capture_output=True)

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
            atomic_json(service / "endpoint.json", dict(civilizationId=civilization, url=self.host.url + "/agent/" + civilization,
                                                         tokenFile=str(service / "token.json"), caFile=str(self.certificate)))
        self.environment = dict(os.environ, UNCIV_OPERATOR_TOKEN="must-not-reach-model")

    def command(self, *args):
        return [sys.executable, "-B", str(SCRIPTS / "run_agents"), *args, "--state-dir", str(self.state)]

    def run_cli(self, *args, timeout=15):
        return subprocess.run(self.command(*args), env=self.environment, text=True, capture_output=True, timeout=timeout)

    def background(self, *args):
        process = subprocess.Popen(self.command(*args), env=self.environment, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        def cleanup():
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
            process.stdout.close()
            process.stderr.close()
        self.addCleanup(cleanup)
        return process

    def saved(self):
        return json.loads((self.state / "runner/run.json").read_text())

    def test_consecutive_rounds_use_player_sessions_tokens_and_authoritative_handoffs(self):
        result = self.run_cli("--max-turns", "2", "--max-invocations", "8", "--max-seconds", "30")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual([play["civ"] for play in self.host.plays], ["Rome", "Greece", "Rome", "Greece"])
        self.assertEqual(self.host.plays[0]["thread"], self.host.plays[2]["thread"])
        self.assertNotEqual(self.host.plays[0]["thread"], self.host.plays[1]["thread"])
        self.assertEqual(len({play["token"] for play in self.host.plays}), 4)
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
        self.assertEqual(self.host.plays[0]["thread"], self.host.plays[1]["thread"])
        self.assertIn("exact original request ID", self.host.plays[1]["request"]["prompt"])

    def test_lost_end_turn_response_uses_committed_handoff_without_replay(self):
        self.host.behaviors = ["lost_response", "complete"]
        result = self.run_cli("--max-turns", "1", "--max-seconds", "30")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual([play["civ"] for play in self.host.plays], ["Rome", "Greece"])
        self.assertEqual(self.saved()["handoffs"], 2)

    def test_hung_process_is_killed_and_not_automatically_retried(self):
        self.host.behaviors = ["hang"]
        result = self.run_cli("--turn-timeout", "1", "--max-seconds", "30")
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertEqual(len(self.host.plays), 1)
        self.assertEqual(self.saved()["status"], "needs_inspection")
        self.assertEqual(self.host.phase, "PAUSED")
        state = self.saved()
        self.assertEqual(state["status"], "needs_inspection")
        self.assertEqual(state["inFlight"]["agentPhase"], "INTERRUPTED")
        self.assertEqual(self.host.phase, "PAUSED")
        resumed = self.run_cli("resume")
        self.assertEqual(resumed.returncode, 2)
        self.assertEqual(len(self.host.plays), 1)

    def test_hung_report_after_handoff_does_not_prevent_next_player(self):
        self.host.behaviors = ["after_handoff_hang", "complete"]
        result = self.run_cli("--max-turns", "1", "--max-seconds", "30", "--turn-timeout", "10")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual([play["civ"] for play in self.host.plays], ["Rome", "Greece"])
        self.assertEqual(self.saved()["handoffs"], 2)

    def test_operator_pause_stops_child_and_resume_keeps_conversation_and_budgets(self):
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
        self.assertEqual(self.host.plays[0]["thread"], self.host.plays[1]["thread"])
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

    def test_crash_recovery_does_not_restart_an_uncertain_mutation(self):
        self.host.behaviors = ["hang"]
        process = self.background("--max-seconds", "30")
        self.assertTrue(self.host.wait_for_plays(1))
        time.sleep(0.15)  # Allow the thread.started event to be persisted.
        process.kill()
        process.communicate(timeout=5)
        self.host.lease = None  # Simulate host lease expiry after the supervisor crash.
        self.host.phase = "PAUSED"
        resumed = self.run_cli("resume")
        self.assertEqual(resumed.returncode, 2, resumed.stdout + resumed.stderr)
        self.assertEqual(len(self.host.plays), 1)
        self.assertEqual(self.saved()["status"], "needs_inspection")

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

    def test_explicit_limit_increases_keep_usage_conversations_and_time_origin(self):
        first = self.run_cli("--max-invocations", "1", "--max-seconds", "30")
        self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
        origin = self.saved()["startedAt"]
        resumed = self.run_cli("resume", "--max-invocations", "3", "--max-seconds", "60")
        self.assertEqual(resumed.returncode, 0, resumed.stdout + resumed.stderr)
        self.assertEqual(self.saved()["invocations"], 3)
        self.assertEqual(self.saved()["deadline"], origin + 60)
        self.assertEqual(self.host.plays[0]["thread"], self.host.plays[2]["thread"])
        refused = self.run_cli("resume", "--max-invocations", "2")
        self.assertEqual(refused.returncode, 2)
        self.assertEqual(len(self.host.plays), 3)

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
            Operator(self.host.url.replace("localhost", "127.0.0.1"), self.host.operator_token, self.certificate).call("status")

    def test_unconfirmed_agent_cancellation_prevents_next_dispatch(self):
        self.host.behaviors = ["refuse_interrupt"]
        result = self.run_cli("--max-turns", "1", "--max-seconds", "30", "--turn-timeout", "1")
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertEqual(len(self.host.plays), 1)

    def test_new_activation_names_the_current_turn_without_replaying_historical_end_turn(self):
        prompt = player_prompt("Greece", game_turn=2)
        self.assertIn("numbered 2 in player_turn", prompt)
        self.assertIn("A previous turn or report does not satisfy this request", prompt)
        self.assertIn("earlier completed END_TURN", prompt)
        self.assertNotIn("handoff", prompt)
        recovering = player_prompt("Greece", recovering=True, game_turn=2)
        self.assertIn("exact original request ID", recovering)
        self.assertIn("Do not repeat a mutation with a new request ID", recovering)

    def test_old_service_gets_restart_guidance_without_changing_turn_scheduling(self):
        result = self.run_cli("--max-turns", "1", "--max-seconds", "30")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("Restart launch_agent_service", result.stdout)
        self.assertEqual(self.host.handoff, 2)
        self.assertEqual(self.host.phase, "PAUSED")

    def test_missing_conversation_id_cannot_create_a_replay_conversation(self):
        self.host.behaviors = ["missing_thread"]
        failed = self.run_cli("--max-seconds", "30")
        self.assertEqual(failed.returncode, 2, failed.stdout + failed.stderr)
        resumed = self.run_cli("resume", "--continue-turn")
        self.assertEqual(resumed.returncode, 2, resumed.stdout + resumed.stderr)
        self.assertEqual(len(self.host.plays), 1)


if __name__ == "__main__":
    unittest.main()
