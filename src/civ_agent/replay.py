"""Verify and export the private native replay archive without running the game."""

import gzip
import hashlib
import json
import re
from collections.abc import Iterator
from decimal import Decimal
from pathlib import Path


class ReplayArchive:
    def __init__(self, checkpoint: Path):
        saved = json.loads(checkpoint.read_text())
        cursor = saved.get("replay")
        if cursor is None:
            raise ValueError("This checkpoint has no replay archive.")
        self.directory = checkpoint.with_name(checkpoint.name + ".replay")
        self.sequence = cursor["sequence"]
        self.sha256 = cursor["sha256"]
        self.expected = json.loads(saved["gameJson"], parse_float=Decimal)
        self.communication_streams = saved.get("communication", {}).get("streams", {})
        self._chain = None

    def _frame(self, sequence: int, digest: str, *, exact_numbers=False) -> dict:
        if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise ValueError(f"Invalid replay digest at frame {sequence}.")
        with gzip.open(self.directory / f"{sequence}-{digest}.json.gz", "rb") as stream:
            raw = stream.read()
        if hashlib.sha256(raw).hexdigest() != digest:
            raise ValueError(f"Replay digest mismatch at frame {sequence}.")
        frame = json.loads(raw, parse_float=Decimal if exact_numbers else float)
        if frame["version"] != 1 or frame["sequence"] != sequence:
            raise ValueError(f"Invalid replay frame {sequence}.")
        return frame

    def frames(self, *, exact_numbers=False) -> Iterator[dict]:
        # Walk back from the committed cursor. Interrupted writes may have left
        # files at the same sequence or later sequences outside this chain.
        if self._chain is None:
            chain = []
            digest = self.sha256
            for sequence in range(self.sequence, -1, -1):
                frame = self._frame(sequence, digest)
                chain.append(digest)
                digest = frame["previous"]
            if digest is not None:
                raise ValueError("Replay chain does not start at frame zero.")
            self._chain = chain[::-1]
        for sequence, digest in enumerate(self._chain):
            yield self._frame(sequence, digest, exact_numbers=exact_numbers)

    def verify(self) -> dict:
        world = None
        events = 0
        for frame in self.frames(exact_numbers=True):
            if frame["world"] is not None:
                world = frame["world"]
            if world is None:
                raise ValueError("Replay is missing its starting world.")
            apply_changes(world, frame["changes"])
            if world_hash(world) != frame["worldSha256"]:
                raise ValueError(f"Reconstructed world differs at frame {frame['sequence']}.")
            events += len(frame["events"])
        if world != self.expected:
            raise ValueError("Reconstructed world differs from the committed checkpoint.")
        return {"frames": self.sequence + 1, "events": events, "final_world_matches": True}


def world_hash(world: dict) -> str:
    def canonical(value):
        if isinstance(value, dict):
            keys = sorted(value, key=lambda key: key.encode("utf-16-be"))
            return "{" + ",".join(f"{canonical(key)}:{canonical(value[key])}" for key in keys) + "}"
        if isinstance(value, list):
            return "[" + ",".join(map(canonical, value)) + "]"
        if isinstance(value, Decimal):
            if not value:
                return "0"
            number = format(value, "f")
            return number.rstrip("0").rstrip(".") if "." in number else number
        return json.dumps(value, ensure_ascii=False)

    return hashlib.sha256(canonical(world).encode()).hexdigest()


def apply_changes(world: dict, changes: list[dict]) -> None:
    for change in changes:
        *parents, key = change["path"]
        target = world
        for component in parents:
            target = target[component]
        if change.get("remove"):
            del target[key]
        else:
            target[key] = change["value"]


def replay_civilizations(first_frame: dict) -> list[str]:
    return list(
        dict.fromkeys(event["civilization"] for event in first_frame["events"] if event["kind"] == "viewStarted")
    )


class ReplayPerspective:
    def __init__(self, civilization: str):
        self.civilization = civilization
        self.unit_ids: dict[int, int] = {}
        self.epoch = None

    def local_id(self, global_id: int) -> int:
        return self.unit_ids.setdefault(global_id, len(self.unit_ids) + 1)

    def chapter(self, frame: dict) -> dict | None:
        if frame["sequence"] == 0 and self.civilization not in replay_civilizations(frame):
            raise ValueError("The archive has no perspective for this civilization.")
        if self.epoch == frame["epoch"]:
            return None
        self.epoch = frame["epoch"]
        self.unit_ids.clear()
        return {"kind": "chapter", "epoch": self.epoch, "handoff": frame["handoff"], "civilization": self.civilization}

    def project(self, event: dict) -> dict | None:
        civilization, kind = self.civilization, event["kind"]
        if kind in {"unitMoved", "unitPlaced", "unitRemoved"}:
            saw_from = civilization in event["visibleBefore"]
            saw_to = civilization in event.get("visibleAfter", [])
            if not (saw_from or saw_to):
                return None
            if kind == "unitMoved" and not (saw_from and saw_to):
                kind = "unitAppeared" if saw_to else "unitDisappeared"
            return {
                "kind": kind,
                "turn": event["turn"],
                "unit": self.local_id(event["unit"]),
                "owner": event["owner"],
                "name": event["name"],
                **({"from": event["from"]} if saw_from else {}),
                **({"to": event["to"]} if saw_to else {}),
                **({"transit": event["transit"]} if event["owner"] == civilization and kind == "unitMoved" else {}),
            }
        if kind == "visibilityReset":
            return {"kind": kind, "turn": event["turn"]}
        if event.get("civilization") != civilization or kind not in {
            "visibility",
            "exploration",
            "tiles",
            "combat",
            "turnStarted",
        }:
            return None
        result = {key: value for key, value in event.items() if key != "activeCivilization"}
        if kind == "tiles":
            result["tiles"] = [
                {**tile, "units": [{**unit, "unit": self.local_id(unit["unit"])} for unit in tile["units"]]}
                for tile in event["tiles"]
            ]
        return result


def perspective(archive: ReplayArchive, civilization: str) -> Iterator[dict]:
    view = ReplayPerspective(civilization)
    for frame in archive.frames():
        if chapter := view.chapter(frame):
            yield chapter
        for event in frame["events"]:
            if projected := view.project(event):
                yield projected
