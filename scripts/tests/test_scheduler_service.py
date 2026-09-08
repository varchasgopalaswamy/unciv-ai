"""Scheduler + real HTTPS agent services + fake native Unix app-server integration."""

import hashlib
import json
import os
from pathlib import Path
import socket
import ssl
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.request

from test_scheduler import FakeHost, SCRIPTS
from unciv_runner import atomic_json


class SchedulerServiceTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tls = tempfile.TemporaryDirectory(prefix="unciv service integration tls ")
        cls.addClassCleanup(cls.tls.cleanup)
        cls.certificate = Path(cls.tls.name) / "cert.pem"
        cls.private_key = Path(cls.tls.name) / "key.pem"
        subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1",
                        "-keyout", str(cls.private_key), "-out", str(cls.certificate), "-subj", "/CN=localhost",
                        "-addext", "subjectAltName=DNS:localhost"], check=True, capture_output=True)

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="unciv service integration ")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.host = FakeHost(self.certificate, self.private_key)
        self.addCleanup(self.host.close)
        self.state_dir = self.root / "match"
        self.state_dir.mkdir(mode=0o700)
        atomic_json(self.state_dir / "operator.json", {"token": self.host.operator_token})
        atomic_json(self.state_dir / "endpoints.json", {"hostUrl": self.host.url, "caFile": str(self.certificate)})
        self.trace = self.root / "rpc.jsonl"
        self.environment = dict(os.environ, CIV_TEST_RPC_TRACE=str(self.trace), CIV_TEST_CA=str(self.certificate),
                                CIV_TEST_HOST=self.host.url, UNCIV_OPERATOR_TOKEN="must-not-enter-codex")
        self.services = []
        for civilization in ("Rome", "Greece"):
            with socket.socket() as reservation:
                reservation.bind(("127.0.0.1", 0))
                port = reservation.getsockname()[1]
            output = (self.root / (civilization + ".log")).open("w+")
            self.addCleanup(output.close)
            process = subprocess.Popen([
                sys.executable, "-B", str(SCRIPTS / "launch_agent_service"), civilization,
                "--state-dir", str(self.state_dir), "--port", str(port),
                "--mcp-url", self.host.url + "/mcp", "--mcp-ca-file", str(self.certificate),
                "--cert-file", str(self.certificate), "--key-file", str(self.private_key), "--ca-file", str(self.certificate),
                "--codex", str(SCRIPTS / "tests/fake_app_server.py"),
            ], env=self.environment, stdout=output, stderr=output)
            self.services.append((civilization, process, output))
            self.addCleanup(self.stop_process, process)
            identity = hashlib.sha256(civilization.encode()).hexdigest()
            endpoint_file = self.state_dir / "agent-services" / identity / "endpoint.json"
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                if process.poll() is not None:
                    output.seek(0)
                    self.fail("Agent service exited: " + output.read())
                if endpoint_file.exists():
                    endpoint = json.loads(endpoint_file.read_text())
                    token = json.loads(Path(endpoint["tokenFile"]).read_text())["token"]
                    request = urllib.request.Request(endpoint["url"], data=b'{"op":"status"}',
                                                     headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"})
                    try:
                        with urllib.request.urlopen(request, context=ssl.create_default_context(cafile=self.certificate), timeout=0.5) as response:
                            if json.load(response)["status"]["phase"] == "IDLE":
                                break
                    except (urllib.error.URLError, TimeoutError):
                        pass
                time.sleep(0.05)
            else:
                output.seek(0)
                self.fail("Agent service did not become ready: " + output.read())

    @staticmethod
    def stop_process(process):
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=8)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=3)

    def run_cli(self, *arguments):
        return subprocess.run([sys.executable, "-B", str(SCRIPTS / "run_agents"), *arguments,
                               "--state-dir", str(self.state_dir)], env=self.environment,
                              capture_output=True, text=True, timeout=30)

    def saved(self):
        return json.loads((self.state_dir / "runner/run.json").read_text())

    def records(self):
        return [json.loads(line) for line in self.trace.read_text().splitlines()]

    def test_real_services_keep_threads_tokens_epoch_workspaces_and_usage(self):
        result = self.run_cli("--max-turns", "2", "--max-seconds", "60")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual([play["civ"] for play in self.host.plays], ["Rome", "Greece", "Rome", "Greece"])
        self.assertEqual(self.saved()["handoffs"], 4)
        self.assertEqual(self.saved()["usage"]["input_tokens"], 40)
        records = self.records()
        starts = [record for record in records if record["method"] == "thread/start"]
        resumes = [record for record in records if record["method"] == "thread/resume"]
        self.assertEqual((len(starts), len(resumes)), (2, 2))
        self.assertEqual({item["params"]["threadId"] for item in resumes}, {self.host.plays[0]["thread"], self.host.plays[1]["thread"]})
        self.assertEqual(len({record["tokenHash"] for record in records}), 4)
        for record in starts + resumes:
            self.assertFalse(record["operatorTokenPresent"])
            self.assertIn("mcp_servers.civ", " ".join(record["argv"]))
            self.assertIn(self.host.url + "/mcp", " ".join(record["argv"]))
            self.assertIn("unix://", " ".join(record["argv"]))
            self.assertNotIn(self.host.operator_token, json.dumps(record))
            self.assertEqual(record["params"]["approvalPolicy"], "never")
            self.assertEqual(record["params"]["sandbox"], "workspace-write")
            self.assertEqual(Path(record["params"]["cwd"]).name, hashlib.sha256(self.host.epoch.encode()).hexdigest())
        self.assertEqual(len({record["params"]["cwd"] for record in starts + resumes}), 2)

    def test_real_service_interruption_fences_hung_activation_and_preserves_resume(self):
        self.host.behaviors = ["hang", "complete", "complete"]
        process = subprocess.Popen([sys.executable, "-B", str(SCRIPTS / "run_agents"), "--max-turns", "1", "--max-seconds", "60",
                                    "--state-dir", str(self.state_dir)], env=self.environment,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.addCleanup(self.stop_process, process)
        self.addCleanup(process.stdout.close)
        self.addCleanup(process.stderr.close)
        self.assertTrue(self.host.wait_for_plays(1, timeout=10))
        paused = self.run_cli("pause")
        self.assertEqual(paused.returncode, 0, paused.stdout + paused.stderr)
        output, error = process.communicate(timeout=10)
        self.assertEqual(process.returncode, 0, output + error)
        self.assertEqual(self.saved()["inFlight"]["agentPhase"], "INTERRUPTED")
        resumed = self.run_cli("resume")
        self.assertEqual(resumed.returncode, 0, resumed.stdout + resumed.stderr)
        self.assertEqual([play["civ"] for play in self.host.plays], ["Rome", "Rome", "Greece"])
        self.assertEqual(self.host.plays[0]["thread"], self.host.plays[1]["thread"])
        self.assertEqual(self.saved()["handoffs"], 2)

    def test_full_event_pagination_mirrors_visible_text_and_large_results(self):
        self.host.behaviors = ["many_events"]
        result = self.run_cli("--max-invocations", "1", "--max-seconds", "60")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        sources = list((self.state_dir / "agent-services").glob("*/history/*/events.jsonl"))
        mirrors = list((self.state_dir / "runner/history").glob("*/*/*/events.jsonl"))
        self.assertEqual((len(sources), len(mirrors)), (1, 1))
        source_events = [json.loads(line) for line in sources[0].read_text().splitlines()]
        mirror_events = [json.loads(line) for line in mirrors[0].read_text().splitlines()]
        self.assertGreater(len(source_events), 100)
        self.assertEqual(source_events, mirror_events)
        markdown = mirrors[0].with_name("history.md").read_text()
        self.assertEqual(markdown.count("Settled and chose research."), 1)
        self.assertEqual(result.stdout.count("Settled and chose research."), 1)
        self.assertIn("Visible strategy summary.", markdown)
        self.assertIn("z" * 200000, markdown)
        self.assertNotIn("RAW_REASONING_SENTINEL", markdown + mirrors[0].read_text() + result.stdout)
        self.assertNotIn(self.host.operator_token, markdown + mirrors[0].read_text() + result.stdout)
        before = mirrors[0].read_bytes()
        resumed = self.run_cli("resume")
        self.assertEqual(resumed.returncode, 0, resumed.stdout + resumed.stderr)
        self.assertEqual(before, mirrors[0].read_bytes())
        self.assertNotIn("Settled and chose research.", resumed.stdout)

    def test_live_assistant_text_is_visible_before_any_handoff(self):
        self.host.behaviors = ["hang"]
        log = self.root / "live.log"
        with log.open("w") as output:
            process = subprocess.Popen([sys.executable, "-B", str(SCRIPTS / "run_agents"), "--max-seconds", "60",
                                        "--state-dir", str(self.state_dir)], env=self.environment, stdout=output, stderr=output)
            self.addCleanup(self.stop_process, process)
            deadline = time.monotonic() + 10
            while "Checking current position." not in log.read_text() and time.monotonic() < deadline:
                time.sleep(0.05)
            self.assertIn("[Rome] Assistant: Checking current position.", log.read_text())
            self.assertEqual(self.host.handoff, 0)
            paused = self.run_cli("pause")
            self.assertEqual(paused.returncode, 0, paused.stdout + paused.stderr)
            process.wait(timeout=10)
            self.assertEqual(process.returncode, 0, log.read_text())

    def test_committed_handoff_allows_a_slow_final_report_to_finish(self):
        self.host.behaviors = ["slow_report", "complete"]
        result = self.run_cli("--max-turns", "1", "--max-seconds", "60", "--turn-timeout", "15")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.host.handoff, 2)
        records = [json.loads(path.read_text()) for path in (self.state_dir / "agent-services").glob("*/activations/*.json")]
        self.assertEqual(len(records), 2)
        self.assertTrue(all(record["phase"] == "COMPLETED" for record in records))
        self.assertEqual(result.stdout.count("Settled and chose research."), 2)


if __name__ == "__main__":
    unittest.main()
