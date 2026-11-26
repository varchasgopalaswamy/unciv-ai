"""Project Unciv rankings and reported model usage into replay charts."""

import json
import re
import sqlite3
from collections import Counter, defaultdict
from contextlib import closing
from pathlib import Path

_RANKINGS = {
    "S": "Score",
    "N": "Population",
    "P": "Production per turn",
    "C": "Food surplus per turn",
    "G": "Gold treasury",
    "T": "Territory tiles",
    "F": "Military strength",
    "H": "Happiness",
    "W": "Technologies researched",
    "A": "Policies adopted",
    "E": "Tiles explored",
}
_ENCODED = re.compile(r"([A-Za-z])(-?\d+)")
_TOKEN_METRICS = {
    "Cached input tokens": "cached",
    "Uncached input tokens": "uncached",
    "Output tokens": "output",
}
_COST_METRIC = "Estimated API cost (USD)"

# OpenAI Standard short-context text rates. Values are cents per million
# cached input, uncached input, and output tokens, respectively.
# https://developers.openai.com/api/docs/pricing
_STANDARD_RATES = {
    "gpt-6.2-astra": (40, 800, 4000),
    "gpt-6.2-sol": (10, 200, 1000),
    "gpt-6-astra": (100, 1000, 5000),
    "gpt-6.1-sol": (10, 200, 1000),
    "gpt-6.1-terra": (5, 100, 500),
    "gpt-6-luna": (1, 10, 50),
}


def ranking_series(world, included):
    histories = {
        civ.get("civID", civ.get("civName")): civ.get("statsHistory", {}) for civ in world.get("civilizations", [])
    }
    series = {label: {} for label in _RANKINGS.values()}
    final_turn = world.get("turns", 0)
    for name in included:
        for recorded_turn, encoded in sorted(histories.get(name, {}).items(), key=lambda row: int(row[0])):
            turn = int(recorded_turn)
            if turn > final_turn:
                continue
            rankings = _ENCODED.findall(encoded) if isinstance(encoded, str) else encoded.items()
            for key, value in rankings:
                if label := _RANKINGS.get(key):
                    series[label].setdefault(name, []).append([turn, int(value)])
    return {label: data for label, data in series.items() if data}


def usage_series(state_dir: Path, included, first_turn: int, last_turn: int):
    """Read one cumulative usage record per model job from the host ledger."""
    path = state_dir / "exploration.json.accounting.sqlite3"
    if not path.is_file():
        return {}

    by_civ = defaultdict(lambda: defaultdict(Counter))
    with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as db:
        if not db.execute("SELECT 1 FROM sqlite_schema WHERE type = 'table' AND name = 'records'").fetchone():
            return {}
        for name, turn, model, raw_usage in db.execute(
            """
            SELECT json_extract(payload, '$.civilization'), json_extract(payload, '$.gameTurn'),
                   json_extract(payload, '$.telemetry.model'), json_extract(payload, '$.usage')
            FROM records WHERE collection = 'jobs'
            """
        ):
            if name not in included or type(turn) is not int or turn > last_turn or raw_usage is None:
                continue
            usage = json.loads(raw_usage)
            if not isinstance(usage, dict):
                continue
            cached = usage.get("cached_input_tokens")
            total_input = usage.get("input_tokens")
            output = usage.get("output_tokens")
            if type(cached) is not int or type(total_input) is not int or type(output) is not int:
                continue
            if min(cached, total_input, output) < 0:
                continue
            if cached > total_input or not (total_input or output):
                continue
            uncached = total_input - cached
            counts = by_civ[name][max(first_turn, turn)]
            counts.update(cached=cached, uncached=uncached, output=output)
            if rates := _STANDARD_RATES.get(model):
                counts["cost"] += sum(
                    count * rate for count, rate in zip((cached, uncached, output), rates, strict=True)
                )
            else:
                counts["unpriced"] += 1

    series = {label: {} for label in (*_TOKEN_METRICS, _COST_METRIC)}
    for name in included:
        if name not in by_civ:
            continue
        for metric in series.values():
            metric[name] = []
        running = Counter()
        for turn in range(first_turn, last_turn + 1):
            running.update(by_civ[name].get(turn, {}))
            for label, field in _TOKEN_METRICS.items():
                series[label][name].append([turn, running[field]])
            if not running["unpriced"]:
                series[_COST_METRIC][name].append([turn, running["cost"] / 100_000_000])
    return {label: data for label, data in series.items() if any(data.values())}
