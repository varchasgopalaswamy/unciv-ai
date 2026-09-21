"""Trusted, readable per-turn gameplay records and deliberately small turn memory.

The service's event log is the source of truth. Files in the player's workspace are
replaceable copies and are never read to build the next turn's context.
"""

import fcntl
import json
import os
import re
from collections import Counter
from collections.abc import Iterable
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from civ_agent.memory_history import action_text, summary_markdown
from civ_agent.memory_records import TurnSummary
from civ_agent.protocol import JsonObject
from civ_agent.tls import private_directory, regular_file, write_private, write_private_json


class TurnArchiveError(ValueError):
    pass


_SECRET_KEY = re.compile(r"token|authorization|password|secret|api.?key", re.I)
_PRIVATE_FIELDS = {
    "encrypted_content",
    "encryptedContent",
    "raw_reasoning",
    "rawReasoning",
    "reasoning_text",
    "reasoningText",
}
_ANSI = re.compile(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07\x1b]*(?:\x07|\x1b\\))")
_TOOL_KINDS = {"tool_call", "tool_result", "tool_output"}
_VISIBLE_KINDS = _TOOL_KINDS | {"assistant_message", "assistant_delta", "lifecycle", "error"}


def civilization_directory_name(civilization):
    """Encode one path segment injectively; ordinary civilization names stay readable."""
    if not isinstance(civilization, str) or not civilization:
        raise TurnArchiveError("Civilization name must be a nonempty string.")
    try:
        encoded = "".join(
            chr(byte) if chr(byte).isascii() and (chr(byte).isalnum() or byte in (45, 95)) else f"%{byte:02X}"
            for byte in civilization.encode("utf-8")
        )
    except UnicodeError:
        raise TurnArchiveError("Civilization name must be valid Unicode.") from None
    if len(encoded) > 255:
        raise TurnArchiveError("Civilization name is too long for a directory name.")
    return encoded


def _plain(value):
    value = _ANSI.sub("", value)
    return "".join(char for char in value if char in "\n\t" or ord(char) >= 32 and ord(char) != 127)


def _sanitize(value: Any, secrets: Iterable[str]) -> Any:
    if isinstance(value, str):
        for secret in sorted(secrets, key=len, reverse=True):
            value = value.replace(secret, "[REDACTED]")
        value = re.sub(r"(?i)\bBearer\s+[A-Za-z0-9_.~-]+", "Bearer [REDACTED]", value)
        value = re.sub(r"(?<![A-Za-z0-9_-])[A-Za-z0-9_-]{43}(?![A-Za-z0-9_-])", "[REDACTED]", value)
        return _plain(value)
    if isinstance(value, dict):
        return {
            _sanitize(str(key), secrets): "[REDACTED]" if _SECRET_KEY.search(str(key)) else _sanitize(item, secrets)
            for key, item in value.items()
            if key not in _PRIVATE_FIELDS
        }
    if isinstance(value, list):
        return [_sanitize(item, secrets) for item in value]
    return value if value is None or isinstance(value, (bool, int, float)) else None


def _directory(path):
    path = Path(os.path.abspath(path))
    # Do not resolve first: that would hide a symlink in a writable workspace.
    for candidate in (path, *path.parents):
        if candidate.is_symlink():
            raise TurnArchiveError("Turn archive paths must not contain symlinks.")
    return private_directory(path)


def _load(path):
    regular_file(path, private=True)
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, UnicodeError):
        raise TurnArchiveError("Turn archive metadata is corrupt.") from None


