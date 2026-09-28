"""Background completion using a persisted, activation-specific handoff proof.

Workers never mutate scheduler state or read the current game. The scheduler
publishes their results and owns ordering, budgets and turn boundaries.
"""

import time


def _idempotent_call(agent, operation, **payload):
    try:
        return agent.call(operation, **payload)
    except Exception as error:
        if getattr(error, "code", None) != "TRANSPORT_TIMEOUT":
            raise
        # Only activation-bound, idempotent control work is retried. A lost
        # acknowledgement never starts a model or replays a game command.
        return agent.call(operation, **payload)


def finalize_turn(agent, flight):
    """Drain provider completion and accounting after a committed native handoff."""
    payload = {
        "activationId": flight["activationId"],
        "epoch": flight["epoch"],
        "gameTurn": flight["gameTurn"],
        "committedHandoff": flight["confirmedHandoff"],
    }
    status = _idempotent_call(agent, "finish_handoff", **payload)
    deadline = time.monotonic() + 90
    while status["phase"] not in {"COMPLETED", "FAILED", "INTERRUPTED"}:
        if time.monotonic() >= deadline:
            raise ValueError("Provider completion has not drained after END_TURN.")
        time.sleep(0.1)
        status = agent.call("heartbeat", activationId=flight["activationId"])
    if status.get("error") in {
        "DURABLE_RECORD_FAILED",
        "REPEATED_STALE_ASSUMPTIONS",
        "STATE_UPDATE_FAILED",
        "STOP_UNCONFIRMED",
        "STATE_STORE_FAILED",
    }:
        raise ValueError(status["error"])
    finalized = _idempotent_call(agent, "begin_finalize", **payload)
    deadline = time.monotonic() + 300
    while finalized.get("finalizationState") != "COMPLETED":
        if finalized.get("finalizationState") == "FAILED":
            raise ValueError("Turn finalization failed: " + finalized.get("finalizationError", "unknown error"))
        if time.monotonic() >= deadline:
            raise ValueError("Turn finalization has not completed; its pending proof is retained.")
        time.sleep(0.1)
        finalized = agent.call("status", activationId=flight["activationId"])
    if finalized.get("committedHandoff") != flight["confirmedHandoff"]:
        raise ValueError("Agent did not confirm the queued turn's handoff.")
    if any(finalized.get(key) != flight[key] for key in ("activationId", "epoch", "gameTurn", "handoff")):
        raise ValueError("Agent finalized another activation.")
    return finalized


def finish_turn(agent, flight, history):
    finalized = finalize_turn(agent, flight)
    while True:
        before = history.cursor
        page = agent.events(flight["activationId"], before)
        if not page["available"]:
            break
        history.append(page["events"])
        if page["nextCursor"] != history.cursor or (page["hasMore"] and history.cursor == before):
            raise ValueError("Final history cursor did not match its durable mirror.")
        if not page["hasMore"]:
            break
    history.output.finish_line()
    return finalized
