"""Run-private presentation state; compact native observations never reach models or Monty."""

import copy
import json
from dataclasses import dataclass, field, replace
from typing import Any

from pydantic_ai.capabilities import AbstractCapability
from pydantic_ai.messages import ToolReturn, UserPromptPart

from civ_agent.coordination_updates import apply_coordination_update
from civ_agent.observations import DetailQuery, detail_scope
from civ_agent.orchestration.contracts import digest
from civ_agent.orchestration.store import JournalConflict
from civ_agent.payload_updates import apply_detail_response
from civ_agent.response_compaction import expand_response
from civ_agent.state_updates import apply_player_turn


@dataclass(kw_only=True)
class PlayerObservations(AbstractCapability[Any]):
    invoke: Any
    on_delivered: Any = None
    startup: str | None = None
    on_startup_delivered: Any = None
    export_context: bool = False
    id: str | None = "player-observations-v1"
    overview: dict | None = field(default=None, init=False)
    details: dict = field(default_factory=dict, init=False)
    coordination: dict | None = field(default=None, init=False)
    owner: tuple | None = field(default=None, init=False)
    seeded: bool = field(default=False, init=False)
    startup_visible: bool = field(default=False, init=False)
    startup_acknowledged: bool = field(default=False, init=False)
    recoveries: int = field(default=0, init=False)
    recovery_receipts: list = field(default_factory=list, init=False)
    opening_candidate: dict | None = field(default=None, init=False)
    opening_snapshot: str | None = field(default=None, init=False)
    snapshot_checked: bool = field(default=False, init=False)

    async def for_run(self, ctx):
        # Capabilities survive history compaction. Every new Agent.run gets a
        # separate instance, even with the same durable job or resumed epoch.
        return type(self)(
            invoke=self.invoke,
            on_delivered=self.on_delivered,
            startup=self.startup,
            on_startup_delivered=self.on_startup_delivered,
            export_context=self.export_context,
        )

    def check(self, ctx):
        scope = ctx.deps.scope
        owner = (scope.game, scope.player, scope.epoch, ctx.deps.job, ctx.run_id)
        if self.owner is None:
            self.owner = owner
        elif self.owner != owner:
            raise JournalConflict("PLAYER_OBSERVATION_OWNER_CONFLICT")

    async def before_model_request(self, ctx, request_context):
        self.check(ctx)
        if not self.seeded:
            self.seeded = True
            try:
                expected = json.loads(self.startup) if self.startup is not None else None
            except ValueError:
                expected = None
            messages = copy.deepcopy(list(request_context.messages))
            for message in messages:
                for index, part in enumerate(message.parts):
                    if not isinstance(part, UserPromptPart) or not isinstance(part.content, str):
                        continue
                    try:
                        value = json.loads(part.content)
                    except (ValueError, TypeError):
                        continue
                    if isinstance(value, dict) and "role" in value and "reads" in value:
                        if self.startup is not None and digest(value) != digest(expected):
                            continue
                        self.startup_visible = True
                        value = await self.materialize(ctx, value, call_id="opening")
                        message.parts[index] = replace(part, content=json.dumps(value))
                        self.opening_candidate = copy.deepcopy(value)
            return replace(request_context, messages=messages)
        return request_context

    async def after_model_request(self, ctx, *, request_context, response):
        if not self.startup_acknowledged:
            self.check(ctx)
            # Read the final request context after all presentation/compaction
            # hooks. Private preparation, or an opening removed before inference,
            # never makes a snapshot available to generated code.
            serialized = None
            for message in request_context.messages:
                for part in message.parts:
                    if not isinstance(part, UserPromptPart) or not isinstance(part.content, str):
                        continue
                    try:
                        value = json.loads(part.content)
                    except (ValueError, TypeError):
                        continue
                    if self.opening_candidate is not None and value == self.opening_candidate:
                        serialized = part.content
            # Inspection credit and the code copy use the same final delivered
            # bytes. An earlier presentation hook alone cannot establish either.
            self.startup_visible = serialized is not None
            if self.on_startup_delivered is not None:
                await self.on_startup_delivered(ctx.deps, ctx.run_id, self.startup_visible)
            self.startup_acknowledged = True
            self.snapshot_checked = True
            if self.export_context and self.startup is not None:
                self.opening_snapshot = serialized
            self.opening_candidate = None
        return response

    def delivered_context(self, ctx):
        self.check(ctx)
        if not self.export_context or self.opening_snapshot is None:
            return None
        # A new copy on each access protects the frozen, originally delivered
        # bytes from local program mutations. It is historical, never refreshed.
        return json.loads(self.opening_snapshot)

    async def recover(self, ctx, tool, arguments, call_id):
        self.recoveries += 1
        identity = "observation-recovery:" + digest([ctx.run_id, call_id, self.recoveries, tool, arguments])
        step = ctx.deps.model_step_offset + ctx.run_step
        receipt = await self.invoke(
            ctx.deps,
            identity,
            step,
            {"kind": "inspect", "operations": [{"tool": tool, "arguments": arguments, "recoverBaseline": "missing"}]},
        )
        self.recovery_receipts.append(receipt.model_dump(mode="json"))
        results = receipt.result.get("receipts", [])
        if len(results) != 1 or results[0].get("status") != "confirmed" or "error" in results[0].get("result", {}):
            raise JournalConflict("PLAYER_OBSERVATION_RECOVERY_FAILED")
        recovered = results[0]["result"]
        # Code Mode consumes children before another inference step. Acknowledge
        # only this read, under its own identity; no historical receipt is promoted.
        from civ_agent.runtime.code_mode import _parent

        if _parent.get() is not None and self.on_delivered is not None:
            await self.on_delivered(ctx.deps, identity, step)
        return recovered

    async def materialize(self, ctx, value, *, call_id, rich_bundle=False):
        self.check(ctx)
        result = expand_response(value, startup="reads" in value)
        if rich_bundle and result.get("receiptVersion") == 1 and isinstance(result.get("feedback"), dict):
            # A rich bundle has several projections of the same receipts.
            # Reduce each native response once, then reconstruct all projections;
            # processing an identical delta twice can demand a false recovery.
            feedback = await self.materialize(ctx, result["feedback"], call_id=call_id)
            receipts = feedback.get("receipts", [])
            by_id = {row.get("operationId"): row for row in receipts}
            if len(by_id) != len(receipts) or None in by_id:
                raise JournalConflict("BUNDLE_PRESENTATION_RECEIPT_CONFLICT")
            entries = result.get("receiptEntries", [])
            if not isinstance(entries, list):
                raise JournalConflict("BUNDLE_PRESENTATION_RECEIPT_CONFLICT")
            for entry in entries:
                receipt = entry.get("receipt")
                if receipt is not None:
                    materialized = by_id.get(receipt.get("operationId"))
                    if materialized is None:
                        raise JournalConflict("BUNDLE_PRESENTATION_RECEIPT_CONFLICT")
                    entry["receipt"] = copy.deepcopy(materialized)
            result["feedback"] = feedback
            result["results"] = [
                {"tool": entry["tool"], "arguments": entry["arguments"], "result": entry["receipt"].get("result", {})}
                for entry in entries
                if entry.get("receipt") is not None and entry["receipt"].get("status") != "unexecuted"
            ]
            return result
        for key in ("receipts", "results"):
            for row in result.get(key, []):
                if isinstance(row.get("result"), dict):
                    row["result"] = await self.materialize(ctx, row["result"], call_id=call_id)
        for name, reply in result.get("reads", {}).items():
            result["reads"][name] = await self.materialize(ctx, reply, call_id=call_id)
        for index, reply in enumerate(result.get("decisionDetails", [])):
            result["decisionDetails"][index] = await self.materialize(ctx, reply, call_id=call_id)
        # invalidatedDetails revokes action evidence, not the historical bytes
        # used to decode the next native read. The gateway retains those same
        # compression baselines. Keep them private: only a new response can
        # materialize data, and the gateway separately enforces inspection credit.
        # Dropping them here makes ordinary post-action deltas require recovery
        # repeatedly, including section reads based on a broader prior query.
        if result.get("refreshRequired"):
            self.details.clear()
            self.overview = None
        if result.get("stateUpdate", {}).get("kind") in {"unavailable", "turn_ended"}:
            result["observationStatus"] = result.pop("stateUpdate")
            result["refreshRequired"] = True
            self.overview = None
            self.details.clear()
        if "stateUpdate" in result or "stateId" in result and ("revision" in result or "turn" in result):
            try:
                self.overview = apply_player_turn(self.overview, result)
            except ValueError:
                recovered = await self.recover(ctx, "player_turn", {}, call_id)
                self.overview = apply_player_turn(None, recovered)
                result["observationRecovery"] = (
                    "A fresh overview was recovered. Original receipts and focused fields retain their original basis; refresh focused evidence before dependent actions."
                )
            result.pop("stateUpdate", None)
            # A focused read may return a complete military review beside an
            # overview delta containing only its summary. Preserve the explicit
            # read; reconstructed overview fields must not replace those facts.
            result = {**copy.deepcopy(self.overview), **result}
        if "detailUpdate" in result and "query" in result:
            try:
                state = apply_detail_response(self.details, result)
            except ValueError:
                query = result["query"]
                recovered = await self.recover(ctx, query["tool"], query["arguments"], call_id)
                result = {**result, **expand_response(recovered)}
                state = apply_detail_response(self.details, result)
            self.details[detail_scope(DetailQuery.model_validate(state["query"]))] = state
            result["data"] = copy.deepcopy(state["value"])
            result["detailUpdate"] = {key: state[key] for key in ("scopeId", "sequence", "stateId")} | {"kind": "full"}
        if "coordinationUpdate" in result or "economyCoordination" in result:
            try:
                self.coordination = apply_coordination_update(self.coordination, result)
            except ValueError:
                recovered = await self.recover(ctx, "player_turn", {}, call_id)
                self.coordination = apply_coordination_update(None, recovered)
            result.pop("coordinationUpdate", None)
            if self.coordination is not None:
                result["economyCoordination"] = copy.deepcopy(self.coordination)
        if "coordination" in result and isinstance(result["coordination"], dict):
            self.coordination = copy.deepcopy(result["coordination"])
        return result

    async def after_tool_execute(self, ctx, *, call, tool_def, args, result):
        value = result.return_value if isinstance(result, ToolReturn) else result
        if (
            not isinstance(value, dict)
            or call.tool_name == "run_code"
            or (tool_def.metadata or {}).get("code_mode_result_helper") is True
        ):
            return result
        first = len(self.recovery_receipts)
        materialized = await self.materialize(
            ctx, value, call_id=call.tool_call_id, rich_bundle=call.tool_name == "inspect_bundle"
        )
        if materialized == value and len(self.recovery_receipts) == first:
            return result
        metadata = {
            **(result.metadata or {} if isinstance(result, ToolReturn) else {}),
            "raw_player_observation": copy.deepcopy(value),
            "observation_recoveries": self.recovery_receipts[first:],
        }
        return (
            replace(result, return_value=materialized, metadata=metadata)
            if isinstance(result, ToolReturn)
            else ToolReturn(
                return_value=materialized,
                metadata=metadata,
            )
        )
