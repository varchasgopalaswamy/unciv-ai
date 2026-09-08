import copy
import json
import unittest

import test_agent_service as service_tests
from agent_results import ResultError, validate_result
from agent_service import ServiceError


def result_payload(civilization="Rome", epoch="epoch-one"):
    return {"civilization": civilization, "epoch": epoch,
            "result": {"outcome": "Victory" if civilization == "Rome" else "Defeat", "winningCivilizationId": "Rome",
                       "winningCivilizationName": "Rome", "victoryType": "Domination", "victoryTurn": 4,
                       "title": "Rome has won a Domination Victory!", "paragraphs": ["The game has ended."]},
            "messages": [],
            "combatReports": [{"id": civilization + "-event", "turn": 4, "kind": "CityCapture",
                               "text": civilization + " permitted report", "locations": [{"x": -1, "y": 0}]}]}


class FinalResultServiceTest(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = service_tests.AgentServiceTests.asyncSetUp
    asyncTearDown = service_tests.AgentServiceTests.asyncTearDown
    make_service = service_tests.AgentServiceTests.make_service
    payload = service_tests.AgentServiceTests.payload
    terminal = service_tests.AgentServiceTests.terminal

    async def test_result_can_reach_a_player_before_its_first_turn_without_starting_codex(self):
        payload = result_payload()
        response = await self.service.result(payload)
        self.assertEqual({"civilization": "Rome", "epoch": "epoch-one", "received": True}, response)
        self.assertEqual(payload, json.loads((self.root / "workspace/result.json").read_text()))
        self.assertIn("Rome: Victory", (self.root / "workspace/result.md").read_text())
        self.assertIn("Rome permitted report", (self.root / "workspace/result.md").read_text())
        self.assertEqual([], self.factory.instances)
        with self.assertRaisesRegex(ServiceError, "GAME_FINISHED"):
            await self.service.start(self.payload())

    async def test_result_replay_restores_workspace_copies_from_private_state_after_restart(self):
        payload = result_payload()
        first = await self.service.result(payload)
        self.assertEqual(first, await self.service.result(copy.deepcopy(payload)))
        (self.root / "workspace/result.json").write_text('{"fake":true}')
        (self.root / "workspace/result.md").write_text("An invented outcome")
        await self.service.close()
        self.service = self.make_service()
        self.assertEqual(payload, json.loads((self.root / "workspace/result.json").read_text()))
        self.assertNotIn("invented", (self.root / "workspace/result.md").read_text())
        self.assertEqual(first, await self.service.result(payload))
        conflicting = copy.deepcopy(payload)
        conflicting["result"]["outcome"] = "Defeat"
        with self.assertRaisesRegex(ServiceError, "RESULT_CONFLICT"):
            await self.service.result(conflicting)
        self.assertEqual(payload, json.loads((self.root / "service/result.json").read_text()))
        self.assertEqual([], self.factory.instances)

    async def test_results_reject_wrong_player_epoch_and_active_model(self):
        with self.assertRaisesRegex(ServiceError, "INVALID_BINDING"):
            await self.service.result(result_payload("Greece"))
        self.assertIsNone(self.service.match)
        self.factory.complete = False
        await self.service.start(self.payload())
        with self.assertRaisesRegex(ServiceError, "AGENT_BUSY"):
            await self.service.result(result_payload())
        with self.assertRaisesRegex(ServiceError, "WORKSPACE_EPOCH_CONFLICT"):
            await self.service.result(result_payload(epoch="other-epoch"))
        await self.service.interrupt("activation-one")
        self.assertTrue((await self.service.result(result_payload()))["received"])

    async def test_terminal_activation_archives_without_handoff_and_cannot_be_aborted_or_replayed(self):
        await self.service.start(self.payload())
        await self.terminal()
        payload = {"activationId": "activation-one", "epoch": "epoch-one", "gameTurn": 0, "handoff": 0}
        finalized = await self.service.finalize_terminal(payload)
        self.assertTrue(finalized["terminalOutcome"])
        self.assertNotIn("committedHandoff", finalized)
        self.assertEqual(finalized, await self.service.finalize_terminal(payload))
        turn = json.loads((self.root / "workspace/turn_1/turn.json").read_text())
        self.assertTrue(turn["attempts"][0]["terminalOutcome"])
        with self.assertRaises(ServiceError):
            await self.service.abort(payload)
        with self.assertRaisesRegex(ServiceError, "GAME_FINISHED"):
            await self.service.start(self.payload("activation-two"))
        with self.assertRaises(ServiceError):
            await self.service.finalize({"activationId": "activation-one", "epoch": "epoch-one", "gameTurn": 0,
                                         "committedHandoff": 1})
        await self.service.close()
        self.service = self.make_service()
        self.assertTrue(self.service.status("activation-one")["terminalOutcome"])
        self.assertEqual(1, len(self.factory.instances))

    async def test_terminal_finalization_requires_exact_activation_binding(self):
        await self.service.start(self.payload())
        await self.terminal()
        payload = {"activationId": "activation-one", "epoch": "epoch-one", "gameTurn": 0, "handoff": 0}
        for altered in ({**payload, "handoff": 1}, {**payload, "gameTurn": 1}, {**payload, "epoch": "different"}):
            with self.assertRaises(ServiceError):
                await self.service.finalize_terminal(altered)
        self.assertNotIn("terminalOutcome", self.service.status("activation-one"))
        await self.service.finalize({"activationId": "activation-one", "epoch": "epoch-one", "gameTurn": 0,
                                     "committedHandoff": 1})
        with self.assertRaisesRegex(ServiceError, "FINALIZATION_CONFLICT"):
            await self.service.finalize_terminal(payload)

    async def test_schema_rejects_hidden_fields_and_invalid_report_shapes_before_writing(self):
        invalid = [None, {}, {**result_payload(), "privateGameState": {}}]
        for mutate in (
                lambda p: p["result"].update(outcome="Paused"),
                lambda p: p["result"].update(victoryTurn=True),
                lambda p: p["combatReports"][0].update(hiddenUnitHealth=50),
                lambda p: p["combatReports"][0].update(locations=[{"x": float("nan"), "y": 0}]),
                lambda p: p["combatReports"].append(p["combatReports"][0].copy()),
        ):
            payload = result_payload()
            mutate(payload)
            invalid.append(payload)
        for payload in invalid:
            with self.subTest(payload=payload):
                with self.assertRaises(ResultError):
                    validate_result(payload)
                with self.assertRaises(ServiceError):
                    await self.service.result(payload)
        self.assertFalse((self.root / "workspace/result.json").exists())
        self.assertIsNone(self.service.match)

    async def test_full_kotlin_report_schema_preserves_interception_roles_and_nullable_fields(self):
        payload = result_payload()
        report = payload["combatReports"][0]
        report.update(dict.fromkeys(("attacker", "defender", "outcome", "damageReceived", "retaliationDamage",
                                     "attackerDefeatedByDefender", "showZeroDamage", "ourUnit", "enemyUnit", "ourDamage",
                                     "enemyDamage", "ourDestroyed", "enemyDestroyed", "isAttacking", "weaponType",
                                     "attackingCivilizationName", "improvementName", "unitType", "hitOurTerritory")))
        validate_result(payload)
        report.update(kind="Interception", ourUnit={"kind": "MapUnit", "name": "Fighter", "instanceName": None, "isOwn": True},
                      enemyUnit={"kind": "MapUnit", "name": "Bomber", "instanceName": None, "isOwn": False},
                      ourDamage=0, enemyDamage=35, ourDestroyed=False, enemyDestroyed=False, isAttacking=False)
        await self.service.result(payload)
        self.assertEqual(payload, json.loads((self.root / "workspace/result.json").read_text()))

    async def test_final_messages_preserve_all_popup_content_in_json_and_readable_result(self):
        payload = result_payload()
        payload["messages"] = [{"title": "Alexander", "paragraphs": ["The last words of the defeated leader.", "A second paragraph."],
                                "quote": "A final quotation.", "acknowledgement": "Farewell.",
                                "additionalAcknowledgements": ["Until we meet again."]}]
        await self.service.result(payload)
        self.assertEqual(payload, json.loads((self.root / "workspace/result.json").read_text()))
        readable = (self.root / "workspace/result.md").read_text()
        for value in ("Alexander", "The last words of the defeated leader.", "A second paragraph.",
                      "A final quotation.", "Farewell.", "Until we meet again."):
            self.assertIn(value, readable)
