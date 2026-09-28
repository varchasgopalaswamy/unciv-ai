"""Restartable strategy work that overlaps other players and precedes the next own turn."""

import asyncio
import contextlib
import copy
import json

from civ_agent.agent_history import HISTORY_METHODS
from civ_agent.agent_settings import codex_thread_settings, valid_setting
from civ_agent.civilization_state import job_id
from civ_agent.observations import overview
from civ_agent.proposal_gateway import JobAccess, public_result
from civ_agent.review_store import ReviewStore
from civ_agent.strategic_review import (
    REVIEW_READ_TOOLS,
    StrategicAdvice,
    advice_prompt,
    parse_advice,
    review_due,
    review_facts,
)
from civ_agent.tls import private_directory
from civ_agent.victory_snapshot import capture_victory


class ReviewFatalError(RuntimeError):
    """Retrying cannot safely repair persistence or unconfirmed process shutdown."""


class OfflineConnection:
    async def list_tools(self):
        raise ValueError("REVIEW_READ_ONLY")

    async def tool(self, name, arguments):
        raise ValueError("REVIEW_READ_ONLY")

    async def close(self):
        pass


class ReviewRuntime:
    def __init__(self, service, interval=5, attempts=3):
        review_due(0, interval)
        if type(attempts) is not int or attempts < 1:
            raise ValueError("Review attempts must be positive")
        self.service, self.interval, self.attempts = service, interval, attempts
        self.store = ReviewStore(service.memory)
        self.worker = None
        self.pending = asyncio.Event()
        self.failure = None
        self.unconfirmed_process = None

    async def prepare(self, job, call_id):
        """Persist the position before END_TURN; a failed command cannot start work."""
        try:
            await self._prepare(job)
        except Exception as error:
            service = self.service
            service.diagnostics.emit("strategic_review_capture_failed", error=error, job=job.id)
            await service.io.call(
                service.memory.fail_run,
                job.id,
                call_id,
                "STRATEGIC_REVIEW_CAPTURE_FAILED",
                {"exceptionType": type(error).__name__, "message": str(error)},
                {
                    "error": "STRATEGIC_REVIEW_CAPTURE_FAILED",
                    "applied": False,
                    "recoverable": False,
                    "instruction": "Stop. Review inputs could not be saved, so END_TURN was not dispatched. The run requires operator inspection.",
                },
            )
            await service.gateway.sync_failure()
            raise

    async def _prepare(self, job):
        service = self.service
        record = service.records.get(job.id)
        if record is None:  # Interactive sessions have no scheduled model/review lifecycle.
            return
        turn = record["gameTurn"]
        if not review_due(turn, self.interval):
            return
        if not await service.io.call(self.store.available, job.id):
            return
        snapshot = copy.deepcopy(job.observation)
        reads = {"player_turn": public_result(review_facts(overview(snapshot)))}

        async def read(name, args):
            counted = await service.io.call(service.accounting.begin_call, job.id, name, args, source="startup")
            outcome = "incomplete"
            try:
                result = await job.connection.tool(name, args)
                if "error" in result:
                    raise ValueError("STRATEGIC_REVIEW_CAPTURE_FAILED: " + name)
                outcome = "success"
                return result
            finally:
                await service.io.call(service.accounting.end_call, counted, outcome, False)

        reads["victory_progress"] = await capture_victory(read, turn)
        for name, field in (("research_details", "technologies"), ("policy_details", "policies")):
            catalogue = await read(name, {})
            names = [row["name"] for row in catalogue[field]]
            details = []
            for offset in range(0, len(names), 16):
                details.extend((await read(name, {"names": names[offset : offset + 16]}))[field])
            reads[name] = {**catalogue, field: details}
        social = await read("player_communication", {"op": "state", "epoch": job.epoch})
        messages = (await service.io.call(service.memory.snapshot, "gameplay"))["events"]
        reads["chat_read"] = {**social, "messages": messages}
        catalogue = [tool for tool in await service.gateway.tools(job) if tool["name"] in REVIEW_READ_TOOLS]
        for tool in catalogue:
            if tool["name"] == "player_turn":
                tool["description"] = "Read the frozen end-of-turn overview being reviewed."
            else:
                tool["description"] = "Read only the frozen review position. " + tool.get("description", "")
        actions = []
        for recent_turn in range(max(0, turn - self.interval + 1), turn + 1):
            offset = 0
            while True:
                page = await service.io.call(service.memory.history, recent_turn, offset)
                actions.extend(
                    {"turn": recent_turn, **{key: action[key] for key in ("action", "outcome")}}
                    for action in page["actions"]
                    if action["outcome"] == "confirmed"
                )
                if not page["hasMore"]:
                    break
                offset = page["nextOffset"]
        total = len(actions)
        # Keep factual actions without carrying the player's strategic rationales
        # into an independent assessment. Full reasons remain in the audit history.
        while actions and len(json.dumps(actions, ensure_ascii=False)) > 10000:
            actions.pop(0)
        inputs = {
            "turn": turn,
            "civilization": service.civilization,
            "observation": review_facts(snapshot),
            "reads": {name: public_result(review_facts(value)) for name, value in reads.items()},
            "catalogue": catalogue,
            "actions": {"entries": actions, "omittedEarlierActions": total - len(actions)},
        }
        await service.io.call(
            self.store.prepare,
            job.id,
            inputs,
            record.get("effectiveSettings", record["settings"]),
            record["timeoutSeconds"],
        )

    async def confirm(self, parent):
        await self.service.io.call(self.store.confirm, parent)
        self.pending.set()

    def start(self):
        if self.unconfirmed_process is not None:
            return
        if self.worker is None or self.worker.done():
            self.failure = None
            self.worker = asyncio.create_task(self.dispatch())
        self.pending.set()

    async def close(self):
        if self.worker is not None:
            self.worker.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self.worker
            self.worker = None
        if self.unconfirmed_process is not None:
            # Reconnect/restore may proceed only after shutdown really succeeds.
            await self.unconfirmed_process.close()
            self.unconfirmed_process = None

    async def dispatch(self):
        service = self.service
        try:
            while True:
                await self.pending.wait()
                self.pending.clear()
                async with service.gateway.admit():
                    task = await service.io.call(self.store.pending)
                if task is None:
                    continue
                if task["attempts"] >= self.attempts:
                    raise ValueError("STRATEGIC_REVIEW_ATTEMPTS_EXHAUSTED")
                attempt_id = job_id()
                # Paid jobs are subject to the same invocation budget and host accounting.
                grant = await service.chat.social("job_claim", job=attempt_id, purpose="review", parentJob=task["job"])
                if not grant["granted"]:
                    await asyncio.sleep(1)
                    self.pending.set()
                    continue
                try:
                    async with service.gateway.admit():
                        task = await service.io.call(self.store.attempt, task["job"], self.attempts)
                    await self.compute(task, attempt_id)
                except asyncio.CancelledError:
                    raise
                except ReviewFatalError:
                    raise
                except Exception as error:
                    service.diagnostics.emit(
                        "strategic_review_failed", error=error, job=attempt_id, gameTurn=task["turn"]
                    )
                    async with service.gateway.admit():
                        await service.io.call(self.store.failed, task["job"], str(error) or type(error).__name__)
                    self.pending.set()
                finally:
                    with contextlib.suppress(Exception):
                        await service.flush_telemetry()
                        await service.chat.social("job_release", job=attempt_id)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            self.failure = {"error": str(error), "diagnosticId": "strategic-review", "job": "strategic-review"}
            service.diagnostics.emit("strategic_review_stopped", error=error)

    async def compute(self, task, id):
        """Local conversation state permits reviews, chat and gameplay concurrently."""
        from civ_agent.agent_service import USAGE_KEYS

        service = self.service
        inputs, settings = json.loads(task["inputs"]), json.loads(task["settings"])
        epoch = service.chat.epoch
        job = JobAccess(
            id,
            "review",
            epoch,
            OfflineConnection(),
            {},
            verified=True,
            catalogue=inputs["catalogue"],
            review_inputs=inputs,
        )
        record = {
            "activationId": id,
            "civilization": service.civilization,
            "epoch": epoch,
            "gameTurn": task["turn"],
            "reviewOf": task["job"],
            "purpose": "review",
            "modelPhase": "strategic_review",
            "threadId": None,
            "phase": "STARTING",
            "usage": dict.fromkeys(USAGE_KEYS, 0),
            "settings": settings,
            "requestedServiceTier": "fast" if service.fast else None,
        }
        done = asyncio.Event()
        expected = None
        completed = None
        closing = False
        stopped = False
        process = None
        final_messages, early_usage, early_completions = {}, {}, {}

        async def save():
            try:
                await service._save(record)
            except Exception as error:
                raise ReviewFatalError("REVIEW_RECORD_FAILED") from error

        async def stop():
            nonlocal closing, stopped
            closing = True
            job.closed = True
            try:
                if process is not None:
                    await process.close()
            except BaseException as error:
                self.unconfirmed_process = process
                record["error"] = "STOP_UNCONFIRMED"
                raise ReviewFatalError("STOP_UNCONFIRMED") from error
            stopped = True

        async def usage(value):
            total = value.get("total", {})
            if not isinstance(total, dict) or any(
                type(total.get(field, 0)) is not int or total.get(field, 0) < 0 for field in USAGE_KEYS.values()
            ):
                return
            for key, field in USAGE_KEYS.items():
                record["usage"][key] = max(record["usage"][key], total.get(field, 0))
            record["usageReported"] = all(field in total for field in USAGE_KEYS.values())
            await save()

        async def notification(method, params):
            nonlocal completed
            if method in HISTORY_METHODS:
                await service.io.call(service.history.notify, id, record["threadId"], method, params)
            if not closing and completed is None and method in {"service/disconnected", "service/approvalRequired"}:
                record["error"] = "REVIEW_PROCESS_FAILED"
                done.set()
            if params.get("threadId") != record["threadId"]:
                return
            if method == "thread/tokenUsage/updated":
                if expected is None:
                    early_usage[params.get("turnId")] = params.get("tokenUsage", {})
                elif params.get("turnId") == expected:
                    await usage(params.get("tokenUsage", {}))
            elif method == "turn/completed":
                turn = params.get("turn", {})
                if expected is None:
                    early_completions[turn.get("id")] = turn.get("status")
                elif turn.get("id") == expected:
                    completed = turn.get("status")
                    done.set()
            elif method == "item/completed":
                item = params.get("item", {})
                if item.get("type") == "agentMessage" and item.get("phase") == "final_answer":
                    final_messages[params.get("turnId")] = item.get("text", "")

        success = False
        try:
            service.gateway.jobs[job.token] = job
            service.records[id] = record
            try:
                workspace = private_directory(service.workspace / "reviews" / id)
                prompt = advice_prompt(inputs, strategy_guide=service.strategy_guide)
                record["startup"] = prompt.metadata()
                await service.io.call(service.accounting.start, id, epoch, True)
                await service.io.call(service.history.begin, record, None, job.token)
                await service.io.call(service.history.next_conversation, id, "strategic_review")
                await service.io.call(service.history.startup_context, id, prompt.instructions, role="developer")
                await service.io.call(service.history.startup_context, id, prompt.user)
            except Exception as error:
                raise ReviewFatalError("REVIEW_RECORD_FAILED") from error
            await save()
            process = service.process_factory(
                service.executable, workspace, service.gateway_url, service.gateway_ca_file, notification
            )
            async with asyncio.timeout(task["timeout"]):
                await process.start(job.token, settings["model"])
                thread = await process.request(
                    "thread/start",
                    {
                        "cwd": str(workspace),
                        "approvalPolicy": "never",
                        "sandbox": "workspace-write",
                        "developerInstructions": prompt.instructions,
                        **codex_thread_settings(settings, fast=service.fast),
                    },
                )
                record["threadId"] = thread["thread"]["id"]
                record["effectiveSettings"] = {
                    "model": thread.get("model"),
                    "reasoningEffort": thread.get("reasoningEffort"),
                }
                record["serviceTier"] = thread.get("serviceTier")
                if not valid_setting(record["effectiveSettings"]["model"]) or any(
                    settings.get(key) is not None and record["effectiveSettings"][key] != settings[key]
                    for key in ("model", "reasoningEffort")
                ):
                    raise ValueError("REVIEW_SETTINGS_CHANGED")
                await save()
                response = await process.request(
                    "turn/start",
                    {
                        "threadId": record["threadId"],
                        "input": [{"type": "text", "text": prompt.user}],
                        "approvalPolicy": "never",
                        "summary": "auto",
                        "model": record["effectiveSettings"]["model"],
                        **({"effort": settings["reasoningEffort"]} if settings.get("reasoningEffort") else {}),
                        **({"serviceTier": "fast"} if service.fast else {}),
                        "outputSchema": StrategicAdvice.model_json_schema(),
                    },
                )
                expected = response["turn"]["id"]
                record["turnId"] = expected
                await service.io.call(service.history.bind_turn, id, expected)
                if expected in early_usage:
                    await usage(early_usage[expected])
                if expected in early_completions:
                    completed = early_completions[expected]
                    done.set()
                record["phase"] = "RUNNING"
                await save()
                while not done.is_set():
                    try:
                        await asyncio.wait_for(done.wait(), 3)
                    except TimeoutError:
                        if service.gateway.run_failure:
                            raise ValueError(service.gateway.run_failure["error"]) from None
                        if not (await service.chat.social("job_heartbeat", job=id))["granted"]:
                            raise ValueError("REVIEW_PERMIT_EXPIRED") from None
                        await save()
                        await service.flush_telemetry()
                if completed != "completed" or record.get("error"):
                    raise ValueError(record.get("error", "STRATEGIC_REVIEW_FAILED"))
                advice = parse_advice(final_messages.get(expected, ""), inputs["reads"]["player_turn"])
            await stop()
            # A checkpoint may capture pending work while a model is computing.
            # Publish only after the hold, so rewind restores exactly that boundary.
            async with service.gateway.admit():
                try:
                    await service.io.call(self.store.complete, task["job"], advice)
                except Exception as error:
                    raise ReviewFatalError("REVIEW_RECORD_FAILED") from error
            success = True
        except BaseException as error:
            record["error"] = (
                "INTERRUPTED"
                if isinstance(error, asyncio.CancelledError)
                else str(error)
                if isinstance(error, ReviewFatalError)
                else type(error).__name__ + ": " + str(error)
            )
            raise
        finally:
            job.closed = True
            try:
                if not closing:
                    await stop()
            finally:
                await service.gateway.close(job, success)
                if stopped:
                    record["phase"] = (
                        "COMPLETED" if success else "INTERRUPTED" if record.get("error") == "INTERRUPTED" else "FAILED"
                    )
                record["usageComplete"] = stopped and completed == "completed" and record.get("usageReported", False)
                await save()
