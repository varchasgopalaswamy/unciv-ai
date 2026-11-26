"""Reconstruct a turn-based web replay from saves that predate native recording."""

import json
import re
import sqlite3
from collections import defaultdict
from contextlib import closing
from pathlib import Path

from civ_agent.civilization_paths import civilization_directory_name
from civ_agent.replay_details import archived_observation, observed_progress, observed_tile_details
from civ_agent.replay_relations import observed_relations, snapshot_relations
from civ_agent.replay_statistics import ranking_series, usage_series
from civ_agent.replay_web import _annotation, _diplomacy, _notebook, _player_labels, _text
from civ_agent.tls import regular_file

_MOVE = re.compile(r": \((-?\d+), (-?\d+)\) to \((-?\d+), (-?\d+)\);")
_ATTACK = re.compile(r" attacked .+ at \((-?\d+), (-?\d+)\): dealt \d+")
_MOVE_COMMANDS = {"MOVE_UNIT", "SET_UNIT_DESTINATION", "REBASE_UNIT", "PARADROP"}


def _position(x, y):
    return {"x": x, "y": y}


def _key(position):
    return position.get("x", 0), position.get("y", 0)


def _action_path(command, summary):
    if command.get("kind") in _MOVE_COMMANDS and (match := _MOVE.search(summary)):
        x, y, end_x, end_y = map(int, match.groups())
        source, target = _position(x, y), _position(end_x, end_y)
        if source != target:
            return [source, target]
    if command.get("kind") == "ATTACK" and (match := _ATTACK.search(summary)):
        source = command.get("attackFrom")
        target = _position(*map(int, match.groups()))
        if isinstance(source, dict) and source != target:
            return [source, target]
    return []


def _snapshot_event(turn, civilization, observations, previous_tiles, unit_ids):
    tiles, visible, units = {}, [], []
    for tile in observations["map"]["tiles"]:
        x, y = _key(tile["position"])
        position = _position(x, y)
        details = observed_tile_details(tile)
        if tile["visible"]:
            details.pop("observedTurn", None)
        tiles[x, y] = {
            "position": position,
            "terrain": tile.get("terrain"),
            "features": tile.get("features") or [],
            "owner": tile.get("owner"),
            "city": tile.get("cityName"),
            "improvement": tile.get("improvement"),
            "details": details,
        }
        if tile["visible"]:
            visible.append(position)
            for unit in tile["units"]:
                if unit["civilization"] != civilization:
                    units.append(
                        {
                            "unit": -len(units) - 1,
                            "name": unit["name"],
                            "owner": unit["civilization"],
                            "position": position,
                            "health": unit["health"],
                        }
                    )
    for unit in observations["map"]["units"]:
        handle = unit["handle"]
        units.append(
            {
                "unit": unit_ids.setdefault(handle, len(unit_ids) + 1),
                "name": unit["name"],
                "owner": civilization,
                "position": unit["position"],
                "health": unit["health"],
            }
        )
    return tiles, {
        "kind": "turnSnapshot",
        "turn": turn,
        "tiles": [tile for key, tile in tiles.items() if tile != previous_tiles.get(key)],
        "removed": [_position(*key) for key in previous_tiles.keys() - tiles.keys()],
        "visible": visible,
        "units": units,
        "progress": observed_progress(observations, civilization),
    }


def _read_notebook(root, civilization, epoch, final_turn):
    directory = root / "coordinator" / civilization_directory_name(civilization) / "notebook"
    path = directory / "memory.sqlite3"
    if not path.exists() and not path.is_symlink():
        return {}, {}, {}, {}
    regular_file(path, private=True)
    snapshots, actions, relations = {}, defaultdict(list), {}
    with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as db:
        db.execute("BEGIN")
        heads = {
            head["identity"]["job"]: head
            for (raw,) in db.execute("SELECT value FROM meta WHERE key LIKE 'nativeGatewayJob:%'")
            if "identity" in (head := json.loads(raw))
        }
        jobs = db.execute(
            """
            SELECT id, game_turn, purpose, rowid FROM jobs WHERE epoch = ?
            ORDER BY CASE purpose WHEN 'gameplay' THEN 0 WHEN 'military' THEN 1 WHEN 'economy' THEN 2 ELSE 3 END,
                     rowid DESC
            """,
            (epoch,),
        ).fetchall()
        candidates = defaultdict(list)
        last_active_job = max((rowid for _, turn, _, rowid in jobs if turn is not None), default=0)
        for identifier, turn, purpose, rowid in jobs:
            if identifier not in heads:
                continue
            if turn is not None and turn <= final_turn and purpose in {"gameplay", "military", "economy"}:
                candidates[turn].append(heads[identifier])
            elif turn is None and rowid > last_active_job:
                # Off-turn communication can observe the final world after this player's last activation.
                candidates[final_turn].append(heads[identifier])
        previous_tiles, unit_ids = {}, {}
        for turn, options in sorted(candidates.items()):
            for head in options:
                if observed := archived_observation(head, directory, civilization, epoch, turn):
                    previous_tiles, snapshots[turn] = _snapshot_event(
                        turn, civilization, observed, previous_tiles, unit_ids
                    )
                    relations[turn] = observed_relations(observed, civilization)
                    break
        seen = set()
        for (raw,) in db.execute(
            """
            SELECT records.body FROM records JOIN jobs ON records.job = jobs.id
            WHERE jobs.epoch = ? AND records.category = 'decision'
              AND json_extract(records.body, '$.outcome') = 'confirmed'
            ORDER BY records.rowid
            """,
            (epoch,),
        ):
            record = json.loads(raw)
            turn = record.get("turn")
            command = record.get("command") or {}
            request = command.get("requestId")
            if type(turn) is not int or not request or request in seen:
                continue
            seen.add(request)
            summary = _text((record.get("facts") or {}).get("actionSummary"))
            title = summary or (_text(command.get("kind")) or "Confirmed action").replace("_", " ").capitalize()
            reason = (record.get("intent") or {}).get("reason")
            actions[turn].append((_annotation(title, reason, turn=turn), _action_path(command, summary)))
    _, reviews = _notebook(root, civilization)
    return (
        snapshots,
        actions,
        {turn: note for (review_epoch, turn), note in reviews.items() if review_epoch == epoch},
        relations,
    )


