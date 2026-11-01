"""Civilization-private war episodes and negotiated strategic instructions.

These records are service-owned, transactional, and included in memory checkpoints.
They deliberately do not impose rules on the engine or on social promises.
"""

import copy
import hashlib
import json
import secrets
from typing import Annotated, Literal

from pydantic import Field, RootModel, model_validator

from civ_agent.coordination_context import history_page, working_context
from civ_agent.protocol import JsonObject
from civ_agent.schemas import BoundaryModel

Text = Annotated[str, Field(min_length=1, max_length=2000)]
ROLE_TOOLS = frozenset({"war_goals", "escort_assignment", "military_operation", "role_message"})
MILITARY_COMMANDS = frozenset(
    {
        "MOVE_UNIT",
        "SET_UNIT_DESTINATION",
        "CANCEL_UNIT_MOVEMENT",
        "REBASE_UNIT",
        "PARADROP",
        "AIR_SWEEP",
        "ATTACK",
        "CITY_ATTACK",
        "SET_UNIT_ORDER",
        "SWAP_UNITS",
        "RENAME_UNIT",
        "SAVE_UNIT_PROMOTIONS",
        "PROMOTE_UNIT",
        "UPGRADE_UNIT",
        "PILLAGE",
        "DISBAND_UNIT",
    }
)


class WarGoal(BoundaryModel):
    outcome: Text
    priority: int = Field(default=1, ge=1, le=10, description="1 is the highest priority; 10 is the lowest.")
    completion: Text
    acceptableCosts: Text
    reconsiderWhen: Text


class ReadWarGoals(BoundaryModel):
    action: Literal["read"] = "read"
    section: Literal[
        "current", "episodes", "drafts", "escorts", "operations", "operation_history", "messages", "requests"
    ] = "current"
    ids: list[Text] = Field(default_factory=list, max_length=20)
    cursor: int = Field(default=0, ge=0)
    limit: int = Field(default=10, ge=1, le=20)
    status: Literal[
        "all", "active", "completed", "abandoned", "superseded", "legacy", "unread", "open", "acknowledged", "resolved"
    ] = "all"
    role: Literal["gameplay", "economy", "military"] | None = None


class InitialWarGoals(BoundaryModel):
    action: Literal["initial"]
    opponent: Text
    goals: list[WarGoal] = Field(min_length=1, max_length=20)
    reason: Text


class WarGoalRevision(BoundaryModel):
    opponent: Text
    version: int = Field(ge=0, description="The accepted goal version read from war_goals.")


class ProposeWarGoals(WarGoalRevision):
    action: Literal["propose"]
    goals: list[WarGoal] = Field(min_length=1, max_length=20)
    reason: Text


class RespondWarGoals(WarGoalRevision):
    action: Literal["respond"]
    proposal: Text
    agree: bool
    reason: Text


class FinalizeWarGoals(WarGoalRevision):
    action: Literal["finalize"]
    proposal: Text
    reason: str = Field(
        default="", max_length=2000, description="Main must explain a decision overriding disagreement."
    )


class WarGoals(
    RootModel[
        Annotated[
            ReadWarGoals | InitialWarGoals | ProposeWarGoals | RespondWarGoals | FinalizeWarGoals,
            Field(discriminator="action"),
        ]
    ]
):
    model_config = {"json_schema_extra": {"type": "object"}}

    @model_validator(mode="before")
    @classmethod
    def default_read(cls, value):
        if isinstance(value, dict) and "action" not in value:
            return {"action": "read", **value}
        return value


class EscortAssignment(BoundaryModel):
    unit: Text
    civilian: str = ""
    reason: Text
    release: bool = False


class UpdateMilitaryOperation(BoundaryModel):
    action: Literal["upsert"] = "upsert"
    id: Text = Field(description="Stable operation ID. Update this ID across turns; do not create a turn journal.")
    opponents: list[str] = Field(default_factory=list, max_length=20)
    plan: Text
    progress: Text
    reconsiderWhen: Text
    sources: list[str] = Field(default_factory=list, max_length=20)
    reviewTurn: int | None = Field(default=None, ge=0)
    status: Literal["active", "completed", "abandoned", "superseded"] = "active"
    supersedes: list[Text] = Field(default_factory=list, max_length=20)


