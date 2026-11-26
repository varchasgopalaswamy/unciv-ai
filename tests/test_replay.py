import gzip
import hashlib
import json
import sqlite3
from contextlib import closing

import pytest

from civ_agent.cli import main
from civ_agent.game_config import GameConfig
from civ_agent.replay import ReplayArchive, world_hash
from civ_agent.replay_relations import observed_relations, saved_relations, snapshot_relations
from civ_agent.replay_statistics import ranking_series, usage_series
from civ_agent.replay_web import _notebook, web_payload


def test_replay_verifies_world_and_exports_only_witnessed_movement(tmp_path):
    checkpoint = tmp_path / "exploration.json"
    directory = tmp_path / "exploration.json.replay"
    directory.mkdir()
    initial = {"turns": 0, "civilizations": [{"civName": "Rome"}, {"civName": "Egypt"}], "temporary": True}
    final = {**initial, "turns": 1}
    del final["temporary"]

    def write(sequence, previous, **fields):
        raw = json.dumps(
            {
                "version": 1,
                "sequence": sequence,
                "previous": previous,
                "epoch": "fixture",
                "handoff": sequence,
                "world": None,
                "changes": [],
                "events": [],
                "worldSha256": world_hash(initial if sequence == 0 else final),
                **fields,
            }
        ).encode()
        digest = hashlib.sha256(raw).hexdigest()
        (directory / f"{sequence}-{digest}.json.gz").write_bytes(gzip.compress(raw))
        return digest

    start = write(0, None, world=initial, events=[{"kind": "viewStarted", "civilization": "Rome"}])
    end = write(
        1,
        start,
        changes=[
            {"path": ["turns"], "value": 1},
            {"path": ["temporary"], "remove": True},
        ],
        events=[
            {
                "kind": "unitMoved",
                "turn": 1,
                "unit": 42,
                "owner": "Egypt",
                "name": "Warrior",
                "from": {"x": 12, "y": 12},
                "to": {"x": 13, "y": 13},
                "transit": [{"x": 12, "y": 13}],
                "visibleBefore": ["Egypt", "Rome"],
                "visibleAfter": ["Egypt"],
            }
        ],
    )
    write(2, end, events=[{"kind": "uncommitted"}])
    checkpoint.write_text(json.dumps({"gameJson": json.dumps(final), "replay": {"sequence": 1, "sha256": end}}))
    output = tmp_path / "rome.jsonl"
    assert main(["replay", "--state-dir", str(tmp_path), "--civilization", "Rome", "--output", str(output)]) == 0
    _, move = [json.loads(line) for line in output.read_text().splitlines()]
    assert move == {
        "kind": "unitDisappeared",
        "turn": 1,
        "unit": 1,
        "owner": "Egypt",
        "name": "Warrior",
        "from": {"x": 12, "y": 12},
    }
    assert ReplayArchive(checkpoint).verify()["frames"] == 2
    (directory / f"0-{start}.json.gz").write_bytes(gzip.compress(b"{}"))
    with pytest.raises(ValueError, match="digest mismatch at frame 0"):
        ReplayArchive(checkpoint).verify()


