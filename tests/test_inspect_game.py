"""Inspection uses copies of temporary checkpoints; no desktop or live game is opened."""

import contextlib
import hashlib
import io
import json
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from civ_agent import cli
from civ_agent.commands import inspect as inspection
from civ_agent.protocol import JsonObject


class InspectGameTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="inspection tests ")
        self.addCleanup(temporary.cleanup)
        self.state = Path(temporary.name)
        original_umask = os.umask(0o077)
        os.umask(original_umask)
        self.addCleanup(os.umask, original_umask)
        self.checkpoint = self.state / "exploration.json"
        self.game: JsonObject = {
            "currentPlayer": "Greece",
            "turns": 7,
            "customSaveLocation": str(self.checkpoint),
            "civilizations": [
                {"civID": "Rome", "playerType": "Human", "cities": [{"name": "Rome", "population": 2}]},
                {"civID": "Greece", "playerType": "Human", "tech": {"techsToResearch": ["Pottery"]}},
                {"civID": "Barbarians"},
            ],
            "tileMap": {"tiles": [{"position": {"x": 2, "y": 3}, "units": [{"id": 12}]}]},
        }
        self.save_checkpoint()

    def save_checkpoint(self):
        self.checkpoint.write_text(
            json.dumps(
                {
                    "formatVersion": 6,
                    "epoch": "private-epoch",
                    "paused": False,
                    "gameJson": json.dumps(self.game),
                    "players": {"Rome": {"receipts": ["private-receipt"]}},
                }
            )
        )

    def launch(self, *arguments):
        output = io.StringIO()
        with (
            patch.object(inspection.os, "execv") as execute,
            patch.object(inspection.os, "chdir") as change_directory,
            contextlib.redirect_stdout(output),
            contextlib.redirect_stderr(output),
        ):
            code = cli.main(["host", "inspect", "--state-dir", str(self.state), *arguments])
        return code, execute, change_directory, output.getvalue()

    def exports(self):
        return list((self.state / "inspection").glob("snapshot-*/SaveFiles"))

    def test_snapshot_preserves_game_state_and_exports_player_views_without_host_metadata(self):
        original = self.checkpoint.read_bytes()
        code, execute, _, output = self.launch("--export-only")
        self.assertEqual(code, 0)
        execute.assert_not_called()
        (saves,) = self.exports()
        expected = dict(self.game)
        expected.pop("customSaveLocation")
        snapshot = json.loads((saves / "Agent snapshot.json").read_text())
        self.assertEqual(snapshot, expected)
        for name in ("Rome", "Greece"):
            view = json.loads((saves / f"Inspect {name}.json").read_text())
            self.assertEqual(view, dict(expected, currentPlayer=name))
        self.assertEqual(len(list(saves.iterdir())), 3)
        self.assertEqual(original, self.checkpoint.read_bytes())
        self.assertFalse((self.state / "exploration.json.lock").exists())
        self.assertIn("actual active civilization: Greece", output)
        self.assertNotIn("private-epoch", output + json.dumps(snapshot))
        self.assertNotIn("private-receipt", output + json.dumps(snapshot))

    def test_launcher_passes_selected_save_data_and_engine_paths_as_individual_arguments(self):
        code, execute, change_directory, output = self.launch("rome", "--unciv-dir", "../engine checkout")
        self.assertEqual(code, 0)
        executable, command = execute.call_args.args
        (saves,) = self.exports()
        self.assertEqual(executable, str(Path(__file__).resolve().parents[1] / "gradlew"))
        self.assertIn("runInspectUi", command)
        self.assertIn(f"-PinspectSave={saves / 'Inspect Rome.json'}", command)
        self.assertIn(f"-PinspectDataDir={saves.parent}", command)
        self.assertIn(f"-PuncivDir={(Path(__file__).resolve().parents[1] / '../engine checkout').resolve()}", command)
        change_directory.assert_called_once_with(Path(__file__).resolve().parents[1])
        self.assertIn("Viewing as Rome", output)

    def test_default_launch_opens_the_saved_active_player(self):
        code, execute, _, _ = self.launch()
        self.assertEqual(code, 0)
        (saves,) = self.exports()
        self.assertIn(f"-PinspectSave={saves / 'Agent snapshot.json'}", execute.call_args.args[1])
        self.assertEqual(json.loads((saves / "Agent snapshot.json").read_text())["currentPlayer"], "Greece")

    def test_exports_are_private_and_each_run_preserves_earlier_inspection_changes(self):
        self.launch("--export-only")
        (first,) = self.exports()
        edited = first / "Inspect Rome.json"
        edited.write_text("user's inspection edits")
        self.launch("--export-only")
        self.assertEqual(len(self.exports()), 2)
        self.assertEqual(edited.read_text(), "user's inspection edits")
        for saves in self.exports():
            self.assertEqual(stat.S_IMODE(saves.parent.stat().st_mode), 0o700)
            for save in saves.iterdir():
                self.assertEqual(stat.S_IMODE(save.stat().st_mode), 0o600)

    def test_custom_checkpoint_and_output_directory(self):
        custom = self.state / "other checkpoint.json"
        self.checkpoint.rename(custom)
        output_root = self.state / "other exports"
        code, execute, _, _ = self.launch(
            "--checkpoint", str(custom), "--output-dir", str(output_root), "--export-only"
        )
        self.assertEqual(code, 0)
        execute.assert_not_called()
        self.assertEqual(len(list(output_root.glob("snapshot-*/SaveFiles/Agent snapshot.json"))), 1)
        self.assertFalse((self.state / "inspection").exists())

    def test_invalid_checkpoint_fails_before_export_or_launch_without_echoing_content(self):
        bad_inputs = [
            '{"DO-NOT-PRINT":',
            json.dumps({"formatVersion": 1, "gameJson": "{}"}),
            json.dumps({"formatVersion": 2, "gameJson": "{}"}),
            json.dumps({"formatVersion": 6, "gameJson": "DO-NOT-PRINT"}),
            json.dumps({"formatVersion": 6, "gameJson": "{}"}),
        ]
        for content in bad_inputs:
            with self.subTest(content=content):
                self.checkpoint.write_text(content)
                code, execute, _, output = self.launch()
                self.assertEqual(code, 2)
                self.assertNotIn("DO-NOT-PRINT", output)
                self.assertFalse((self.state / "inspection").exists())
                execute.assert_not_called()
        self.checkpoint.unlink()
        code, execute, _, output = self.launch()
        self.assertEqual(code, 2)
        self.assertIn("No checkpoint found", output)
        execute.assert_not_called()

    def test_unknown_ai_and_ambiguous_civilizations_fail_before_writing(self):
        for name in ("Unknown", "Barbarians"):
            code, execute, _, _ = self.launch(name)
            self.assertEqual(code, 2)
            execute.assert_not_called()
        self.game["civilizations"].append({"civID": "ROME", "playerType": "Human"})
        self.save_checkpoint()
        code, execute, _, _ = self.launch("rome")
        self.assertEqual(code, 2)
        self.assertFalse((self.state / "inspection").exists())
        execute.assert_not_called()

    def test_unusual_civilization_names_stay_inside_the_export_directory(self):
        self.game["civilizations"][0]["civID"] = "../../outside"
        self.save_checkpoint()
        code, _, _, _ = self.launch("../../outside", "--export-only")
        self.assertEqual(code, 0)
        (saves,) = self.exports()
        views = [path for path in saves.iterdir() if json.loads(path.read_text())["currentPlayer"] == "../../outside"]
        self.assertEqual(len(views), 1)
        self.assertEqual(views[0].parent, saves)
        self.assertFalse((self.state / "outside.json").exists())

    def test_environment_default_and_omitted_zero_turn(self):
        with patch.dict(os.environ, {"UNCIV_HOST_DIR": str(self.state)}):
            self.assertEqual(inspection.parser().parse_args([]).state_dir, self.state)
        self.game.pop("turns")
        self.save_checkpoint()
        code, _, _, output = self.launch("--export-only")
        self.assertEqual(code, 0)
        self.assertIn("Captured turn 0", output)

    def test_online_saves_cannot_start_a_desktop_that_might_upload_turns(self):
        self.game["gameParameters"] = {"isOnlineMultiplayer": True}
        self.save_checkpoint()
        code, execute, _, output = self.launch()
        self.assertEqual(code, 2)
        self.assertIn("Online multiplayer", output)
        self.assertFalse((self.state / "inspection").exists())
        execute.assert_not_called()

    def test_readable_and_hashed_names_cannot_overwrite_each_others_views(self):
        unusual = "../outside"
        digest = hashlib.sha256(unusual.encode()).hexdigest()
        self.game["currentPlayer"] = digest
        self.game["civilizations"] = [
            {"civID": unusual, "playerType": "Human"},
            {"civID": digest, "playerType": "Human"},
        ]
        self.save_checkpoint()
        code, _, _, _ = self.launch("--export-only")
        self.assertEqual(code, 0)
        (saves,) = self.exports()
        self.assertEqual(len(list(saves.iterdir())), 3)
        for name in (unusual, digest):
            view = json.loads((saves / inspection.view_filename(name)).read_text())
            self.assertEqual(view["currentPlayer"], name)
