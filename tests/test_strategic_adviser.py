"""Background reviews are frozen, read-only, restartable and independent of gameplay."""

import asyncio
import json
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import select

from civ_agent import service_recovery
from civ_agent.civilization_state import StateConflict
from civ_agent.monitoring import read_database
from civ_agent.protocol import JsonObject
from civ_agent.review_runtime import ReviewProgress
from civ_agent.strategic_review import advice_prompt
from civ_agent.telemetry import jobs as accounting_jobs
from tests.test_agent_service import AgentServiceTestCase, FakeCodex, FakeFactory, ServiceError, StartupHost
from tests.test_strategic_review import VICTORIES, assessment
from tests.test_victory_snapshot import position


def test_review_progress_tracks_parallel_tools_retries_and_activity_without_text():
    progress = ReviewProgress()
    progress.observe("item/reasoning/textDelta", {"delta": "RAW_PRIVATE_REASONING"})
    last = progress.value["lastActivityAtMs"]
    for id in ("map", "tech"):
        progress.observe("item/started", {"item": {"id": id, "type": "mcpToolCall", "tool": "research_details"}})
    assert progress.value["stage"] == "Reading" and progress.value["toolCalls"] == 2
    progress.observe("item/completed", {"item": {"id": "map", "type": "mcpToolCall"}})
    assert progress.value["stage"] == "Reading"
    progress.observe("item/completed", {"item": {"id": "tech", "type": "mcpToolCall"}})
    assert progress.value["stage"] == "Thinking"
    last = progress.value["lastActivityAtMs"]
    progress.observe("thread/tokenUsage/updated", {"total": {"inputTokens": 500}})
    progress.observe("service/modelStatus", {"state": "retrying", "message": "Model connection timed out; retrying."})
    assert progress.value["lastActivityAtMs"] == last
    assert progress.value["modelStatus"]["state"] == "retrying"
    progress.observe("item/reasoning/summaryTextDelta", {"delta": "PUBLIC_SUMMARY"})
    assert "modelStatus" not in progress.value
    assert "RAW_PRIVATE_REASONING" not in json.dumps(progress.value)
    assert "PUBLIC_SUMMARY" not in json.dumps(progress.value)


class ReviewHost(StartupHost):
    async def list_tools(self):
        return [
            *await super().list_tools(),
            *[
                {
                    "name": name,
                    "description": name,
                    "inputSchema": {
                        "type": "object",
                        "properties": {"names": {"type": "array", "items": {"type": "string"}}},
                    },
                }
                for name in ("research_details", "policy_details")
            ],
        ]

    async def tool(self, name, args):
        if name == "victory_progress":
            self.factory.native_reads += 1
            return position(self.factory.service.records[self.factory.service.active_id]["gameTurn"], VICTORIES)
        if name in {"research_details", "policy_details"}:
            self.factory.native_reads += 1
            field = "technologies" if name == "research_details" else "policies"
            return {field: [{"name": "TEST", "cost": 80, "description": "Frozen rules"}], "turn": 5}
        result: JsonObject = await super().tool(name, args)
        if name == "player_turn":
            result["rules"] = {"victoryTypes": VICTORIES}
        return result


class ReviewCodex(FakeCodex):
    async def request(self, method, payload):
        if method == "turn/start" and "outputSchema" in payload:
            self.complete = False
            result = await super().request(method, payload)
            self.turn_id = result["turn"]["id"]
            self.factory.review_process = self
            self.factory.review_started.set()
            return result
        return await super().request(method, payload)

    async def finish_review(self, text=None):
        common = {"threadId": self.thread_id, "turnId": self.turn_id}
        await self.notification(
            "item/completed",
            {
                **common,
                "item": {
                    "id": "review-result",
                    "type": "agentMessage",
                    "phase": "final_answer",
                    "text": text if text is not None else json.dumps(assessment()),
                },
            },
        )
        await self.notification(
            "turn/completed", {"threadId": self.thread_id, "turn": {"id": self.turn_id, "status": "completed"}}
        )


class ReviewFactory(FakeFactory):
    def __init__(self):
        super().__init__()
        self.review_started = asyncio.Event()
        self.review_process: ReviewCodex | None = None
        self.native_reads = 0

    def __call__(self, executable, workspace, mcp_url, ca_file, notification):
        process = ReviewCodex(self, workspace, notification)
        self.instances.append(process)
        return process

    def connection(self, *args, **kwargs):
        return ReviewHost(self)