class SetOperationStatus(BoundaryModel):
    action: Literal["set_status"]
    ids: list[Text] = Field(min_length=1, max_length=20)
    status: Literal["active", "completed", "abandoned", "superseded"]
    reason: Text


class MilitaryOperation(
    RootModel[Annotated[UpdateMilitaryOperation | SetOperationStatus, Field(discriminator="action")]]
):
    model_config = {"json_schema_extra": {"type": "object"}}

    @model_validator(mode="before")
    @classmethod
    def default_update(cls, value):
        if isinstance(value, dict) and "action" not in value:
            return {"action": "upsert", **value}
        return value


class SendRoleMessage(BoundaryModel):
    action: Literal["send"] = "send"
    to: Literal["gameplay", "military", "economy"]
    text: Text
    kind: Literal["notice", "request"] = "notice"


class AcknowledgeRoleMessages(BoundaryModel):
    action: Literal["acknowledge"]
    ids: list[Text] = Field(min_length=1, max_length=50)


class ResolveRoleMessages(BoundaryModel):
    action: Literal["resolve"]
    ids: list[Text] = Field(min_length=1, max_length=50)
    reason: Text


class RoleMessage(
    RootModel[Annotated[SendRoleMessage | AcknowledgeRoleMessages | ResolveRoleMessages, Field(discriminator="action")]]
):
    model_config = {"json_schema_extra": {"type": "object"}}

    @model_validator(mode="before")
    @classmethod
    def default_send(cls, value):
        if isinstance(value, dict) and "action" not in value:
            return {"action": "send", **value}
        return value


def war_opponents(view):
    diplomacy = view.get("diplomacy") or {}
    return {
        row["civilizationId"]
        for key in ("knownCivilizations", "cityStates")
        for row in diplomacy.get(key, [])
        if row.get("atWar")
        and not row.get("defeated")
        and str(row.get("status", "")).lower() != "defeated"
        and row.get("civilizationId") not in {None, "Barbarians", "Spectator"}
    }


def tactical_basis(view, state, terms):
    """Ignore changing review/read IDs; include all permitted tactical facts and exact terms."""
    facts = {
        key: view.get(key)
        for key in (
            "turn",
            "phase",
            "units",
            "cities",
            "map",
            "militaryReadiness",
            "diplomacy",
            "economy",
        )
    }
    episodes = {k: v for k, v in state["episodes"].items() if v["active"]}
    value = {"facts": facts, "episodes": episodes, "terms": terms}
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def goal_versions(state):
    return {key: episode["version"] for key, episode in state["episodes"].items() if episode.get("active", True)}