def test_web_replay_combines_civilization_views_and_places_owned_notes_on_the_timeline(tmp_path):
    names = ["Egypt", "Greece", "Rome"]
    world = {"turns": 0, "civilizations": [{"civName": name} for name in names]}
    directory = tmp_path / "exploration.json.replay"
    directory.mkdir()

    def write(sequence, previous, events):
        raw = json.dumps(
            dict(
                version=1,
                sequence=sequence,
                previous=previous,
                epoch="active-epoch",
                handoff=sequence,
                world=world if sequence == 0 else None,
                changes=[],
                events=events,
                worldSha256=world_hash(world),
            )
        ).encode()
        digest = hashlib.sha256(raw).hexdigest()
        (directory / f"{sequence}-{digest}.json.gz").write_bytes(gzip.compress(raw))
        return digest

    first = write(0, None, [{"kind": "viewStarted", "civilization": name, "turn": 0} for name in names])
    private_text = "Meet east of the river. </script><script>throw Error('injected')</script>"

    def message(identifier, text, *, public=False):
        return {
            "id": identifier,
            "kind": "message",
            "body": {
                "author": "Rome",
                "participants": names if public else ["Greece", "Rome"],
                "text": text,
                "public": public,
                "turn": 0,
            },
        }

    private_message = message(2, private_text)
    public_message = message(1, "Our envoys have arrived.", public=True)
    command = {
        "kind": "input",
        "type": "command",
        "civilization": "Rome",
        "turn": 0,
        "payload": {
            "request": {"requestId": "scout-move", "kind": "MOVE_UNIT"},
            "receipt": {"actionSummary": "Moved the scout east."},
        },
    }
    final = write(
        1,
        first,
        [
            command,
            *[
                {"kind": "diplomacy", "civilization": name, "turn": 0, "delivered": private_message}
                for name in ["Greece", "Rome"]
            ],
        ],
    )
    checkpoint = tmp_path / "exploration.json"
    checkpoint.write_text(
        json.dumps(
            {
                "gameJson": json.dumps(world),
                "replay": {"sequence": 1, "sha256": final},
                "communication": {"streams": {"Rome": [public_message, private_message], "Egypt": [public_message]}},
            }
        )
    )
    for name, epoch, reason in (
        ("Rome", "active-epoch", "Scout the river crossing."),
        ("Egypt", "active-epoch", "An unrelated civilization's private rationale."),
        ("Greece", "old-epoch", "An abandoned rationale."),
    ):
        notebook = tmp_path / "coordinator" / name / "notebook" / "memory.sqlite3"
        notebook.parent.mkdir(parents=True)
        notebook.touch(mode=0o600)
        with closing(sqlite3.connect(notebook)) as db, db:
            db.executescript(
                """
                CREATE TABLE jobs (id TEXT PRIMARY KEY, epoch TEXT);
                CREATE TABLE records (job TEXT, category TEXT, body TEXT);
                CREATE TABLE reviews (job TEXT, turn INTEGER, status TEXT, advice TEXT);
                CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);
                """
            )
            body = {
                "outcome": "confirmed",
                "command": {"kind": "MOVE_UNIT"},
                "native": {"operationId": "scout-move", "transport": "player_command"},
                "intent": {"reason": reason},
            }
            db.execute("INSERT INTO jobs VALUES ('active-job', ?), ('abandoned-job', 'old-epoch')", (epoch,))
            db.execute("INSERT INTO records VALUES ('active-job', 'decision', ?)", (json.dumps(body),))
            body["intent"]["reason"] = "An abandoned rationale."
            db.execute("INSERT INTO records VALUES ('abandoned-job', 'decision', ?)", (json.dumps(body),))
            if name == "Rome":
                db.execute(
                    "INSERT INTO meta VALUES ('nativeReviewCycle:0', ?)",
                    (
                        json.dumps(
                            {
                                "scope": {"activation": "review-job", "epoch": epoch},
                                "status": "READY",
                                "committed": True,
                            }
                        ),
                    ),
                )
                db.execute(
                    "INSERT INTO reviews VALUES ('review-job', 0, 'COMPLETED', ?)",
                    (json.dumps({"assessment": "Secure a defensible crossing."}),),
                )

    archive = ReplayArchive(checkpoint)
    assert archive.verify()["frames"] == 2
    assert web_payload(archive, tmp_path)["playerLabels"] == {}
    config = GameConfig.model_validate(
        {
            "game": {"civilizations": names, "randomize": False},
            "agents": {"model": "gpt-6.1-terra"},
            "players": [
                {"name": "Native", "controller": "built_in_ai"},
                {"name": "Local", "controller": "human"},
                {"name": "Model"},
            ],
        }
    ).resolved()
    (tmp_path / "resolved.json").write_text(config.model_dump_json())
    payload = web_payload(archive, tmp_path)
    views = payload["civilizations"]
    assert list(views) == names
    assert payload["playerLabels"] == {"Egypt": "Unciv AI", "Greece": "Human", "Rome": "gpt-6.1-terra"}
    assert [(step, note["text"][0]) for step, note in views["Rome"]["decisions"]] == [
        (1, "Secure a defensible crossing."),
        (1, "Scout the river crossing."),
    ]
    assert not views["Greece"]["decisions"] and not views["Egypt"]["decisions"]
    assert [(step, note["text"][0]) for step, note in views["Greece"]["diplomacy"]] == [(2, private_text)]
    exact, legacy = [note for _, note in views["Rome"]["diplomacy"]]
    assert exact["text"] == [private_text] and "turnOnly" not in exact
    assert legacy["text"] == ["Our envoys have arrived."] and legacy["turnOnly"]
    assert views["Egypt"]["diplomacy"] == [[payload["lastStep"], legacy]]

    output = tmp_path / "replay.html"
    assert main(["replay", "--state-dir", str(tmp_path), "--web", str(output)]) == 0
    html = output.read_text()
    assert private_text not in html and "An abandoned rationale." not in html
    embedded = html.split('<script id="replay-data" type="application/json">')[1].split("</script>")[0]
    assert json.loads(embedded) == payload
    art = json.loads(html.split('<script id="art-data" type="application/json">')[1].split("</script>")[0])
    assert art["sheets"]["Tilesets"].startswith("data:image/png;base64,iVBORw0KGgo")
    assert art["tileRules"]["Grassland+Forest"] == ["Grassland", "ForestG"]
    assert art["regions"]["TileSets/AbsoluteUnits/Units/Warrior"][0] == "AbsoluteUnits"
    assert art["regions"]["NationIcons/Rome"][0] == "NationIcons"
    assert art["regions"]["ResourceIcons/Iron"][0] == "Icons"
    assert art["resourceTypes"]["Iron"] == "Strategic"
    assert art["regions"]["TileSets/FantasyHex/Borders/ConvexConcaveInner"][0] == "Tilesets"
    assert art["regions"]["UnitFlagIcons/UnitFlagCivilian"][0] == "ConstructionIcons"
    assert art["regions"]["UnitIcons/Warrior"][0] == "ConstructionIcons"
    assert art["nationColors"]["France"] == [[38, 98, 255], [239, 236, 148]]
    assert "Settler" in art["civilianUnits"] and "Warrior" not in art["civilianUnits"]
    assert output.stat().st_mode & 0o777 == 0o600

    subset = web_payload(archive, tmp_path, civilization="Egypt")
    assert list(subset["civilizations"]) == ["Egypt"]
    assert subset["playerLabels"] == {"Egypt": "Unciv AI"}
    assert private_text not in json.dumps(subset) and "Scout the river crossing." not in json.dumps(subset)