class StrategicAdviserServiceTests(AgentServiceTestCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        await self.service.close()
        self.factory = ReviewFactory()
        self.service = self.make_service(fast=True)

    async def begin_turn(self, turn=5):
        assert isinstance(self.factory, ReviewFactory)
        self.factory.complete = False
        await self.service.start(self.payload(gameTurn=turn, model="gpt-5.6-luna", reasoningEffort="max"))
        for _ in range(300):
            if self.service.status()["phase"] == "RUNNING":
                break
            await asyncio.sleep(0.01)
        return self.service.gateway.jobs[self.factory.tokens[0]]

    async def handoff(self, turn=5):
        job = await self.begin_turn(turn)
        result = await self.service.gateway.call(job, "player_command", {"kind": "END_TURN"}, "end")
        assert result["kind"] == "END_TURN" and "memoryPhase" not in result
        # Lost acknowledgements replay the committed command without preparing another review.
        assert await self.service.gateway.call(job, "player_command", {"kind": "END_TURN"}, "end") == result
        with pytest.raises(ValueError, match="TURN_ENDED"):
            await self.service.gateway.call(job, "memory_update", {"changes": []}, "too-late")
        await self.factory.instances[0].notification(
            "turn/completed",
            {
                "threadId": self.service.status()["threadId"],
                "turn": {"id": self.service.turn_id, "status": "completed"},
            },
        )
        record = await self.terminal()
        proof = {
            "activationId": record["activationId"],
            "epoch": record["epoch"],
            "gameTurn": turn,
            "committedHandoff": 1,
        }
        await self.service.finish_handoff(proof)
        await self.service.finalize(proof)
        return record

    async def start_review(self):
        assert isinstance(self.factory, ReviewFactory)
        self.service.chat.epoch = "epoch-one"
        self.service.chat.social = AsyncMock(return_value={"granted": True})
        self.service.reviews.start()
        await asyncio.wait_for(self.factory.review_started.wait(), 3)

    async def finished_review(self):
        assert isinstance(self.factory, ReviewFactory)
        for _ in range(300):
            if self.service.reviews.store.pending() is None:
                return
            await asyncio.sleep(0.01)
        self.fail(str(self.service.reviews.failure))

    async def test_review_retains_factual_actions_and_city_context_without_past_rationales(self):
        job = await self.begin_turn()
        assert job.observation is not None
        job.observation["civilizationProfile"] = {
            "name": "Rome",
            "ability": {
                "name": "The Glory of Rome",
                "effects": [{"text": "+25% Production towards any buildings that already exist in the Capital"}],
            },
            "uniqueUnits": [{"name": "Legion", "missingTechnologies": ["Iron Working"]}],
            "uniqueBuildings": [],
        }
        job.observation["map"]["tiles"] = [
            {
                "position": {"x": 3, "y": 2},
                "owner": "Egypt",
                "cityName": "Thebes",
                "visible": False,
                "observedTurn": 4,
                "cityIsOriginalMajorCapital": True,
            }
        ]
        job.observation["cities"][0]["development"] = {"position": {"x": 0, "y": 0}}
        history = {
            "actions": [
                {"action": "Queued Library in Rome", "reason": "OLD_SCIENCE_FIRST_PLAN", "outcome": "confirmed"},
                {"action": "Uncertain conquest", "reason": "OLD_SCIENCE_FIRST_PLAN", "outcome": "uncertain"},
            ],
            "hasMore": False,
        }
        with patch.object(self.service.memory, "history", return_value=history):
            await self.service.reviews.prepare(job, "prepare-review")
        await self.service.reviews.confirm(job.id)
        task = self.service.reviews.store.pending()
        assert task is not None
        inputs = json.loads(task["inputs"])
        assert inputs["actions"]["entries"] == [
            {"turn": turn, "action": "Queued Library in Rome", "commandStatus": "confirmed"} for turn in range(1, 6)
        ]
        prompt = advice_prompt(inputs)
        assert '"name":"The Glory of Rome"' in prompt.user
        facts = json.JSONDecoder().raw_decode(prompt.user.split("Frozen position as of end of turn 5:\n", 1)[1])[0]
        assert facts["player_turn"]["civilizationProfile"]["uniqueUnits"] == [
            {"name": "Legion", "missingTechnologies": ["Iron Working"]}
        ]
        assert "OLD_SCIENCE_FIRST_PLAN" not in prompt.user and "Uncertain conquest" not in prompt.user
        assert "Queued Library in Rome" in prompt.user
        assert '"name":"Thebes"' in prompt.user and '"originalMajorCapital":true' in prompt.user
        assert '"observedTurn":4' in prompt.user and '"visible":false' in prompt.user
        assert '"nearestOwnCity":"Rome"' in prompt.user and '"distanceInHexes":3' in prompt.user
        assert history["actions"][0]["reason"] == "OLD_SCIENCE_FIRST_PLAN"
        job.observation["map"]["tiles"].clear()
        job.observation["civilizationProfile"].clear()
        assert "Thebes" in advice_prompt(inputs).user  # Review remains frozen.
        assert "The Glory of Rome" in advice_prompt(inputs).user

    async def test_actual_receipt_effects_survive_history_and_bounded_review_capture(self):
        job = await self.begin_turn()
        stopped = "Warrior stopped (1, 0); no attack on Brute at (3, 0) occurred."
        attacked = (
            "Warrior attacked Brute at (3, 0): dealt 35, received 20 damage. Target destroyed. Warrior health 60."
        )
        # Fill the interval beyond the review budget; trim complete old entries, not effects.
        summaries = [f"Scout: ({i}, 0) to ({i + 1}, 0); 0 movement remaining." for i in range(100)] + [
            stopped,
            attacked,
        ]
        for i, summary in enumerate(summaries):
            command = {
                "kind": "ATTACK" if i >= 100 else "MOVE_UNIT",
                "target": "unit1",
                "destination": {"x": 3, "y": 0},
            }
            request = self.service.memory.queue(
                job.id, "player_command", command, {}, f"receipt-{i}", {"reason": "OLD_PLAN"}
            )
            self.service.memory.receipt(request["id"], {"kind": command["kind"], "actionSummary": summary})
        await self.service.reviews.prepare(job, "prepare-review")
        await self.service.reviews.confirm(job.id)
        task = self.service.reviews.store.pending()
        assert task is not None
        inputs = json.loads(task["inputs"])
        history = inputs["actions"]
        entries = history["entries"]
        assert history["omittedEarlierActions"] > 0 and len(json.dumps(entries, ensure_ascii=False)) <= 10000
        assert entries[-2:] == [
            {"turn": 5, "action": summary, "commandStatus": "confirmed"} for summary in (stopped, attacked)
        ]
        assert all(entry["action"] in summaries for entry in entries)
        prompt = advice_prompt(inputs).user
        assert stopped in prompt and attacked in prompt and "OLD_PLAN" not in prompt

    async def test_next_own_turn_waits_for_review_and_preloads_its_report(self):
        assert isinstance(self.factory, ReviewFactory)
        record = await self.handoff()
        assert record["usageComplete"]
        await self.start_review()
        pending = self.service.reviews.store.pending()
        assert pending is not None
        inputs = json.loads(pending["inputs"])
        assert "old strategy" not in advice_prompt(inputs).user
        assert len(self.factory.instances) == 2
        reviewer = next(job for job in self.service.gateway.jobs.values() if job.review_inputs is not None)
        before = self.factory.native_reads
        for name in ("player_command", "memory_read", "memory_update", "chat_send", "trade_offer", "movement_preview"):
            assert (await self.service.gateway.call(reviewer, name, {}, "1"))["error"] == "REVIEW_READ_ONLY"
        assert "memory_read" not in {t["name"] for t in await self.service.gateway.tools(reviewer)}
        assert (await self.service.gateway.call(reviewer, "research_details", {"names": ["TEST"]}, "2"))[
            "technologies"
        ][0]["cost"] == 80
        # No live read, no private history, no future facts.
        assert self.factory.native_reads == before
        readiness = await self.service.turn_readiness({"epoch": "epoch-one", "gameTurn": 6})
        assert readiness == {
            "civilization": "Rome",
            "epoch": "epoch-one",
            "gameTurn": 6,
            "pendingReview": {"job": "activation-one", "turn": 5, "status": "PENDING", "attempts": 1},
        }
        next_payload = self.payload("next-turn", gameTurn=6, handoff=1, model="gpt-5.6-luna", reasoningEffort="max")
        with pytest.raises(ServiceError, match="STRATEGIC_REVIEW_PENDING") as blocked:
            await self.service.start(next_payload)
        assert blocked.value.status == 409 and "next-turn" not in self.service.records
        # Idempotent replay of the source activation still works while its review runs.
        replay = await self.service.start(self.payload(gameTurn=5, model="gpt-5.6-luna", reasoningEffort="max"))
        assert replay["activationId"] == record["activationId"]
        before = self.factory.native_reads
        frozen = await self.service.gateway.call(
            reviewer,
            "victory_progress",
            {"sections": ["charts", "rankings"], "metrics": ["Production"], "civilizations": ["Rome"], "fromTurn": 0},
            "frozen-victory",
        )
        assert frozen["turn"] == 5 and frozen["charts"]["turns"][-1] == 5
        assert len(frozen["charts"]["series"]) == 1 and frozen["rankings"]["rows"][0]["values"] == [7]
        assert self.factory.native_reads == before
        assert self.service.reviews.store.pending() is not None
        assert len(self.factory.instances) == 2
        assert self.factory.review_process is not None
        await self.factory.review_process.finish_review()
        await self.finished_review()
        assert (await self.service.turn_readiness({"epoch": "epoch-one", "gameTurn": 6}))["pendingReview"] is None
        review_record = next(row for row in self.service.records.values() if row.get("purpose") == "review")
        assert review_record["effectiveSettings"] == record["effectiveSettings"]
        assert review_record["usage"] == record["usage"]
        assert review_record["reviewOf"] == record["activationId"]
        assert review_record["usageComplete"]
        assert not self.service.gateway.jobs
        review_call = [p for m, p in self.factory.calls if m == "turn/start" and "outputSchema" in p][0]
        assert (
            review_call["model"] == "gpt-5.6-luna"
            and review_call["effort"] == "max"
            and review_call["serviceTier"] == "fast"
        )
        self.factory.complete = True
        await self.service.start(next_payload)
        next_record = await self.terminal()
        assert next_record["phase"] == "COMPLETED"
        main_prompt = [p for m, p in self.factory.calls if m == "turn/start"][-1]["input"][0]["text"]
        assert main_prompt.count("Latest completed strategic advice") == 1
        assert '"asOfTurn":5' in main_prompt
        proof = {"activationId": "next-turn", "epoch": record["epoch"], "gameTurn": 6, "committedHandoff": 2}
        await self.service.finish_handoff(proof)
        await self.service.finalize(proof)
        await self.service.start(
            self.payload("later-turn", gameTurn=7, handoff=2, model="gpt-5.6-luna", reasoningEffort="max")
        )
        await self.terminal()
        later_prompt = [p for m, p in self.factory.calls if m == "turn/start"][-1]["input"][0]["text"]
        assert later_prompt.count("Latest completed strategic advice") == 1
        assert '"asOfTurn":5' in later_prompt

    async def test_turn_before_interval_has_no_review_inputs_or_extra_process(self):
        assert isinstance(self.factory, ReviewFactory)
        await self.handoff(4)
        assert self.service.reviews.store.pending() is None
        assert (await self.service.turn_readiness({"epoch": "epoch-one", "gameTurn": 5}))["pendingReview"] is None
        assert self.factory.native_reads == 1 and len(self.factory.instances) == 1  # Ordinary victory preload only.

    async def test_review_activity_is_published_to_operator_database(self):
        assert isinstance(self.factory, ReviewFactory)
        await self.handoff()
        await self.start_review()
        process = self.factory.review_process
        assert process is not None
        common = {"threadId": process.thread_id, "turnId": process.turn_id}
        await process.notification("item/reasoning/textDelta", {**common, "delta": "RAW_REASONING_MUST_NOT_BE_SAVED"})
        await process.notification(
            "item/reasoning/summaryTextDelta",
            {**common, "itemId": "reasoning", "summaryIndex": 0, "delta": "Exposed summary. " * 8},
        )
        await process.notification(
            "item/started", {**common, "item": {"id": "draft", "type": "agentMessage", "phase": "final_answer"}}
        )
        await process.notification(
            "item/agentMessage/delta", {**common, "itemId": "draft", "delta": "Report fragment. " * 8}
        )
        record = next(r for r in self.service.records.values() if r.get("purpose") == "review")
        # The existing heartbeat publishes progress without saving every token.
        await self.service._save(record)
        with read_database(self.service.accounting.path) as db:
            saved = json.loads(
                db.scalar(select(accounting_jobs.c.payload).where(accounting_jobs.c.id == record["activationId"]))
            )
        assert saved["reviewProgress"]["stage"] == "Drafting"
        assert saved["reviewProgress"]["draftCharacters"] == len("Report fragment. " * 8)
        assert saved["reviewProgress"]["lastActivityAtMs"] > 0
        assert "RAW_REASONING" not in json.dumps(saved)
        assert "Exposed summary" in json.dumps(saved["reviewTrace"])
        assert "Report fragment" in json.dumps(saved["reviewTrace"])
        assert "Report fragment" not in json.dumps(saved["reviewProgress"])
        # A different turn cannot alter the review's progress.
        before = dict(record["reviewProgress"])
        await process.notification(
            "item/agentMessage/delta", {**common, "turnId": "other", "itemId": "draft", "delta": "ignored"}
        )
        assert record["reviewProgress"] == before
        await process.finish_review()
        await self.finished_review()
        assert record["reviewProgress"]["stage"] == "Completed"

    async def test_capture_failure_stops_before_native_handoff(self):
        job = await self.begin_turn()
        native = job.connection.tool

        async def fail_catalogue(name, arguments):
            if name == "research_details":
                raise ValueError("TEST_CAPTURE_FAILED")
            return await native(name, arguments)

        with patch.object(job.connection, "tool", side_effect=fail_catalogue):
            with pytest.raises(ValueError, match="TEST_CAPTURE_FAILED"):
                await self.service.gateway.call(job, "player_command", {"kind": "END_TURN"}, "end")
        record = await self.terminal()
        assert record["error"] == "STRATEGIC_REVIEW_CAPTURE_FAILED"
        assert isinstance(job.connection, ReviewHost)
        assert job.connection.executed == []
        assert self.service.reviews.store.pending() is None

    async def test_invalid_review_exhaustion_is_visible_without_replaying_gameplay(self):
        assert isinstance(self.factory, ReviewFactory)
        self.service.reviews.attempts = 1
        await self.handoff()
        await self.start_review()
        assert self.factory.review_process is not None
        await self.factory.review_process.finish_review("invalid")
        assert self.service.reviews.worker is not None
        await asyncio.wait_for(self.service.reviews.worker, 3)
        failure = self.service.health()["runFailure"]
        assert failure is not None and failure["error"] == "STRATEGIC_REVIEW_ATTEMPTS_EXHAUSTED"
        pending = self.service.reviews.store.pending()
        assert pending is not None and pending["inputs"] and pending["attempts"] == 1
        assert (await self.service.turn_readiness({"epoch": "epoch-one", "gameTurn": 6}))["pendingReview"][
            "attempts"
        ] == 1
        assert len(self.factory.instances) == 2 and all(p.closed for p in self.factory.instances)
        assert self.service.memory.snapshot("gameplay", 6)["strategicAdvice"] is None
        assert len(self.service.memory.requests_for_job("activation-one")) == 1

    async def test_pause_retains_frozen_review_for_restart_without_blocking_handoff(self):
        assert isinstance(self.factory, ReviewFactory)
        await self.handoff()
        await self.start_review()
        pending = self.service.reviews.store.pending()
        await self.service.reviews.close()
        assert not self.service.gateway.jobs
        after = self.service.reviews.store.pending()
        assert after is not None and pending is not None and after["inputs"] == pending["inputs"]
        assert self.service.status("activation-one")["finalizationState"] == "COMPLETED"
        self.factory.review_started.clear()
        self.service.reviews.start()
        await asyncio.wait_for(self.factory.review_started.wait(), 3)
        assert self.factory.review_process is not None
        await self.factory.review_process.finish_review()
        await self.finished_review()
        assert len([r for r in self.service.records.values() if r.get("purpose") == "review"]) == 2

    async def test_unconfirmed_reviewer_shutdown_stops_without_retry_or_publication(self):
        assert isinstance(self.factory, ReviewFactory)
        await self.handoff()
        await self.start_review()
        self.factory.close_failure = True
        assert self.factory.review_process is not None
        await self.factory.review_process.finish_review()
        assert self.service.reviews.worker is not None
        await asyncio.wait_for(self.service.reviews.worker, 3)
        failure = self.service.health()["runFailure"]
        assert failure is not None and failure["error"] == "STOP_UNCONFIRMED"
        pending = self.service.reviews.store.pending()
        assert pending is not None and pending["inputs"] and pending["attempts"] == 1
        assert len(self.factory.instances) == 2 and not self.service.gateway.jobs
        assert self.service.memory.snapshot("gameplay", 6)["strategicAdvice"] is None
        record = next(row for row in self.service.records.values() if row.get("purpose") == "review")
        assert record["phase"] == "RUNNING" and not record["usageComplete"]
        with pytest.raises(ServiceError, match="STOP_UNCONFIRMED"):
            await self.service.start(self.payload("must-not-start", gameTurn=6, handoff=1))
        self.service.reviews.start()
        assert self.service.health()["runFailure"] == failure
        with pytest.raises(ServiceError, match="TEST_STOP_FAILED"):
            await self.service.reviews.close()
        self.factory.close_failure = False
        await self.service.reviews.close()
        assert all(process.closed for process in self.factory.instances)

    async def test_review_publication_failure_is_fatal_and_keeps_inputs(self):
        assert isinstance(self.factory, ReviewFactory)
        await self.handoff()
        await self.start_review()
        with patch.object(self.service.reviews.store, "complete", side_effect=OSError("disk full")):
            assert self.factory.review_process is not None
            await self.factory.review_process.finish_review()
            assert self.service.reviews.worker is not None
            await asyncio.wait_for(self.service.reviews.worker, 3)
        failure = self.service.health()["runFailure"]
        assert failure is not None and failure["error"] == "REVIEW_RECORD_FAILED"
        pending = self.service.reviews.store.pending()
        assert pending is not None and pending["inputs"] and pending["attempts"] == 1
        assert not self.service.gateway.jobs and all(process.closed for process in self.factory.instances)

    async def test_checkpoint_holds_publication_and_rewind_restarts_frozen_review(self):
        assert isinstance(self.factory, ReviewFactory)
        await self.handoff()
        await self.start_review()
        assert (await service_recovery.barrier(self.service, True))["ready"]
        saved = await service_recovery.capture(self.service, "turn-6")
        reviewer = next(job for job in self.service.gateway.jobs.values() if job.review_inputs is not None)
        assert self.factory.review_process is not None
        await self.factory.review_process.finish_review()
        for _ in range(300):
            if self.factory.review_process.closed:
                break
            await asyncio.sleep(0.01)
        assert self.factory.review_process.closed
        assert self.service.memory.snapshot("gameplay", 6)["strategicAdvice"] is None
        await service_recovery.barrier(self.service, False)
        await self.finished_review()
        assert self.service.memory.snapshot("gameplay", 6)["strategicAdvice"] is not None
        assert (await service_recovery.barrier(self.service, True))["ready"]
        restored = await service_recovery.restore(
            self.service, {"id": saved["id"], "sha256": saved["sha256"], "epoch": "restored"}
        )
        assert restored["pendingReviews"] == ["activation-one"]
        pending = self.service.reviews.store.pending()
        assert pending is not None and pending["inputs"] and pending["attempts"] == 1
        assert (await self.service.turn_readiness({"epoch": "restored", "gameTurn": 6}))["pendingReview"]["turn"] == 5
        with pytest.raises(ServiceError, match="WORKSPACE_EPOCH_CONFLICT"):
            await self.service.turn_readiness({"epoch": "epoch-one", "gameTurn": 6})
        assert self.service.memory.snapshot("gameplay", 6)["strategicAdvice"] is None
        await service_recovery.barrier(self.service, False)
        with pytest.raises(StateConflict, match="JOB_PAUSED_OR_EXPIRED"):
            await self.service.gateway.call(reviewer, "player_turn", {}, "late")
        self.factory.review_started.clear()
        self.service.chat.epoch = "restored"
        self.service.reviews.start()
        await asyncio.wait_for(self.factory.review_started.wait(), 3)
        assert self.factory.review_process is not None
        await self.factory.review_process.finish_review()
        await self.finished_review()
        assert self.service.memory.snapshot("gameplay", 6)["strategicAdvice"]["asOfTurn"] == 5