def _territory_snapshots(world, civilization):
    changes = defaultdict(lambda: {"tiles": [], "removed": []})
    for tile in world["tileMap"]["tileList"]:
        position = _position(*_key(tile.get("position", {})))
        owned = False
        for turn, (owner, center) in sorted(tile.get("history", {}).items(), key=lambda item: int(item[0])):
            if owner == civilization:
                changes[int(turn)]["tiles"].append(
                    {
                        "position": position,
                        "terrain": tile["baseTerrain"],
                        "owner": owner,
                        "city": {"C": "Capital", "R": "City"}.get(center),
                    }
                )
            elif owned:
                changes[int(turn)]["removed"].append(position)
            owned = owner == civilization
    return {turn: {"kind": "territorySnapshot", "turn": turn, **event} for turn, event in changes.items()}


def snapshot_web_payload(checkpoint, world, root: Path, civilization: str | None = None):
    labels = _player_labels(root, {civ["civName"] for civ in world["civilizations"]})
    if not labels or civilization is not None and civilization not in labels:
        raise ValueError("The save has no matching player in its resolved roster.")
    roster = [name for name in labels if civilization in {None, name}]
    final_turn = world["turns"]
    snapshots, actions, reviews, relations = {}, {}, {}, {}
    played = {}
    snapshot_counts = {}
    for name in roster:
        snapshots[name], actions[name], reviews[name], relations[name] = _read_notebook(
            root, name, checkpoint["epoch"], final_turn
        )
        snapshot_counts[name] = len(snapshots[name])
        played[name] = {"map": [], "decisions": [], "diplomacy": [], "territory": not snapshots[name]}
        if not snapshots[name]:
            snapshots[name] = _territory_snapshots(world, name)

    turns, step = [], 0
    for turn in range(world.get("historyStartTurn", 0), final_turn + 1):
        turns.append([step, turn])
        for name in roster:
            if snapshot := snapshots[name].get(turn):
                played[name]["map"].append([step, snapshot])
            if review := reviews[name].get(turn):
                played[name]["decisions"].append([step, review])
        for name in roster:
            for note, path in actions[name].get(turn, []):
                step += 1
                played[name]["map"].append([step, {"kind": "snapshotAction", "path": path, "text": note["title"]}])
                played[name]["decisions"].append([step, note])
        step += 1
    last_step = step - 1
    turn_steps = {turn: at for at, turn in turns}
    streams = checkpoint.get("communication", {}).get("streams", {})
    for name in roster:
        for delivery in streams.get(name, []):
            turn = delivery["body"].get("turn")
            if note := _diplomacy(delivery):
                pending = delivery["kind"] != "trade" or delivery["body"].get("status") == "PENDING"
                if pending and turn in turn_steps:
                    played[name]["diplomacy"].append([turn_steps[turn], {**note, "turn": turn, "turnOnly": True}])
                else:
                    played[name]["diplomacy"].append([last_step, {**note, "timingUnknown": True}])
        played[name]["diplomacy"].sort(key=lambda row: row[0])
    outcome = world.get("victoryData")
    return {
        "version": 1,
        "reconstruction": True,
        "lastStep": last_step,
        "turns": turns,
        "civilizations": played,
        "playerLabels": {name: labels[name] for name in roster},
        "outcome": outcome,
        "relations": snapshot_relations(world, roster, turns, last_step, relations, streams),
        "statistics": ranking_series(world, roster)
        | usage_series(root, roster, world.get("historyStartTurn", 0), final_turn),
    }, snapshot_counts