def test_snapshot_web_replay_keeps_recorded_endpoints_and_player_scope(tmp_path):
    game = GameConfig.model_validate(
        {
            "game": {"civilizations": ["Rome", "Egypt"], "randomize": False},
            "players": [{"name": "Model"}, {"name": "Native", "controller": "built_in_ai"}],
        }
    ).resolved()
    (tmp_path / "resolved.json").write_text(game.model_dump_json())
    start, stop, goal = [{"x": x, "y": 0} for x in range(3)]
    world = {
        "turns": 1,
        "civilizations": [{"civName": "Rome"}, {"civName": "Egypt"}],
        "tileMap": {
            "tileList": [
                {"position": goal, "baseTerrain": "Plains", "history": {"0": ["Egypt", "C"], "1": ["Rome", "R"]}}
            ],
        },
    }
    message = {"kind": "message", "body": {"author": "Rome", "text": "Roman dispatch", "turn": 0}}
    (tmp_path / "exploration.json").write_text(
        json.dumps({"epoch": "epoch", "gameJson": json.dumps(world), "communication": {"streams": {"Rome": [message]}}})
    )
    notebook = tmp_path / "coordinator" / "Rome" / "notebook"
    archives = notebook / "native-archives"
    archives.mkdir(parents=True)
    db_path = notebook / "memory.sqlite3"
    db_path.touch(mode=0o600)
    with closing(sqlite3.connect(db_path)) as db, db:
        db.executescript(
            """
            CREATE TABLE jobs (id TEXT, epoch TEXT, game_turn INTEGER, purpose TEXT);
            CREATE TABLE meta (key TEXT, value TEXT);
            CREATE TABLE records (job TEXT, category TEXT, body TEXT);
            CREATE TABLE reviews (job TEXT, turn INTEGER, status TEXT, advice TEXT);
            """
        )
        for turn, position in enumerate([start, stop]):
            job = f"job-{turn}"
            identity = {"job": job, "scope": {"epoch": "epoch"}}
            observation = {
                "civilization": "Rome",
                "epoch": "epoch",
                "turn": turn,
                "map": {
                    "tiles": [{"position": position, "terrain": "Grassland", "visible": True, "units": []}],
                    "units": [{"handle": "unit1", "name": "Warrior", "health": 100, "position": position}],
                },
            }
            raw = json.dumps({"identity": identity, "state": {"data": {"observation": observation}}}).encode()
            digest = hashlib.sha256(raw).hexdigest()
            file = archives / f"{digest}.json.gz"
            file.touch(mode=0o600)
            file.write_bytes(gzip.compress(raw))
            db.execute("INSERT INTO jobs VALUES (?, 'epoch', ?, 'gameplay')", (job, turn))
            db.execute(
                "INSERT INTO meta VALUES (?, ?)",
                (f"nativeGatewayJob:{job}", json.dumps({"identity": identity, "archive": digest})),
            )
        command = {
            "turn": 1,
            "outcome": "confirmed",
            "command": {"requestId": "r1", "kind": "SET_UNIT_DESTINATION", "destination": goal},
            "facts": {"actionSummary": "Warrior: (0, 0) to (1, 0); Requested destination (2, 0) not reached."},
            "intent": {"reason": "Scout the valley."},
        }
        db.execute("INSERT INTO records VALUES ('job-1', 'decision', ?)", (json.dumps(command),))

    output = tmp_path / "snapshot.html"
    assert main(["replay", "--state-dir", str(tmp_path), "--from-snapshots", "--web", str(output)]) == 0
    html = output.read_text()
    data = json.loads(html.split('<script id="replay-data" type="application/json">')[1].split("</script>")[0])
    rome, egypt = (data["civilizations"][civ] for civ in ("Rome", "Egypt"))
    assert rome["map"][1][1]["removed"] == [start]
    assert rome["map"][1][1]["units"][0]["position"] == stop
    assert rome["map"][2][1]["path"] == [start, stop]
    assert rome["decisions"][0][1]["text"] == ["Scout the valley."]
    assert rome["diplomacy"][0][1]["text"] == ["Roman dispatch"]
    assert egypt["territory"] and egypt["map"][0][1]["tiles"][0]["owner"] == "Egypt"
    assert egypt["map"][1][1]["removed"] == [goal] and not egypt["diplomacy"]
    assert output.stat().st_mode & 0o777 == 0o600


