"""Native persisted formats for operator watch tests; no agents or services."""

import asyncio
from datetime import UTC, datetime, timedelta

from pydantic_ai.messages import ModelRequest, ModelResponse, SystemPromptPart, TextPart, ToolCallPart, ToolReturnPart
from pydantic_ai_harness.step_persistence import ContinuableSnapshot, RunRecord, SqliteStepStore, StepEvent

from civ_agent.civilization_state import CivilizationState
from civ_agent.monitoring import MonitorStore, path_for, read_record
from civ_agent.orchestration.contracts import PlayerRole, Reference, RoleRequest, Scope, TurnTicket, canonical, digest
from civ_agent.orchestration.job_contracts import JobPhase, JobTelemetry, NativeJob, host_job_id
from civ_agent.orchestration.store import WorkflowStore
from civ_agent.player_paths import ManagedPlayerPaths
from civ_agent.tls import write_private, write_private_json
from tests.archive_fixtures import request as managed_request
from tests.test_native_status import host_publication, native_files, publish_host


def publish(root, collection, key, value, *, epoch="g1", civilization=None):
    store = MonitorStore(path_for(root))
    store.put(collection, key, value, epoch=epoch, civilization=civilization)
    store.engine.dispose()


def persistent_files(root):
    # SQLite mode=ro may create transient WAL reader sidecars. It cannot change
    # committed records; compare every durable file, including all store bodies.
    return {
        path: path.read_bytes()
        for path in root.rglob("*")
        if path.is_file() and not path.name.endswith(("-wal", "-shm"))
    }


def fixture(root):
    _, status, _ = native_files(root, identifier="game", epoch="g1", turn=2)
    (root / "native-runtime/result.json").unlink()
    request = managed_request(max_seconds=600).model_copy(
        update={"authority": managed_request(max_seconds=600).authority.model_copy(update={"epoch": "g1"})}
    )
    status = status.model_copy(
        update={
            "deadline": status.initial_started_at + timedelta(seconds=600),
            "phase": "RUNNING",
            "handoff": 2,
            "initial_turn": 0,
        }
    )
    write_private_json(root / "native-runtime/game.json", request.model_dump(mode="json"))
    write_private_json(root / "native-runtime/status.json", status.model_dump(mode="json"))
    host = host_publication(root, epoch="g1", finished=False)
    host["scores"][1]["controller"] = "AGENT"
    publish_host(root, host)
    for index, seat in enumerate(request.seats):
        paths = ManagedPlayerPaths(root, index, seat.civilization)
        paths.prepare()
        memory = CivilizationState(paths.coordinator / "notebook", seat.civilization)
        memory.bind("g1")
        with memory.transaction() as db:
            memory._set(db, "nativeGatewayOwner", {"version": 1, "game": "game", "player": paths.player})
        memory.engine.dispose()
        WorkflowStore(paths.store, player=paths.player).bind_epoch("game", "g1")
    for handoff, (name, turn) in enumerate((("Rome", 1), ("Greece", 1), ("Rome", 2))):
        value = {
            "schemaVersion": 1,
            "matchId": host["matchId"],
            "turnId": str(handoff),
            "turn": {
                "epoch": "g1",
                "civilization": name,
                "gameTurn": turn,
                "handoff": handoff,
                "status": "active" if handoff == 2 else "ended",
                "startedAtMs": 1000,
                "endedAtMs": None if handoff == 2 else 2000,
            },
            "jobs": [],
            "accountingComplete": handoff < 2,
            "usage": host["usage"],
            "hostCalls": host["hostCalls"],
            "modelRequests": 2,
        }
        publish(root, "turns", str(handoff), value, civilization=name)
    return request, status


def memory(root, civilization="Rome"):
    index = ("Rome", "Greece").index(civilization)
    paths = ManagedPlayerPaths(root, index, civilization)
    return CivilizationState(paths.coordinator / "notebook", civilization)