class WarStore:
    def __init__(self, memory):
        self.memory = memory

    def _load(self, db):
        state = self.memory._meta(
            db,
            "war:state",
            {
                "episodes": {},
                "drafts": {},
                "escorts": {},
                "operations": {},
                "messages": [],
                "receipts": {},
            },
        )
        for name in ("episodes", "drafts", "escorts", "operations", "receipts"):
            state.setdefault(name, {})
        state.setdefault("messages", [])
        if state.get("schemaVersion", 1) < 2:
            # Legacy notes have no lifecycle. Preserve them for explicit model
            # reconciliation; elapsed time never establishes their disposition.
            for key, operation in state["operations"].items():
                operation.setdefault("id", key)
                operation.setdefault("status", "legacy")
                operation.setdefault("revision", 1)
            state["operationHistory"] = {}
            state["schemaVersion"] = 2
            self.memory._set(db, "war:state", state)
        return state

    def state(self):
        with self.memory.transaction() as db:
            value = self._load(db)
            value.pop("receipts", None)
            return value

    def read(self, role, arguments, requests=()) -> JsonObject:
        with self.memory.transaction() as db:
            return self._read(self._load(db), role, arguments, requests)

    @staticmethod
    def _read(state, role, arguments, requests=()) -> JsonObject:
        section = arguments.get("section", "current")
        if section == "current":
            projected = working_context({"warState": state, "requests": list(requests)}, role)
            return {**projected["warState"], "history": projected["history"]}
        if section == "requests":
            rows = list(requests)
        elif section == "messages":
            rows = list(state["messages"])
        elif section == "operation_history":
            rows = [
                {"id": key, **revision}
                for key, revisions in sorted(state.get("operationHistory", {}).items())
                for revision in revisions
            ]
        else:
            rows = [{"id": key, **value} for key, value in sorted(state[section].items())]
        ids, status, recipient = arguments.get("ids", []), arguments.get("status", "all"), arguments.get("role")
        if ids:
            rows = [r for r in rows if r.get("id") in ids]
        if recipient:
            rows = [r for r in rows if r.get("to") == recipient or r.get("author") == recipient]
        if status != "all":

            def matches(row):
                if section == "messages":
                    return {
                        "unread": not row.get("acknowledged", False),
                        "open": row.get("kind") == "request" and not row.get("resolved", False),
                        "acknowledged": row.get("acknowledged", False),
                        "resolved": row.get("resolved", False),
                    }.get(status, False)
                if section == "episodes":
                    return {"active": row.get("active", False), "completed": not row.get("active", False)}.get(
                        status, False
                    )
                return row.get("status", "legacy") == status

            rows = [r for r in rows if matches(r)]
        page = history_page(rows, arguments.get("cursor", 0), arguments.get("limit", 10))
        return {"section": section, **page}

    def synchronize(self, epoch, view):
        with self.memory.transaction() as db:
            if self.memory._meta(db, "epoch") != epoch:
                raise ValueError("STALE_EPOCH")
            state = self._load(db)
            current = war_opponents(view)
            active = {v["opponent"]: v for v in state["episodes"].values() if v["active"]}
            for opponent, episode in active.items():
                if opponent not in current:
                    episode.update(active=False, endedTurn=view.get("turn"))
            for opponent in sorted(current - active.keys()):
                identifier = "war" + secrets.token_hex(6)
                draft = state["drafts"].pop(opponent, None)
                state["episodes"][identifier] = {
                    "id": identifier,
                    "opponent": opponent,
                    "active": True,
                    "startedTurn": view.get("turn"),
                    "version": 1 if draft else 0,
                    "goals": draft["goals"] if draft else [],
                    "history": [draft] if draft else [],
                    "proposals": {},
                }
            # Native escort formations already in progress stay with main, even after unpairing.
            units = {u["handle"]: u for u in view.get("units", (view.get("map") or {}).get("units", []))}
            for handle, unit in units.items():
                escort = unit.get("escort") or {}
                if unit.get("military") and escort.get("active") and handle not in state["escorts"]:
                    state["escorts"][handle] = {
                        "unit": handle,
                        "civilian": escort["partner"],
                        "reason": "Inherited native escort duty.",
                    }
            state["escorts"] = {k: v for k, v in state["escorts"].items() if k in units}
            self.memory._set(db, "war:state", state)
        return self.state()

    def apply(self, epoch, role, name, args, identity, turn=None) -> JsonObject:
        """A tool receipt and its mutation commit together, including exact retries."""
        with self.memory.transaction() as db:
            if self.memory._meta(db, "epoch") != epoch:
                raise ValueError("STALE_EPOCH")
            state = self._load(db)
            fingerprint = json.dumps([role, name, args], sort_keys=True, separators=(",", ":"))
            if identity in state["receipts"]:
                receipt = state["receipts"][identity]
                if receipt["fingerprint"] != fingerprint:
                    return {"error": "REQUEST_ID_CONFLICT", "applied": False}
                return receipt["result"]
            result = self._apply(state, role, name, args, turn)
            if "error" not in result:
                state["receipts"][identity] = {"fingerprint": fingerprint, "result": copy.deepcopy(result)}
                self.memory._set(db, "war:state", state)
            return result

    def _apply(self, state, role, name, args, turn=None) -> JsonObject:
        def error(code):
            return {"error": code, "applied": False}

        if name == "war_goals":
            action = args.get("action", "read")
            if action == "read":
                return self._read(state, role, args)
            if role not in {"gameplay", "military"}:
                return error("MAIN_OR_MILITARY_REQUIRED")
            opponent = args.get("opponent")
            episode = next((v for v in state["episodes"].values() if v["active"] and v["opponent"] == opponent), None)
            if action == "initial":
                if role != "gameplay" or not opponent or not args.get("goals") or not args.get("reason"):
                    return error("MAIN_INITIAL_GOALS_REQUIRED")
                revision = {"goals": args["goals"], "reason": args["reason"], "author": role}
                if episode is None:
                    state["drafts"][opponent] = revision
                elif episode["version"] == 0:
                    episode.update(version=1, goals=args["goals"])
                    episode["history"].append(revision)
                else:
                    return error("GOAL_DISCUSSION_REQUIRED")
                return {"initialGoals": revision, "opponent": opponent}
            if episode is None:
                return error("ACTIVE_WAR_REQUIRED")
            if args.get("version") != episode["version"]:
                return error("STALE_GOAL_VERSION")
            if action == "propose":
                if not args.get("goals") or not args.get("reason"):
                    return error("GOALS_AND_REASON_REQUIRED")
                identifier = "goal" + secrets.token_hex(6)
                proposal = {
                    "id": identifier,
                    "version": episode["version"],
                    "author": role,
                    "goals": args["goals"],
                    "reason": args["reason"],
                    "responses": {},
                    "finalized": False,
                }
                episode["proposals"][identifier] = proposal
                return {"proposal": copy.deepcopy(proposal)}
            proposal = episode["proposals"].get(args.get("proposal"))
            if proposal is None or proposal["finalized"] or proposal["version"] != episode["version"]:
                return error("CURRENT_GOAL_PROPOSAL_REQUIRED")
            if action == "respond":
                if role == proposal["author"] or not args.get("reason"):
                    return error("COUNTERPART_RESPONSE_REQUIRED")
                proposal["responses"][role] = {"agree": args.get("agree", False), "reason": args["reason"]}
                return {"proposal": copy.deepcopy(proposal)}
            counterpart = "military" if proposal["author"] == "gameplay" else "gameplay"
            response = proposal["responses"].get(counterpart)
            if response is None:
                return error("COUNTERPART_RESPONSE_REQUIRED")
            if not response["agree"] and (role != "gameplay" or not args.get("reason")):
                return error("MAIN_TIE_BREAK_REQUIRED")
            proposal["finalized"] = True
            episode["version"] += 1
            episode["goals"] = proposal["goals"]
            episode["history"].append(
                {**copy.deepcopy(proposal), "decidedBy": role, "decisionReason": args.get("reason", "Agreed.")}
            )
            return {"episode": copy.deepcopy(episode)}
        if name == "escort_assignment":
            if role != "gameplay":
                return error("MAIN_PLAYER_DECISION_REQUIRED")
            if args.get("release"):
                state["escorts"].pop(args["unit"], None)
            else:
                state["escorts"][args["unit"]] = args
            return {"escorts": copy.deepcopy(state["escorts"])}
        if name == "military_operation":
            if role != "military":
                return error("MILITARY_REQUIRED")
            if args.get("action") == "set_status":
                if any(key not in state["operations"] for key in args["ids"]):
                    return error("UNKNOWN_OPERATION")
                for key in args["ids"]:
                    self._operation(
                        state,
                        {**state["operations"][key], "status": args["status"], "statusReason": args["reason"]},
                        turn,
                    )
                return {"updated": args["ids"], "status": args["status"]}
            supersedes = args.get("supersedes", [])
            if args["id"] in supersedes or any(key not in state["operations"] for key in supersedes):
                return error("UNKNOWN_SUPERSEDED_OPERATION")
            operation = {k: copy.deepcopy(v) for k, v in args.items() if k not in {"action", "supersedes"}}
            operation.setdefault("status", "active")
            self._operation(state, operation, turn)
            for key in supersedes:
                self._operation(
                    state, {**state["operations"][key], "status": "superseded", "supersededBy": args["id"]}, turn
                )
            return {"operation": copy.deepcopy(state["operations"][args["id"]])}
        if name == "role_message":
            if args.get("action") in {"acknowledge", "resolve"}:
                selected = [m for m in state["messages"] if m["id"] in args["ids"]]
                missing = sorted(set(args["ids"]) - {m["id"] for m in selected})
                if missing:
                    return {
                        **error("UNKNOWN_ROLE_MESSAGE"),
                        "unknownIds": missing,
                        "recoverable": True,
                        "recovery": {
                            "reads": [
                                {
                                    "tool": "war_goals",
                                    "arguments": {"action": "read", "section": "messages", "role": role},
                                }
                            ]
                        },
                        "instruction": "Use IDs from role messages addressed to you. Economy request IDs are handled "
                        "by Economy in the finish decision's arguments.requests, not by role_message. "
                        "No messages were changed.",
                    }
                if any(m.get("to") != role for m in selected):
                    return {
                        **error("MESSAGE_RECIPIENT_REQUIRED"),
                        "recipients": {m["id"]: m["to"] for m in selected},
                        "recoverable": True,
                        "instruction": "Only a message's addressed recipient may acknowledge or resolve it. "
                        "You may send that role a follow-up message when needed. No messages were changed.",
                    }
                for message in selected:
                    message["acknowledged"] = True
                    if args["action"] == "resolve":
                        message.update(resolved=True, resolution=args["reason"])
                return {"messageIds": args["ids"], "action": args["action"]}
            if role not in {"gameplay", "military", "economy"} or args["to"] == role:
                return error("OTHER_ROLE_REQUIRED")
            message = {
                "id": "msg" + secrets.token_hex(6),
                "author": role,
                **{k: v for k, v in args.items() if k != "action"},
                "createdTurn": turn,
            }
            state["messages"].append(message)
            return {"message": message}
        return error("ROLE_ACTION_NOT_PERMITTED")

    @staticmethod
    def _operation(state, operation, turn):
        key = operation["id"]
        previous = state["operations"].get(key)
        if previous is not None:
            state.setdefault("operationHistory", {}).setdefault(key, []).append(copy.deepcopy(previous))
        operation["revision"] = (previous or {}).get("revision", 0) + 1
        operation["updatedTurn"] = turn
        state["operations"][key] = operation


