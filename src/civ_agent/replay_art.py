"""Embed the selected Unciv checkout's native texture atlases for offline playback."""

import base64
import json
import re
from pathlib import Path

from civ_agent.checkout import repository_root

ATLASES = {
    "Tilesets": ("TileSets/HexaRealm/Tiles/", "TileSets/FantasyHex/Tiles/", "TileSets/FantasyHex/Borders/"),
    "AbsoluteUnits": ("TileSets/AbsoluteUnits/Units/",),
    "ConstructionIcons": ("UnitIcons/", "UnitFlagIcons/"),
    "Icons": ("ResourceIcons/",),
    "NationIcons": ("NationIcons/",),
}
BASE_RULESETS = ("Civ V - Vanilla", "Civ V - Gods & Kings")


def _assets(unciv_dir: Path | None) -> Path | None:
    if unciv_dir is not None:
        return unciv_dir.expanduser() / "android/assets"
    packaged = Path("/opt/unciv/engine/android/assets")
    if packaged.is_dir():
        return packaged
    try:
        return repository_root() / "vendor/Unciv/android/assets"
    except ValueError:
        return None


def _regions(atlas: Path, prefixes: tuple[str, ...]):
    """Read the unrotated, untrimmed regions of Unciv's bundled libGDX atlases."""
    source = atlas.read_text(encoding="utf-8") + "\n"
    for match in re.finditer(r"^([^\s][^\r\n]*)\n((?:[ \t]+[^\r\n]*\n)+)", source, re.MULTILINE):
        name, fields = match.groups()
        if not name.startswith(prefixes):
            continue
        props = dict(line.strip().split(": ", 1) for line in fields.splitlines())
        if props.get("rotate") != "false" or props.get("offset") != "0, 0" or props["size"] != props["orig"]:
            raise ValueError(f"Unsupported sprite packing for {name} in {atlas.name}.")
        yield name, [int(value) for value in (props["xy"] + ", " + props["size"]).split(",")]


def _unciv_json(path: Path):
    # Unciv's rules files accept comments and trailing commas.
    tokens = re.compile(r'"(?:\\.|[^"\\])*"|//[^\r\n]*|/\*[\s\S]*?\*/')
    source = path.read_text(encoding="utf-8")
    source = tokens.sub(lambda token: token[0] if token[0].startswith('"') else "", source)
    return json.loads(
        re.sub(r'"(?:\\.|[^"\\])*"|,(?=\s*[}\]])', lambda token: token[0] if token[0] != "," else "", source)
    )


def _rules(assets: Path, base_ruleset: str | None) -> Path:
    return assets / "jsons" / (base_ruleset if base_ruleset in BASE_RULESETS else BASE_RULESETS[0])


def resource_rules(unciv_dir: Path | None, base_ruleset: str | None):
    assets = _assets(unciv_dir)
    if assets is None or base_ruleset not in BASE_RULESETS:
        return {}
    path = _rules(assets, base_ruleset) / "TileResources.json"
    if not path.is_file():
        return {}
    return {
        item["name"]: (item.get("revealedBy"), item.get("resourceType") == "Strategic") for item in _unciv_json(path)
    }


def city_health_rules(unciv_dir: Path | None, base_ruleset: str | None):
    assets = _assets(unciv_dir)
    if assets is None or base_ruleset not in BASE_RULESETS:
        return None
    path = _rules(assets, base_ruleset) / "Buildings.json"
    if not path.is_file():
        return None
    return {building["name"]: building.get("cityHealth", 0) for building in _unciv_json(path)}


def replay_art(unciv_dir: Path | None = None, *, base_ruleset: str | None = None):
    assets = _assets(unciv_dir)
    if assets is None or not (assets / "Tilesets.atlas").is_file():
        if unciv_dir is not None:
            raise ValueError(f"Unciv's texture atlases were not found under {assets}.")
        return {}
    sheets, regions = {}, {}
    for atlas, prefixes in ATLASES.items():
        for name, bounds in _regions(assets / f"{atlas}.atlas", prefixes):
            regions[name] = [atlas, *bounds]
        png = (assets / f"{atlas}.png").read_bytes()
        sheets[atlas] = "data:image/png;base64," + base64.b64encode(png).decode("ascii")
    rules = _rules(assets, base_ruleset)
    nation_colors = {
        nation["name"]: [nation["outerColor"], nation.get("innerColor") or [17, 17, 17]]
        for nation in _unciv_json(rules / "Nations.json")
    }
    civilian_units = [
        unit["name"]
        for unit in _unciv_json(rules / "Units.json")
        if not (unit.get("strength", 0) > 0 or unit.get("rangedStrength", 0) > 0)
    ]
    return {
        "sheets": sheets,
        "regions": regions,
        "tileRules": _unciv_json(assets / "jsons/TileSets/HexaRealm.json")["ruleVariants"],
        "nationColors": nation_colors,
        "civilianUnits": civilian_units,
        "resourceTypes": {item["name"]: item["resourceType"] for item in _unciv_json(rules / "TileResources.json")},
        "researchTree": [
            {
                "name": tech["name"],
                "column": column["columnNumber"],
                "row": tech["row"],
                "era": column["era"],
                "requires": tech.get("prerequisites", []),
            }
            for column in _unciv_json(rules / "Techs.json")
            for tech in column["techs"]
        ],
        "policyTree": [
            {
                "name": branch["name"],
                "era": branch["era"],
                "policies": [
                    {key: policy[key] for key in ("name", "column", "row", "requires") if key in policy}
                    for policy in branch["policies"]
                ],
            }
            for branch in _unciv_json(rules / "Policies.json")
        ],
    }
