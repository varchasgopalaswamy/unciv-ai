"""Turn memory comes from service events and committed handoffs, never player files."""

import json
import tempfile
import unittest
from pathlib import Path
from urllib.parse import unquote

from civ_agent.protocol import JsonObject
from civ_agent.tls import TLSConfigError
from civ_agent.turn_archive import TurnArchive, TurnArchiveError, civilization_directory_name


class TurnArchiveTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="turn-archive-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.trusted, self.workspace = self.root / "service" / "archive", self.root / "agents" / "Rome"
        self.archive = TurnArchive(
            self.trusted, self.workspace, "Rome", "epoch", secrets=("service-credential", "player-credential")
        )

    def event(self, kind, **data) -> JsonObject:
        return {"time": "2026-09-08T12:00:00Z", "kind": kind, "data": data}

    def events(self, *events):
        return [{"sequence": index, **event} for index, event in enumerate(events)]

    def command(self, identifier="command", request_id="turn-1-end", game_turn=0, kind="END_TURN", **arguments):
        request = {"requestId": request_id, "epoch": "epoch", "revision": "before", "kind": kind, **arguments}
        receipt = {
            "requestId": request_id,
            "epoch": "epoch",
            "revision": "after",
            "kind": kind,
            "turn": game_turn,
            "isYourTurn": kind != "END_TURN",
        }
        return [
            self.event(
                "tool_call",
                itemId=identifier,
                type="mcpToolCall",
                server="civ",
                tool="player_command",
                arguments=request,
            ),
            self.event(
                "tool_result",
                itemId=identifier,
                type="mcpToolCall",
                status="completed",
                result={"structuredContent": receipt, "content": [{"type": "text", "text": json.dumps(receipt)}]},
            ),
        ]

    def report(self, text="Ended the turn to let the settler finish training."):
        return self.event("assistant_message", itemId="final", phase="final_answer", text=text)

    def debug_summary(self, game_turn=0, attempt=1):
        return (self.workspace / f"turn_{game_turn}/attempts/attempt_{attempt}/summary.md").read_text()

    def test_civilization_names_are_readable_reversible_and_collision_free(self):
        names = ["Rome", "Greece", "../Rome", "a/b", "a\\b", "a%2Fb", ".", "..", "Éire", "Éire", "中国", "New World"]
        encoded = [civilization_directory_name(name) for name in names]
        self.assertEqual(["Rome", "Greece"], encoded[:2])
        self.assertEqual(names, [unquote(name) for name in encoded])
        self.assertEqual(len(encoded), len(set(encoded)))
        for name in encoded:
            self.assertNotIn("/", name)
            self.assertNotIn("\\", name)
            self.assertNotIn(name, (".", ".."))
        for name in ("", None, "x" * 256, "\ud800"):
            with self.assertRaises(TurnArchiveError):
                civilization_directory_name(name)

    def test_manifest_binds_memory_to_one_civilization_and_epoch(self):
        TurnArchive(self.trusted, self.workspace, "Rome", "epoch")
        for civilization, epoch in (("Greece", "epoch"), ("Rome", "other-epoch")):
            with self.assertRaises(TurnArchiveError):
                TurnArchive(self.trusted, self.workspace, civilization, epoch)
        with self.assertRaises(TurnArchiveError):
            TurnArchive(self.workspace / "private", self.workspace, "Rome", "epoch")

    def test_artifacts_separate_evidence_from_claims_and_keep_full_commands(self):
        events = self.events(*self.command(), self.report("I explored the entire continent and founded six cities."))
        artifacts = self.archive.update(0, "private-activation-id", events, phase="COMPLETED", handoff=1)
        self.assertEqual(0, artifacts["turn"])
        self.assertEqual(artifacts["gameTurn"], artifacts["turn"])
        self.assertEqual("# Turn 0\n\nI explored the entire continent and founded six cities.\n", artifacts["summary"])
        debug = self.debug_summary()
        self.assertIn("Host handoff: confirmed.", debug)
        self.assertIn("host confirmed success: END_TURN ×1", debug)
        self.assertIn("self-reported", debug)
        self.assertNotIn("host confirmed success: FOUND_CITY", debug)
        self.assertNotIn("private-activation-id", json.dumps(artifacts))
        rows = [json.loads(line) for line in artifacts["commands"].splitlines()]
        self.assertEqual(2, len(rows))
        self.assertEqual("turn-1-end", rows[0]["data"]["arguments"]["requestId"])
        self.assertEqual(events[1]["data"], rows[1]["data"])
        for name in ("commands.jsonl", "summary.md", "turn.json"):
            if name != "turn.json":
                self.assertEqual(
                    (self.trusted / "turn_0" / name).read_bytes(), (self.workspace / "turn_0" / name).read_bytes()
                )
            self.assertEqual(0o600, (self.workspace / "turn_0" / name).stat().st_mode & 0o777)
        player_metadata = json.loads((self.workspace / "turn_0/turn.json").read_text())
        self.assertNotIn("handoff", player_metadata["attempts"][0])
        self.assertTrue(player_metadata["attempts"][0]["committed"])

    def test_completed_model_response_without_handoff_is_not_completed_turn(self):
        artifacts = self.archive.update(0, "attempt", self.events(self.report("Turn finished.")), phase="COMPLETED")
        self.assertEqual("# Turn 0\n\nTurn finished.\n", artifacts["summary"])
        with self.assertRaisesRegex(TurnArchiveError, "no confirmed host handoff"):
            self.archive.context(1)
        self.assertEqual("", self.archive.context(0))
        self.archive.update(0, "attempt", self.events(self.report("Turn finished.")), phase="COMPLETED", handoff=1)
        self.assertIn("Turn finished.", self.archive.context(1))

    def test_terminal_outcome_preserves_committed_commands_without_fake_handoff(self):
        events = self.events(
            *self.command(kind="RESOLVE_CAPTURED_CITY", target="city-alert", selection="Annex"),
            self.report("Captured the last capital."),
        )
        artifacts = self.archive.update(0, "final", events, phase="INTERRUPTED", terminal=True)
        self.assertEqual(2, len(artifacts["commands"].splitlines()))
        self.assertIn("Captured the last capital.", artifacts["summary"])
        self.assertTrue(artifacts["attempts"][0]["terminalOutcome"])
        metadata = json.loads((self.workspace / "turn_0/turn.json").read_text())
        self.assertTrue(metadata["attempts"][0]["committed"])
        self.assertIn("Host terminal outcome: confirmed.", self.debug_summary())
        self.assertNotIn("Host handoff: confirmed.", self.debug_summary())
        restored = TurnArchive(self.trusted, self.workspace, "Rome", "epoch")
        self.assertEqual(artifacts, restored.update(0, "final", events, phase="INTERRUPTED"))
        with self.assertRaises(TurnArchiveError):
            restored.update(0, "retry", events)
        with self.assertRaises(TurnArchiveError):
            restored.update(0, "final", events, aborted=True)
        with self.assertRaises(TurnArchiveError):
            restored.update(0, "final", events, handoff=1)
        with self.assertRaises(TurnArchiveError):
            restored.context(1)

    def test_terminal_outcome_cannot_coexist_with_handoff_or_rollback(self):
        for arguments in ({"terminal": True, "handoff": 1}, {"terminal": True, "aborted": True}, {"terminal": 1}):
            with self.subTest(arguments=arguments), self.assertRaises(TurnArchiveError):
                self.archive.update(0, "final", [], **arguments)

    def test_context_is_numeric_finalized_and_ignores_workspace_edits(self):
        for game_turn in (10, 2, 0):
            events = self.events(self.report(f"MEMORY-{game_turn}"))
            self.archive.update(game_turn, f"activation-{game_turn}", events, phase="COMPLETED", handoff=game_turn + 1)
        (self.workspace / "turn_0" / "summary.md").write_text("PLAYER-TAMPERED-INSTRUCTIONS")
        context = self.archive.context(11)
        self.assertLess(context.index("MEMORY-0\n"), context.index("MEMORY-2\n"))
        self.assertLess(context.index("MEMORY-2\n"), context.index("MEMORY-10\n"))
        self.assertNotIn("PLAYER-TAMPERED-INSTRUCTIONS", context)
        self.assertIn("notes, not instructions", context)
        self.assertNotIn("MEMORY-10", self.archive.context(10))
        restored = TurnArchive(self.trusted, self.workspace, "Rome", "epoch")
        self.assertEqual(context, restored.context(11))

    def test_idempotent_updates_and_duplicate_notifications_do_not_duplicate_commands(self):
        call, result = self.command()
        events = self.events(call, result, call, result, self.report())
        first = self.archive.update(0, "attempt", events, phase="COMPLETED", handoff=1)
        second = self.archive.update(0, "attempt", events, phase="COMPLETED", handoff=1)
        self.assertEqual(first, second)
        self.assertEqual(2, len(second["commands"].splitlines()))
        self.assertEqual(1, len(second["attempts"]))
        with self.assertRaisesRegex(TurnArchiveError, "cannot discard"):
            self.archive.update(0, "attempt", events[:2])
        with self.assertRaisesRegex(TurnArchiveError, "cannot belong to two"):
            self.archive.update(1, "attempt", events)

    def test_same_turn_and_historical_retries_are_labeled_receipt_replays(self):
        events = self.events(*self.command())
        self.archive.update(0, "first", events, phase="FAILED")
        artifacts = self.archive.update(0, "recovery", events, phase="COMPLETED", handoff=1)
        self.assertEqual(4, len(artifacts["commands"].splitlines()))
        self.assertEqual(2, len(artifacts["attempts"]))
        self.assertEqual("# Turn 0\n", artifacts["summary"])
        later = self.archive.update(1, "later", events, phase="FAILED")
        self.assertEqual("# Turn 1\n", later["summary"])
        self.assertNotIn("host confirmed success: END_TURN", self.debug_summary(1))
        self.assertIn("replayed successful receipt", self.debug_summary(1))

    def test_failed_missing_and_mismatched_receipts_are_not_claimed_successful(self):
        call, result = self.command()
        result["data"]["result"] = {"isError": True, "content": [{"type": "text", "text": "STALE_REVISION"}]}
        unknown, mismatched = self.command(identifier="unknown", request_id="unknown")
        mismatched["data"]["result"]["structuredContent"]["requestId"] = "wrong-id"
        # The structured and textual representations must both be from the wrong request.
        mismatched["data"]["result"]["content"] = []
        unresolved, _unused = self.command(identifier="unresolved", request_id="unresolved")
        artifacts = self.archive.update(
            0, "attempt", self.events(call, result, unknown, mismatched, unresolved), phase="FAILED"
        )
        self.assertEqual("# Turn 0\n", artifacts["summary"])
        debug = self.debug_summary()
        self.assertNotIn("host confirmed success:", debug)
        self.assertIn("failed: END_TURN ×1", debug)
        self.assertIn("STALE_REVISION", debug)
        self.assertIn("uncertain: no matching successful host receipt: END_TURN ×2", debug)

    def test_large_chunked_results_reassemble_losslessly_and_partial_logs_recover(self):
        data = {
            "itemId": "observation",
            "result": {"content": [{"type": "text", "text": "雪🐈" * 40000}], "tiles": list(range(5000))},
        }
        encoded = json.dumps(data, ensure_ascii=False)
        fragments = [encoded[index : index + 4096] for index in range(0, len(encoded), 4096)]
        events = self.events(
            *(
                self.event(
                    "tool_result",
                    itemId="observation",
                    encoding="json",
                    chunkIndex=index,
                    chunkCount=len(fragments),
                    text=fragment,
                )
                for index, fragment in enumerate(fragments)
            )
        )
        partial = self.archive.update(0, "attempt", events[:2], phase="RUNNING")
        self.assertEqual("# Turn 0\n", partial["summary"])
        self.assertIn("incomplete chunks", self.debug_summary())
        self.assertTrue(all(json.loads(line)["incompleteChunk"] for line in partial["commands"].splitlines()))
        artifacts = self.archive.update(0, "attempt", events, phase="COMPLETED", handoff=1)
        rows = [json.loads(line) for line in artifacts["commands"].splitlines()]
        self.assertEqual([data], [row["data"] for row in rows])
        self.assertNotIn("雪🐈", artifacts["summary"])
        self.assertNotIn("incomplete chunks", artifacts["summary"])

    def test_secrets_and_raw_reasoning_are_excluded_from_every_player_artifact(self):
        secret = "A" * 43
        events = self.events(
            self.event("reasoning_summary", text="RAW-REASONING-MARKER"),
            self.event("reasoning_text", text="RAW-REASONING-MARKER"),
            self.event(
                "tool_call",
                itemId="tool",
                type="mcpToolCall",
                tool="player_turn",
                server="civ",
                arguments={
                    "authorization": "Bearer secret",
                    "extra": "service-credential",
                    "nested": {"apiKey": "a-key"},
                },
            ),
            self.event(
                "tool_result",
                itemId="tool",
                result={
                    "message": "player-credential " + secret,
                    "encrypted_content": "PRIVATE-CIPHERTEXT",
                    "rawReasoning": "RAW-MARKER",
                },
            ),
            self.report("Public note player-credential\x1b[31m."),
        )
        artifacts = self.archive.update(0, "private-activation", events, phase="COMPLETED", handoff=1)
        serialized = json.dumps(artifacts)
        for forbidden in (
            "service-credential",
            "player-credential",
            secret,
            "a-key",
            "RAW-REASONING-MARKER",
            "PRIVATE-CIPHERTEXT",
            "RAW-MARKER",
            "\\u001b",
            "private-activation",
        ):
            self.assertNotIn(forbidden, serialized)
        self.assertIn("[REDACTED]", serialized)
        self.assertIn("Public note", artifacts["summary"])

    def test_summary_is_bounded_while_full_receipts_are_preserved(self):
        events = []
        for number in range(200):
            events.extend(
                self.command(
                    identifier=f"c{number}",
                    request_id=f"r{number}",
                    kind="MOVE_UNIT",
                    destination={"x": number, "y": 0},
                )
            )
        events.append(self.report("A reason. " * 10000))
        artifacts = self.archive.update(0, "attempt", self.events(*events), phase="COMPLETED", handoff=1)
        self.assertEqual(400, len(artifacts["commands"].splitlines()))
        self.assertNotIn("MOVE_UNIT", artifacts["summary"])
        self.assertIn("MOVE_UNIT ×200", self.debug_summary())
        self.assertLess(len(artifacts["summary"]), 2450)
        self.assertNotIn("Public report excerpt", artifacts["summary"])
        self.assertEqual(200, len(artifacts["summary"].split("\n\n", 1)[1].split()))
        self.assertIn("A reason. " * 10000, (self.workspace / "turn_0/attempts/attempt_1/events.jsonl").read_text())

    def test_rolled_back_attempts_remain_detailed_but_do_not_supply_memory(self):
        events = self.events(
            *self.command(kind="MOVE_UNIT", target="ROLLED-BACK-UNIT"), self.report("ROLLED-BACK-STRATEGY")
        )
        self.archive.update(0, "aborted", events, phase="INTERRUPTED")
        self.archive.update(0, "aborted", events, phase="INTERRUPTED", aborted=True)
        self.archive.update(0, "aborted", events, phase="INTERRUPTED")
        artifacts = self.archive.update(
            0, "fresh", self.events(*self.command(), self.report("CURRENT-STRATEGY")), phase="COMPLETED", handoff=1
        )
        self.assertNotIn("ROLLED-BACK-STRATEGY", artifacts["summary"])
        self.assertNotIn("replayed successful receipt", artifacts["summary"])
        self.assertEqual("# Turn 0\n\nCURRENT-STRATEGY\n", artifacts["summary"])
        self.assertIn("CURRENT-STRATEGY", self.archive.context(1))
        self.assertNotIn("ROLLED-BACK", self.archive.context(1))
        self.assertNotIn("ROLLED-BACK", artifacts["commands"])
        rows = [json.loads(line) for line in artifacts["commands"].splitlines()]
        self.assertEqual([False, False], [row["aborted"] for row in rows])
        self.assertTrue(artifacts["attempts"][0]["aborted"])
        failed = self.workspace / "turn_0/attempts/attempt_1"
        self.assertIn("ROLLED-BACK-STRATEGY", (failed / "summary.md").read_text())
        self.assertIn("effects were discarded", (failed / "summary.md").read_text())
        self.assertIn("ROLLED-BACK-UNIT", (failed / "commands.jsonl").read_text())
        self.assertIn("ROLLED-BACK-STRATEGY", (failed / "events.jsonl").read_text())
        metadata = json.loads((failed / "source.json").read_text())
        self.assertTrue(metadata["aborted"])
        self.assertFalse(metadata["committed"])
        self.assertEqual(
            ("Rome", "epoch", 0, 1),
            (metadata["civilization"], metadata["epoch"], metadata["gameTurn"], metadata["attempt"]),
        )
        restored = TurnArchive(self.trusted, self.workspace, "Rome", "epoch")
        self.assertNotIn("ROLLED-BACK", restored.context(1))
        prior = (failed / "commands.jsonl").read_text()
        self.archive.update(0, "aborted", events, phase="INTERRUPTED", aborted=True)
        self.assertEqual(prior, (failed / "commands.jsonl").read_text())
        with self.assertRaisesRegex(TurnArchiveError, "confirmed handoff"):
            self.archive.update(0, "fresh", self.events(*self.command(), self.report("CURRENT-STRATEGY")), aborted=True)

    def test_aborted_only_turn_has_no_active_commands_or_report(self):
        events = self.events(*self.command(kind="FOUND_CITY", target="FAILED-CITY"), self.report("FAILED-REPORT"))
        artifacts = self.archive.update(0, "failed", events, phase="FAILED", aborted=True)
        self.assertEqual("", artifacts["commands"])
        self.assertNotIn("FAILED-CITY", artifacts["summary"])
        self.assertNotIn("FAILED-REPORT", artifacts["summary"])
        self.assertNotIn("Attempt 1", artifacts["summary"])
        self.assertEqual("# Turn 0\n", artifacts["summary"])
        self.assertIn("FAILED-CITY", (self.workspace / "turn_0/attempts/attempt_1/commands.jsonl").read_text())

    def test_attempt_details_remain_sanitized_and_cannot_redirect_writes(self):
        events = self.events(self.report("Visible player-credential"))
        self.archive.update(0, "failed", events, phase="FAILED", aborted=True)
        attempt_path = self.workspace / "turn_0/attempts/attempt_1"
        for path in attempt_path.iterdir():
            self.assertNotIn("player-credential", path.read_text())
            self.assertEqual(0o600, path.stat().st_mode & 0o777)
        target = self.root / "outside"
        target.mkdir(mode=0o700)
        (self.workspace / "turn_0/attempts/attempt_2").symlink_to(target, target_is_directory=True)
        with self.assertRaises(TurnArchiveError):
            self.archive.update(0, "new", events, phase="COMPLETED", handoff=1)
        self.assertEqual([], list(target.iterdir()))

    def test_symlink_workspace_cannot_redirect_artifact_writes(self):
        target = self.root / "outside"
        target.mkdir(mode=0o700)
        (self.workspace / "turn_0").symlink_to(target, target_is_directory=True)
        with self.assertRaises(TurnArchiveError):
            self.archive.update(0, "attempt", self.events(self.report()), phase="COMPLETED", handoff=1)
        self.assertEqual([], list(target.iterdir()))
        # Trusted artifacts commit even if the player's mutable copy cannot be refreshed.
        self.assertIn("# Turn 0\n\nEnded the turn", self.archive.context(1))

    def test_symlink_files_and_parent_segments_are_rejected(self):
        self.archive.update(0, "attempt", self.events(self.report()), phase="COMPLETED", handoff=1)
        summary = self.trusted / "turn_0" / "summary.md"
        summary.unlink()
        summary.symlink_to(self.workspace / "turn_0" / "summary.md")
        with self.assertRaises((TurnArchiveError, TLSConfigError)):
            self.archive.context(1)
        (self.root / "linked").symlink_to(self.workspace, target_is_directory=True)
        with self.assertRaises(TurnArchiveError):
            TurnArchive(self.root / "new-trusted", self.root / "linked" / "child", "Rome", "epoch")
