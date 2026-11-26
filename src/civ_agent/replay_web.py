"""Compose a browser replay from committed outcomes and civilization-owned records."""

import base64
import hashlib
import json
import os
import sqlite3
from collections import defaultdict
from contextlib import closing
from importlib.resources import files
from itertools import chain
from pathlib import Path
from tempfile import NamedTemporaryFile

from civ_agent.civilization_paths import civilization_directory_name
from civ_agent.game_config import ResolvedGameConfig
from civ_agent.replay import ReplayArchive, ReplayPerspective, apply_changes, replay_civilizations
from civ_agent.replay_art import city_health_rules, replay_art, resource_rules
from civ_agent.replay_details import (
    committed_city_details,
    committed_progress,
    committed_tile_details,
    observed_construction_estimates,
    position_key,
)
from civ_agent.replay_relations import record_relations, saved_relations
from civ_agent.replay_statistics import ranking_series, usage_series
from civ_agent.tls import regular_file


def _text(value) -> str:
    return value.strip() if isinstance(value, str) else ""


def _annotation(title, *text, **fields):
    return {"title": title, "text": [s for value in text if (s := _text(value))], **fields}


def _notebook(root, civilization):
    path = root / "coordinator" / civilization_directory_name(civilization) / "notebook" / "memory.sqlite3"
    if not path.exists() and not path.is_symlink():
        return {}, {}
    regular_file(path, private=True)
    reasons, reviews = {}, {}
    with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as connection:
        connection.execute("BEGIN")
        for epoch, kind, request, reason in connection.execute(
            """
            SELECT j.epoch, json_extract(r.body, '$.command.kind'),
                   json_extract(r.body, '$.native.operationId'), json_extract(r.body, '$.intent.reason')
            FROM records r JOIN jobs j ON r.job = j.id
            WHERE r.category = 'decision' AND json_extract(r.body, '$.outcome') = 'confirmed'
              AND json_extract(r.body, '$.native.transport') = 'player_command'
              AND json_type(r.body, '$.intent.reason') = 'text'
            ORDER BY r.rowid
            """
        ):
            if isinstance(kind, str) and isinstance(request, str) and _text(reason):
                reasons[epoch, request, kind] = reason
        for epoch, source_turn, target_turn, raw in connection.execute(
            """
            SELECT json_extract(m.value, '$.scope.epoch'), r.turn,
                   coalesce(json_extract(m.value, '$.target_turn'), r.turn), r.advice
            FROM reviews r JOIN meta m ON json_extract(m.value, '$.scope.activation') = r.job
              AND coalesce(json_extract(m.value, '$.source_turn'), r.turn) = r.turn
            WHERE r.status = 'COMPLETED' AND r.advice IS NOT NULL
              AND m.key LIKE 'nativeReviewCycle:%'
              AND m.key = 'nativeReviewCycle:' || coalesce(json_extract(m.value, '$.target_turn'), r.turn)
              AND json_extract(m.value, '$.committed') = 1
              AND json_extract(m.value, '$.status') = 'READY'
            ORDER BY r.turn
            """
        ):
            advice = json.loads(raw)
            review = advice.get("review", {})
            preferred = review.get("preferredPlan")
            plans = [
                {
                    "title": _text(plan.get("name")),
                    "preferred": index == preferred,
                    "objective": _text(plan.get("objective")),
                    "investment": _text(plan.get("investment")),
                    "tradeoff": _text(plan.get("relativeOutcome")),
                    "risks": _text(plan.get("risks")),
                    "firstActions": [_text(text) for text in plan.get("firstActions", [])],
                    "expectedObservations": [_text(text) for text in plan.get("expectedObservations", [])],
                }
                for index, plan in enumerate(review.get("plans", []), 1)
            ]
            diplomacy = review.get("diplomacy") or {}
            reviews[epoch, target_turn] = _annotation(
                "Strategic review",
                advice.get("assessment"),
                review.get("outcomeAssessment"),
                review.get("reconsiderWhen"),
                kind="review",
                turn=target_turn,
                sourceTurn=source_turn,
                horizonTurns=review.get("horizonTurns"),
                assumptions=[_text(item.get("assessment")) for item in review.get("materialAssumptions", [])],
                diplomacy={
                    "assessment": _text(diplomacy.get("assessment")),
                    "initiatives": [
                        {
                            "players": [_text(player) for player in item.get("players", [])],
                            "plan": item.get("plan"),
                            **{
                                field: _text(item.get(field))
                                for field in ("objective", "incentives", "approach", "nextStep", "reconsiderWhen")
                            },
                        }
                        for item in diplomacy.get("initiatives", [])
                    ],
                },
                plans=plans,
                turnOnly=True,
            )
    return reasons, reviews


