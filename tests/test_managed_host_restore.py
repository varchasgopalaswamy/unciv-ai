"""Synthetic native restore semantics plus actual Temporal retry/replay boundaries."""

import base64
import copy
import hashlib

from civ_agent.protocol import JsonObject

PRIVATE_GAME = "PRIVATE_OTHER_PLAYER_NATIVE_GAME_CONTENT"
PRIVATE_ERROR = "PRIVATE_TOKEN_IN_TRANSPORT_DIAGNOSTIC"
ACTUAL_USAGE = {"input_tokens": 100, "cached_input_tokens": 30, "output_tokens": 70, "reasoning_output_tokens": 20}


def native_hash(token):
    return base64.urlsafe_b64encode(hashlib.sha256(token.encode()).digest()).rstrip(b"=").decode()


class Host:
    """Serial native-rule fixture, not a JVM/engine restore acceptance claim."""

    def __init__(self):
        self.status = {
            "epoch": "e1",
            "serverInstance": "server",
            "phase": "PAUSED",
            "turn": 3,
            "handoff": 6,
            "activeCivilization": "Rome",
            "activeController": "AGENT",
            "scheduled": True,
            "leaseId": "lease",
            "leaseExpiresInMs": 30000,
            "modelInvocations": 9,
            "modelUsage": copy.deepcopy(ACTUAL_USAGE),
        }
        self.snapshot = {
            "formatVersion": 9,
            "epoch": "e1",
            "handoff": 6,
            "paused": True,
            "communicationPaused": True,
            "scheduled": True,
            "gameJson": PRIVATE_GAME,
            "players": {"Rome": {"receipts": {"original": {"requestId": "original"}}}},
            "communication": {
                "restorePending": False,
                "playerCredentials": {},
                "requests": {"chat-original": {"messageId": "message-original"}},
            },
        }
        self.calls = []
        self.losses = set()
        self.rotation = 0
        self.forbidden = False
        self.drop_usage = False

    def call(self, operation, **fields):
        assert not self.forbidden, "Replay cannot touch private host or credentials"
        self.calls.append((operation, copy.deepcopy(fields)))
        players = None
        if operation == "status":
            assert not fields
        else:
            assert fields["leaseId"] == self.status["leaseId"]
            if operation == "restore":
                assert self.snapshot["paused"] and self.snapshot["communicationPaused"] and self.snapshot["scheduled"]
                assert fields["checkpoint"]["handoff"] == self.status["handoff"]
                # Native restore is deliberately not idempotent on newEpoch.
                # Repeating it would destroy a target change at this handoff.
                self.snapshot = copy.deepcopy(fields["checkpoint"])
                self.status["epoch"] = self.snapshot["epoch"] = fields["epoch"]
                self.snapshot["communication"].update(restorePending=True, playerCredentials={})
                if self.drop_usage:
                    self.status["modelUsage"]["reasoning_output_tokens"] = 0
            elif operation == "rotate_players":
                assert fields["epoch"] == self.status["epoch"] and self.snapshot["communicationPaused"]
                self.rotation += 1
                players = [
                    {
                        "civilizationId": name,
                        "controller": controller,
                        "token": base64.urlsafe_b64encode(bytes([self.rotation + i]) * 32).rstrip(b"=").decode(),
                    }
                    for i, (name, controller) in enumerate((("Rome", "AGENT"), ("France", "HUMAN")))
                ]
                self.snapshot["communication"]["playerCredentials"] = {
                    native_hash(row["token"]): row["civilizationId"] for row in players
                }
            elif operation == "restore_complete":
                assert fields["epoch"] == self.status["epoch"]
                self.snapshot["communication"]["restorePending"] = False
            else:
                assert operation == "snapshot"
        if operation in self.losses:
            self.losses.remove(operation)
            raise OSError(PRIVATE_ERROR)
        result: JsonObject = {"status": copy.deepcopy(self.status)}
        if operation == "snapshot":
            result["checkpoint"] = copy.deepcopy(self.snapshot)
        if players is not None:
            result["players"] = players
        return result

    def count(self, operation):
        return sum(name == operation for name, _ in self.calls)
