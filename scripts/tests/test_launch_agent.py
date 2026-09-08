import contextlib
import importlib.machinery
import importlib.util
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch


SCRIPT = Path(__file__).resolve().parents[1] / "launch_agent"
sys.path.insert(0, str(SCRIPT.parent))
loader = importlib.machinery.SourceFileLoader("launch_agent", str(SCRIPT))
spec = importlib.util.spec_from_loader(loader.name, loader)
agent = importlib.util.module_from_spec(spec)
loader.exec_module(agent)


class LaunchAgentTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="player scripts ")
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.players = self.root / "players.json"
        self.players.write_text(json.dumps({"players": [
            {"civilizationId": "Rome", "token": "A" * 43},
            {"civilizationId": "Greece", "token": "B" * 43},
        ]}))
        self.players.chmod(0o600)
        self.ca = self.root / "public-ca.pem"
        self.ca.write_text("test CA")
        (self.root / "endpoints.json").write_text(json.dumps({"hostUrl": "https://localhost:8765", "caFile": str(self.ca)}))
        original_umask = os.umask(0o077)
        os.umask(original_umask)
        self.addCleanup(os.umask, original_umask)

    def launch(self, civilization="Rome", *extra, connected=True):
        output = io.StringIO()
        with patch.object(agent.shutil, "which", return_value="/tools/codex"), \
                patch.object(agent.os, "execvpe") as execute, \
                patch.object(agent, "verify_https_connection") as connect, \
                patch.object(agent, "codex_trust_environment", side_effect=lambda ca, env, cwd: dict(env, CODEX_CA_CERTIFICATE=str(ca))) as trust, \
                contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
            if not connected:
                connect.side_effect = ConnectionRefusedError()
            code = agent.main([civilization, "--state-dir", str(self.root), *extra])
        return code, execute, output.getvalue()

    def test_each_player_receives_only_its_selected_token_and_distinct_directory(self):
        original = self.players.read_bytes()
        for name, token in [("Rome", "A" * 43), ("Greece", "B" * 43)]:
            code, execute, output = self.launch(name)
            self.assertEqual(code, 0)
            binary, command, env = execute.call_args.args
            self.assertEqual(env["UNCIV_PLAYER_TOKEN"], token)
            self.assertEqual(env["CODEX_CA_CERTIFICATE"], str(self.ca))
            self.assertEqual(command[command.index("--cd") + 1], str(self.root / "agents" / name))
            self.assertIn(name, command[-1])
            self.assertNotIn(token, " ".join(command) + output)
            self.assertEqual(binary, "/tools/codex")
            self.assertIn('bearer_token_env_var="UNCIV_PLAYER_TOKEN"', command[command.index("-c") + 1])
            self.assertIn("required=true", command[command.index("-c") + 1])
        self.assertEqual(original, self.players.read_bytes())

    def test_dry_run_exposes_no_token_and_does_not_create_workspace(self):
        code, execute, output = self.launch("rome", "--dry-run", "--port", "8989")
        self.assertEqual(code, 0)
        execute.assert_not_called()
        self.assertFalse((self.root / "agents").exists())
        self.assertIn("https://localhost:8989/mcp", output)
        self.assertNotIn("A" * 43, output)
        self.assertNotIn("B" * 43, output)

    def test_default_prompt_exercises_diplomacy_only_for_the_observed_close_start_scenario(self):
        for civilization in ("Rome", "Greece"):
            code, execute, _ = self.launch(civilization)
            self.assertEqual(code, 0)
            prompt = execute.call_args.args[1][-1]
            self.assertIn("Only when player_turn.scenario.name is CLOSE_START_DIPLOMACY", prompt)
            self.assertIn("Rome declares war on Greece when legal", prompt)
            self.assertIn("Rome proposes peace when negotiatePeace.available", prompt)
            self.assertIn("Greece reads and accepts that incoming offer when legal", prompt)
            self.assertIn("COMPLETE: never redeclare", prompt)
            self.assertIn("reviewId=turnReview.id", prompt)
            self.assertIn("deferReason explaining idle units with legalDestinations>0", prompt)
            self.assertIn("previousTurnReview", prompt)

    def test_resume_is_explicit_and_keeps_player_configuration(self):
        code, execute, _ = self.launch("Greece", "--resume", "conversation-id", "--model", "chosen-model",
                                       "--reasoning-effort", "high", "--prompt", "Continue my turn")
        self.assertEqual(code, 0)
        command = execute.call_args.args[1]
        self.assertEqual(command[-4:], ["resume", "--", "conversation-id", "Continue my turn"])
        self.assertEqual(command[command.index("--model") + 1], "chosen-model")
        self.assertIn('model_reasoning_effort="high"', command)
        self.assertNotIn("--last", command)

    def test_reasoning_alias_is_quoted_and_unset_settings_leave_codex_configuration(self):
        code, execute, _ = self.launch("Rome", "--reasoning", "custom-level")
        self.assertEqual(0, code)
        self.assertIn('model_reasoning_effort="custom-level"', execute.call_args.args[1])
        code, execute, _ = self.launch("Rome")
        self.assertEqual(0, code)
        command = execute.call_args.args[1]
        self.assertNotIn("--model", command)
        self.assertFalse(any("model_reasoning_effort" in argument for argument in command))

    def test_invalid_model_or_reasoning_is_rejected_before_credentials_or_connections(self):
        for flag in ("--model", "--reasoning-effort"):
            for value in ("", " ", "high\n", "x" * 129):
                with self.subTest(flag=flag, value=value), contextlib.redirect_stderr(io.StringIO()):
                    with self.assertRaises(SystemExit) as failure:
                        agent.parser().parse_args(["Rome", flag, value])
                    self.assertEqual(2, failure.exception.code)

    def test_prompt_and_session_values_cannot_become_codex_options(self):
        code, execute, _ = self.launch("Rome", "--prompt=--help")
        self.assertEqual(code, 0)
        self.assertEqual(execute.call_args.args[1][-2:], ["--", "--help"])
        code, execute, _ = self.launch("Rome", "--resume=--last", "--prompt=--help")
        self.assertEqual(code, 0)
        self.assertEqual(execute.call_args.args[1][-4:], ["resume", "--", "--last", "--help"])

    def test_invalid_unknown_or_missing_credentials_prevent_launch(self):
        code, execute, _ = self.launch("Unknown")
        self.assertEqual(code, 2)
        execute.assert_not_called()
        self.players.write_text('{"secret-token": "DO-NOT-PRINT-THIS"')
        code, execute, output = self.launch()
        self.assertEqual(code, 2)
        self.assertNotIn("DO-NOT-PRINT-THIS", output)
        execute.assert_not_called()
        self.players.unlink()
        code, execute, output = self.launch()
        self.assertEqual(code, 2)
        self.assertIn("launch_host", output)
        execute.assert_not_called()

    def test_insecure_credentials_and_duplicate_tokens_are_rejected(self):
        self.players.chmod(0o644)
        self.assertEqual(self.launch()[0], 2)
        self.players.chmod(0o600)
        config = json.loads(self.players.read_text())
        config["players"][1]["token"] = config["players"][0]["token"]
        self.players.write_text(json.dumps(config))
        code, execute, _ = self.launch()
        self.assertEqual(code, 2)
        execute.assert_not_called()

    def test_unavailable_host_fails_before_launching_codex(self):
        code, execute, output = self.launch(connected=False)
        self.assertEqual(code, 2)
        self.assertIn("MCP-READY", output)
        execute.assert_not_called()

    def test_unusual_civilization_cannot_escape_workspace_directory(self):
        self.assertNotIn("/", agent.workspace_name("../../elsewhere"))
        self.assertNotEqual(agent.workspace_name("."), ".")
        self.assertNotEqual(agent.workspace_name(".."), "..")
        self.assertEqual(agent.workspace_name("Rome"), "Rome")
        self.assertEqual(agent.workspace_name("Greece"), "Greece")
        self.assertNotEqual(agent.workspace_name("City/State"), agent.workspace_name("City%2FState"))
        self.assertNotEqual(agent.workspace_name("New Rome"), agent.workspace_name("New_Rome"))

    def test_symlinked_player_workspace_prevents_launch(self):
        outside = self.root / "outside"
        outside.mkdir(mode=0o700)
        (self.root / "agents").mkdir(mode=0o700)
        (self.root / "agents/Rome").symlink_to(outside, target_is_directory=True)
        code, execute, _ = self.launch()
        self.assertEqual(code, 2)
        execute.assert_not_called()
        self.assertEqual(list(outside.iterdir()), [])

    def test_https_remote_override_does_not_need_shared_host_state(self):
        (self.root / "endpoints.json").unlink()
        code, execute, _ = self.launch("Rome", "--url", "https://game.example.com:443/mcp", "--ca-file", str(self.ca))
        self.assertEqual(code, 0)
        self.assertIn('url="https://game.example.com:443/mcp"', execute.call_args.args[1][4])
        self.assertFalse((self.root / "endpoints.json").exists())

    def test_plaintext_and_ambiguous_urls_never_launch_or_expose_tokens(self):
        for url in ("http://localhost:8765", "https://user:secret@localhost", "https://localhost/?token=secret", "https://localhost/#token"):
            code, execute, output = self.launch("Rome", "--url", url)
            self.assertEqual(code, 2)
            execute.assert_not_called()
            self.assertNotIn("A" * 43, output)

    def test_dry_run_does_not_verify_connections_or_create_trust_bundle(self):
        with patch.object(agent, "verify_https_connection") as verify, patch.object(agent, "codex_trust_environment") as trust:
            with patch.object(agent.shutil, "which", return_value="/tools/codex"), contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(0, agent.main(["Rome", "--state-dir", str(self.root), "--dry-run"]))
        verify.assert_not_called()
        trust.assert_not_called()
        self.assertFalse((self.root / "agents").exists())


if __name__ == "__main__":
    unittest.main()