def test_native_replay_projects_progress_and_details_to_each_visible_perspective(tmp_path):
    directory = tmp_path / "exploration.json.replay"
    directory.mkdir()
    known = {"x": 1, "y": 1}
    hidden = {"x": 2, "y": 2}
    world = {
        "turns": 0,
        "civilizations": [
            {
                "civName": "Rome",
                "cities": [
                    {
                        "name": "Rome",
                        "location": known,
                        "population": {"population": 4},
                        "health": 125,
                        "cityConstructions": {
                            "constructionQueue": ["Scout"],
                            "productionOverflow": 2,
                            "builtBuildings": ["Walls"],
                        },
                    }
                ],
                "tech": {"techsToResearch": ["Iron Working"]},
            },
            {
                "civName": "Egypt",
                "cities": [
                    {
                        "name": "Thebes",
                        "location": hidden,
                        "population": {"population": 8},
                        "cityConstructions": {"constructionQueue": ["Worker"]},
                    }
                ],
            },
        ],
        "tileMap": {
            "tileList": [
                {"position": known, "resource": "Iron", "roadStatus": "Road"},
                {"position": hidden, "resource": "Gold", "roadStatus": "Railroad"},
            ]
        },
    }
    first = dict(
        version=1,
        sequence=0,
        previous=None,
        epoch="active",
        handoff=0,
        changes=[],
        world=world,
        events=[
            {"kind": "viewStarted", "civilization": "Rome", "turn": 0},
            {"kind": "viewStarted", "civilization": "Egypt", "turn": 0},
            {"kind": "visibility", "civilization": "Rome", "turn": 0, "visible": [known], "fogged": []},
        ],
        worldSha256=world_hash(world),
    )
    raw = json.dumps(first).encode()
    digest = hashlib.sha256(raw).hexdigest()
    (directory / f"0-{digest}.json.gz").write_bytes(gzip.compress(raw))
    file = tmp_path / "exploration.json"
    file.write_text(json.dumps({"gameJson": json.dumps(world), "replay": {"sequence": 0, "sha256": digest}}))
    notebook = tmp_path / "coordinator" / "Rome" / "notebook"
    notebook.mkdir(parents=True)
    database = notebook / "memory.sqlite3"
    database.touch(mode=0o600)
    with closing(sqlite3.connect(database)) as db, db:
        db.executescript("""
            CREATE TABLE jobs (id TEXT, epoch TEXT);
            CREATE TABLE meta (key TEXT, value TEXT);
            CREATE TABLE records (job TEXT, category TEXT, body TEXT);
            CREATE TABLE reviews (job TEXT, turn INTEGER, status TEXT, advice TEXT);
        """)
        for job, overflow, turns in (("matching", 2, 3), ("different-overflow", 0, 4)):
            observation = {
                "epoch": "active",
                "civilization": "Rome",
                "turn": 0,
                "map": {"tiles": [{"position": known, "owner": "Rome", "cityName": "Rome"}]},
                "cities": [
                    {
                        "name": "Rome",
                        "constructionQueue": ["Scout"],
                        "development": {"population": 4},
                        "constructionEstimates": {
                            "Scout": {
                                "production": {
                                    "cost": 25,
                                    "stored": 0,
                                    "overflow": overflow,
                                    "turnsIfStartedNow": turns,
                                }
                            }
                        },
                    }
                ],
            }
            head = {"identity": {"scope": {"epoch": "active"}}, "state": {"data": {"observation": observation}}}
            db.execute("INSERT INTO meta VALUES (?, ?)", ("nativeGatewayJob:" + job, json.dumps(head)))
    payload = web_payload(
        ReplayArchive(file),
        tmp_path,
        "Rome",
        resources={"Iron": ("Iron Working", True), "Gold": (None, False)},
        building_health={"Walls": 50},
    )
    result = payload["civilizations"]["Rome"]["map"][-1][1]
    assert result["kind"] == "checkpointDetails"
    assert result["tiles"] == [
        {
            "position": known,
            "details": {"road": "Road", "cityPopulation": 4, "cityHealth": 125, "cityMaxHealth": 250},
        }
    ]
    assert result["progress"]["cities"][0]["queue"] == ["Scout"]
    assert result["progress"]["cities"][0]["turns"] == 3
    assert result["progress"]["cities"][0]["population"] == 4
    assert result["progress"]["research"]["queue"] == ["Iron Working"]
    assert "Thebes" not in json.dumps(payload)