def task(
    root, civilization="Rome", role: PlayerRole = "gameplay", turn=2, *, phase: JobPhase = "RUNNING", schedule=None
):
    request, _ = load(root)
    index = ("Rome", "Greece").index(civilization)
    paths = ManagedPlayerPaths(root, index, civilization)
    scope = Scope(
        game="game", player=paths.player, epoch=request.authority.epoch, activation=f"ticket-{civilization}-{turn}"
    )
    ticket = TurnTicket(scope=scope, turn=turn, handoff=turn, index=turn)
    identity = scope.model_copy(update={"activation": digest([scope.activation, role + ":0"])})
    prepared = RoleRequest(
        scope=identity,
        job=identity.activation,
        purpose=role,
        selection=request.seats[index].session.get_selection(role),
        startup=Reference(sha256=digest("startup")),
        catalogue=Reference(sha256=digest("catalogue")),
    )
    state = memory(root, civilization)
    with state.transaction() as db:
        key = "nativeTurn:" + scope.activation
        record = state._meta(db, key, {"ticket": ticket.model_dump(mode="json"), "requests": {}, "roles": {}})
        record["requests"][role + ":0"] = prepared.model_dump(mode="json")
        if schedule is not None:
            record["roles"]["economySchedule"] = schedule
        state._set(db, key, record)
    state.engine.dispose()
    main = NativeJob(scope=scope, job=scope.activation, purpose="gameplay")
    native = (
        main if role == "gameplay" else NativeJob(scope=identity, job=identity.activation, purpose=role, parent=main)
    )
    job = {
        "epoch": scope.epoch,
        "civilization": civilization,
        "purpose": role,
        "gameTurn": turn,
        "claimedAtMs": turn * 1000,
        "telemetry": JobTelemetry(
            sequence=1,
            phase=phase,
            startedAtMs=1000,
            endedAtMs=2000 if phase == "COMPLETED" else None,
            elapsedMs=1000,
            usageReported=True,
            usageComplete=phase == "COMPLETED",
            model=prepared.selection.model,
            provider=prepared.selection.provider,
            reasoningEffort=prepared.selection.reasoning_effort,
        ).model_dump(mode="json"),
        "usage": {"input_tokens": 200, "cached_input_tokens": 100, "output_tokens": 30, "reasoning_output_tokens": 10},
    }
    publish(
        root,
        "jobs",
        host_job_id(native),
        {"schemaVersion": 1, "matchId": "engine-game-uuid", "jobId": host_job_id(native), "job": job},
        civilization=civilization,
        epoch=scope.epoch,
    )
    return prepared


async def history(
    root,
    request,
    text="Rome is expanding.",
    *,
    run_id=None,
    messages=None,
    agent_name=None,
    conversation_id=None,
    metadata=None,
):
    index = int(request.scope.player.removeprefix("p"))
    civilization = ("Rome", "Greece")[index]
    paths = ManagedPlayerPaths(root, index, civilization)
    path = paths.store / "agent-steps.sqlite3"
    if not path.exists():
        write_private(path, b"")
    store = SqliteStepStore(database=path)
    run_id = run_id or request.scope.activation
    agent = agent_name or ("strategic_review_v1" if request.__class__.__name__ == "ReviewRequest" else "player_role_v1")
    conversation = conversation_id or request.scope.identity(
        "conversation", "review" if agent == "strategic_review_v1" else "role"
    )
    stamp = datetime(2026, 11, 5, 12, tzinfo=UTC)
    if await store.get_run(run_id=run_id) is None:
        await store.register_run(
            RunRecord(
                run_id=run_id,
                conversation_id=conversation,
                agent_name=agent,
                metadata={"scope": canonical(request.scope.model_dump(mode="json")), **(metadata or {})},
                started_at=stamp,
            )
        )
        await store.append_event(
            StepEvent(run_id=run_id, kind="model_request_started", step_index=0, agent_name=agent, timestamp=stamp)
        )
    messages = (
        messages
        if messages is not None
        else [
            ModelRequest([SystemPromptPart("Repeated startup context")], run_id=run_id),
            ModelResponse(
                [TextPart(text), ToolCallPart("movement_preview", {}, tool_call_id="same-call")],
                run_id=run_id,
                timestamp=stamp,
            ),
            ModelRequest(
                [ToolReturnPart("movement_preview", {"error": "Stale move"}, tool_call_id="same-call")], run_id=run_id
            ),
        ]
    )
    await store.save_snapshot(
        ContinuableSnapshot(
            run_id=run_id,
            agent_name=agent,
            conversation_id=conversation,
            step_index=len(messages),
            messages=messages,
            timestamp=stamp,
        )
    )
    return store


def load(root):
    from civ_agent.orchestration.managed_game_contracts import ManagedGameRequest, ManagedGameStatus

    return ManagedGameRequest.model_validate_json(
        (root / "native-runtime/game.json").read_bytes()
    ), ManagedGameStatus.model_validate_json((root / "native-runtime/status.json").read_bytes())


def base_history(root):
    for name, turn in (("Rome", 2), ("Greece", 1)):
        prepared = task(root, name, turn=turn)
        asyncio.run(history(root, prepared, f"{name} is expanding."))


def install_test_epoch(root, epoch):
    """Synthetic publication boundary, not a production rewind implementation."""
    request, status = load(root)
    old = request.authority.epoch
    request = request.model_copy(update={"authority": request.authority.model_copy(update={"epoch": epoch})})
    write_private_json(root / "native-runtime/game.json", request.model_dump(mode="json"))
    write_private_json(
        root / "native-runtime/status.json", status.model_copy(update={"epoch": epoch}).model_dump(mode="json")
    )
    host = read_record(path_for(root), "state", "result")
    assert host is not None
    host["epoch"] = epoch
    publish_host(root, host)
    for index, seat in enumerate(request.seats):
        paths = ManagedPlayerPaths(root, index, seat.civilization)
        state = memory(root, seat.civilization)
        with state.transaction() as db:
            state._set(db, "epoch", epoch)
        state.engine.dispose()
        WorkflowStore(paths.store, player=paths.player).bind_epoch("game", epoch, previous=old)
