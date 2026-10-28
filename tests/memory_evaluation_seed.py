"""Operator-only seeds for isolated evaluations; never edit an existing match."""

import hashlib
import json
from pathlib import Path

from civ_agent.civilization_state import CivilizationState
from civ_agent.tls import private_directory, write_private_json
from civ_agent.war_state import MilitaryOperation, WarGoals, WarStore, war_opponents


def seed_memory(root: Path, source: Path, civilization: str, epoch: str, starting_turn: int, *, position=None):
    raw = source.read_bytes()
    seed = json.loads(raw)
    if seed["civilization"] != civilization:
        raise ValueError("Memory seed belongs to another civilization")
    directory = root / "agent-services" / civilization / "civilization"
    if directory.exists():
        raise ValueError("Memory evaluation requires a fresh private store")
    entries = seed.get("records", [])
    if any(type(entry["turn"]) is not int or not 0 <= entry["turn"] < starting_turn for entry in entries):
        raise ValueError("Seed memories must precede the evaluated turn")
    wars, operations = seed.get("wars", []), seed.get("operations", [])
    if wars or operations:
        if not position or position.get("turn") != starting_turn:
            raise ValueError("Coordination seeds require the evaluated turn's permitted position")
        if (position.get("civilizationProfile") or {}).get("name") != civilization:
            raise ValueError("Seed position belongs to another civilization")
    seen = set()
    initial_goals = []
    for war in wars:
        if war["opponent"] in seen or war["opponent"] not in war_opponents(position):
            raise ValueError("Seed war must identify a distinct current native opponent")
        seen.add(war["opponent"])
        if type(war["startedTurn"]) is not int or not 0 <= war["startedTurn"] < starting_turn:
            raise ValueError("Seed wars must precede the evaluated turn")
        initial_goals.append(
            WarGoals.model_validate(
                {"action": "initial", **{k: war[k] for k in ("opponent", "goals", "reason")}}
            ).root.model_dump()
        )
    prepared_operations = [MilitaryOperation.model_validate(operation).model_dump() for operation in operations]
    if len({operation["id"] for operation in prepared_operations}) != len(prepared_operations):
        raise ValueError("Seed operation IDs must be distinct")
    # Creating only the leaf memory directory leaves its new service parent at
    # the caller's umask; the real launcher requires this parent to be private.
    private_directory(root / "agent-services")
    private_directory(directory.parent)
    memory = CivilizationState(directory, civilization)
    try:
        memory.bind(epoch)
        store = WarStore(memory)
        prior_opponents = []
        for index, (war, goals) in enumerate(
            sorted(zip(wars, initial_goals, strict=True), key=lambda pair: pair[0]["startedTurn"])
        ):
            result = store.apply(epoch, "gameplay", "war_goals", goals, f"evaluation-war-{index}")
            if "error" in result:
                raise ValueError(f"Rejected evaluation war seed: {result['error']}")
            prior_opponents.append({"civilizationId": war["opponent"], "atWar": True})
            # Only seed the already-observed opponent relation, not hypothetical
            # troops or historical observations outside the supplied position.
            store.synchronize(epoch, {"turn": war["startedTurn"], "diplomacy": {"knownCivilizations": prior_opponents}})
        if wars or operations:
            store.synchronize(epoch, position)
        for index, operation in enumerate(prepared_operations):
            result = store.apply(epoch, "military", "military_operation", operation, f"evaluation-operation-{index}")
            if "error" in result:
                raise ValueError(f"Rejected evaluation operation seed: {result['error']}")
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
