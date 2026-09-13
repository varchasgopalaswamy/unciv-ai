"""Job-scoped HTTPS tools. Only this trusted controller holds host credentials."""

import asyncio
import copy
import hmac
import json
import secrets
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

import aiohttp
from aiohttp import web

from civ_agent.civilization_state import CivilizationState, StaleAssumptions, StateConflict, encode
from civ_agent.protocol import JsonObject
from civ_agent.service_io import ServiceIO
from civ_agent.tls import client_ssl_context, https_url

BOOKKEEPING = {"requestId", "epoch", "revision", "reviewId", "socialVersion"}
READ_TOOLS = {
    "player_capabilities",
    "player_turn",
    "player_result",
    "research_details",
    "policy_details",
    "player_summary",
    "owned_cities",
    "free_great_person_choice",
}
COMMUNICATION_READ_TOOLS = {"player_turn", "player_result"}
RECOVERY_INSTRUCTIONS = (
    "STALE_ASSUMPTIONS applied nothing: follow recovery.reads, reconsider only the affected action, "
    "then make a fresh decision or skip it if another job already handled it. "
    "If it conflicts again, defer that action and continue other work; do not stop the gameplay turn for this. "
)
HOSTILE = {"ATTACK", "CITY_ATTACK", "DECLARE_WAR"}
ECONOMIC = {
    "BUY_CONSTRUCTION",
    "BUY_TILE",
    "SELL_BUILDING",
    "UPGRADE_UNIT",
    "DISBAND_UNIT",
    "QUEUE_CONSTRUCTION",
    "FOUND_CITY",
    "ACTIVATE_GREAT_PERSON",
}


def public_result(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: public_result(
                {name: part for name, part in item.items() if name != "id"}
                if key == "turnReview" and isinstance(item, dict)
                else item
            )
            for key, item in value.items()
            if key not in BOOKKEEPING and key not in {"peaceVersion", "_evidence"}
        }
    if isinstance(value, list):
        return [public_result(item) for item in value]
    return value


def tool_result(value: JsonObject) -> JsonObject:
    return {
        "content": [{"type": "text", "text": encode(value)}],
        "structuredContent": value,
        "isError": "error" in value,
    }


class HostConnection:
    def __init__(self, url: str, token: str, ca_file: Path | None):
        self.url, self.token = https_url(url, ("/mcp",)), token
        self.ssl = client_ssl_context(ca_file)
        self.sequence = 0

    async def rpc(self, method: str, parameters: JsonObject) -> JsonObject:
        self.sequence += 1
        request = {"jsonrpc": "2.0", "id": self.sequence, "method": method, "params": parameters}
        async with aiohttp.ClientSession(trust_env=False, timeout=aiohttp.ClientTimeout(total=35)) as session:
            async with session.post(
                self.url,
                json=request,
                ssl=self.ssl,
                allow_redirects=False,
                headers={
                    "Authorization": "Bearer " + self.token,
                    "Accept": "application/json, text/event-stream",
                    "MCP-Protocol-Version": "2025-06-18",
                },
            ) as response:
                if response.status != 200:
                    raise StateConflict("HOST_UNAVAILABLE")
                body = await response.text()
                if response.content_type == "text/event-stream":
                    messages = [json.loads(line[5:].strip()) for line in body.splitlines() if line.startswith("data:")]
                    payload = next((value for value in messages if value.get("id") == request["id"]), {})
                else:
                    payload = json.loads(body)
                if "error" in payload or "result" not in payload:
                    raise StateConflict("HOST_PROTOCOL_ERROR")
                return payload["result"]

    async def tool(self, name: str, arguments: JsonObject) -> JsonObject:
        result = await self.rpc("tools/call", {"name": name, "arguments": arguments})
        value = result.get("structuredContent")
        if value is None:
            value = json.loads(next(item["text"] for item in result["content"] if item.get("type") == "text"))
        if not isinstance(value, dict):
            raise StateConflict("HOST_PROTOCOL_ERROR")
        return value


class PlayerConnection(Protocol):
    async def rpc(self, method: str, parameters: JsonObject, /) -> JsonObject: ...
    async def tool(self, name: str, arguments: JsonObject, /) -> JsonObject: ...


