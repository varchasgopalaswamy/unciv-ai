"""Launcher tests use temporary match state and never start a JVM."""

from contextlib import redirect_stderr, redirect_stdout
import importlib.machinery
import importlib.util
import io
import json
import os
from pathlib import Path
import stat
import subprocess
import tempfile
import unittest
from unittest.mock import patch


SCRIPT = Path(__file__).resolve().parents[1] / "launch_host"
LOADER = importlib.machinery.SourceFileLoader("launch_host", str(SCRIPT))
SPEC = importlib.util.spec_from_loader(LOADER.name, LOADER)
host = importlib.util.module_from_spec(SPEC)
LOADER.exec_module(host)


class HostLauncherTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="host launcher tests ")
        self.addCleanup(self.temporary.cleanup)
        self.state = Path(self.temporary.name) / "match state"
        self.output = io.StringIO()
        self.errors = io.StringIO()
        self.addCleanup(patch.stopall)
        self.build = patch.object(host.subprocess, "run").start()
        self.execute = patch.object(host.os, "execv").start()

    def run_launcher(self, *args):
        with redirect_stdout(self.output), redirect_stderr(self.errors):
            return host.main(["--state-dir", str(self.state), *args])

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

    def test_new_game_creates_private_distinct_credentials_and_executes_argv(self):
        self.assertEqual(0, self.run_launcher("--seed", "42", "--port", "8766"))
        players_path = self.state / "players.json"
        players = json.loads(players_path.read_text())["players"]
        self.assertEqual(["Rome", "Greece"], [entry["civilizationId"] for entry in players])
        self.assertNotEqual(players[0]["token"], players[1]["token"])
        for entry in players:
            self.assertRegex(entry["token"], r"^[A-Za-z0-9_-]{43}$")
            self.assertNotIn(entry["token"], self.output.getvalue() + self.errors.getvalue())
        self.assertEqual(0o600, stat.S_IMODE(players_path.stat().st_mode))
        self.build.assert_called_once_with(
            [str(host.REPO_DIR / "gradlew"), "installDist", "--console=plain"],
            cwd=host.REPO_DIR,
            check=True,
        )
        executable = str(host.REPO_DIR / "build/install/unciv-ai/bin/unciv-ai")
        self.execute.assert_called_once_with(executable, [
            executable, "--new-duel", "--checkpoint", str(self.state / "exploration.json"),
            "--seed", "42", "--data-dir", str(self.state / "data"),
            "--mcp-config", str(players_path), "--port", "8766",
        ])

    def test_existing_checkpoint_resumes_and_preserves_credentials(self):
        checkpoint = self.checkpoint()
        players = self.write_players()
        original = players.read_bytes()
        self.assertEqual(0, self.run_launcher())
        self.assertEqual(original, players.read_bytes())
        self.assertEqual("existing checkpoint", checkpoint.read_text())
        command = self.execute.call_args.args[1]
        self.assertEqual(["--resume-checkpoint", str(checkpoint)], command[1:3])
        self.assertNotIn("--new-duel", command)

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

    def test_custom_paths_with_spaces_and_engine_override_are_single_arguments(self):
        custom_players = self.write_players(Path(self.temporary.name) / "private players.json")
        custom_checkpoint = Path(self.temporary.name) / "other match" / "save.json"
        self.assertEqual(0, self.run_launcher(
            "--players", str(custom_players), "--checkpoint", str(custom_checkpoint),
            "--unciv-dir", "../engine checkout",
        ))
        build_args = self.build.call_args.args[0]
        self.assertEqual(f"-PuncivDir={(host.REPO_DIR / '../engine checkout').resolve()}", build_args[-1])
        command = self.execute.call_args.args[1]
        self.assertEqual(str(custom_checkpoint), command[command.index("--checkpoint") + 1])
        self.assertEqual(str(custom_players), command[command.index("--mcp-config") + 1])

    def test_environment_selects_default_state_directory(self):
        with patch.dict(os.environ, {"UNCIV_HOST_DIR": str(self.state)}):
            self.assertEqual(str(self.state), host.parse_args([]).state_dir)

    def test_insecure_existing_credentials_are_not_modified(self):
        players = self.write_players()
        players.chmod(0o644)
        original = players.read_bytes()
        self.assertEqual(2, self.run_launcher())
        self.assertEqual(original, players.read_bytes())
        self.assertEqual(0o644, stat.S_IMODE(players.stat().st_mode))
        self.assertIn("chmod 600", self.errors.getvalue())
        self.build.assert_not_called()

    def test_build_failure_does_not_launch_host(self):
        self.build.side_effect = subprocess.CalledProcessError(1, ["gradlew"])
        self.assertEqual(1, self.run_launcher())
        self.assertIn("Host build failed", self.errors.getvalue())
        self.execute.assert_not_called()

    def test_rejects_invalid_ports_and_out_of_range_seeds(self):
        for arguments in (["--port", "0"], ["--port", "65536"], ["--port", "bad"], ["--seed", str(2**63)]):
            with self.subTest(arguments=arguments), redirect_stderr(self.errors):
                with self.assertRaises(SystemExit) as failure:
                    host.parse_args(arguments)
                self.assertEqual(2, failure.exception.code)


if __name__ == "__main__":
    unittest.main()
