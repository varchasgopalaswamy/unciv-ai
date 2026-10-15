"""Read-only, private reconstruction of routing evidence across benchmark games.

Old routing inputs were not retained. Incoming histories use only earlier event
IDs in the same epoch; outgoing histories remain unknown because call insertion
order does not establish completion order. Missing timestamps remain unknown. No current/future
memory context, private intent, credentials, provider requests, or gameplay
changes are used. Human/assistant review must add labels before paid evaluation.
"""

import argparse
import json
import math
import sqlite3
from collections import Counter, defaultdict
from contextlib import closing
from pathlib import Path

from civ_agent.conversation_context import audience
from civ_agent.decisions_ledger import fingerprint
from civ_agent.diplomacy_store import build_routing_bundle
from civ_agent.protocol import JsonObject
from civ_agent.tls import private_directory, write_private_json


def _json(text, default=None) -> JsonObject:
    try:
        value = json.loads(text) if text is not None else default
        return value if isinstance(value, dict) else {}
    except (TypeError, ValueError):
        return default if isinstance(default, dict) else {}


def _epoch(source):
    return source.rsplit(":", 1)[0]


def _thread_context(player, thread, messages):
    selected = [message for message in messages if audience(message, player) == thread]
    return {
        "threads": [
            {
                "public": thread[0],
                "participants": list(thread[1]),
                "messages": selected[-6:],
                "hasEarlierMessages": len(selected) > 6,
                "omittedForBudget": 0,
            }
        ]
    }


def _case(game, player, direction, identity, thread, bundle, source, *, protected=False):
    group = fingerprint({"game": game, "audience": thread})
    return {
        "id": "observed-"
        + fingerprint({"game": game, "player": player, "direction": direction, "identity": identity})[:24],
        "group": "game-" + fingerprint(game)[:24],
        "threadGroup": "thread-" + group[:24],
        "split": "holdout" if int(fingerprint(game)[:8], 16) % 4 == 0 else "calibration",
        "labelStatus": "unlabeled",
        "direction": direction,
        "protected": protected,
        "source": source,
        "bundle": bundle,
    }


def collect_database(path, root):
    """Use one consistent read-only SQLite transaction per player database."""
    player = path.parent.parent.name
    marker = path.parts.index("agent-services")
    game_path = Path(*path.parts[:marker])
    game = game_path.relative_to(root).as_posix()
    artifact = path.relative_to(root).as_posix()
    cases, outcomes = [], {}
    with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as db:
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA query_only=ON")
        db.execute("BEGIN")
        tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if not {"events", "records", "calls", "jobs"} <= tables:
            return [], {}, {"unavailableSchema": 1}
        events = list(db.execute("SELECT id,source,body FROM events WHERE kind='message' ORDER BY id"))
        deliveries = {}
        routing = defaultdict(list)
        for row in db.execute(
            "SELECT key,version,category,body FROM records WHERE category IN ('delivery','routing') ORDER BY key,version"
        ):
            body = _json(row["body"], {})
            if row["category"] == "delivery":
                deliveries[row["key"]] = body
            else:
                for identifier in body.get("coveredEventIds", []):
                    routing[(body.get("epoch"), identifier)].append(
                        {"recordKey": row["key"], "version": row["version"], "decision": body.get("decision", {})}
                    )
        previous = defaultdict(list)
        for row in events:
            event = _json(row["body"], {})
            message = event.get("body", {})
            epoch = _epoch(row["source"])
            thread = audience(message, player)
            if thread is not None and message.get("author") != player and isinstance(message.get("text"), str):
                bundle = build_routing_bundle(
                    player,
                    thread,
                    [message],
                    _thread_context(player, thread, previous[(epoch, thread)]),
                    current_turn=message.get("turn"),
                )
                bundle["historyCoverage"] = "reconstructed_prior_event_prefix_only"
                source = {
                    "kind": "historical_reconstruction",
                    "artifact": artifact,
                    "game": game,
                    "player": player,
                    "epoch": epoch,
                    "eventId": row["id"],
                    "message": message.get("message"),
                    "originalBundleAvailable": False,
                    "timestampMs": None,
                    "timestampBasis": "unknown; event ID establishes ingestion order only",
                    "contextBasis": "only lower event IDs in this player epoch; un-echoed earlier sends unavailable",
                    "timingKnown": False,
                }
                matches = routing.get((epoch, row["id"]), [])
                protected = bool(message.get("nativeAction")) or any(
                    item["decision"].get("fallback") == "protected" for item in matches
                )
                case = _case(
                    game,
                    player,
                    "incoming",
                    {"source": row["source"], "message": message.get("message")},
                    thread,
                    bundle,
                    source,
                    protected=protected,
                )
                cases.append(case)
                if matches:
                    outcomes[case["id"]] = {
                        "matches": matches,
                        "matchBasis": "coveredEventIds; original coalesced input unavailable",
                        "exactOriginalInput": False,
                    }
            if thread is not None:
                previous[(epoch, thread)].append(message)
                if len(previous[(epoch, thread)]) > 7:
                    del previous[(epoch, thread)][0]
        for row in db.execute("SELECT rowid,job,id,arguments,result FROM calls WHERE name='chat_send' ORDER BY rowid"):
            arguments, result = _json(row["arguments"], {}), _json(row["result"], {})
            if not isinstance(arguments.get("text"), str):
                continue
            recipients = arguments.get("recipients") or ([arguments["to"]] if arguments.get("to") else [])
            thread = (False, tuple(sorted({player, *recipients}))) if recipients else (True, ())
            context, history = {}, {"threads": []}
            job = db.execute("SELECT game_turn FROM jobs WHERE id=?", (row["job"],)).fetchone()
            bundle = build_routing_bundle(player, thread, [], history, current_turn=job["game_turn"] if job else None)
            bundle["proposedMessage"] = {"author": player, "text": arguments["text"]}
            bundle["historyCoverage"] = "unavailable"
            delivery = deliveries.get(result.get("deliveryId"), {})
            time = delivery.get("createdAt")
            source = {
                "kind": "historical_reconstruction",
                "artifact": artifact,
                "game": game,
                "player": player,
                "job": row["job"],
                "callId": row["id"],
                "callRowId": row["rowid"],
                "priorReadRowId": None,
                "originalBundleAvailable": False,
                "timestampMs": round(time * 1000)
                if isinstance(time, (float, int)) and not isinstance(time, bool) and math.isfinite(time)
                else None,
                "timestampBasis": "persisted delivery draft creation"
                if time is not None
                else "unknown; call row ID establishes insertion order only",
                "contextBasis": "unknown; call-row insertion order does not prove read completion; final job snapshots can mutate",
                "timingKnown": False,
            }
            protected = arguments.get("delivery") == "immediate" or bool(arguments.get("nativeAction"))
            members = set(context.get("chatRecipients", [])) if thread[0] else set(thread[1])
            protected |= bool(members & set(context.get("humanPlayers", [])))
            protected |= bool(delivery.get("decision", {}).get("fallback") in {"protected", "immediate"})
            case = _case(
                game,
                player,
                "outgoing",
                {"job": row["job"], "call": row["id"]},
                thread,
                bundle,
                source,
                protected=protected,
            )
            cases.append(case)
            if delivery.get("decision"):
                outcomes[case["id"]] = {
                    "deliveryId": result["deliveryId"],
                    "decision": delivery["decision"],
                    "matchBasis": "chat_send result deliveryId",
                    "exactOriginalInput": False,
                }
    return cases, outcomes, {"databases": 1}