def _diplomacy(delivery):
    kind, body = delivery["kind"], delivery["body"]
    author = _text(body.get("author"))
    partner = _text(body.get("partner"))
    title = {
        "message": "Public message" if body.get("public") else "Private message",
        "offer": "Proposed commitment",
        "consent": _text(body.get("decision")).replace("_", " ").capitalize() or "Response",
        "trade": "Trade · " + _text(body.get("status")).replace("_", " ").lower(),
        "engine": _text(body.get("action")).replace("_", " ").capitalize() or "Game outcome",
        "agreement": "Agreement · " + _text(body.get("change") or body.get("status")).replace("_", " ").lower(),
        "diplomacy": "Diplomatic action",
    }.get(kind)
    if title is None:
        return None
    parties = [name for name in (body.get("participants") or [author, partner]) if _text(name)]
    clauses = []
    for clause in body.get("clauses", []):
        item = clause.get("item", {})
        name = _text((clause.get("city") or {}).get("name")) or _text(item.get("name")) or _text(item.get("type"))
        amount = item.get("amount")
        quantity = f" × {amount}" if type(amount) is int and amount > 1 else ""
        if name:
            duration = item.get("duration")
            term = f" for {duration} turns" if type(duration) is int and duration > 0 else ""
            clauses.append(f"{_text(clause.get('from'))} → {_text(clause.get('to'))} · {name}{quantity}{term}")
    if friendship := body.get("friendship"):
        clauses.append(f"{friendship.get('name', 'Declaration of Friendship')} · {friendship['duration']} turns")
    note = _annotation(
        title,
        body.get("text"),
        body.get("terms"),
        body.get("conditions"),
        body.get("reason"),
        body.get("failure"),
        *clauses,
        author=author,
        parties=parties,
        kind=kind,
    )
    return note


def _delivery_key(delivery):
    return (delivery.get("id"), delivery["kind"], json.dumps(delivery["body"], sort_keys=True))


def _player_labels(state_dir: Path, civilizations):
    path = state_dir / "resolved.json"
    if not path.is_file():
        return {}
    config = ResolvedGameConfig.model_validate_json(path.read_bytes())
    return {
        player["civilization"]: player.get("model")
        or {"built_in_ai": "Unciv AI", "human": "Human"}.get(player["controller"])
        for player in config.player_records()
        if player["civilization"] in civilizations
    }