def _jsonl(events):
    return "".join(json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n" for event in events)


def _events(events, secrets):
    """Expand complete HistoryLog chunks; retain an interrupted final chunk losslessly."""
    events = list(events)
    expanded, index = [], 0
    for expected, event in enumerate(events):
        if not isinstance(event, dict) or type(event.get("sequence")) is not int or event["sequence"] != expected:
            raise TurnArchiveError("Expected a complete, contiguous service event log.")
        if not isinstance(event.get("data"), dict):
            raise TurnArchiveError("Invalid service event data.")
    while index < len(events):
        event = events[index]
        data = event["data"]
        if data.get("encoding") == "json" and "chunkIndex" in data:
            count = data.get("chunkCount")
            if type(count) is not int or count < 1 or data.get("chunkIndex") != 0:
                raise TurnArchiveError("Invalid chunked service event.")
            chunks = events[index : index + count]
            if not all(
                part["kind"] == event["kind"]
                and part["data"].get("chunkIndex") == offset
                and part["data"].get("chunkCount") == count
                and part["data"].get("itemId") == data.get("itemId")
                and isinstance(part["data"].get("text"), str)
                for offset, part in enumerate(chunks)
            ):
                raise TurnArchiveError("Chunked service events are out of order.")
            if len(chunks) < count:
                expanded.extend({**part, "incompleteChunk": True} for part in chunks)
                break
            try:
                data = json.loads("".join(part["data"]["text"] for part in chunks))
            except (ValueError, UnicodeError):
                raise TurnArchiveError("Chunked service event is not valid JSON.") from None
            if not isinstance(data, dict):
                raise TurnArchiveError("Invalid reassembled service event.")
            event = {**event, "data": data}
            index += count
        else:
            index += 1
        expanded.append(event)
    result, seen = [], set()
    for event in expanded:
        kind, data = event.get("kind"), event["data"]
        if kind not in _VISIBLE_KINDS:
            continue
        identity = (kind, data.get("itemId"))
        if (
            kind in {"tool_call", "tool_result", "assistant_message"}
            and identity[1]
            and not event.get("incompleteChunk")
        ):
            if identity in seen:
                continue
            seen.add(identity)
        result.append(_sanitize(event, secrets))
    return result


def _object(value):
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (ValueError, UnicodeError):
            return None
    return value if isinstance(value, dict) else None


def _receipt(data):
    result = _object(data.get("result"))
    if result is None or result.get("isError") or data.get("error"):
        return None
    candidates = [result.get("structuredContent")]
    candidates.extend(
        item.get("text") for item in result.get("content", []) if isinstance(item, dict) and item.get("type") == "text"
    )
    for candidate in candidates:
        value = _object(candidate)
        if value is not None and all(
            key in value for key in ("requestId", "epoch", "kind", "revision", "turn", "isYourTurn")
        ):
            if type(value["turn"]) is int and isinstance(value["isYourTurn"], bool):
                return value
    return None


def _short(value, limit=180):
    value = _plain(str(value)).replace("\n", " ")
    return value if len(value) <= limit else value[:limit] + "…"


def _description(arguments):
    detail = {key: value for key, value in arguments.items() if key not in {"epoch", "revision", "requestId", "kind"}}
    return _short(json.dumps(detail, ensure_ascii=False, separators=(",", ":"))) if detail else ""


def _failure(data):
    result = _object(data.get("result")) or {}
    if data.get("error"):
        return _short(data["error"])
    if result.get("isError"):
        content = " ".join(str(item.get("text", "")) for item in result.get("content", []) if isinstance(item, dict))
        return _short(content) or "Tool returned an error."
    if data.get("status") in {"failed", "cancelled", "interrupted"}:
        return str(data["status"])
    return None


class TurnArchive:
    """A single civilization's epoch-bound archive, with untrusted workspace copies."""

    def __init__(self, directory, workspace, civilization, epoch, secrets=()):
        civilization_directory_name(civilization)
        if not isinstance(epoch, str) or not epoch:
            raise TurnArchiveError("An epoch is required for turn memory.")
        self.directory, self.workspace = _directory(directory), _directory(workspace)
        if self.directory == self.workspace or self.directory.is_relative_to(self.workspace):
            raise TurnArchiveError("Trusted turn memory must be outside the player's writable workspace.")
        self.civilization, self.epoch = civilization, epoch
        self.secrets = tuple(secret for secret in secrets if isinstance(secret, str) and secret)
        self.binding = {"version": 2, "civilization": civilization, "epoch": epoch}
        with self._lock():
            for root in (self.directory, self.workspace):
                manifest = root / "match.json"
                if manifest.exists() or manifest.is_symlink():
                    if _load(manifest) != self.binding:
                        raise TurnArchiveError(
                            "This civilization directory has a different match or archive format; choose a new state directory."
                        )
                else:
                    write_private_json(manifest, self.binding)

    @contextmanager
    def _lock(self):
        _directory(self.directory)
        descriptor = os.open(self.directory / ".lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            yield
        finally:
            os.close(descriptor)

    def _turn(self, game_turn):
        if type(game_turn) is not int or game_turn < 0:
            raise TurnArchiveError("Game turn must be a nonnegative integer.")
        return f"turn_{game_turn}"

    def _turns(self):
        result = []
        for path in self.directory.iterdir():
            if re.fullmatch(r"turn_(0|[1-9][0-9]*)", path.name):
                _directory(path)
                result.append((int(path.name[5:]), path))
        return sorted(result)

    def _attempts(self, turn_path: Path) -> list[JsonObject]:
        index_path = turn_path / "attempts.json"
        return _load(index_path) if index_path.exists() or index_path.is_symlink() else []

    def _read_events(self, turn_path, attempt):
        path = turn_path / f"attempt_{attempt['attempt']}.jsonl"
        regular_file(path, private=True)
        try:
            return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
        except (ValueError, UnicodeError):
            raise TurnArchiveError("Turn archive event file is corrupt.") from None

    def update(
        self,
        game_turn,
        activation_id,
        events,
        *,
        phase=None,
        handoff=None,
        aborted=False,
        terminal=False,
        structured_summary=None,
    ):
        """Replace one attempt from its complete trusted event log, then regenerate artifacts.

        ``handoff`` is the scheduler's confirmed committed counter, never a model
        assertion. Repeating this call is safe, including after a service restart.
        """
        turn_name = self._turn(game_turn)
        if not isinstance(activation_id, str) or not activation_id:
            raise TurnArchiveError("An activation identity is required.")
        if handoff is not None and (type(handoff) is not int or handoff < 1):
            raise TurnArchiveError("Committed handoff must be a positive integer.")
        if type(terminal) is not bool or (terminal and (handoff is not None or aborted)):
            raise TurnArchiveError("Terminal completion requires a separate, non-aborted outcome.")
        if type(aborted) is not bool:
            raise TurnArchiveError("Aborted must be a boolean.")
        if phase is not None and phase not in {"STARTING", "RUNNING", "STOPPING", "COMPLETED", "INTERRUPTED", "FAILED"}:
            raise TurnArchiveError("Invalid activation phase.")
        source_events = list(events)
        normalized = _events(source_events, self.secrets)
        with self._lock():
            turn_path = _directory(self.directory / turn_name)
            attempts = self._attempts(turn_path)
            for other_turn, other_path in self._turns():
                if other_turn != game_turn and any(
                    item["activationId"] == activation_id for item in self._attempts(other_path)
                ):
                    raise TurnArchiveError("An activation cannot belong to two game turns.")
            attempt: JsonObject | None = next(
                (item for item in attempts if item["activationId"] == activation_id), None
            )
            if attempt is None:
                if any(item.get("terminalOutcome") for item in attempts):
                    raise TurnArchiveError("A completed game turn cannot be retried.")
                attempt = {
                    "attempt": len(attempts) + 1,
                    "activationId": activation_id,
                    "phase": None,
                    "handoff": None,
                    "sourceCount": 0,
                    "aborted": False,
                }
                attempts.append(attempt)
            if attempt["sourceCount"] > len(source_events):
                raise TurnArchiveError("A turn update cannot discard previously archived service events.")
            if attempt["handoff"] is not None and handoff is not None and attempt["handoff"] != handoff:
                raise TurnArchiveError("A confirmed handoff cannot change.")
            if handoff is not None and any(item["handoff"] not in (None, handoff) for item in attempts):
                raise TurnArchiveError("A game turn cannot have two committed handoffs.")
            if (aborted or attempt.get("aborted")) and (handoff is not None or attempt["handoff"] is not None):
                raise TurnArchiveError("A confirmed handoff cannot be marked rolled back.")
            terminal = terminal or attempt.get("terminalOutcome", False)
            if terminal and (
                aborted or attempt.get("aborted") or handoff is not None or attempt["handoff"] is not None
            ):
                raise TurnArchiveError("A terminal outcome cannot be rolled back or replaced with a handoff.")
            attempt["terminalOutcome"] = terminal
            attempt["aborted"] = aborted or attempt.get("aborted", False)
            attempt["sourceCount"] = len(source_events)
            if phase is not None:
                attempt["phase"] = phase
            if handoff is not None:
                attempt["handoff"] = handoff
            write_private(turn_path / f"attempt_{attempt['attempt']}.jsonl", _jsonl(normalized).encode())
            write_private_json(turn_path / "attempts.json", attempts)
            artifacts = self._render(game_turn, turn_path, attempts)
            if structured_summary is not None:
                data = TurnSummary.model_validate(_sanitize(structured_summary, self.secrets)).model_dump(
                    exclude_none=True
                )
                if data["turn"] != game_turn:
                    raise TurnArchiveError("Summary turn does not match archive")
                artifacts["summaryData"] = data
                artifacts["summary"] = summary_markdown(data)
            attempt_artifacts = self._render(game_turn, turn_path, [attempt], include_aborted=True)
            attempt_metadata = {
                **self.binding,
                "gameTurn": game_turn,
                "attempt": attempt["attempt"],
                "phase": attempt["phase"],
                "aborted": attempt["aborted"],
                "committed": attempt["handoff"] is not None or attempt.get("terminalOutcome", False),
                "terminalOutcome": attempt.get("terminalOutcome", False),
            }
            public_events = [
                event for event in normalized if event["kind"] in _TOOL_KINDS | {"assistant_message", "assistant_delta"}
            ]
            # The generated trusted artifact is committed before refreshing workspace
            # copies. A bad workspace path cannot contaminate future context.
            for root in (turn_path, self.workspace / turn_name):
                _directory(root)
                write_private(root / "commands.jsonl", artifacts["commands"].encode())
                write_private(root / "summary.md", artifacts["summary"].encode())
                write_private_json(root / "summary.json", artifacts["summaryData"])
                metadata = {
                    key: value for key, value in artifacts.items() if key not in {"commands", "summary", "summaryData"}
                }
                if root != turn_path:
                    metadata["attempts"] = [
                        {key: value for key, value in item.items() if key != "handoff"}
                        | {"committed": item["handoff"] is not None or item.get("terminalOutcome", False)}
                        for item in artifacts["attempts"]
                    ]
                write_private_json(root / "turn.json", metadata)
                attempt_path = _directory(_directory(root / "attempts") / f"attempt_{attempt['attempt']}")
                write_private(attempt_path / "commands.jsonl", attempt_artifacts["commands"].encode())
                write_private(attempt_path / "summary.md", attempt_artifacts["summary"].encode())
                write_private(attempt_path / "events.jsonl", _jsonl(public_events).encode())
                write_private_json(attempt_path / "source.json", attempt_metadata)
            return artifacts

    def _render(self, game_turn, turn_path, attempts, *, include_aborted=False):
        seen_requests = set()
        for earlier_turn, earlier_path in self._turns():
            if earlier_turn >= game_turn:
                break
            for earlier_attempt in self._attempts(earlier_path):
                if earlier_attempt.get("aborted"):
                    continue
                for event in self._read_events(earlier_path, earlier_attempt):
                    receipt = (
                        _receipt(event["data"])
                        if event["kind"] == "tool_result" and not event.get("incompleteChunk")
                        else None
                    )
                    if receipt is not None and receipt["epoch"] == self.epoch:
                        seen_requests.add((receipt["epoch"], receipt["requestId"]))
        public_attempts = [
            {key: attempt[key] for key in ("attempt", "phase", "handoff", "aborted")}
            | {"terminalOutcome": attempt.get("terminalOutcome", False)}
            for attempt in attempts
        ]
        committed = next((item["handoff"] for item in attempts if item["handoff"] is not None), None)
        summary = [
            f"# {_plain(self.civilization)} — turn {game_turn}\n",
            "Historical record. Read a fresh player_turn for current state; old commands and receipts must not be replayed as new actions.\n",
            "Host terminal outcome: confirmed.\n"
            if any(item.get("terminalOutcome") for item in attempts)
            else "Host handoff: confirmed.\n"
            if committed is not None
            else "Host handoff: unconfirmed. A completed assistant response does not establish that the game turn ended.\n",
        ]
        aborted_count = sum(bool(attempt.get("aborted")) for attempt in attempts)
        if aborted_count:
            summary.append(
                "This attempt was rolled back. The commands, receipts and reports below are for debugging; their effects were discarded.\n"
                if include_aborted
                else f"{aborted_count} failed attempt(s) were rolled back; debugging details are in attempts/.\n"
            )
        dense = []
        structured = TurnSummary(turn=game_turn).model_dump()
        for attempt in attempts:
            if attempt.get("aborted") and not include_aborted:
                continue
            events = self._read_events(turn_path, attempt)
            results = {
                event["data"].get("itemId"): event["data"]
                for event in events
                if event["kind"] == "tool_result" and not event.get("incompleteChunk")
            }
            summary.append(f"\n## Attempt {attempt['attempt']} — {attempt['phase'] or 'unknown status'}\n")
            outcomes, examples, failures = Counter(), [], []
            for event in events:
                if event["kind"] in _TOOL_KINDS:
                    if event["kind"] != "tool_output" or event["data"].get("itemId") not in results:
                        dense.append({"attempt": attempt["attempt"], "aborted": attempt.get("aborted", False), **event})
                if event["kind"] != "tool_call" or event.get("incompleteChunk"):
                    continue
                call = event["data"]
                if (
                    call.get("type") != "mcpToolCall"
                    or call.get("server") != "civ"
                    or call.get("tool") != "player_command"
                ):
                    continue
                arguments = _object(call.get("arguments")) or {}
                if "requestId" not in arguments:
                    # Keep the model's proposal in the detailed transcript. Its
                    # confirmed effect is counted from the controller's record.
                    continue
                result = results.get(call.get("itemId"))
                receipt = _receipt(result) if result is not None else None
                command_kind = _short(arguments.get("kind", "Unrecognized command"), 70)
                label = command_kind
                detail = _description(arguments)
                if detail:
                    label += " " + detail
                if (
                    receipt is not None
                    and all(receipt[key] == arguments.get(key) for key in ("epoch", "requestId", "kind"))
                    and receipt["epoch"] == self.epoch
                ):
                    key = (receipt["epoch"], receipt["requestId"])
                    if key in seen_requests or receipt["turn"] < game_turn:
                        outcome = "replayed successful receipt; no new effect established"
                    else:
                        outcome = "host confirmed success"
                    seen_requests.add(key)
                else:
                    failure = _failure(result) if result is not None else None
                    outcome = "failed: " + failure if failure else "uncertain: no matching successful host receipt"
                category = "failed" if outcome.startswith("failed:") else outcome
                outcomes[(category, command_kind)] += 1
                if outcome == "host confirmed success":
                    structured["actions"].append(
                        {
                            "decision": f"action{len(structured['actions']) + 1}",
                            "action": action_text(arguments, receipt),
                            "reason": arguments.get("intent", {}).get("reason", ""),
                            "outcome": "confirmed",
                            "command": arguments,
                            "receipt": receipt,
                        }
                    )
                if outcome == "host confirmed success" and detail and len(examples) < 6:
                    examples.append(label)
                if outcome.startswith(("failed:", "uncertain:")) and len(failures) < 3:
                    failures.append(f"{label}: {outcome}")
            summary.append("\nReceipt evidence:\n")
            if outcomes:
                categories = dict.fromkeys(category for category, _kind in outcomes)
                for category in categories:
                    counts = [(kind, count) for (candidate, kind), count in outcomes.items() if candidate == category]
                    displayed = ", ".join(f"{kind} ×{count}" for kind, count in counts[:12])
                    if len(counts) > 12:
                        displayed += (
                            f", and {sum(count for _kind, count in counts[12:])} other calls (see commands.jsonl)"
                        )
                    summary.append(f"- {category}: {displayed}.\n")
                if examples:
                    summary.append("- Successful action details: " + "; ".join(examples) + ".\n")
                for failure in failures:
                    summary.append("- " + failure + ".\n")
            else:
                summary.append("No successful player commands were established by matched calls and receipts.\n")
            messages = [
                event["data"]
                for event in events
                if event["kind"] == "assistant_message" and not event.get("incompleteChunk")
            ]
            finals = [
                data["text"]
                for data in messages
                if data.get("phase") == "final_answer" and isinstance(data.get("text"), str) and data["text"].strip()
            ]
            if finals:
                summary.append("\nAgent's public report and reasons (self-reported):\n\n" + self._quote(finals[-1]))
            else:
                notes = [
                    data["text"] for data in messages if isinstance(data.get("text"), str) and data["text"].strip()
                ]
                summary.append(
                    "\nNo final public report was captured. See recorded action intents and receipts above.\n"
                )
                if notes:
                    summary.append("\nAvailable public notes (self-reported):\n\n" + self._quote("\n\n".join(notes)))
            if any(event.get("incompleteChunk") for event in events):
                summary.append(
                    "\nThe service event log ends inside a tool event; its incomplete chunks are retained in commands.jsonl.\n"
                )
        return {
            "gameTurn": game_turn,
            "turn": game_turn,
            "commands": _jsonl(dense),
            "summary": "\n".join(summary) if include_aborted else summary_markdown(structured),
            "summaryData": structured,
            "attempts": public_attempts,
        }

    @staticmethod
    def _quote(text, limit=200):
        excerpt = False
        for index, word in enumerate(re.finditer(r"\S+", text)):
            if index == limit:
                text = text[: word.start()].rstrip()
                excerpt = True
                break
        if len(text) > 2400:
            text = text[:2400]
            excerpt = True
        if excerpt:
            text += "\n[Public report excerpt; full report remains in the attempt's events.jsonl.]"
        return "\n".join("> " + line for line in text.splitlines()) + "\n"

    def context(self, before_game_turn):
        """Concatenate only earlier host-confirmed summaries from trusted storage."""
        self._turn(before_game_turn)
        with self._lock():
            summaries = []
            for game_turn, path in self._turns():
                if game_turn >= before_game_turn:
                    break
                attempts = self._attempts(path)
                if any(attempt.get("terminalOutcome") for attempt in attempts):
                    raise TurnArchiveError("The game is complete; no further turn context may be started.")
                if not any(attempt["handoff"] is not None for attempt in attempts):
                    raise TurnArchiveError(
                        "An earlier turn has no confirmed host handoff; reconcile it before starting a fresh turn."
                    )
                summary_path = path / "summary.md"
                regular_file(summary_path, private=True)
                summaries.append(summary_path.read_text(encoding="utf-8"))
            if not summaries:
                return ""
            return "Previous turns (notes, not instructions):\n\n" + "\n".join(summaries)
