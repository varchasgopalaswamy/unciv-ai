"""Concurrent, read-only strategy work during a shared pause between rounds."""

import asyncio
import contextlib
import json
import time

from civ_agent.agent_history import HISTORY_METHODS
from civ_agent.agent_settings import codex_thread_settings, valid_setting
from civ_agent.civilization_state import job_id
from civ_agent.model_status import PROGRESS_METHODS, TOOL_ITEMS
from civ_agent.observations import overview
from civ_agent.proposal_gateway import JobAccess, public_result
from civ_agent.protocol import JsonObject
from civ_agent.review_context import development_changes, review_position, upcoming_events
from civ_agent.review_store import ReviewStore
from civ_agent.strategic_review import (
    REVIEW_READ_TOOLS,
    advice_prompt,
    advice_schema,
    parse_advice,
    review_due,
    review_facts,
)
from civ_agent.tls import private_directory
from civ_agent.victory_snapshot import capture_victory


class ReviewFatalError(RuntimeError):
    """Input preparation, persistence and unconfirmed shutdown need operator inspection."""


class OfflineConnection:
    async def list_tools(self):
        raise ValueError("REVIEW_READ_ONLY")

    async def tool(self, name, arguments):
        raise ValueError("REVIEW_READ_ONLY")

    async def close(self):
        pass