def web_payload(
    archive: ReplayArchive, state_dir: Path, civilization: str | None = None, *, resources=None, building_health=None
):
    frames = archive.frames()
    first = next(frames)
    roster = replay_civilizations(first)
    if civilization is not None and civilization not in roster:
        raise ValueError("The archive has no perspective for this civilization.")
    views = {name: ReplayPerspective(name) for name in roster if civilization in {None, name}}
    if not views:
        raise ValueError("The archive has no civilization perspectives.")
    notebooks = {name: _notebook(state_dir, name) for name in views}
    construction = {
        name: observed_construction_estimates(state_dir, name, archive.expected.get("turns", 0)) for name in views
    }
    played = {name: {"map": [], "decisions": [], "diplomacy": []} for name in views}
    recorded_deliveries = defaultdict(set)
    turn_ends = {}
    epoch_turn_ends = {}
    turns = []
    relations = []
    step = 0
    visible = {name: set() for name in views}
    tile_details = {name: {} for name in views}
    previous_progress = {}
    world = None

    def mark_turn(epoch, turn):
        if not turns or turns[-1][1] != turn:
            turns.append([step, turn])
        turn_ends[turn] = step
        epoch_turn_ends[epoch, turn] = step

    for frame in chain([first], frames):
        frame_start = step
        initial = not turns
        epoch = frame["epoch"]
        baseline_turn = frame["world"].get("turns", 0) if frame["world"] is not None else turns[-1][1]
        chapters = [(name, chapter) for name, view in views.items() if (chapter := view.chapter(frame)) is not None]
        if chapters:
            if not initial:
                step += 1
            mark_turn(epoch, baseline_turn)
            for name, chapter in chapters:
                visible[name].clear()
                tile_details[name].clear()
                previous_progress.pop(name, None)
                played[name]["map"].append([step, chapter])
        for event in frame["events"]:
            kind = event["kind"]
            owner = event.get("civilization")
            projected = [(name, item) for name, view in views.items() if (item := view.project(event)) is not None]
            command = kind == "input" and event.get("type") == "command" and owner in views
            diplomatic = _diplomacy(event["delivered"]) if kind == "diplomacy" and owner in views else None
            # Every map event shares the same clock across viewpoints. A native
            # handoff can contain several ordinary moves, including AI moves.
            if not (projected or command or diplomatic):
                continue
            if not initial:
                step += 1
            mark_turn(epoch, event.get("turn", baseline_turn))
            for name, item in projected:
                played[name]["map"].append([step, item])
                if kind == "visibilityReset":
                    visible[name].clear()
                elif kind == "visibility":
                    visible[name].difference_update(map(position_key, item["fogged"]))
                    visible[name].update(map(position_key, item["visible"]))
            if command:
                payload = event["payload"]
                request, receipt = payload["request"], payload["receipt"]
                reasons, reviews = notebooks[owner]
                turn = event["turn"]
                reason = reasons.get((epoch, request["requestId"], request["kind"]))
                # An end-turn receipt can arrive after the game has entered the next turn.
                if request["kind"] != "END_TURN":
                    if review := reviews.pop((epoch, turn), None):
                        played[owner]["decisions"].append([step, review])
                title = _text(receipt.get("actionSummary")) or request["kind"].replace("_", " ").capitalize()
                played[owner]["decisions"].append([step, _annotation(title, reason, turn=turn)])
            if diplomatic:
                recorded_deliveries[owner].add(_delivery_key(event["delivered"]))
                played[owner]["diplomacy"].append([step, {**diplomatic, "turn": event["turn"]}])

        if frame["world"] is not None:
            world = frame["world"]
        if world is None:
            raise ValueError("Replay is missing its starting world.")
        apply_changes(world, frame["changes"])
        relations_now = saved_relations(world, views)
        relations_changed = bool(relations_now and (not relations or relations[-1][1] != relations_now))
        world_tiles = {
            position_key(tile.get("position", {})): tile for tile in world.get("tileMap", {}).get("tileList", [])
        }
        world_cities = {
            position_key(city.get("location", {})): committed_city_details(city, building_health)
            for civ in world.get("civilizations", [])
            for city in civ.get("cities", [])
        }
        world_turn = world.get("turns", baseline_turn)
        details = {}
        for civ in world.get("civilizations", []):
            name = civ.get("civID", civ.get("civName"))
            if name not in views or not any(field in civ for field in ("cities", "tech", "policies")):
                continue
            progress = committed_progress(civ, construction[name].get((epoch, world_turn)))
            known = set((progress["research"] or {}).get("known", []))
            changed = []
            for position in sorted(visible[name] & world_tiles.keys()):
                item = committed_tile_details(
                    world_tiles[position], known, resources or {}, city=world_cities.get(position)
                )
                if item != tile_details[name].get(position, {}):
                    tile_details[name][position] = item
                    x, y = position
                    changed.append({"position": {"x": x, "y": y}, "details": item})
            if changed or progress != previous_progress.get(name):
                details[name] = {"kind": "checkpointDetails", "tiles": changed, "progress": progress}
                previous_progress[name] = progress
        if details or relations_changed:
            if not initial and step == frame_start:
                step += 1
            mark_turn(epoch, world_turn)
            for name, event in details.items():
                played[name]["map"].append([step, event])
            if relations_changed:
                record_relations(relations, step, relations_now)

    for name in views:
        for key, note in notebooks[name][1].items():
            if key in epoch_turn_ends:
                played[name]["decisions"].append([epoch_turn_ends[key], note])
        played[name]["decisions"].sort(key=lambda row: row[0])
        for delivery in archive.communication_streams.get(name, []):
            turn = delivery["body"].get("turn")
            if turn in turn_ends and _delivery_key(delivery) not in recorded_deliveries[name]:
                if note := _diplomacy(delivery):
                    # A legacy trade's turn can describe the original proposal,
                    # even if this delivery is a later status update.
                    if delivery["kind"] == "trade" and delivery["body"].get("status") != "PENDING":
                        played[name]["diplomacy"].append([step, {**note, "timingUnknown": True}])
                    else:
                        played[name]["diplomacy"].append([turn_ends[turn], {**note, "turn": turn, "turnOnly": True}])
        played[name]["diplomacy"].sort(key=lambda row: row[0])
    return {
        "version": 1,
        "lastStep": step,
        "turns": turns,
        "civilizations": played,
        "playerLabels": _player_labels(state_dir, views),
        "outcome": archive.expected.get("victoryData"),
        "relations": relations,
        "statistics": ranking_series(archive.expected, views)
        | usage_series(state_dir, views, turns[0][1], turns[-1][1]),
    }


