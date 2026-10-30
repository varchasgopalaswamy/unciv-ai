"""Role authority derived from native unit capabilities and explicit escort duty."""

WORKER_COMMANDS = frozenset(
    {
        "START_WORKER_JOB",
        "STOP_WORKER_JOB",
        "CONNECT_ROAD",
        "MOVE_UNIT",
        "SET_UNIT_DESTINATION",
        "CANCEL_UNIT_MOVEMENT",
        "SET_UNIT_ORDER",
        "SWAP_UNITS",
        "RENAME_UNIT",
        "SAVE_UNIT_PROMOTIONS",
        "PROMOTE_UNIT",
        "UPGRADE_UNIT",
        "DISBAND_UNIT",
    }
)


def unit_owner(unit, state):
    escorts = state.get("escorts", {})
    handle = unit["handle"]
    if (
        handle in escorts
        or any(assignment.get("civilian") == handle for assignment in escorts.values())
        or (unit.get("escort") or {}).get("active")
    ):
        return "gameplay"
    if unit.get("military"):
        return "military"
    # Raw observations contain the native worker picker; compact rosters retain
    # its presence as a boolean. Names, current jobs and movement are not proof.
    if isinstance(unit.get("worker"), dict) or unit.get("canBuildImprovements") is True:
        return "economy"
    return "gameplay"
