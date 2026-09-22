"""Background archive work using a persisted, activation-specific handoff proof.

Workers never mutate scheduler state or read the current game. The scheduler
publishes their results and owns retries, ordering, budgets and checkpoint barriers.
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
    """Finish memory and archive work using the original committed handoff proof."""
    payload = {
        "activationId": flight["activationId"],
        "epoch": flight["epoch"],
        "gameTurn": flight["gameTurn"],
        "committedHandoff": flight["confirmedHandoff"],
    }
    status = _idempotent_call(agent, "finish_handoff", **payload)
    deadline = time.monotonic() + status["memoryTimeoutSeconds"] + 30
    last_heartbeat = time.monotonic()
    while status["phase"] not in agent.TERMINAL:
        if time.monotonic() >= deadline:
            raise ValueError("Post-turn memory work has not completed; its pending finalization is retained.")
        time.sleep(0.1)
        operation = "heartbeat" if time.monotonic() - last_heartbeat >= 3 else "status"
        status = agent.call(operation, activationId=flight["activationId"])
        if operation == "heartbeat":
            last_heartbeat = time.monotonic()
    finalized = _idempotent_call(agent, "begin_finalize", **payload)
    deadline = time.monotonic() + 300
    while finalized.get("finalizationState") != "COMPLETED":
        if finalized.get("finalizationState") == "FAILED":
            raise ValueError("Archive finalization failed: " + finalized.get("finalizationError", "unknown error"))
        if time.monotonic() >= deadline:
            raise ValueError("Archive finalization has not completed; its pending proof is retained.")
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
    archive = agent.archive(flight["activationId"])
    if archive["gameTurn"] != flight["gameTurn"]:
        raise ValueError("Agent returned another turn's archive.")
    history.publish_archive(archive)
    if finalized.get("memoryPhase") and finalized["phase"] == "FAILED":
        raise ValueError("Post-turn memory work failed: " + finalized.get("error", finalized["phase"]))
    return finalized