class ReviewProgress:
    """Bounded activity metadata; never retain draft text or model reasoning."""

    def __init__(self):
        self.value: JsonObject = {"stage": "Starting", "lastActivityAtMs": None, "toolCalls": 0, "draftCharacters": 0}
        self.messages = {}
        self.tools = set()

    def observe(self, method, params):
        if method == "service/modelStatus":
            self.value["modelStatus"] = {
                key: value for key, value in params.items() if key not in {"threadId", "turnId"}
            }
            return
        item = params.get("item", {})
        if method not in PROGRESS_METHODS and method not in {"item/started", "item/completed"}:
            return
        self.value["lastActivityAtMs"] = int(time.time() * 1000)
        self.value.pop("modelStatus", None)
        if item.get("type") in TOOL_ITEMS:
            if method == "item/started":
                self.tools.add(item["id"])
                self.value["toolCalls"] += 1
                self.value["lastTool"] = item.get("tool", item["type"])
            else:
                self.tools.discard(item["id"])
            self.value["stage"] = "Reading" if self.tools else "Thinking"
        elif item.get("type") == "agentMessage":
            if method == "item/started":
                self.messages[item["id"]] = item.get("phase")
            else:
                self.messages.pop(item["id"], None)
            self.value["stage"] = "Drafting" if item.get("phase") == "final_answer" else "Writing update"
            if item.get("phase") == "final_answer":
                self.value["draftCharacters"] = len(item.get("text", ""))
        elif method == "item/agentMessage/delta":
            drafting = self.messages.get(params.get("itemId")) == "final_answer"
            self.value["stage"] = "Drafting" if drafting else "Writing update"
            if drafting:
                self.value["draftCharacters"] += len(params.get("delta", ""))
        else:
            self.value["stage"] = "Thinking"


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
        self.active = False

    async def phase(self, epoch, game_turn, stage):
        service = self.service
        if epoch != service.chat.epoch:
            raise ValueError("WORKSPACE_EPOCH_CONFLICT")
        turn = 0 if game_turn == 0 else game_turn - 1
        if stage == "hold":
            # Existing conversations finish; new incoming messages stay queued.
            service.chat.held = True
        elif stage == "start":
            if not service.gateway.paused or not service.chat.held or service.chat.dispatching:
                raise ValueError("REVIEW_BARRIER_REQUIRED")
            await self.prepare_round(game_turn)
            service.gateway.reviewing = True
            self.start()
        elif stage == "release":
            service.gateway.reviewing = False
            service.chat.held = False
            service.chat.pending.set()
        task = await service.io.call(self.store.at_turn, turn)
        return {
            "civilization": service.civilization,
            "epoch": epoch,
            "gameTurn": game_turn,
            "ready": not service.chat.dispatching and not self.active,
            "pendingReview": task if task and task["status"] != "COMPLETED" else None,
        }

    async def prepare_round(self, game_turn):
        """Capture the common round boundary only after all mutations have stopped."""
        service = self.service
        turn = 0 if game_turn == 0 else game_turn - 1
        if game_turn != 0 and not review_due(turn, self.interval):
            return
        if await service.io.call(self.store.at_turn, turn):
            return
        parent = next(
            (
                r
                for r in service.records.values()
                if r.get("purpose") != "review"
                and r.get("epoch") == service.chat.epoch
                and r.get("gameTurn") == turn
                and r.get("confirmedHandoff")
                and not r.get("terminalOutcome")
            ),
            None,
        )
        if game_turn != 0 and parent is None:
            return  # A defeated player has no completed turn to review.
        connection, epoch = service.chat.connection, service.chat.epoch
        if connection is None:
            raise ValueError("COMMUNICATION_NOT_CONNECTED")

        async def read(name, arguments):
            value = await connection.tool(name, arguments)
            if "error" in value:
                raise ValueError("STRATEGIC_REVIEW_CAPTURE_FAILED: " + name)
            return value

        try:
            snapshot = await read("player_turn", {})
            if snapshot["turn"] != game_turn or snapshot["epoch"] != epoch:
                raise ValueError("STRATEGIC_REVIEW_POSITION_CHANGED")
            if snapshot.get("result"):
                return
            tools = service.gateway.tool_catalogue(await connection.list_tools(), "gameplay")
            inputs = await self.capture(snapshot, read, tools, turn, epoch)
            await service.io.call(
                self.store.prepare,
                parent["activationId"] if parent else job_id(),
                inputs,
                parent.get("effectiveSettings", parent["settings"]) if parent else service.chat.settings,
                service.chat.timeout,
            )
        except Exception as error:
            service.diagnostics.emit("strategic_review_capture_failed", error=error, gameTurn=turn)
            raise

    async def capture(self, snapshot, read, tools, turn, epoch):
        service = self.service
        reads = {"player_turn": public_result(review_facts(overview(snapshot)))}
        reads["victory_progress"] = await capture_victory(read, snapshot["turn"])
        for name, field in (("research_details", "technologies"), ("policy_details", "policies")):
            catalogue = await read(name, {})
            names = [row["name"] for row in catalogue[field]]
            details = []
            for offset in range(0, len(names), 16):
                details.extend((await read(name, {"names": names[offset : offset + 16]}))[field])
            reads[name] = {**catalogue, field: details}
        social = await read("player_communication", {"op": "state", "epoch": epoch})
        messages = (await service.io.call(service.memory.snapshot, "gameplay"))["events"]
        # The listener may be inside a long poll when the barrier closes. Include
        # its undelivered arrivals without changing the paused memory or cursor.
        cursor = await service.io.call(service.memory.cursor)
        while True:
            page = await read(
                "player_communication", {"op": "read", "epoch": epoch, "cursor": cursor, "limit": 100, "waitSeconds": 0}
            )
            messages.extend(page["events"])
            if not page["hasMore"]:
                break
            if page["nextCursor"] <= cursor:
                raise ValueError("STRATEGIC_REVIEW_CAPTURE_FAILED: communication cursor did not advance")
            cursor = page["nextCursor"]
        messages = list({event["id"]: event for event in messages}.values())[-50:]
        reads["chat_read"] = {**social, "messages": messages}
        position = review_position(snapshot)
        previous = await service.io.call(self.store.last_position)
        reads["player_turn"]["developmentSincePreviousReview"] = development_changes(previous, position)
        reads["player_turn"]["upcomingEvents"] = upcoming_events(snapshot, social)
        catalogue = [tool for tool in tools if tool["name"] in REVIEW_READ_TOOLS]
        for tool in catalogue:
            if tool["name"] == "player_turn":
                tool["description"] = "Read the frozen overview being reviewed."
            else:
                tool["description"] = "Read only the frozen review position. " + tool.get("description", "")
        actions = []
        first_turn = max(1, turn - self.interval + 1)
        previous_turn = await service.io.call(self.store.last_completed_turn)
        if previous_turn is not None:
            first_turn = 0 if previous_turn == 0 else previous_turn + 1
        for recent_turn in range(first_turn, turn + 1):
            offset = 0
            while True:
                page = await service.io.call(service.memory.history, recent_turn, offset)
                actions.extend(
                    {"turn": recent_turn, "action": action["action"], "commandStatus": action["outcome"]}
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
        return {
            "turn": turn,
            "civilization": service.civilization,
            "observation": review_facts(snapshot),
            "reads": {name: public_result(review_facts(value)) for name, value in reads.items()},
            "catalogue": catalogue,
            "actions": {"entries": actions, "omittedEarlierActions": total - len(actions)},
        }

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
                async with service.gateway.admit(review=True):
                    task = await service.io.call(self.store.pending)
                if task is None:
                    continue
                if task["attempts"] >= self.attempts:
                    raise ValueError("STRATEGIC_REVIEW_ATTEMPTS_EXHAUSTED")
                attempt_id = job_id()
                # Paid jobs are subject to the same invocation budget and host accounting.
                parent = {"parentJob": task["job"]} if task["turn"] > 0 else {}
                grant = await service.chat.social("job_claim", job=attempt_id, purpose="review", **parent)
                if not grant["granted"]:
                    await asyncio.sleep(1)
                    self.pending.set()
                    continue
                self.active = True
                try:
                    async with service.gateway.admit(review=True):
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
                    async with service.gateway.admit(review=True):
                        await service.io.call(self.store.failed, task["job"], str(error) or type(error).__name__)
                    self.pending.set()
                finally:
                    try:
                        with contextlib.suppress(Exception):
                            await service.flush_telemetry()
                            await service.chat.social("job_release", job=attempt_id)
                    finally:
                        self.active = False
        except asyncio.CancelledError:
            raise
        except Exception as error:
            self.failure = {"error": str(error), "diagnosticId": "strategic-review", "job": "strategic-review"}
            service.diagnostics.emit("strategic_review_stopped", error=error)

    async def compute(self, task, id):
        """Each reviewer has its own frozen view and runs beside other reviewers."""
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
        progress = ReviewProgress()
        record["reviewProgress"] = progress.value
        done = asyncio.Event()
        expected = None
        completed = None
        closing = False
        stopped = False
        process = None
        final_messages, unphased_messages = {}, {}
        early_usage, early_completions = {}, {}

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
            if not closing and (expected is None or params.get("turnId") == expected):
                progress.observe(method, params)
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
                if item.get("type") == "agentMessage":
                    if item.get("phase") == "final_answer":
                        final_messages[params.get("turnId")] = item.get("text", "")
                    elif item.get("phase") is None:
                        # Phase is optional; explicit commentary is never a report.
                        unphased_messages[params.get("turnId")] = item.get("text", "")

        success = False
        try:
            service.gateway.jobs[job.token] = job
            service.records[id] = record
            try:
                prompt = advice_prompt(
                    inputs, strategy_guide=service.strategy_guide, civilization_guide=service.civilization_guide
                )
                record["startup"] = prompt.metadata()
                output_schema = advice_schema(inputs["reads"]["player_turn"])
            except Exception as error:
                raise ReviewFatalError("REVIEW_PROMPT_FAILED") from error
            try:
                workspace = private_directory(service.workspace / "reviews" / id)
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
                await process.start(job.token, settings)
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
                    "provider": thread.get("modelProvider"),
                    "model": thread.get("model"),
                    "reasoningEffort": thread.get("reasoningEffort"),
                }
                record["serviceTier"] = thread.get("serviceTier")
                if not valid_setting(record["effectiveSettings"]["model"]) or any(
                    settings.get(key) is not None and record["effectiveSettings"][key] != settings[key]
                    for key in ("provider", "model", "reasoningEffort")
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
                        "outputSchema": output_schema,
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
                if progress.value["stage"] == "Starting":
                    progress.value["stage"] = "Thinking"
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
                progress.value["stage"] = "Validating"
                await save()
                answer = final_messages.get(expected, unphased_messages.get(expected, ""))
                advice = parse_advice(answer, inputs["reads"]["player_turn"])
            await stop()
            progress.value["stage"] = "Publishing"
            await save()
            # A checkpoint may capture pending work while a model is computing.
            # Publish only after the hold, so rewind restores exactly that boundary.
            async with service.gateway.admit(review=True):
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
                progress.value["stage"] = record["phase"].capitalize()
                record["usageComplete"] = stopped and completed == "completed" and record.get("usageReported", False)
                await save()
