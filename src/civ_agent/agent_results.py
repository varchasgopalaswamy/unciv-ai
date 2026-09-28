"""Trusted delivery of one player's final, permission-filtered game result."""

import json
import os

from civ_agent.result_schema import PlayerResult
from civ_agent.tls import private_directory, regular_file, write_private_json


class ResultError(ValueError):
    pass


def validate_result(payload):
    """Reject malformed or broader-than-player-view data before any workspace write."""

    try:
        PlayerResult.model_validate(payload)
        if len(json.dumps(payload, allow_nan=False).encode()) > 32 * 1024 * 1024:
            raise ValueError()
    except (ValueError, TypeError):
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
        # One authoritative delivery record makes lost acknowledgements retryable.
        write_private_json(self.path, payload)
        descriptor = os.open(self.directory, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        return {"civilization": self.civilization, "epoch": payload["epoch"], "received": True}