@dataclass
class JobAccess:
    id: str
    purpose: str
    epoch: str
    connection: PlayerConnection
    memory: JsonObject
    token: str = field(default_factory=lambda: secrets.token_urlsafe(32))
    observation: JsonObject | None = None
    social: JsonObject | None = None
    trades: dict[str, JsonObject] = field(default_factory=dict)
    closed: bool = False
    verified: bool = False
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


class ProposalGateway:
    def __init__(self, memory: CivilizationState, io: ServiceIO, workspace: Path):
        self.memory, self.io, self.workspace = memory, io, workspace
        self.jobs: dict[str, JobAccess] = {}
        self.paused = False
        self.dispatch = asyncio.Lock()
        self.in_flight = 0
        self.deferred_completions = []

    async def open(
        self,
        id: str,
        purpose: str,
        epoch: str,
        connection: PlayerConnection,
        settings: JsonObject,
        game_turn: int | None = None,
    ) -> JobAccess:
        await self.io.call(self.memory.bind, epoch)
        snapshot = await self.io.call(self.memory.start_job, id, purpose, settings, game_turn)
        job = JobAccess(id, purpose, epoch, connection, snapshot)
        self.jobs[job.token] = job
        return job

    async def close(self, job: JobAccess, completed: bool):
        job.closed = True
        self.jobs.pop(job.token, None)
        if self.paused:
            self.deferred_completions.append((job.epoch, job.id, completed))
            return
        await self.io.call(self.memory.finish_job, job.id, completed)
        await self.io.call(self.memory.export, self.workspace)

    def check(self, job: JobAccess):
        if self.paused or job.closed or self.jobs.get(job.token) is not job:
            raise StateConflict("JOB_PAUSED_OR_EXPIRED")

    async def tools(self, job: JobAccess):
        upstream = (await job.connection.rpc("tools/list", {}))["tools"]
        tools = []
        for tool in upstream:
            if tool["name"] in READ_TOOLS and (job.purpose == "gameplay" or tool["name"] in COMMUNICATION_READ_TOOLS):
                entry = copy.deepcopy(tool)
                if tool["name"] == "player_turn":
                    entry["description"] = (
                        "Read current permitted game state, full popups and diplomatic terms, legal action choices, and turnReview. Re-observe after each command. Read confirmation consequences before acknowledging them."
                        if job.purpose == "gameplay"
                        else "Read the full permitted gameplay view only when a question needs map, city or other detail absent from chat_read. Routine negotiation uses chat_read, trade_read and trade_options."
                    )
                tools.append(entry)
            elif tool["name"] == "player_command" and job.purpose == "gameplay":
                entry = copy.deepcopy(tool)
                schema = entry["inputSchema"]
                schema["properties"] = {
                    key: value for key, value in schema["properties"].items() if key not in BOOKKEEPING
                }
                schema["required"] = [key for key in schema.get("required", []) if key not in BOOKKEEPING]
                schema["additionalProperties"] = False
                entry["description"] = (
                    "Propose one offered game action. The service handles authentication, request identity, observation versions and retries. Re-read player_turn after each action and read full alerts and confirmations before acknowledging. END_TURN requires reading turnReview and a deferReason for intentionally idle mobile units. Supply gameplay choices only."
                )
                tools.append(entry)

        def add(name, description, properties, required=()):
            tools.append(
                {
                    "name": name,
                    "description": description,
                    "annotations": {
                        "readOnlyHint": name
                        in {"history_read", "chat_read", "chat_history", "memory_read", "trade_options", "trade_read"},
                        "destructiveHint": False,
                        "openWorldHint": False,
                    },
                    "inputSchema": {
                        "type": "object",
                        "properties": properties,
                        "required": list(required),
                        "additionalProperties": False,
                    },
                }
            )

        string = {"type": "string"}
        strings = {"type": "array", "items": string}
        trade_items = {
            "type": "array",
            "maxItems": 64,
            "items": {
                "type": "object",
                "properties": {
                    "type": {"enum": ["Gold", "Gold_Per_Turn", "Luxury_Resource", "Strategic_Resource", "Treaty"]},
                    "name": string,
                    "amount": {"type": "integer", "minimum": 1},
                    "duration": {"type": "integer"},
                },
                "required": ["type", "name", "amount", "duration"],
                "additionalProperties": False,
            },
        }
        add(
            "trade_options",
            "Inspect trade quantities, exact names/durations, peace eligibility and active agreements with a met player. At war, requiresPeaceTreaty is true: include the offered Treaty named Peace Treaty on both sides. give/receive are from your perspective. Available during either player's turn.",
            {"partner": string},
            ("partner",),
        )
        add(
            "trade_read",
            "Read a host-owned binding trade before accepting, declining, withdrawing or counteroffering. Every clause has an explicit from/to direction, identical for both players. Pending deal IDs appear in chat_read. Re-read after changed terms; never poll.",
            {"proposal": string},
            ("proposal",),
        )
        add(
            "trade_offer",
            "Publish binding economic or peace terms and your consent, using trade_options names and durations. At war include exactly one offered Peace Treaty on each side; other treaties are unsupported. give/receive are from YOUR perspective. Empty one side for a gift. Set proposal to counteroffer or revise a deal you inspected with trade_read; this replaces its terms and requires fresh consent. expiresTurn defaults to current turn + 3. No transfer occurs until the other player accepts.",
            {
                "partner": string,
                "give": trade_items,
                "receive": trade_items,
                "expiresTurn": {"type": "integer"},
                "proposal": string,
            },
            ("partner", "give", "receive"),
        )
        add(
            "trade_decide",
            "Accept or decline the exact trade you inspected with trade_read; an author can withdraw it. Supply only its ID and decision. Acceptance rechecks legality and transfers the stored terms atomically, including outside your gameplay turn. Only an ENACTED result with an engine outcome confirms execution. Changed, expired or unavailable terms require a new decision.",
            {"proposal": string, "decision": {"enum": ["accept", "decline", "withdraw"]}},
            ("proposal", "decision"),
        )
        add(
            "history_read",
            "Inspect confirmed actions and detailed receipts from one earlier game turn when needed. Turn numbers are those shown by player_turn.",
            {"turn": {"type": "integer", "minimum": 0}, "offset": {"type": "integer", "minimum": 0}},
            ("turn",),
        )
        add(
            "chat_read",
            "Read compact current context (turn, your scenario instructions, economy and known diplomatic relations), messages, social commitments and binding trade IDs once. Inspect a binding proposal with trade_read before deciding. Incoming private messages, trades and public mentions wake communication work automatically; never poll or wait for replies.",
            {},
        )
        add(
            "chat_history",
            "Find earlier permitted messages and commitments. Returns the most recent 20 matches; narrow query for older evidence. No polling is needed.",
            {"with": string, "query": string},
        )
        add(
            "chat_send",
            "Send authenticated text. Omit to for public chat; mention players explicitly to notify them. This creates no in-game contact.",
            {"to": string, "text": string, "mentions": strings},
            ("text",),
        )
        add(
            "commitment_offer",
            "Propose exact social terms. This is not an engine trade or peace treaty. Everyone must explicitly consent. kind=peace fences conflicting attacks until deliberate reconsideration. conditions describes when the promise applies; use 'Unconditional' if applicable.",
            {
                "participants": strings,
                "terms": string,
                "conditions": string,
                "kind": {"enum": ["peace", "other"]},
                "expiresTurn": {"type": "integer"},
                "sources": strings,
                "proposal": string,
            },
            ("participants", "terms", "conditions", "kind"),
        )
        add(
            "commitment_decide",
            "Accept terms you read with chat_read, withdraw consent, or explicitly reconsider a promise before breaking it. Changing terms requires a new offer and consent. No engine action is implicit.",
            {"proposal": string, "decision": {"enum": ["accept", "withdraw", "reconsider"]}, "reason": string},
            ("proposal", "decision"),
        )
        add(
            "memory_read",
            "Read selected private records. Observations are evidence at an earlier time; beliefs include uncertainty; player statements do not establish world facts.",
            {"keys": strings},
            ("keys",),
        )
        add(
            "memory_update",
            "Propose private notes; the service merges only records that have not changed since you read them. keys: SOUL, STRATEGY, economy, relationships/<civilization>, note/<name>. category: policy, plan, belief. body.text is concise prose. Beliefs require sources (message IDs read with chat_read, observation:game for the full player_turn result, or observation:communication for the ledger read with chat_read) and uncertainty=low/medium/high. Notes cannot replace authoritative observations or commitments.",
            {
                "changes": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "key": string,
                            "category": {"enum": ["policy", "plan", "belief"]},
                            "body": {"type": "object"},
                        },
                        "required": ["key", "category", "body"],
                    },
                }
            },
            ("changes",),
        )
        return tools

    async def read_social(self, job: JobAccess):
        result = await job.connection.tool("player_communication", {"op": "state", "epoch": job.epoch})
        if "error" in result:
            raise StateConflict("COMMUNICATION_UNAVAILABLE")
        self.check(job)
        version = await self.io.call(self.memory.observe, "communication", result, job.id)
        job.memory["records"]["observation:communication"] = {
            "version": version,
            "category": "observation",
            "body": result,
        }
        job.social = result
        return result

    async def call(self, job: JobAccess, name: str, arguments: JsonObject, call_id: str) -> JsonObject:
        self.check(job)
        if not isinstance(name, str) or not isinstance(arguments, dict):
            raise ValueError("INVALID_ARGUMENT")
        async with job.lock:
            previous = await self.io.call(self.memory.call, job.id, call_id, name, arguments)
            if previous is not None:
                return previous
            # The action receipt may be durable even if a barrier interrupted
            # saving the MCP response. Replay it without needing a new observation.
            receipt = await self.io.call(self.memory.call_receipt, job.id, call_id)
            if receipt is not None:
                return public_result(receipt)
            try:
                result = await self._call(job, name, arguments, call_id)
            except StaleAssumptions as conflict:
                # Nothing was queued or sent. Cache the rejection too: replaying
                # this call after a read must never execute an old decision.
                reads = []
                private = [key for key in conflict.keys if not key.startswith("observation:")]
                if private:
                    reads.append({"tool": "memory_read", "arguments": {"keys": private}})
                if any(key.startswith("observation:") for key in conflict.keys):
                    reads.append({"tool": "player_turn", "arguments": {}})
                if name in {"trade_offer", "trade_decide"}:
                    reads.append(
                        {"tool": "trade_read", "arguments": {"proposal": arguments["proposal"]}}
                        if "proposal" in arguments
                        else {"tool": "trade_options", "arguments": {"partner": arguments["partner"]}}
                    )
                result = {
                    "error": "STALE_ASSUMPTIONS",
                    "applied": False,
                    "recoverable": True,
                    "changedRecords": conflict.keys,
                    "recovery": {"reads": reads, "instruction": RECOVERY_INSTRUCTIONS},
                }
            await self.io.call(self.memory.call, job.id, call_id, name, arguments, result)
            return result

    async def _call(self, job: JobAccess, name: str, arguments: JsonObject, call_id: str) -> JsonObject:
        self.check(job)
        if not job.verified:
            identity = await job.connection.tool("player_communication", {"op": "state", "epoch": job.epoch})
            if identity.get("civilization") != self.memory.civilization or identity.get("epoch") != job.epoch:
                raise StateConflict("PLAYER_CREDENTIAL_MISMATCH")
            job.verified = True
        if BOOKKEEPING & arguments.keys():
            raise ValueError("Supply gameplay choices only; the service manages request bookkeeping.")
        if name == "history_read":
            if set(arguments) - {"turn", "offset"}:
                raise ValueError("INVALID_ARGUMENT")
            return public_result(await self.io.call(self.memory.history, arguments["turn"], arguments.get("offset", 0)))
        self.check(job)
        if name in {"trade_options", "trade_read"}:
            key = "partner" if name == "trade_options" else "proposal"
            if set(arguments) != {key} or not isinstance(arguments[key], str):
                raise ValueError("INVALID_ARGUMENT")
            value = await job.connection.tool("player_communication", {**arguments, "op": name, "epoch": job.epoch})
            self.check(job)
            if "error" not in value:
                scope = ("trade_options/" if name == "trade_options" else "trade/") + arguments[key]
                version = await self.io.call(self.memory.observe, scope, value, job.id)
                job.memory["records"]["observation:" + scope] = {
                    "version": version,
                    "category": "observation",
                    "body": value,
                }
                if name == "trade_read":
                    job.trades[arguments["proposal"]] = copy.deepcopy(value["trade"])
            return public_result(value)
        if name in READ_TOOLS:
            if job.purpose == "communication" and name not in COMMUNICATION_READ_TOOLS:
                raise StateConflict("TOOL_NOT_PERMITTED")
            value = await job.connection.tool(name, arguments)
            self.check(job)
            if name == "player_turn" and "error" not in value:
                job.observation = None
                # Persist the complete interface evidence returned to this job,
                # including attributed social records and confirmed outcomes.
                value = {**value, "communication": await self.read_social(job)}
                observed = await self.io.call(self.memory.observe_player, value, job.id)
                job.memory["records"].update(observed)
                job.observation = value
            return public_result(value)
        if name == "chat_read":
            if arguments:
                raise ValueError("INVALID_ARGUMENT")
            social = await self.read_social(job)
            recent = await self.io.call(self.memory.snapshot, job.purpose)
            events = recent["events"]
            await self.io.call(
                self.memory.refresh_memory, job.id, ["event:" + str(event["id"]) for event in events], recent["cursor"]
            )
            return public_result({**social, "messages": events})
        if name == "chat_history":
            if set(arguments) - {"with", "query"}:
                raise ValueError("INVALID_ARGUMENT")
            page = await self.io.call(self.memory.chat_history, arguments.get("with"), arguments.get("query", ""))
            await self.io.call(
                self.memory.refresh_memory, job.id, ["event:" + str(event["id"]) for event in page["events"]]
            )
            return public_result(page)
        if name == "memory_read":
            if (
                set(arguments) != {"keys"}
                or not isinstance(arguments["keys"], list)
                or len(arguments["keys"]) > 20
                or any(not isinstance(key, str) for key in arguments["keys"])
            ):
                raise ValueError("INVALID_ARGUMENT")
            chosen = await self.io.call(self.memory.refresh_memory, job.id, arguments["keys"])
            job.memory["records"].update(chosen)
            return public_result(
                {key: {"category": record["category"], "body": record["body"]} for key, record in chosen.items()}
            )
        if name == "memory_update":
            if (
                set(arguments) != {"changes"}
                or not isinstance(arguments["changes"], list)
                or len(arguments["changes"]) > 20
            ):
                raise ValueError("INVALID_ARGUMENT")
            result = await self.io.call(self.memory.patch, job.id, arguments["changes"])
            accepted = [change["key"] for change in result["changes"] if change["accepted"]]
            refreshed = await self.io.call(self.memory.refresh_memory, job.id, accepted)
            job.memory["records"].update(refreshed)
            await self.io.call(self.memory.export, self.workspace)
            return result
        if name == "player_command":
            if job.purpose != "gameplay":
                raise StateConflict("GAMEPLAY_NOT_PERMITTED")
            if job.observation is None or job.social is None:
                raise StateConflict("Read player_turn and its full content before proposing another action.")
            payload = {**arguments, "epoch": job.epoch, "revision": job.observation["revision"]}
            if arguments.get("kind") == "END_TURN":
                payload["reviewId"] = job.observation.get("turnReview", {}).get("id")
            if arguments.get("kind") in HOSTILE:
                payload["socialVersion"] = job.social["peaceVersion"]
            transport = "player_command"
        elif name in {"trade_offer", "trade_decide"}:
            allowed = (
                {"partner", "give", "receive", "expiresTurn", "proposal"}
                if name == "trade_offer"
                else {"proposal", "decision"}
            )
            required = {"partner", "give", "receive"} if name == "trade_offer" else allowed
            if set(arguments) - allowed or not required <= arguments.keys():
                raise ValueError("INVALID_ARGUMENT")
            if "proposal" in arguments and not isinstance(arguments["proposal"], str):
                raise ValueError("INVALID_ARGUMENT")
            payload = {**arguments, "op": name, "epoch": job.epoch}
            if "proposal" in arguments:
                selected = job.trades.get(arguments["proposal"])
                if selected is None:
                    raise StateConflict("Read this proposal's exact terms with trade_read before deciding.")
                payload["version"] = selected["version"]
            transport = "player_communication"
        elif name in {"chat_send", "commitment_offer", "commitment_decide"}:
            payload = {**arguments, "epoch": job.epoch}
            if name == "chat_send":
                payload["op"] = "send"
            elif name == "commitment_offer":
                payload["op"] = "revise" if arguments.get("proposal") else "offer"
            else:
                payload["op"] = payload.pop("decision")
                if job.social is None:
                    raise StateConflict("Read the current terms with chat_read before deciding.")
                offers = [offer for offer in job.social["offers"] if offer["proposal"] == arguments.get("proposal")]
                if not offers:
                    raise StateConflict("PROPOSAL_NOT_OBSERVED")
                selected = (
                    offers[-1]
                    if payload["op"] == "accept"
                    else next((offer for offer in offers if offer.get("effective")), offers[-1])
                )
                payload["version"] = selected["version"]
            transport = "player_communication"
        else:
            raise ValueError("TOOL_NOT_PERMITTED")
        policy_keys = {"SOUL", "STRATEGY"}
        if name in {"trade_offer", "trade_decide"}:
            policy_keys.add("economy")
        if transport == "player_command":
            policy_keys.add("observation:turn")
            if arguments.get("kind") in ECONOMIC:
                policy_keys.update({"economy", "observation:economy"})
            if arguments.get("kind") in {"SET_RESEARCH_QUEUE", "CHOOSE_FREE_TECHNOLOGY"}:
                policy_keys.add("observation:research")
            if arguments.get("kind") in {"ADOPT_POLICY", "DISMISS_POLICY_PICKER"}:
                policy_keys.add("observation:policy")
            for scope in ("unit/", "city/", "diplomacy/"):
                key = "observation:" + scope + str(arguments.get("target"))
                if key in job.memory["records"]:
                    policy_keys.add(key)
        other = arguments.get("to") or arguments.get("target") or arguments.get("partner")
        if name == "trade_decide":
            selected = job.trades[arguments["proposal"]]
            other = selected["partner"] if selected["author"] == self.memory.civilization else selected["author"]
        if other:
            policy_keys.add("relationships/" + str(other))
        dependencies = {key: job.memory["records"].get(key, {}).get("version", 0) for key in policy_keys}
        # State validation and persistence are short. Network execution is
        # serialized separately, never monopolizing the event inbox.
        async with self.dispatch:
            self.check(job)
            request = await self.io.call(self.memory.queue, job.id, transport, payload, dependencies, call_id)
            if request["result"] is not None:
                return public_result(json.loads(request["result"]))
            self.check(job)
            self.in_flight += 1
            try:
                value = await job.connection.tool(transport, json.loads(request["payload"]))
                await self.io.call(self.memory.receipt, request["id"], value)
            except (OSError, ValueError, TimeoutError, aiohttp.ClientError):
                # A new call ID cannot duplicate a possibly committed action.
                # Recovery checks the durable request with the host first.
                job.closed = True
                raise
            finally:
                self.in_flight -= 1
            if (
                transport == "player_command" or name == "trade_decide" and value.get("outcome")
            ) and "error" not in value:
                job.observation = None
            return public_result(value)

    async def handle(self, request: web.Request):
        if not request.secure or "Origin" in request.headers or len(request.headers.getall("Authorization", [])) != 1:
            raise web.HTTPForbidden()
        token = request.headers.get("Authorization", "").removeprefix("Bearer ")
        job = next((job for candidate, job in self.jobs.items() if hmac.compare_digest(candidate, token)), None)
        if job is None or job.closed:
            raise web.HTTPUnauthorized()
        body = await request.json()
        if not isinstance(body, dict) or body.get("jsonrpc") != "2.0":
            raise web.HTTPBadRequest()
        method = body.get("method")
        if "id" not in body:
            return web.Response(status=202)
        try:
            self.check(job)
            if method == "initialize":
                result = {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "civilization", "version": "1"},
                }
            elif method == "ping":
                result = {}
            elif method == "tools/list":
                result = {"tools": await self.tools(job)}
            elif method == "tools/call":
                parameters = body.get("params", {})
                value = await self.call(job, parameters["name"], parameters.get("arguments", {}), str(body["id"]))
                result = tool_result(value)
            else:
                return web.json_response(
                    {"jsonrpc": "2.0", "id": body["id"], "error": {"code": -32601, "message": "Method not available"}}
                )
        except (StateConflict, ValueError, KeyError) as error:
            result = tool_result({"error": str(error)})
        except (OSError, TimeoutError, aiohttp.ClientError):
            result = tool_result(
                {"error": "Response uncertain; the service retained the request for reconciliation. Stop this job."}
            )
        return web.json_response({"jsonrpc": "2.0", "id": body["id"], "result": result})
