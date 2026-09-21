"""Background archive work using a persisted, activation-specific handoff proof.

Workers never mutate scheduler state or read the current game. The scheduler
publishes their results and owns retries, ordering, budgets and checkpoint barriers.
"""

import time


def finish_turn(agent, flight, history):
    payload = {
        "activationId": flight["activationId"],
        "epoch": flight["epoch"],
        "gameTurn": flight["gameTurn"],
        "committedHandoff": flight["confirmedHandoff"],
    }
    status = agent.call("finish_handoff", **payload)
    deadline = time.monotonic() + 45
    while status["phase"] not in agent.TERMINAL:
        if time.monotonic() >= deadline:
            raise ValueError("Agent shutdown has not completed; its pending turn finalization is retained.")
        time.sleep(0.1)
        status = agent.call("status", activationId=flight["activationId"])
    finalized = agent.call("finalize", **payload)
    if finalized.get("committedHandoff") != flight["confirmedHandoff"]:
        raise ValueError("Agent did not confirm the queued turn's handoff.")
    if any(finalized.get(key) != flight[key] for key in ("activationId", "epoch", "gameTurn", "handoff")):
        raise ValueError("Agent finalized another activation.")
    while not flight.get("eventsUnsupported"):
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
    return finalized
