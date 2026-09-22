"""Read existing operator accounting for display without changing scheduling or making API calls."""

import json


def number(value):
    return f"{value:,}" if type(value) is int and value >= 0 else "unavailable"


def seconds(value):
    return f"{value / 1000:.1f}s" if type(value) is int and value >= 0 else "unavailable"


def api_calls(calls):
    if not isinstance(calls, dict):
        return None
    total = 0
    for key, value in calls.items():
        if not key.startswith("tool:"):
            continue
        count = value.get("calls") if isinstance(value, dict) else None
        if type(count) is not int or count < 0:
            return None
        total += count
    return total


def memory_edits_text(counts):
    if not isinstance(counts, dict):
        return "Memory edits unavailable"
    summary = (
        f"Memory edits {number(counts.get('accepted'))} accepted · {number(counts.get('rejected'))} rejected "
        f"in {number(counts.get('rejectedBatches'))}/{number(counts.get('batches'))} batches"
    )
    if counts.get("complete") is not True:
        summary += " (partial accounting)"
    reasons = counts.get("rejectionReasons", {})
    if reasons:
        summary += "; " + ", ".join(f"{reason}: {number(value)}" for reason, value in sorted(reasons.items()))
    return summary


def turn_metrics(state_directory, flight):
    try:
        with (state_directory / "telemetry/turns.jsonl").open() as source:
            for line in source:
                value = json.loads(line)
                if not isinstance(value, dict) or not isinstance(value.get("turn"), dict):
                    continue
                turn = value["turn"]
                if all(turn.get(key) == flight.get(key) for key in ("epoch", "civilization", "gameTurn", "handoff")):
                    if flight.get("activationId") in value.get("jobs", []):
                        return value
    except (OSError, ValueError, TypeError):
        pass
    return None


def display_turn(renderer, state_directory, flight, status):
    civilization = flight["civilization"]
    metrics = turn_metrics(state_directory, flight)
    # Without a matching host export, status describes this activation only.
    usage = metrics.get("usage", {}) if metrics else status.get("usage", {})
    complete = metrics.get("accountingComplete") is True if metrics else status.get("usageComplete") is True
    available = complete or status.get("usageReported") is True
    if metrics and isinstance(usage, dict):
        available = available or any(type(value) is int and value > 0 for value in usage.values())
    scope = "turn" if metrics else "activation"
    renderer.line("Done", f"Turn {flight['gameTurn']} complete", civilization=civilization)
    timing = metrics or status
    if timing.get("gameplayElapsedMs") is not None:
        elapsed = seconds(timing["gameplayElapsedMs"]) + " gameplay"
    elif metrics:
        elapsed = seconds(metrics.get("elapsedMs")) + " turn elapsed (including pauses)"
    else:
        elapsed = "gameplay time unavailable"
    if timing.get("finalizationElapsedMs") is not None:
        elapsed += " · " + seconds(timing["finalizationElapsedMs"]) + " bookkeeping"
    if not metrics:
        elapsed += " (activation)"
    renderer.line("Time", elapsed, civilization=civilization)
    total = api_calls(metrics.get("hostCalls")) if metrics else None
    renderer.line("Calls", f"Game API {number(total)} · Model requests unavailable", civilization=civilization)
    renderer.line("Memory", memory_edits_text((metrics or {}).get("memoryEdits")), civilization=civilization)
    if available and isinstance(usage, dict):
        renderer.line(
            "Tokens",
            f"Input {number(usage.get('input_tokens'))} (cached {number(usage.get('cached_input_tokens'))})\n"
            f"Output {number(usage.get('output_tokens'))} (reasoning {number(usage.get('reasoning_output_tokens'))})\n"
            f"{scope.capitalize()} usage {'complete' if complete else 'partial'}; cached/reasoning counts are included above.",
            civilization=civilization,
        )
    else:
        renderer.line("Tokens", "Usage unavailable", civilization=civilization)
