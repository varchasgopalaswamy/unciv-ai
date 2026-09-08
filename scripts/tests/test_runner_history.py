"""Readable turn archives preserve restart identity and player boundaries."""

import contextlib
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from runner_history import EventHistory, HistoryError


class ReadableHistoryTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / "history"

    def history(self, civilization="Rome", activation="1234-abcd", game_turn=0, epoch="match-one", secrets=()):
        return EventHistory(self.root, epoch, civilization, activation, secrets, game_turn=game_turn)

    def test_player_and_game_turn_directories_are_readable_and_attempts_are_distinct(self):
        first = self.history()
        retry = self.history(activation="retry-5678")
        following = self.history(game_turn=1)
        other = self.history(civilization="Greece")
        self.assertEqual(first.directory, self.root / "Rome/turn_1/attempt_1234-abcd")
        self.assertEqual(retry.turn_directory, first.turn_directory)
        self.assertNotEqual(retry.directory, first.directory)
        self.assertEqual(following.turn_directory, self.root / "Rome/turn_2")
        self.assertEqual(other.turn_directory, self.root / "Greece/turn_1")
        self.assertIn("Rome — turn 1", first.markdown.read_text())

    def test_readable_history_resumes_without_duplicate_events_or_console_output(self):
        first = self.history()
        event = dict(sequence=0, kind="assistant_message", data=dict(itemId="message", text="Settled Rome."))
        with contextlib.redirect_stdout(io.StringIO()):
            first.append([event])
        capture = io.StringIO()
        with contextlib.redirect_stdout(capture):
            restored = self.history()
            restored.append([event])
        self.assertEqual(restored.cursor, 1)
        self.assertEqual(capture.getvalue(), "")
        self.assertEqual(restored.markdown.read_text().count("Settled Rome."), 1)

    def test_unusual_names_cannot_escape_or_collide_with_encoded_names(self):
        unusual = self.history(civilization="../Rome", activation="../../attempt")
        literal = self.history(civilization="%2E%2E%2FRome", activation="../../attempt")
        self.assertTrue(unusual.directory.is_relative_to(self.root))
        self.assertEqual(len(unusual.directory.relative_to(self.root).parts), 3)
        self.assertNotEqual(unusual.directory, literal.directory)
        self.assertNotIn("..", unusual.directory.relative_to(self.root).parts)

    def test_match_or_source_identity_cannot_be_overwritten_by_restart(self):
        first = self.history()
        original = (first.directory / "source.json").read_bytes()
        with self.assertRaises(HistoryError):
            self.history(epoch="different-match")
        self.assertEqual((first.directory / "source.json").read_bytes(), original)
        (first.directory / "source.json").write_text(json.dumps(dict(first.source, activationId="different-activation")))
        with self.assertRaises(HistoryError):
            self.history()

    def test_invalid_turn_numbers_do_not_create_history(self):
        for value in (-1, True, 0.5, "0"):
            with self.subTest(value=value), self.assertRaises(HistoryError):
                self.history(game_turn=value)
        self.assertFalse(self.root.exists())

    def test_finalized_archive_mirrors_on_resume_and_redacts_secrets(self):
        secret = "player-token"
        history = self.history(secrets=(secret,))
        archive = dict(civilization="Rome", gameTurn=0,
                       commands=json.dumps(dict(kind="tool_result", data=dict(receipt="accepted", token=secret, raw_reasoning="hidden"))) + "\n",
                       summary="Settled Rome to gain access to fresh water. " + secret)
        history.publish_archive(archive)
        restored = self.history(secrets=(secret,))
        restored.publish_archive(archive)
        self.assertEqual(len((restored.turn_directory / "commands.jsonl").read_text().splitlines()), 1)
        text = (restored.turn_directory / "commands.jsonl").read_text() + (restored.turn_directory / "summary.md").read_text()
        self.assertIn("Settled Rome", text)
        self.assertIn("accepted", text)
        self.assertNotIn(secret, text)
        self.assertNotIn("hidden", text)
        self.assertEqual((restored.turn_directory / "summary.md").stat().st_mode & 0o777, 0o600)

    def test_wrong_player_turn_or_corrupt_archive_leaves_existing_mirror_untouched(self):
        history = self.history()
        valid = dict(civilization="Rome", gameTurn=0, commands='{"receipt":"accepted"}\n', summary="Turn completed.\n")
        history.publish_archive(valid)
        for changes in (dict(civilization="Greece"), dict(gameTurn=1), dict(gameTurn=False),
                        dict(commands="[1, 2]\n"), dict(commands='{"incomplete":'), dict(summary=None)):
            with self.subTest(changes=changes), self.assertRaises(HistoryError):
                history.publish_archive(dict(valid, **changes))
        self.assertEqual((history.turn_directory / "commands.jsonl").read_text(), valid["commands"])
        self.assertEqual((history.turn_directory / "summary.md").read_text(), valid["summary"])

    def test_readable_directory_symlinks_are_rejected_before_writing_through_them(self):
        outside = Path(self.temporary.name) / "outside"
        outside.mkdir(mode=0o700)
        for index, components in enumerate(((), ("Rome",), ("Rome", "turn_1"),
                                             ("Rome", "turn_1", "attempt_1234-abcd"))):
            with self.subTest(components=components):
                root = Path(self.temporary.name) / f"case-{index}"
                target = root
                for component in components:
                    target.mkdir(mode=0o700)
                    target /= component
                target.symlink_to(outside, target_is_directory=True)
                with self.assertRaises(HistoryError):
                    EventHistory(root, "match-one", "Rome", "1234-abcd", game_turn=0)
        self.assertEqual(list(outside.iterdir()), [])

    def test_source_and_artifact_symlinks_cannot_read_or_overwrite_other_files(self):
        history = self.history()
        outside = Path(self.temporary.name) / "outside.json"
        outside.write_text("PRIVATE CONTENT")
        outside.chmod(0o600)
        source = history.directory / "source.json"
        saved_source = source.read_text()
        source.unlink()
        source.symlink_to(outside)
        with self.assertRaises(HistoryError):
            self.history()
        source.unlink()
        source.write_text(saved_source)
        source.chmod(0o600)
        history.jsonl.symlink_to(outside)
        with self.assertRaises(HistoryError):
            history.append([dict(sequence=0, kind="assistant_message", data=dict(text="new message"))])
        (history.turn_directory / "summary.md").symlink_to(outside)
        with self.assertRaises(HistoryError):
            history.publish_archive(dict(civilization="Rome", gameTurn=0, commands="", summary="new summary"))
        self.assertEqual(outside.read_text(), "PRIVATE CONTENT")


if __name__ == "__main__":
    unittest.main()
