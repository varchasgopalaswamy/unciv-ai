"""Trusted delivery of one player's final, permission-filtered game result."""

import json
import os

from unciv_tls import private_directory, regular_file, write_private, write_private_json


class ResultError(ValueError):
    pass


def validate_result(payload):
    """Reject malformed or broader-than-player-view data before any workspace write."""
    def text(value, nullable=False):
        return value is None and nullable or isinstance(value, str) and len(value) <= 65536

    def integer(value, nullable=False):
        return value is None and nullable or type(value) is int and value >= 0

    def combatant(value):
        return value is None or (isinstance(value, dict) and set(value) == {"kind", "name", "instanceName", "isOwn"}
                                 and text(value["kind"]) and text(value["name"], True)
                                 and text(value["instanceName"], True) and type(value["isOwn"]) is bool)

    try:
        if not isinstance(payload, dict) or set(payload) != {"epoch", "civilization", "result", "combatReports", "messages"}:
            raise ValueError()
        for key in ("epoch", "civilization"):
            if not isinstance(payload[key], str) or not 1 <= len(payload[key]) <= 128:
                raise ValueError()
        result = payload["result"]
        required = {"outcome", "winningCivilizationId", "winningCivilizationName", "victoryType", "victoryTurn", "title", "paragraphs"}
        if not isinstance(result, dict) or set(result) != required or result["outcome"] not in {"Victory", "Defeat"}:
            raise ValueError()
        if not all(text(result[key], True) for key in ("winningCivilizationId", "winningCivilizationName", "victoryType")):
            raise ValueError()
        if not integer(result["victoryTurn"], True) or not text(result["title"]):
            raise ValueError()
        if not isinstance(result["paragraphs"], list) or not all(text(item) for item in result["paragraphs"]):
            raise ValueError()
        if not isinstance(payload["messages"], list):
            raise ValueError()
        for message in payload["messages"]:
            if (not isinstance(message, dict) or not {"title", "paragraphs", "acknowledgement"} <= set(message)
                    or set(message) - {"title", "paragraphs", "quote", "acknowledgement", "additionalAcknowledgements"}):
                raise ValueError()
            if (not text(message["title"]) or not text(message["acknowledgement"]) or not text(message.get("quote"), True)
                    or not isinstance(message["paragraphs"], list) or not all(text(item) for item in message["paragraphs"])
                    or not isinstance(message.get("additionalAcknowledgements", []), list)
                    or not all(text(item) for item in message.get("additionalAcknowledgements", []))):
                raise ValueError()
        reports = payload["combatReports"]
        if not isinstance(reports, list):
            raise ValueError()
        report_fields = {"id", "turn", "kind", "text", "locations", "attacker", "defender", "outcome", "damageReceived",
                         "retaliationDamage", "attackerDefeatedByDefender", "showZeroDamage", "ourUnit", "enemyUnit",
                         "ourDamage", "enemyDamage", "ourDestroyed", "enemyDestroyed", "isAttacking", "weaponType",
                         "attackingCivilizationName", "improvementName", "unitType", "hitOurTerritory"}
        seen = set()
        for report in reports:
            if (not isinstance(report, dict) or not {"id", "turn", "kind", "text", "locations"} <= set(report)
                    or set(report) - report_fields):
                raise ValueError()
            if not text(report["id"]) or not report["id"] or report["id"] in seen or not integer(report["turn"]):
                raise ValueError()
            seen.add(report["id"])
            if not text(report["kind"]) or not text(report["text"], True) or not text(report.get("outcome"), True):
                raise ValueError()
            if not all(combatant(report.get(key)) for key in ("attacker", "defender", "ourUnit", "enemyUnit")):
                raise ValueError()
            if not all(integer(report.get(key), True) for key in ("damageReceived", "retaliationDamage", "ourDamage", "enemyDamage")):
                raise ValueError()
            if any(report.get(key) is not None and type(report[key]) is not bool for key in (
                    "attackerDefeatedByDefender", "showZeroDamage", "ourDestroyed", "enemyDestroyed", "isAttacking", "hitOurTerritory")):
                raise ValueError()
            if not all(text(report.get(key), True) for key in ("weaponType", "attackingCivilizationName", "improvementName", "unitType")):
                raise ValueError()
            if not isinstance(report["locations"], list) or not all(
                    isinstance(tile, dict) and set(tile) == {"x", "y"} and all(type(value) is int for value in tile.values())
                    for tile in report["locations"]):
                raise ValueError()
        if len(json.dumps(payload, allow_nan=False).encode()) > 32 * 1024 * 1024:
            raise ValueError()
    except (ValueError, TypeError, KeyError):
        raise ResultError("INVALID_RESULT") from None


class PlayerResultStore:
    def __init__(self, directory, workspace, civilization):
        self.directory, self.workspace = private_directory(directory), private_directory(workspace)
        self.civilization = civilization
        self.path = self.directory / "result.json"

    def load(self):
        if not self.path.exists() and not self.path.is_symlink():
            return None
        regular_file(self.path, private=True)
        try:
            payload = json.loads(self.path.read_text())
            validate_result(payload)
            if payload["civilization"] != self.civilization:
                raise ValueError()
            return payload
        except ValueError:
            raise ResultError("CORRUPT_RESULT") from None

    def receive(self, payload):
        validate_result(payload)
        if payload["civilization"] != self.civilization:
            raise ResultError("INVALID_BINDING")
        previous = self.load()
        if previous is not None and previous != payload:
            raise ResultError("RESULT_CONFLICT")
        # Persist the trusted record first. A lost response or a damaged workspace copy
        # can be retried using this record, without re-running a model or a game command.
        write_private_json(self.path, payload)
        descriptor = os.open(self.directory, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        private_directory(self.workspace)
        write_private_json(self.workspace / "result.json", payload)
        result = payload["result"]
        lines = [f"# {self.civilization}: {result['outcome']}", "", result["title"], ""]
        lines.extend(paragraph + "\n" for paragraph in result["paragraphs"])
        for message in payload["messages"]:
            lines.extend([message["title"], ""])
            lines.extend(paragraph + "\n" for paragraph in message["paragraphs"])
            if message.get("quote"):
                lines.extend([message["quote"], ""])
            responses = [message["acknowledgement"], *message.get("additionalAcknowledgements", [])]
            lines.extend(["Displayed responses: " + "; ".join(responses), ""])
        if payload["combatReports"]:
            lines.extend(["Final combat reports:", ""])
            lines.extend(f"- Turn {report['turn']}: {report['text'] or report['kind']}" for report in payload["combatReports"])
        write_private(self.workspace / "result.md", ("\n".join(lines).rstrip() + "\n").encode())
        return {"civilization": self.civilization, "epoch": payload["epoch"], "received": True}