def write_web_replay(
    archive: ReplayArchive,
    state_dir: Path,
    civilization: str | None,
    output: Path,
    *,
    unciv_dir: Path | None = None,
    title: str | None = None,
    highlights=None,
):
    rules = archive.expected.get("gameParameters", {})
    base = rules.get("baseRuleset")
    resources = {} if rules.get("mods") else resource_rules(unciv_dir, base)
    building_health = None if rules.get("mods") else city_health_rules(unciv_dir, base)
    payload = web_payload(archive, state_dir, civilization, resources=resources, building_health=building_health)
    if title:
        payload["title"] = title
    if highlights:
        payload["highlights"] = {name: notes for name, notes in highlights.items() if name in payload["civilizations"]}
    write_web_payload(payload, output, archive.expected, unciv_dir=unciv_dir)


def write_web_payload(payload, output: Path, world, *, unciv_dir: Path | None = None):
    base_ruleset = world.get("gameParameters", {}).get("baseRuleset")

    def script_data(value):
        return json.dumps(value, separators=(",", ":"), ensure_ascii=True).replace("<", r"\u003c")

    html = _render_web_replay(script_data(payload), script_data(replay_art(unciv_dir, base_ruleset=base_ruleset)))
    with NamedTemporaryFile(mode="w", encoding="utf-8", dir=output.parent) as temporary:
        temporary.write(html)
        temporary.flush()
        os.link(temporary.name, output)


def _render_web_replay(payload_json: str, art_json: str) -> str:
    assets = files("civ_agent").joinpath("replay_assets")
    template = assets.joinpath("viewer.html").read_text(encoding="utf-8")
    css = assets.joinpath("viewer.css").read_text(encoding="utf-8")
    js = (
        "\n".join(
            assets.joinpath(name).read_text(encoding="utf-8")
            for name in ("viewer.js", "map_controls.js", "progress.js", "statistics.js")
        )
        + "\ndraw();\n"
    )

    def csp_hash(content):
        return base64.b64encode(hashlib.sha256(content.encode()).digest()).decode()

    return (
        template.replace("@CSS_HASH@", csp_hash(css))
        .replace("@JS_HASH@", csp_hash(js))
        .replace("@CSS@", css)
        .replace("@JS@", js)
        .replace("@ART@", art_json)
        .replace("@PAYLOAD@", payload_json)
    )
