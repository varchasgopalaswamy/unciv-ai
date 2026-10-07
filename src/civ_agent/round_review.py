"""A shared quiet phase between rounds, with independent reviewers in parallel."""

import time

from civ_agent.strategic_review import review_due


def review_round(scheduler, status):
    from civ_agent.scheduler import RunnerError

    boundary = status["turn"]
    turn = max(0, boundary - 1)
    anchors = scheduler.state.setdefault("reviewAnchors", {})
    war_turn = status.get("warDeclarationTurn")
    forced = type(war_turn) is int and war_turn <= turn and war_turn > scheduler.state.get("reviewedWarTurn", -1)

    def due(name, interval):
        anchor = anchors.get(name)
        return review_due(turn, interval) if type(anchor) is not int else turn - anchor >= interval

    participants = (
        list(scheduler.agents)
        if boundary == 0 or forced
        else [name for name, interval in scheduler.review_intervals.items() if due(name, interval)]
    )

    def call(name, stage):
        fields = {"epoch": status["epoch"], "gameTurn": boundary, "stage": stage}
        if stage == "start":
            # Once an event resets the interval clock, scheduler-selected review
            # turns no longer necessarily satisfy the service's original modulo.
            fields["forceReview"] = True
        agent = scheduler.agents[name]
        # Capturing a position can take time. Metadata polls are short requests
        # and use the loop's heartbeat, without spawning a worker per poll.
        result = (
            scheduler.turns.call_agent(agent, "review_phase", **fields)
            if stage == "start"
            else agent.call("review_phase", **fields)
        )
        if (
            (result.get("civilization"), result.get("epoch"), result.get("gameTurn"))
            != (name, status["epoch"], boundary)
            or type(result.get("ready")) is not bool
            or "pendingReview" not in result
        ):
            raise RunnerError("Invalid shared review response; stopped for inspection.")
        pending = result["pendingReview"]
        if pending is not None and (
            not isinstance(pending, dict)
            or pending.get("turn") != turn
            or pending.get("status") != "PENDING"
            or not isinstance(pending.get("job"), str)
            or type(pending.get("attempts")) is not int
            or pending["attempts"] < 0
        ):
            raise RunnerError("Invalid pending review response; stopped for inspection.")
        return result

    def release():
        for name in scheduler.agents:
            call(name, "release")
        scheduler.turns.release()
        scheduler.round_open = True

    if scheduler.state["reviewedBoundary"] == boundary:
        if not scheduler.round_open:
            release()
        return True
    if not participants:
        scheduler.state["reviewedBoundary"] = boundary
        scheduler.save()
        if not scheduler.round_open:
            release()
        return True

    current, next_heartbeat = status, 0

    def can_continue(*, force=False):
        nonlocal current, next_heartbeat
        requested = scheduler.store.requested() or ("pause" if scheduler.interrupted else None)
        if requested:
            scheduler.note("stopped" if requested == "stop" else "paused", "Operator requested " + requested + ".")
            return False
        if force or time.monotonic() >= next_heartbeat:
            current = scheduler.operator.call("heartbeat", leaseId=scheduler.lease)["status"]
            scheduler.check_status(current)
            if any(current[key] != status[key] for key in ("serverInstance", "handoff", "turn", "activeCivilization")):
                raise RunnerError("Host changed during the shared review; resume to reconcile.")
            if current["phase"] == "HALTED":
                raise RunnerError("Host is HALTED.")
            scheduler.check_agent_health()
            next_heartbeat = time.monotonic() + 3
        reason = scheduler.budget_reason(current)
        if reason:
            scheduler.note("budget_exhausted", reason)
            return False
        return True

    scheduler.collect_finalizations(wait=True)
    if not can_continue():
        return False
    reason = " because war was declared" if forced else ""
    scheduler.output.line("Status", f"Preparing shared strategic review after round {turn}{reason}.")
    scheduler.state["reviewPhase"] = {
        "gameTurn": boundary,
        "reviewTurn": turn,
        "stage": "Finishing communication",
        "civilizations": participants,
        "trigger": "war_declaration" if forced else "interval",
    }
    scheduler.save()
    for name in scheduler.agents:
        call(name, "hold")
    while True:
        if not can_continue():
            return False
        if all(call(name, "hold")["ready"] for name in scheduler.agents):
            break
        time.sleep(0.25)

    # No active player or conversation remains. Both human and model mutations
    # are fenced by the host, and ordinary gateway calls stay behind the barrier.
    scheduler.turns.quiesce()
    scheduler.operator.call("barrier", leaseId=scheduler.lease, pauseReason="STRATEGIC_REVIEW")
    scheduler.round_open = False
    scheduler.state["reviewPhase"]["stage"] = "Reviewing"
    scheduler.save()
    for name in participants:
        if not can_continue(force=True):
            return False
        call(name, "start")
    while True:
        pending = []
        for name in participants:
            progress = call(name, "status")
            if progress["pendingReview"] is not None or not progress["ready"]:
                pending.append(name)
        # Recheck operator requests and budgets even when the last report finished.
        if not can_continue(force=not pending):
            return False
        if not pending:
            break
        if scheduler.state["reviewPhase"]["civilizations"] != pending:
            scheduler.state["reviewPhase"]["civilizations"] = pending
            scheduler.save()
        time.sleep(0.25)

    scheduler.state["reviewedBoundary"] = boundary
    for name in participants:
        anchors[name] = turn
    if forced:
        scheduler.state["reviewedWarTurn"] = war_turn
    scheduler.state.pop("reviewPhase", None)
    scheduler.save()
    release()
    scheduler.output.line("Status", "All strategic reviews ready; resuming play and communication.")
    return True
