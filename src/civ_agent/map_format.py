"""Lossless shared defaults for repeated fields in player-visible map tiles."""

import copy
import json
from collections import Counter

from civ_agent.protocol import JsonObject


def encoded(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def compact_tiles(tiles: list[JsonObject]) -> JsonObject:
    full = {"tiles": copy.deepcopy(tiles)}
    if len(tiles) < 2:
        return full
    # Keep the fields used to scan a map directly on every tile. A default may
    # only cover a field actually present on every source tile: missing is not null.
    fields = set.intersection(*(set(tile) for tile in tiles)) - {"position", "terrain", "visible", "units", "yields"}
    defaults = {}
    rows = copy.deepcopy(tiles)
    for field in sorted(fields):
        values = Counter(encoded(tile[field]) for tile in tiles)
        value = max(values, key=lambda item: (values[item], item))
        if values[value] < 2:
            continue
        defaults[field] = json.loads(value)
        for tile in rows:
            if encoded(tile[field]) == value:
                del tile[field]
    compact = {"tileDefaults": defaults, "tiles": rows}
    return compact if defaults and len(encoded(compact)) < len(encoded(full)) else full


def expand_tiles(region: JsonObject) -> list[JsonObject]:
    """Reconstruct exact tile objects, including explicit null/false/empty overrides."""
    return [copy.deepcopy({**region.get("tileDefaults", {}), **tile}) for tile in region["tiles"]]
