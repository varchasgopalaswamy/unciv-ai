"""Scheduler + real HTTPS agent services + fake native Unix app-server integration."""

import hashlib
import json
import os
import socket
import ssl
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from civ_agent.monitoring import path_for, read_collection, read_record
from civ_agent.runner_history import EventHistory, render_markdown
from civ_agent.scheduler import atomic_json
from tests.concurrent_trade_host import ConcurrentTradeHost
from tests.support import match_command
from tests.test_scheduler import FakeHost


class SchedulerServiceTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tls = tempfile.TemporaryDirectory(prefix="unciv service integration tls ")
        cls.addClassCleanup(cls.tls.cleanup)
        cls.certificate = Path(cls.tls.name) / "cert.pem"
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
        self.environment = dict(
            os.environ,
            CIV_TEST_RPC_TRACE=str(self.trace),
            CIV_TEST_CA=str(self.certificate),
            CIV_TEST_HOST=self.host.url,
            UNCIV_OPERATOR_TOKEN="must-not-enter-codex",
        )
        self.services = []
        for civilization in ("Rome", "Greece"):
            with socket.socket() as reservation:
                reservation.bind(("127.0.0.1", 0))
                port = reservation.getsockname()[1]
            output = (self.root / (civilization + ".log")).open("w+")
            self.addCleanup(output.close)
            process = subprocess.Popen(
                [
                    sys.executable,
                    "-B",
                    "-m",
                    "civ_agent",
                    "agent-server",
                    "start",
                    civilization,
                    "--state-dir",
                    str(self.state_dir),
                    "--port",
                    str(port),
                    "--mcp-url",
                    self.host.url + "/mcp",
                    "--mcp-ca-file",
                    str(self.certificate),
                    "--cert-file",
                    str(self.certificate),
                    "--key-file",
                    str(self.private_key),
                    "--ca-file",
                    str(self.certificate),
                    "--codex",
                    str(Path(__file__).with_name("fake_app_server.py")),
                    "--model",
                    civilization.lower() + "-service-model",
                    "--reasoning-effort",
                    "high",
                ],
                env=self.environment,
                stdout=output,
                stderr=output,
            )
            self.services.append((civilization, process, output))
            self.addCleanup(self.stop_process, process)
            identity = civilization
            endpoint_file = self.state_dir / "agent-services" / identity / "endpoint.json"
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                if process.poll() is not None:
                    output.seek(0)
                    self.fail("Agent service exited: " + output.read())
                if endpoint_file.exists():
                    endpoint = json.loads(endpoint_file.read_text())
                    token = json.loads(Path(endpoint["tokenFile"]).read_text())["token"]
                    request = urllib.request.Request(
                        endpoint["url"],
                        data=b'{"op":"status"}',
                        headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"},
                    )
                    try:
                        with urllib.request.urlopen(
                            request, context=ssl.create_default_context(cafile=self.certificate), timeout=0.5
                        ) as response:
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
        return subprocess.run(
            match_command(*arguments, "--state-dir", str(self.state_dir)),
            env=self.environment,
            capture_output=True,
            text=True,
            timeout=30,
        )

    def saved(self):
        return read_record(path_for(self.state_dir), "state", "run") or {}

    def records(self):
        return [json.loads(line) for line in self.trace.read_text().splitlines()]

    def source_history(self, stream):
        return (
            self.state_dir
            / "agent-services"
            / stream["civilization"]
            / "history"
            / hashlib.sha256(stream["activationId"].encode()).hexdigest()
            / "events.jsonl"
        )

    def gameplay_records(self, civilization="*"):
        return [
            row
            for path in (self.state_dir / "agent-services").glob(civilization + "/activations/*.json")
            if (row := json.loads(path.read_text())).get("purpose", "gameplay") == "gameplay"
        ]

    def test_terminal_results_reach_real_https_services_without_an_extra_model_turn(self):
        self.host.behaviors = ["victory"]
        result = self.run_cli("--max-turns", "2", "--max-seconds", "60")
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertEqual("complete", self.saved()["status"])
        self.assertEqual(0, self.saved()["handoffs"])
        self.assertEqual(1, len(self.host.plays))
        self.assertEqual([], self.host.restores)
        starts = [record for record in self.records() if record["method"] == "turn/start"]
        self.assertEqual(2, sum("outputSchema" in record["params"] for record in starts))
        self.assertEqual(1, sum("outputSchema" not in record["params"] for record in starts))
        for civilization in ("Rome", "Greece"):
            workspace = self.state_dir / "agents" / civilization
            delivered = json.loads((self.state_dir / "agent-services" / civilization / "result.json").read_text())
            self.assertEqual(civilization, delivered["civilization"])
            self.assertEqual("Victory" if civilization == "Rome" else "Defeat", delivered["result"]["outcome"])
            self.assertIn(civilization + " permitted report", json.dumps(delivered))
            self.assertFalse((workspace / "result.md").exists())
        records = self.gameplay_records("Rome")
        self.assertTrue(records[0]["terminalOutcome"])

    def test_real_services_start_fresh_threads_with_turn_memory_and_pinned_settings(self):
        result = self.run_cli("--max-turns", "2", "--max-seconds", "60")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual([play["civ"] for play in self.host.plays], ["Rome", "Greece", "Rome", "Greece"])
        self.assertEqual(self.saved()["handoffs"], 4)
        self.assertEqual(self.saved()["usage"]["input_tokens"], 40)
        records = self.records()
        starts = [record for record in records if record["method"] == "thread/start"]
        resumes = [record for record in records if record["method"] == "thread/resume"]
        self.assertEqual((len(starts), len(resumes)), (6, 0))  # Two opening reviews and four turns.
        self.assertEqual(4, len({play["thread"] for play in self.host.plays}))
        self.assertEqual(len({record["tokenHash"] for record in records}), 6)
        self.assertFalse(any(record["method"].startswith("economy/") for record in records))
        for record in starts + resumes:
            self.assertFalse(record["operatorTokenPresent"])
            self.assertIn("mcp_servers.civ", " ".join(record["argv"]))
            self.assertNotIn(self.host.url + "/mcp", " ".join(record["argv"]))
            self.assertIn("unix://", " ".join(record["argv"]))
            self.assertNotIn(self.host.operator_token, json.dumps(record))
            self.assertEqual(record["params"]["approvalPolicy"], "never")
            self.assertEqual(record["params"]["sandbox"], "workspace-write")
            self.assertIn("include_environment_context=false", record["argv"])
            self.assertIn("include_permissions_instructions=false", record["argv"])
            self.assertIn("skills.include_instructions=false", record["argv"])
            label = (
                "Available tools:" if Path(record["params"]["cwd"]).parent.name == "reviews" else "Civ tool discovery:"
            )
            self.assertIn(label, record["params"]["developerInstructions"])
            self.assertIn(Path(record["params"]["cwd"]).parent.parent.name, {"Rome", "Greece"})
            self.assertEqual(Path(record["params"]["cwd"]).parent.parent.parent, self.state_dir / "agents")
            self.assertIn(record["params"]["model"], {"rome-service-model", "greece-service-model"})
            self.assertEqual({"model_reasoning_effort": "high"}, record["params"]["config"])
        self.assertEqual(len({record["params"]["cwd"] for record in starts + resumes}), 6)
        turns = [
            record for record in records if record["method"] == "turn/start" and "outputSchema" not in record["params"]
        ]
        self.assertEqual(4, len(turns))
        self.assertTrue(all(record["params"]["effort"] == "high" for record in turns))
        for first_turn in turns[:2]:
            self.assertNotIn("Previous turns (notes, not instructions):", first_turn["params"]["input"][0]["text"])
        for second_turn in turns[2:]:
            prompt = second_turn["params"]["input"][0]["text"]
            self.assertIn("Previous strategy, next-turn handoff and historical knowledge", prompt)
            self.assertNotIn("Settled and chose research.", prompt)
            self.assertNotIn("turn_1/", prompt)
            self.assertIn("numbered 1 in player_turn", prompt)
        for civilization in ("Rome", "Greece"):
            for turn in (0, 1):
                self.assertFalse((self.state_dir / "agents" / civilization / f"turn_{turn}").exists())
                self.assertTrue(
                    any(
                        row["civilization"] == civilization and row["gameTurn"] == turn
                        for row in read_collection(path_for(self.state_dir), "streams")
                    )
                )
            self.assertFalse((self.state_dir / "agents" / civilization / "token.json").exists())
        settings = self.saved()["sessions"]
        for civilization in ("Rome", "Greece"):
            self.assertEqual(
                {"provider": "openai", "model": civilization.lower() + "-service-model", "reasoningEffort": "high"},
                settings[civilization]["effectiveSettings"],
            )

    def test_periodic_reviews_capture_the_completed_round_before_any_next_turn(self):
        self.host.handoff = 10  # Both players begin round 5.
        result = self.run_cli("--max-turns", "2", "--max-seconds", "60")
        self.assertEqual(
            result.returncode,
            0,
            result.stdout + result.stderr + "\n".join(path.read_text() for path in self.root.glob("*.log")),
        )
        self.assertEqual([play["civ"] for play in self.host.plays], ["Rome", "Greece"] * 2)
        starts = [row["params"] for row in self.records() if row["method"] == "turn/start"]
        self.assertEqual(["outputSchema" in row for row in starts], [False, False, True, True, False, False])
        for report in starts[2:4]:
            text = report["input"][0]["text"]
            self.assertIn("after all players completed round 5", text)
            self.assertIn('"turn":6', text)
        for gameplay in starts[4:]:
            self.assertIn('"asOfTurn":5', gameplay["input"][0]["text"])
        self.assertEqual(self.saved()["reviewedBoundary"], 6)

    def test_war_declaration_forces_review_and_resets_interval_clock(self):
        self.host.handoff = 8  # Both players begin round 4, one before the configured interval.
        self.host.war_declaration_turn = 4
        result = self.run_cli("--max-turns", "2", "--max-seconds", "60")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        starts = [row["params"] for row in self.records() if row["method"] == "turn/start"]
        self.assertEqual(["outputSchema" in row for row in starts], [False, False, True, True, False, False])
        for report in starts[2:4]:
            self.assertIn("after all players completed round 4", report["input"][0]["text"])
        saved = self.saved()
        self.assertEqual(saved["reviewAnchors"], {"Rome": 4, "Greece": 4})
        self.assertEqual(saved["reviewedWarTurn"], 4)

    def test_run_overrides_reach_native_turns_and_resume_without_repeating_flags(self):
        first = self.run_cli(
            "--model", "run-model", "--reasoning", "custom-effort", "--max-invocations", "1", "--max-seconds", "60"
        )
        self.assertEqual(0, first.returncode, first.stdout + first.stderr)
        resumed = self.run_cli("resume", "--max-invocations", "3")
        self.assertEqual(0, resumed.returncode, resumed.stdout + resumed.stderr)
        records = [
            record for record in self.records() if record["method"] in {"thread/start", "thread/resume", "turn/start"}
        ]
        self.assertEqual(10, len(records))  # Two opening reviews plus three gameplay requests.
        for record in records:
            parameters = record["params"]
            self.assertEqual("run-model", parameters["model"])
            effort = (
                parameters.get("effort")
                if record["method"] == "turn/start"
                else parameters["config"]["model_reasoning_effort"]
            )
            self.assertEqual("custom-effort", effort)
        self.assertNotEqual(self.host.plays[0]["thread"], self.host.plays[2]["thread"])

    def test_real_service_interruption_rolls_back_and_retries_in_a_fresh_thread(self):
        self.host.behaviors = ["hang", "complete", "complete"]
        process = subprocess.Popen(
            match_command("--max-turns", "1", "--max-seconds", "60", "--state-dir", str(self.state_dir)),
            env=self.environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        self.addCleanup(self.stop_process, process)
        assert process.stdout is not None and process.stderr is not None
        self.addCleanup(process.stdout.close)
        self.addCleanup(process.stderr.close)
        self.assertTrue(self.host.wait_for_plays(1, timeout=10))
        paused = self.run_cli("pause")
        self.assertEqual(paused.returncode, 0, paused.stdout + paused.stderr)
        output, error = process.communicate(timeout=10)
        self.assertEqual(process.returncode, 0, output + error)
        # Operator pause revokes the native token before the service receives
        # its interrupt. An in-flight negotiation poll may observe that first.
        # Both outcomes must still roll back and retry in a fresh conversation.
        stopped = self.saved()["inFlight"]["agentPhase"]
        self.assertIn(stopped, {"INTERRUPTED", "FAILED"})
        if stopped == "FAILED":
            self.assertEqual(self.gameplay_records("Rome")[-1]["error"], "NEGOTIATION_MONITOR_FAILED")
        previous_thread = self.gameplay_records("Rome")[-1]["threadId"]
        resumed = self.run_cli("resume")
        self.assertEqual(resumed.returncode, 0, resumed.stdout + resumed.stderr)
        self.assertEqual([play["civ"] for play in self.host.plays], ["Rome", "Rome", "Greece"])
        self.assertNotEqual(self.host.plays[0]["thread"], self.host.plays[1]["thread"])
        self.assertEqual(0, self.host.plays[1]["effectsBefore"])
        self.assertEqual(1, len(self.host.restores))
        records = self.gameplay_records("Rome")
        self.assertNotEqual(previous_thread, records[0]["threadId"])
        self.assertEqual(0, sum(record.get("aborted", False) for record in records))
        self.assertEqual(self.saved()["handoffs"], 2)

    def test_resume_preserves_old_epoch_history_without_replaying_committed_turn(self):
        first = self.run_cli("--max-invocations", "1", "--max-seconds", "60")
        self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
        self.assertEqual(self.host.handoff, 1)
        old = EventHistory(path_for(self.state_dir), "abandoned-epoch", "Rome", "old", game_turn=0)
        old.append([dict(sequence=0, kind="assistant_message", data=dict(text="Old attempt"))])
        resumed = self.run_cli("resume", "--max-invocations", "3")
        self.assertEqual(resumed.returncode, 0, resumed.stdout + resumed.stderr)
        self.assertEqual([play["civ"] for play in self.host.plays], ["Rome", "Greece", "Rome"])
        self.assertEqual(self.host.restores, [])
        self.assertEqual(old.events[0]["data"]["text"], "Old attempt")

    def test_full_event_pagination_mirrors_visible_text_and_large_results(self):
        self.host.behaviors = ["many_events", "many_events"]
        result = self.run_cli("--max-invocations", "1", "--max-seconds", "60")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        sources = list((self.state_dir / "agent-services").glob("*/history/*/events.jsonl"))
        streams = read_collection(path_for(self.state_dir), "streams")
        self.assertEqual(
            (len(sources), len(streams)), (3, 1)
        )  # Two opening reviews and one gameplay job; economy sleeps without cities.
        source_events = [json.loads(line) for line in self.source_history(streams[0]).read_text().splitlines()]
        mirror_events = read_collection(path_for(self.state_dir), "events")
        self.assertGreater(len(source_events), 100)
        self.assertEqual(source_events, mirror_events)
        markdown = render_markdown(streams[0], mirror_events)
        self.assertEqual(markdown.count("Settled and chose research."), 1)
        self.assertEqual(result.stdout.count("Settled and chose research."), 1)
        self.assertIn("Visible strategy summary.", markdown)
        self.assertNotIn("Visible strategy summary.", result.stdout)
        self.assertNotIn("[Rome] Tool result:", result.stdout)
        self.assertIn("z" * 200000, markdown)
        self.assertNotIn("RAW_REASONING_SENTINEL", markdown + json.dumps(mirror_events) + result.stdout)
        self.assertNotIn(self.host.operator_token, markdown + json.dumps(mirror_events) + result.stdout)
        before = mirror_events
        resumed = self.run_cli("resume")
        self.assertEqual(resumed.returncode, 0, resumed.stdout + resumed.stderr)
        self.assertEqual(
            before, [event for event in read_collection(path_for(self.state_dir), "events")][: len(before)]
        )
        self.assertNotIn("Settled and chose research.", resumed.stdout)
        verbose = self.run_cli("resume", "--max-invocations", "2", "--verbose")
        self.assertEqual(verbose.returncode, 0, verbose.stdout + verbose.stderr)
        self.assertIn("[Greece] Reasoning summary: Visible strategy summary.", verbose.stdout)
        self.assertIn("[Greece] Tool result:", verbose.stdout)
        self.assertNotIn("[Rome] Tool result:", verbose.stdout)
        self.assertEqual(
            before, [event for event in read_collection(path_for(self.state_dir), "events")][: len(before)]
        )
        self.assertNotIn("verbose", self.saved())

    def test_model_retry_is_visible_in_quiet_console_history_diagnostics_and_dashboard(self):

        def override(name, _arguments, _token):
            if name == "player_turn":
                return {
                    "turn": self.host.status()["turn"],
                    "canEndTurn": True,
                    "epoch": self.host.epoch,
                    "revision": "rev1",
                    "turnReview": {"id": "review1"},
                    "rules": {"victoryTypes": []},
                    "phase": self.host.phase,
                    "scenario": {"testModelRetry": True},
                }
            return None

        self.host.mcp_override = override
        result = self.run_cli("--max-invocations", "1", "--max-seconds", "60")
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertIn("[Rome] Warning: Model connection idle timeout; retrying (1/5", result.stdout)
        self.assertIn("Model response resumed", result.stdout)
        self.assertEqual(1, result.stdout.count("Model connection idle timeout"))
        identity = read_collection(path_for(self.state_dir), "streams")[0]
        source = self.source_history(identity)
        mirror_events = read_collection(path_for(self.state_dir), "events")
        self.assertEqual(
            [json.loads(line) for line in source.read_text().splitlines()],
            mirror_events,
        )
        notices = [event["data"] for event in mirror_events if event["kind"] == "model_status"]
        self.assertEqual(["retrying", "responding"], [event["state"] for event in notices])
        diagnostics = (self.state_dir / "agent-services/Rome/diagnostics.jsonl").read_text()
        self.assertIn('"event": "model_status"', diagnostics)
        self.assertIn('"reason": "idle_timeout"', diagnostics)
        from civ_agent.watch_state import activities

        activity = activities(path_for(self.state_dir), identity, mirror_events)
        warnings = [entry for entry in activity if entry.label == "Warning"]
        self.assertEqual(1, len(warnings))
        self.assertFalse(warnings[0].verbose)
        self.assertIn("retrying (1/5", warnings[0].text)
        for secret in ("PRIVATE_PROVIDER_HEADER", "PRIVATE_COOKIE"):
            self.assertNotIn(
                secret, result.stdout + source.read_text() + diagnostics + render_markdown(identity, mirror_events)
            )
        records = self.gameplay_records("Rome")
        self.assertEqual("responding", records[0]["modelStatus"]["state"])
        self.assertEqual(1, len(self.host.plays))

    def test_live_assistant_text_is_visible_before_any_handoff(self):
        self.host.behaviors = ["hang"]
        log = self.root / "live.log"
        with log.open("w") as output:
            process = subprocess.Popen(
                match_command("--max-seconds", "60", "--state-dir", str(self.state_dir)),
                env=self.environment,
                stdout=output,
                stderr=output,
            )
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

    def test_next_player_plays_while_previous_provider_completion_drains(self):
        self.host.behaviors = ["slow_report", "complete"]
        result = self.run_cli("--max-turns", "1", "--max-seconds", "60", "--turn-timeout", "15")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.host.handoff, 2)
        records = self.gameplay_records()
        self.assertEqual(len(records), 2)
        self.assertTrue(all(record["phase"] == "COMPLETED" for record in records))
        self.assertEqual(result.stdout.count("Settled and chose research."), 0)
        self.assertTrue(all(record["completionReason"] == "native_handoff" for record in records))
        self.assertTrue(all(record["usage"]["input_tokens"] == 10 for record in records))
        self.assertTrue(all("finalizationElapsedMs" in record for record in records))
        self.assertGreaterEqual(sum(row["method"] == "turn/interrupt" for row in self.records()), 1)
        calls = [row["params"] for row in self.records() if row["method"] == "test/tool"]
        self.assertEqual([play["civ"] for play in self.host.plays], ["Rome", "Greece"])
        self.assertFalse(any(row["tool"] == "memory_update" for row in calls))
        rome = next(row for row in records if row["civilization"] == "Rome")
        greece = next(row for row in records if row["civilization"] == "Greece")
        self.assertLess(greece["gameplayEndedAtMs"], rome["finalizationFinishedAtMs"])

    def test_gameplay_waits_while_chat_enacts_trade_and_scheduling_continues(self):
        self.exercise_concurrent_trade(peace=False)

    def test_turn_checkpoints_overlap_provider_completion_and_replace_previous_boundary(self):
        self.host.behaviors = ["complete", "slow_report", "complete", "complete"]
        result = self.run_cli("--max-turns", "2", "--max-seconds", "60", "--turn-timeout", "15")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.host.handoff, 4)
        self.assertEqual([play["civ"] for play in self.host.plays], ["Rome", "Greece", "Rome", "Greece"])
        records = [
            json.loads(path.read_text()) for path in (self.state_dir / "agent-services").glob("*/activations/*.json")
        ]
        rome_next = next(
            row
            for row in records
            if row.get("purpose", "gameplay") == "gameplay" and row["civilization"] == "Rome" and row["gameTurn"] == 1
        )
        greece_previous = next(
            row
            for row in records
            if row.get("purpose", "gameplay") == "gameplay" and row["civilization"] == "Greece" and row["gameTurn"] == 0
        )
        self.assertLess(rome_next["gameplayEndedAtMs"], greece_previous["finalizationFinishedAtMs"])
        checkpoint = json.loads((self.state_dir / "turn-recovery/current.json").read_text())
        self.assertEqual(checkpoint["handoff"], 3)
        for civilization, component in checkpoint["players"].items():
            directory = self.state_dir / "agent-services" / civilization / "turn-recovery"
            self.assertEqual([path.name for path in directory.glob("*.json")], [component["id"] + ".json"])
            saved = json.loads((directory / (component["id"] + ".json")).read_text())
            self.assertTrue(all("snapshot" not in row for row in saved["jobs"] if row["status"] == "COMPLETED"))

    def test_peace_uses_the_same_off_turn_counteroffer_and_acceptance_flow(self):
        self.exercise_concurrent_trade(peace=True)

    def test_negotiation_wait_does_not_exhaust_the_gameplay_activation_timeout(self):
        self.exercise_concurrent_trade(peace=False, settlement_delay=5, turn_timeout=3)

    def exercise_concurrent_trade(self, *, peace, settlement_delay=0, turn_timeout=30):
        trade = ConcurrentTradeHost(self.host, peace=peace, settlement_delay=settlement_delay)
        result = self.run_cli("--max-turns", "2", "--max-seconds", "60", "--turn-timeout", str(turn_timeout))
        diagnostics = ""
        if result.returncode or self.host.handoff != 4:
            diagnostics = "\n".join(path.read_text() for path in self.state_dir.rglob("diagnostics.jsonl"))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr + diagnostics)
        self.assertEqual(self.host.handoff, 4, result.stdout + result.stderr + diagnostics)
        self.assertEqual(self.saved()["status"], "budget_exhausted")
        self.assertEqual(trade.enacted, {"civilization": "Rome", "activeCivilization": "Rome", "handoff": 0})
        self.assertTrue(trade.gameplay_waiting)
        self.assertEqual(
            trade.writes,
            [("Rome", True, "trade_offer"), ("Greece", False, "trade_offer"), ("Rome", False, "trade_decide")],
        )
        # Each opening review captures its player's position before gameplay.
        self.assertEqual(
            6 if peace else 2, sum(name == "player_turn" and not gameplay for _, gameplay, name, _ in trade.reads)
        )
        records = self.records()
        preloaded = [r["params"] for r in records if r["method"] == "test/preloaded_trade"]
        self.assertEqual(preloaded, [{"proposalId": "d1", "version": 1}, {"proposalId": "d1", "version": 2}])
        self.assertFalse(
            any(
                r["method"] == "test/tool"
                and r["params"].get("purpose") == "communication"
                and r["params"].get("tool") == "trade_read"
                for r in records
            )
        )
        failures = [
            r["params"]["result"] for r in records if r["method"] == "test/tool" and r["params"]["result"].get("error")
        ]
        self.assertEqual(failures, [])
        for path in (self.state_dir / "agent-services").glob("*/activations/*.json"):
            self.assertEqual(json.loads(path.read_text())["phase"], "COMPLETED")
        starts = [r for r in records if r["method"] == "turn/start"]
        # Two opening reviews, four turns, two chat jobs and one gameplay continuation.
        self.assertEqual(len(starts), 15 if peace else 9)
        self.assertEqual(8 if peace else 2, sum("outputSchema" in r["params"] for r in starts))
        if peace:
            titles = [r["params"].get("outputSchema", {}).get("title") for r in starts]
            self.assertEqual(titles.count("TacticalOverview"), 3)
            self.assertEqual(titles.count("PeaceApproval"), 3)
        interruptions = [r for r in records if r["method"] == "turn/interrupt"]
        thread = next(
            r["params"]["threadId"]
            for r in starts
            if r["params"]["input"][0]["text"].startswith("The negotiation has finished.")
        )
        self.assertEqual(sum(r["params"]["threadId"] == thread for r in starts), 2)
        self.assertTrue(any(r["params"]["threadId"] == thread for r in interruptions))
        continuations = [r for r in records if r["method"] == "test/negotiation_result"]
        self.assertEqual(len(continuations), 1)
        self.assertEqual(continuations[0]["params"]["result"]["trade"]["version"], 2)
        opening = next(r for r in self.gameplay_records("Rome") if r["gameTurn"] == 0)
        self.assertEqual(opening["usage"]["input_tokens"], 20)
        self.assertTrue(opening["usageReported"])
        self.assertGreater(opening["negotiationWaitSeconds"], 0)