def sample_cases(cases, size):
    """Round-robin available game/direction buckets; empty buckets do not imply equal direction counts."""
    buckets = defaultdict(list)
    for case in cases:
        if not case["protected"]:
            buckets[(case["source"]["game"], case["direction"])].append(case)
    for rows in buckets.values():
        rows.sort(key=lambda case: fingerprint({"id": case["id"], "bundle": case["bundle"]}))
    selected = []
    while len(selected) < size and buckets:
        for key in sorted(tuple(buckets)):
            selected.append(buckets[key].pop(0))
            if not buckets[key]:
                del buckets[key]
            if len(selected) >= size:
                break
    return selected


def collect(root, output, *, sample_size=120):
    root, output = Path(root).resolve(), Path(output).resolve()
    if root == output or root in output.parents:
        raise ValueError("Keep collection artifacts outside the benchmark input tree")
    output = private_directory(output)
    cases, outcomes, counts = {}, {}, Counter()
    for path in sorted(root.rglob("memory.sqlite3")):
        if path.parent.name != "civilization" or "agent-services" not in path.parts:
            continue
        rows, matched, status = collect_database(path, root)
        counts.update(status)
        for case in rows:
            cases.setdefault(case["id"], case)
        outcomes.update(matched)
    ordered = sorted(
        cases.values(),
        key=lambda case: (case["source"]["game"], case["source"]["player"], case["direction"], case["id"]),
    )
    notes = "Read-only historical reconstruction, not original classifier requests. Labels must be added before evaluation. Timing, private objectives and queued drafts are unknown; no future context included. Outcomes are stored separately for blind review. Split is by whole game."
    fixture = {"version": 2, "labelStatus": "unlabeled", "notes": notes, "cases": ordered}
    write_private_json(output / "all-cases.json", fixture)
    selected = sample_cases(ordered, sample_size)
    write_private_json(
        output / "sample-unlabeled.json",
        {
            **fixture,
            "samplingPolicy": "round-robin game/direction, SHA256 order within bucket; no classifier outputs used",
            "cases": selected,
        },
    )
    write_private_json(output / "observed-outcomes.json", outcomes)
    summary = {
        **counts,
        "cases": len(ordered),
        "sampleCases": len(selected),
        "sampleByDirection": dict(Counter(case["direction"] for case in selected)),
        "games": len({case["source"]["game"] for case in ordered}),
        "byDirection": dict(Counter(case["direction"] for case in ordered)),
        "byGame": dict(Counter(case["source"]["game"] for case in ordered)),
        "protected": sum(case["protected"] for case in ordered),
        "matchedOutcomes": len(outcomes),
        "knownDraftTimestamps": sum(case["source"]["timestampMs"] is not None for case in ordered),
        "sourceFingerprint": fingerprint(fixture),
        "sampleFingerprint": fingerprint(selected),
        "limitations": notes,
    }
    write_private_json(output / "summary.json", summary)
    return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--benchmarks-root", type=Path, default=Path("benchmarks"))
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--sample-size", type=int, default=120)
    args = parser.parse_args()
    if args.sample_size < 0:
        parser.error("Sample size must be nonnegative")
    print(json.dumps(collect(args.benchmarks_root, args.output_dir, sample_size=args.sample_size), indent=2))


if __name__ == "__main__":
    main()