def test_rankings_read_both_save_formats_without_adding_unrecorded_samples():
    world = {
        "turns": 3,
        "civilizations": [
            {"civName": "Rome", "statsHistory": {"2": "S24N12H-3X99", "0": {"S": 9, "N": 2}, "4": "S40"}},
            {"civName": "Egypt", "statsHistory": {"1": "S17"}},
        ],
    }
    assert ranking_series(world, ["Rome"]) == {
        "Score": {"Rome": [[0, 9], [2, 24]]},
        "Population": {"Rome": [[0, 2], [2, 12]]},
        "Happiness": {"Rome": [[2, -3]]},
    }


def test_usage_series_counts_cached_input_and_reasoning_once_and_prices_each_job(tmp_path):
    path = tmp_path / "exploration.json.accounting.sqlite3"
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE records (collection TEXT, key TEXT, payload TEXT, PRIMARY KEY (collection, key))")
        for job, (name, turn, model, total, cached, output) in enumerate(
            [
                ("Rome", 1, "gpt-6.2-astra", 1000, 600, 200),
                ("Rome", 1, "gpt-6.2-sol", 500, 100, 10),
                ("Rome", 3, "unpriced-model", 10, 0, 5),
                ("Rome", 4, "gpt-6.2-astra", 1000, 800, 200),
                ("Egypt", 1, "gpt-6.2-astra", 1000, 800, 200),
            ]
        ):
            report = {
                "civilization": name,
                "gameTurn": turn,
                "telemetry": {"model": model},
                "usage": {
                    "input_tokens": total,
                    "cached_input_tokens": cached,
                    "output_tokens": output,
                    "reasoning_output_tokens": 3,
                },
            }
            db.execute("INSERT INTO records VALUES ('jobs', ?, ?)", (str(job), json.dumps(report)))
    assert usage_series(tmp_path, ["Rome", "Unciv AI"], 0, 3) == {
        "Cached input tokens": {"Rome": [[0, 0], [1, 700], [2, 700], [3, 700]]},
        "Uncached input tokens": {"Rome": [[0, 0], [1, 800], [2, 800], [3, 810]]},
        "Output tokens": {"Rome": [[0, 0], [1, 210], [2, 210], [3, 215]]},
        "Estimated API cost (USD)": {"Rome": [[0, 0], [1, 0.01235], [2, 0.01235]]},
    }


