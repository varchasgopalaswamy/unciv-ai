"""Draw civilization observations on a hex map and encode them with FFmpeg."""

import hashlib
import math
import os
import subprocess
from collections.abc import Iterable
from html import escape
from pathlib import Path
from tempfile import TemporaryDirectory

WIDTH, HEIGHT = 1280, 720
TERRAIN = {
    "Grassland": "#71936a",
    "Plains": "#ac9c60",
    "Desert": "#c4a370",
    "Tundra": "#8b9991",
    "Snow": "#c4d2d4",
    "Mountain": "#69767b",
    "Coast": "#3a7791",
    "Ocean": "#254d66",
    "Lakes": "#3a7791",
}
COLORS = ("#ffc678", "#f297ac", "#bba6ef", "#91d7cd", "#a8d68f", "#c9d9f1")


def coordinate(position):
    return position["x"], position["y"]


def hex_center(coordinates):
    x, y = coordinates
    return 1.5 * (y - x), -math.sqrt(3) * (x + y) / 2


class CivilizationMap:
    def __init__(self, name):
        self.name = name
        self.tiles = {}
        self.visible = set()
        self.units = {}
        self.positions = {}
        self.turn = 0
        self.caption = "Opening position"
        self.path = []

    def color(self, owner):
        if owner == self.name:
            return "#f8e89d"
        return COLORS[hashlib.sha256(owner.encode()).digest()[0] % len(COLORS)]

    def apply(self, event):
        kind = event["kind"]
        self.turn = event.get("turn", self.turn)
        self.path = []
        if kind == "chapter":
            self.__init__(self.name)
        elif kind == "visibilityReset":
            self.visible.clear()
        elif kind == "visibility":
            self.visible.difference_update(map(coordinate, event["fogged"]))
            self.visible.update(map(coordinate, event["visible"]))
        elif kind == "exploration":
            for tile in event["tiles"]:
                self.tiles.setdefault(coordinate(tile["position"]), {}).update(tile)
        elif kind == "tiles":
            changed = {coordinate(tile["position"]): tile for tile in event["tiles"]}
            for unit, position in list(self.positions.items()):
                if position in changed:
                    self.positions.pop(unit)
                    self.units.pop(unit)
            for position, tile in changed.items():
                self.tiles[position] = tile
                for unit in tile["units"]:
                    self.units[unit["unit"]] = unit
                    self.positions[unit["unit"]] = position
        elif kind == "combat":
            self.caption = event["report"].get("text") or "Combat observed"
        elif kind == "turnStarted":
            self.caption = f"{self.name} begins its turn"
        elif kind.startswith("unit"):
            identifier = event["unit"]
            self.caption = f"{event['owner']} · {event['name']} "
            if "to" in event:
                self.positions[identifier] = coordinate(event["to"])
                self.units[identifier] = event
                self.caption += "moves" if kind == "unitMoved" else "appears"
                if "from" in event:
                    self.path = [coordinate(point) for point in [event["from"], *event.get("transit", []), event["to"]]]
            else:
                self.positions.pop(identifier, None)
                self.units.pop(identifier, None)
                self.caption += "is removed" if kind == "unitRemoved" else "leaves sight"
        return bool(self.tiles) and (kind in {"tiles", "turnStarted", "combat"} or kind.startswith("unit"))

    def render(self):
        points = [hex_center(position) for position in self.tiles]
        xs, ys = zip(*points, strict=True)
        left, right, top, bottom = min(xs), max(xs), min(ys), max(ys)
        radius = min(34, (WIDTH - 100) / (right - left + 3), (HEIGHT - 150) / (bottom - top + 3))

        def screen(position):
            x, y = hex_center(position)
            return WIDTH / 2 + (x - (left + right) / 2) * radius, HEIGHT / 2 + 10 + (y - (top + bottom) / 2) * radius

        offsets = [(math.cos(index * math.pi / 3), math.sin(index * math.pi / 3)) for index in range(6)]
        drawing = [
            f'<svg xmlns="http://www.w3.org/2000/svg" width="{WIDTH}" height="{HEIGHT}">',
            '<rect width="100%" height="100%" fill="#111b23"/>',
            '<defs><marker id="arrow" markerWidth="7" markerHeight="7" refX="5" refY="3" orient="auto">'
            '<path d="M0 0 L6 3 L0 6Z" fill="#fff2bf"/></marker></defs>',
        ]
        cities = []
        for position, tile in self.tiles.items():
            x, y = screen(position)
            vertices = " ".join(f"{x + dx * radius:.2f},{y + dy * radius:.2f}" for dx, dy in offsets)
            fill = TERRAIN.get(tile.get("terrain"), "#273743")
            stroke = self.color(tile["owner"]) if tile.get("owner") else "#152835"
            opacity = 1 if position in self.visible else 0.38
            drawing.append(
                f'<polygon points="{vertices}" fill="{fill}" stroke="{stroke}" opacity="{opacity}" stroke-width="1.4"/>'
            )
            if city := tile.get("city"):
                cities.append(f'<rect x="{x - 3:.1f}" y="{y - 3:.1f}" width="6" height="6" fill="{stroke}"/>')
                cities.append(
                    f'<text x="{x + 6:.1f}" y="{y - 5:.1f}" fill="#ffffff" font-size="14" '
                    f'font-family="DejaVu Sans, sans-serif" opacity="{max(opacity, 0.65)}">{escape(city)}</text>'
                )
        drawing.extend(cities)
        for identifier, position in self.positions.items():
            unit = self.units[identifier]
            if position not in self.visible and unit["owner"] != self.name:
                continue
            x, y = screen(position)
            color = self.color(unit["owner"])
            drawing.append(f'<circle cx="{x:.1f}" cy="{y + 4:.1f}" r="{max(2, radius / 4):.1f}" fill="{color}"/>')
        if len(self.path) > 1:
            route = " ".join(f"{x:.1f},{y:.1f}" for x, y in map(screen, self.path))
            drawing.append(
                f'<polyline points="{route}" fill="none" stroke="#fff2bf" stroke-width="3" marker-end="url(#arrow)"/>'
            )
        drawing.extend(
            [
                '<g font-family="DejaVu Sans, sans-serif" fill="#e0ecf1">',
                f'<text x="36" y="46" font-size="26">{escape(self.name)} · Turn {self.turn}</text>',
                f'<text x="36" y="{HEIGHT - 30}" font-size="18">{escape(self.caption[:130])}</text>',
                "</g></svg>",
            ]
        )
        return "\n".join(drawing)


def write_movie(events: Iterable[dict], civilization: str, output: Path) -> None:
    scene = CivilizationMap(civilization)
    with TemporaryDirectory(dir=output.parent, prefix=".replay-movie-") as folder:
        root = Path(folder)
        frames = 0
        for event in events:
            if scene.apply(event):
                (root / f"frame-{frames:08d}.svg").write_text(scene.render(), encoding="utf-8")
                frames += 1
        if not frames:
            raise ValueError("The archive contains no visible map frames for this civilization.")
        video = root / "replay.mp4"
        result = subprocess.run(
            [
                "ffmpeg",
                "-hide_banner",
                "-loglevel",
                "error",
                "-framerate",
                "4",
                "-i",
                str(root / "frame-%08d.svg"),
                "-c:v",
                "libx264",
                "-pix_fmt",
                "yuv420p",
                "-vf",
                "tpad=start_duration=1:stop_duration=2:start_mode=clone:stop_mode=clone",
                "-movflags",
                "+faststart",
                str(video),
            ],
            capture_output=True,
            text=True,
        )
        if result.returncode:
            raise ValueError("FFmpeg could not encode the map movie. SVG decoding and H.264 encoding are required.")
        video.chmod(0o600)
        os.link(video, output)
