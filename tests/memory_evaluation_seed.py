"""Operator-only seeds for isolated evaluations; never edit an existing match."""

import hashlib
import json
from pathlib import Path

from civ_agent.civilization_state import CivilizationState
from civ_agent.tls import write_private_json


def seed_memory(root: Path, source: Path, civilization: str, epoch: str, starting_turn: int):
    raw = source.read_bytes()
    seed = json.loads(raw)
    if seed["civilization"] != civilization:
        raise ValueError("Memory seed belongs to another civilization")
    directory = root / "agent-services" / civilization / "civilization"
    if directory.exists():
        raise ValueError("Memory evaluation requires a fresh private store")
    entries = seed["records"]
    if any(type(entry["turn"]) is not int or not 0 <= entry["turn"] < starting_turn for entry in entries):
        raise ValueError("Seed memories must precede the evaluated turn")
    memory = CivilizationState(directory, civilization)
    try:
        memory.bind(epoch)
        for index, entry in enumerate(sorted(entries, key=lambda r: r["turn"])):
            job = f"evaluation-seed-{index}"
            memory.start_job(job, "gameplay", {}, game_turn=entry["turn"])
            result = memory.patch(job, [{"op": "create", "content": entry["content"]}], "seed")
            if not result["changes"][0]["accepted"]:
                raise ValueError(f"Rejected evaluation seed: {result['changes'][0]['reason']}")
            memory.finish_job(job, True)
        write_private_json(
            root / "memory-seed.json",
            {**seed, "source": str(source.resolve()), "sha256": hashlib.sha256(raw).hexdigest()},
        )
    finally:
        memory.engine.dispose()