def declaration_targets(civilization, name, payload, selected, view):
    """All voluntary declarations, including native replies and canonical trade clauses."""
    targets = set()
    if name == "player_command" and (
        payload.get("kind") == "DECLARE_WAR"
        or payload.get("kind") == "CITY_STATE_ACTION"
        and payload.get("selection") == "declareWar"
    ):
        targets.add(payload.get("target"))
    if name == "trade_offer":
        targets.update(item.get("name") for item in payload.get("give", []) if item.get("type") == "WarDeclaration")
    if name == "trade_decide" and payload.get("decision") == "accept":
        targets.update(
            clause["item"].get("name")
            for clause in (selected or {}).get("clauses", [])
            if clause.get("from") == civilization and clause["item"].get("type") == "WarDeclaration"
        )
    action = (
        payload.get("nativeAction")
        if name == "chat_send"
        else payload
        if payload.get("kind") == "RESPOND_CITY_STATE_ALERT"
        else None
    )
    if action:
        for alert in view.get("alerts", []):
            decision = alert.get("nativeDiplomacy") or {}
            if alert.get("handle") == action.get("target") and any(
                o.get("name") == action.get("selection") and o.get("declaresWar") for o in decision.get("options", [])
            ):
                targets.add(decision["civilizationId"])
    return targets - {None}
