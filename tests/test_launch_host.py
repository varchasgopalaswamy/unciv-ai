"""Launcher tests use temporary match state and never start a JVM."""

import io
import json
import os
import stat
import subprocess
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from civ_agent import cli
from civ_agent.commands import host as host


class HostLauncherTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="host launcher tests ")
        self.addCleanup(self.temporary.cleanup)
        self.state = Path(self.temporary.name) / "match state"
        self.output = io.StringIO()
        self.errors = io.StringIO()
        self.addCleanup(patch.stopall)
        self.build = patch.object(host.subprocess, "run").start()
        patch.object(host.subprocess, "check_output", return_value="test-revision").start()
        self.execute = patch.object(host.os, "execv").start()
        self.tls = patch.object(
            host,
            "ensure_host_tls",
            return_value=(
                self.state / "tls/host/config.json",
                self.state / "tls/ca/cert.pem",
            ),
        ).start()

    def run_launcher(self, *args):
        with redirect_stdout(self.output), redirect_stderr(self.errors):
            return cli.main(["host", "start", "--state-dir", str(self.state), *args])

    def write_players(self, path=None):
        path = path or self.state / "players.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('{"players": []}\n', encoding="utf-8")
        path.chmod(0o600)
        return path

    def checkpoint(self):
        self.state.mkdir(parents=True, exist_ok=True)
        checkpoint = self.state / "exploration.json"
        checkpoint.write_text("existing checkpoint", encoding="utf-8")
        return checkpoint

    def test_mixed_game_distinguishes_humans_without_granting_native_ai_credentials(self):
        self.assertEqual(0, self.run_launcher("--scenario", "mixed-diplomacy"))
        players = json.loads((self.state / "players.json").read_text())["players"]
        self.assertEqual(
            {entry["civilizationId"]: entry.get("controller", "AGENT") for entry in players},
            {"Rome": "AGENT", "Greece": "AGENT", "France": "HUMAN", "Egypt": "HUMAN"},
        )
        self.assertEqual(4, len({entry["token"] for entry in players}))

    def test_ordinary_game_adds_native_ai_without_credentials(self):
        self.assertEqual(0, self.run_launcher("--new", "--built-in-ai", "Egypt", "--city-states", "2"))
        players = json.loads((self.state / "players.json").read_text())["players"]
        self.assertEqual(["Rome", "Greece", "France"], [entry["civilizationId"] for entry in players])
        command = self.execute.call_args.args[1]
        self.assertEqual("Egypt", command[command.index("--built-in-ai") + 1])

    def test_native_ai_option_cannot_change_a_saved_game(self):
        self.checkpoint()
        self.assertEqual(2, self.run_launcher("--built-in-ai", "Egypt"))
        self.build.assert_not_called()

    def test_native_ai_option_rejects_duplicate_agent_nation(self):
        self.assertEqual(2, self.run_launcher("--built-in-ai", "Rome"))
        self.build.assert_not_called()

    def test_new_game_creates_private_distinct_credentials_and_executes_argv(self):
        self.assertEqual(0, self.run_launcher("--scenario", "trade-cycle", "--seed", "42", "--port", "8766"))
        players_path = self.state / "players.json"
        players = json.loads(players_path.read_text())["players"]
        self.assertEqual(["Rome", "Greece"], [entry["civilizationId"] for entry in players])
        self.assertNotEqual(players[0]["token"], players[1]["token"])
        for entry in players:
            self.assertRegex(entry["token"], r"^[A-Za-z0-9_-]{43}$")
            self.assertNotIn(entry["token"], self.output.getvalue() + self.errors.getvalue())
        self.assertEqual(0o600, stat.S_IMODE(players_path.stat().st_mode))
        self.build.assert_called_once_with(
            [str(Path(__file__).resolve().parents[1] / "gradlew"), "installDist", "--console=plain"],
            cwd=Path(__file__).resolve().parents[1],
            check=True,
        )
        executable = str(Path(__file__).resolve().parents[1] / "build/install/unciv-ai/bin/unciv-ai")
        self.execute.assert_called_once_with(
            executable,
            [
                executable,
                "--new-game",
                "--checkpoint",
                str(self.state / "exploration.json"),
                "--seed",
                "42",
                "--scenario",
                "trade-cycle",
                "--data-dir",
                str(self.state / "data"),
                "--mcp-config",
                str(players_path),
                "--operator-config",
                str(self.state / "operator.json"),
                "--tls-config",
                str(self.state / "tls/host/config.json"),
                "--port",
                "8766",
            ],
        )
        self.tls.assert_called_once_with(self.state, "127.0.0.1", "localhost")
        self.assertEqual(
            {"hostUrl": "https://localhost:8766", "caFile": str(self.state / "tls/ca/cert.pem")},
            json.loads((self.state / "endpoints.json").read_text()),
        )
        self.assertEqual(0o600, stat.S_IMODE((self.state / "endpoints.json").stat().st_mode))
        self.assertIn("MCP-READY:https://localhost:8766/mcp", self.output.getvalue())

    def test_existing_checkpoint_resumes_and_preserves_credentials(self):
        checkpoint = self.checkpoint()
        players = self.write_players()
        original = players.read_bytes()
        self.assertEqual(0, self.run_launcher())
        self.assertEqual(original, players.read_bytes())
        self.assertEqual("existing checkpoint", checkpoint.read_text())
        command = self.execute.call_args.args[1]
        self.assertEqual(["--resume-checkpoint", str(checkpoint)], command[1:3])
        self.assertNotIn("--new-game", command)

    def test_resume_without_credentials_refuses_to_rotate_them(self):
        self.checkpoint()
        self.assertEqual(2, self.run_launcher())
        self.assertIn("Restore players.json", self.errors.getvalue())
        self.assertFalse((self.state / "players.json").exists())
        self.build.assert_not_called()
        self.execute.assert_not_called()

    def test_new_refuses_existing_checkpoint_without_mutation(self):
        checkpoint = self.checkpoint()
        self.assertEqual(2, self.run_launcher("--new"))
        self.assertEqual("existing checkpoint", checkpoint.read_text())
        self.assertFalse((self.state / "players.json").exists())
        self.build.assert_not_called()

    def test_seed_is_rejected_when_resuming(self):
        self.checkpoint()
        self.write_players()
        self.assertEqual(2, self.run_launcher("--seed", "-7"))
        self.assertIn("only to a new game", self.errors.getvalue())
        self.build.assert_not_called()

    def test_default_new_game_provisions_all_three_players_without_scenario(self):
        self.assertEqual(0, self.run_launcher("--new", "--seed", "-7", "--trade-counteroffers", "5"))
        players = json.loads((self.state / "players.json").read_text())["players"]
        self.assertEqual(["Rome", "Greece", "France"], [entry["civilizationId"] for entry in players])
        self.assertEqual(3, len({entry["token"] for entry in players}))
        self.assertEqual(0o600, stat.S_IMODE((self.state / "players.json").stat().st_mode))
        for entry in players:
            self.assertRegex(entry["token"], r"^[A-Za-z0-9_-]{43}$")
            self.assertNotIn(entry["token"], self.output.getvalue() + self.errors.getvalue())
        command = self.execute.call_args.args[1]
        self.assertIn("--new-game", command)
        self.assertNotIn("--scenario", command)
        self.assertEqual("-7", command[command.index("--seed") + 1])
        self.assertEqual("5", command[command.index("--trade-counteroffers") + 1])
        self.assertIn("Starting Vanilla Rome/Greece/France game", self.output.getvalue())

    def test_resume_forwards_scenario_for_checkpoint_validation(self):
        self.checkpoint()
        self.write_players()
        for scenario in ("trade-cycle", "war-paid", "war-joint", "agreement-lifecycle"):
            with self.subTest(scenario=scenario):
                self.assertEqual(0, self.run_launcher("--scenario", scenario))
                command = self.execute.call_args.args[1]
                self.assertIn("--resume-checkpoint", command)
                self.assertEqual(scenario, command[command.index("--scenario") + 1])

    def test_new_game_forwards_city_state_count(self):
        self.assertEqual(0, self.run_launcher("--new", "--city-states", "2"))
        command = self.execute.call_args.args[1]
        self.assertEqual("2", command[command.index("--city-states") + 1])
        players = json.loads((self.state / "players.json").read_text())["players"]
        self.assertEqual(["Rome", "Greece", "France"], [entry["civilizationId"] for entry in players])

    def test_city_state_count_rejected_for_prepared_games_and_resume(self):
        self.assertEqual(2, self.run_launcher("--scenario", "war-paid", "--city-states", "2"))
        self.assertFalse(self.state.exists())
        self.checkpoint()
        self.write_players()
        self.assertEqual(2, self.run_launcher("--city-states", "2"))
        self.build.assert_not_called()
        self.execute.assert_not_called()

    def test_removed_scenarios_are_rejected_without_creating_state(self):
        for scenario in (
            "city-management",
            "great-people",
            "async-chat",
            "verbal-betrayal",
            "async-trade",
            "async-peace",
            "bilateral-trade",
        ):
            with self.subTest(scenario=scenario), self.assertRaises(SystemExit) as failure:
                self.run_launcher("--scenario", scenario)
            self.assertEqual(2, failure.exception.code)
        self.assertFalse(self.state.exists())
        self.build.assert_not_called()
        self.execute.assert_not_called()

    def test_custom_paths_with_spaces_and_engine_override_are_single_arguments(self):
        custom_players = self.write_players(Path(self.temporary.name) / "private players.json")
        custom_checkpoint = Path(self.temporary.name) / "other match" / "save.json"
        self.assertEqual(
            0,
            self.run_launcher(
                "--scenario",
                "trade-cycle",
                "--players",
                str(custom_players),
                "--checkpoint",
                str(custom_checkpoint),
                "--unciv-dir",
                "../engine checkout",
            ),
        )
        build_args = self.build.call_args.args[0]
        self.assertEqual(
            f"-PuncivDir={(Path(__file__).resolve().parents[1] / '../engine checkout').resolve()}", build_args[-1]
        )
        command = self.execute.call_args.args[1]
        self.assertEqual(str(custom_checkpoint), command[command.index("--checkpoint") + 1])
        self.assertEqual(str(custom_players), command[command.index("--mcp-config") + 1])

    def test_environment_selects_default_state_directory(self):
        with patch.dict(os.environ, {"UNCIV_HOST_DIR": str(self.state)}):
            self.assertEqual(str(self.state), host.parser().parse_args([]).state_dir)

    def test_insecure_existing_credentials_are_not_modified(self):
        players = self.write_players()
        players.chmod(0o644)
        original = players.read_bytes()
        self.assertEqual(2, self.run_launcher("--scenario", "trade-cycle"))
        self.assertEqual(original, players.read_bytes())
        self.assertEqual(0o644, stat.S_IMODE(players.stat().st_mode))
        self.assertIn("chmod 600", self.errors.getvalue())
        self.build.assert_not_called()

    def test_build_failure_does_not_launch_host(self):
        self.build.side_effect = subprocess.CalledProcessError(1, ["gradlew"])
        self.assertEqual(1, self.run_launcher("--scenario", "trade-cycle"))
        self.assertIn("Host build failed", self.errors.getvalue())
        self.execute.assert_not_called()
        self.assertFalse((self.state / "endpoints.json").exists())

    def test_custom_tls_paths_and_hostname_are_preserved(self):
        self.state.mkdir(parents=True)
        tls = self.state / "custom TLS.json"
        store, password, ca = [self.state / name for name in ("custom.p12", "password.txt", "custom-ca.pem")]
        for path in (store, password, ca):
            path.write_text("temporary test material")
            path.chmod(0o600)
        tls.write_text(
            json.dumps(
                {
                    "keyStorePath": str(store),
                    "keyStorePasswordFile": str(password),
                    "keyAlias": "custom",
                    "listenHost": "0.0.0.0",
                    "publicHost": "game.example.com",
                }
            )
        )
        tls.chmod(0o600)
        self.assertEqual(
            0, self.run_launcher("--scenario", "trade-cycle", "--tls-config", str(tls), "--ca-file", str(ca))
        )
        self.tls.assert_not_called()
        command = self.execute.call_args.args[1]
        self.assertEqual(str(tls), command[command.index("--tls-config") + 1])
        self.assertNotIn("temporary test material", " ".join(command) + self.output.getvalue())
        self.assertEqual(
            "https://game.example.com:8765", json.loads((self.state / "endpoints.json").read_text())["hostUrl"]
        )

    def test_tls_overrides_must_be_complete_and_cannot_conflict(self):
        for arguments in (
            ("--tls-config", "/no/config"),
            ("--ca-file", "/no/ca"),
            ("--tls-config", "/no/config", "--ca-file", "/no/ca", "--public-host", "remote.example"),
        ):
            with self.subTest(arguments=arguments):
                self.assertEqual(2, self.run_launcher("--scenario", "trade-cycle", *arguments))
        self.build.assert_not_called()
        self.tls.assert_not_called()
        self.assertFalse((self.state / "players.json").exists())

    def test_rejects_invalid_ports_and_out_of_range_seeds(self):
        for arguments in (["--port", "0"], ["--port", "65536"], ["--port", "bad"], ["--seed", str(2**63)]):
            with self.subTest(arguments=arguments), redirect_stderr(self.errors):
                with self.assertRaises(SystemExit) as failure:
                    host.parser().parse_args(arguments)
                self.assertEqual(2, failure.exception.code)