def test_replay_reviews_are_dated_by_target_turn_and_keep_only_committed_advice(tmp_path):
    path = tmp_path / "coordinator" / "Rome" / "notebook" / "memory.sqlite3"
    path.parent.mkdir(parents=True)
    path.touch(mode=0o600)
    with closing(sqlite3.connect(path)) as db, db:
        db.executescript("""
            CREATE TABLE jobs (id TEXT, epoch TEXT);
            CREATE TABLE records (job TEXT, category TEXT, body TEXT);
            CREATE TABLE reviews (job TEXT, turn INTEGER, status TEXT, advice TEXT);
            CREATE TABLE meta (key TEXT, value TEXT);
        """)
        for source, target, committed in ((10, 11, True), (20, 21, False)):
            job = f"review-{source}"
            cycle = {
                "scope": {"activation": job, "epoch": "active"},
                "source_turn": source,
                "target_turn": target,
                "status": "READY",
                "committed": committed,
            }
            advice = {
                "assessment": "Protect the river crossing.",
                "_evidence": {"raw": "private payload"},
                "review": {
                    "preferredPlan": 1,
                    "plans": [{"name": "Guard the crossing", "firstActions": ["Move an archer"]}],
                },
            }
            db.execute("INSERT INTO meta VALUES (?, ?)", (f"nativeReviewCycle:{target}", json.dumps(cycle)))
            db.execute("INSERT INTO reviews VALUES (?, ?, 'COMPLETED', ?)", (job, source, json.dumps(advice)))
    _, reviews = _notebook(tmp_path, "Rome")
    assert list(reviews) == [("active", 11)]
    note = reviews["active", 11]
    assert note["sourceTurn"] == 10 and note["turn"] == 11
    assert note["plans"][0]["preferred"] and note["plans"][0]["firstActions"] == ["Move an archer"]
    assert "private payload" not in json.dumps(note)


def test_replay_relations_use_active_native_agreements_and_direction():
    roster = ["Rome", "Egypt", "Greece"]
    world = {
        "civilizations": [
            {
                "civID": "Rome",
                "diplomacy": {
                    "Greece": {},
                    "Egypt": {
                        "diplomaticStatus": "Peace",
                        "flagsCountdown": {"DeclarationOfFriendship": 3, "ResearchAgreement": 0},
                        "trades": [
                            {
                                "ourOffers": [{"name": "Peace Treaty", "duration": 2}],
                                "theirOffers": [{"name": "Open Borders", "duration": 4}],
                            }
                        ],
                    },
                },
            },
            {"civID": "Egypt"},
            {"civID": "Greece"},
        ]
    }
    assert saved_relations(world, roster) == [
        ["Rome", "Egypt", ["Peace treaty", "Friendship", "Egypt grants open borders"]],
        ["Rome", "Greece", ["War"]],
        ["Egypt", "Greece", ["Not met"]],
    ]
    assert saved_relations(world, ["Rome"]) == []


def test_snapshot_relations_keep_unrecorded_pairs_and_conflicting_reports_distinct():
    observation = {
        "diplomacy": {
            "knownCivilizations": [
                {
                    "civilizationId": "Egypt",
                    "atWar": False,
                    "bilateral": {"friendshipTurns": 4, "youMayEnterTheirTerritory": True},
                },
            ]
        }
    }
    rome = observed_relations(observation, "Rome")
    history = snapshot_relations(
        {"civilizations": [{"civID": "Rome", "diplomacy": {"Egypt": {}}}]},
        ["Rome", "Egypt", "Greece"],
        [(0, 0), (2, 1), (4, 2)],
        5,
        {"Rome": {1: rome, 2: rome}, "Egypt": {2: {("Egypt", "Rome"): ["War"]}}, "Greece": {}},
    )
    assert [step for step, _ in history] == [0, 2, 4, 5]
    assert [rows[0][2] for _, rows in history] == [
        ["Unrecorded"],
        ["Peace", "Friendship", "Egypt grants open borders"],
        ["Reports differ"],
        ["War"],
    ]
    treaty = {
        "kind": "engine",
        "body": {
            "turn": 2,
            "participants": ["Rome", "Egypt"],
            "confirmation": {
                "source": "engine",
                "relationships": {
                    "Rome": {"otherCivilizationId": "Egypt", "atWar": False, "peaceTreatyTurns": 10},
                    "Egypt": {"otherCivilizationId": "Rome", "atWar": False, "peaceTreatyTurns": 10},
                },
            },
        },
    }
    history = snapshot_relations(
        {"civilizations": [{"civID": "Rome", "diplomacy": {"Egypt": {}}}]},
        ["Rome", "Egypt"],
        [(0, 2)],
        1,
        {"Rome": {2: rome}, "Egypt": {2: {("Egypt", "Rome"): ["War"]}}},
        {"Rome": [treaty]},
    )
    assert history[0] == [0, [["Rome", "Egypt", ["Peace treaty"]]]]
