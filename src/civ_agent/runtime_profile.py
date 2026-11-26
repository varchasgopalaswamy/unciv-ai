"""Read native SDK boundary timings and host accounting without contacting services."""

import asyncio
import json
import math
import sqlite3
import time
from collections import Counter, defaultdict
from pathlib import Path

from sqlalchemy.exc import SQLAlchemyError

from civ_agent.container_support import ContainerError
from civ_agent.protocol import JsonObject
from civ_agent.tls import regular_file


def sample_summary(values: list[float]) -> JsonObject:
    ordered = sorted(values)
    return {
        "count": len(ordered),
        "sumMs": sum(ordered),
        "meanMs": sum(ordered) / len(ordered) if ordered else None,
        "p50Ms": ordered[math.ceil(len(ordered) * 0.5) - 1] if ordered else None,
        "p95Ms": ordered[math.ceil(len(ordered) * 0.95) - 1] if ordered else None,
    }


def game_directories(roots: list[Path]) -> list[Path]:
    games = []
    for root in roots:
        root = root.expanduser().resolve(strict=True)
        if not root.is_dir():
            raise ValueError("NATIVE_OPERATOR_GAME_DIRECTORY_REQUIRED")
        manifest = root / "batch.json"
        candidates = [root]
        if manifest.exists() or manifest.is_symlink():
            value = json.loads(regular_file(manifest, private=True).read_bytes())
            if value.get("format") != 2 or not isinstance(value.get("runs"), list) or not value["runs"]:
                raise ValueError("NATIVE_OPERATOR_BATCH_FORMAT")
            identifiers = [row["id"] for row in value["runs"]]
            if len(set(identifiers)) != len(identifiers) or any(
                not isinstance(identifier, str) or Path(identifier).name != identifier or identifier in {"", ".", ".."}
                for identifier in identifiers
            ):
                raise ValueError("NATIVE_OPERATOR_BATCH_ID")
            candidates = [root / "games" / identifier for identifier in identifiers]
        for game in candidates:
            if game not in games:
                games.append(game)
    return games


def _restart_time(root: Path) -> int:
    value = json.loads(regular_file(root / "container.json", private=True).read_bytes()).get("launchStartedAtMs")
    if type(value) is not int or value < 0:
        raise ValueError("NATIVE_LAUNCH_TIMESTAMP_UNAVAILABLE")
    return value


async def _sdk_profile(paths, scope, tasks, since, until):
    from civ_agent.orchestration.native_watch import native_step_store, recorded_sdk_runs

    samples, observed = defaultdict(list), Counter()
    with native_step_store(paths, scope) as store:
        if store is None:
            return samples, observed
        for run, task in await recorded_sdk_runs(store, scope, tasks):
            starts = {}
            for event in await store.list_events(run_id=run.run_id):
                if (
                    event.run_id != run.run_id
                    or event.conversation_id not in {None, run.conversation_id}
                    or event.agent_name not in {None, run.agent_name}
                ):
                    raise ValueError("NATIVE_PROFILE_EVENT_SCOPE")
                family = (
                    "model_request"
                    if event.kind.startswith("model_request_")
                    else "tool_call"
                    if event.kind.startswith("tool_call_")
                    else None
                )
                if family is None:
                    continue
                at = event.timestamp.timestamp() * 1000
                if not since <= at <= until:
                    continue
                key = family, event.step_index, event.tool_call_id
                observed[family + ":" + event.kind.rsplit("_", 1)[-1]] += 1
                if event.kind.endswith("_started"):
                    starts[key] = at
                elif key in starts:
                    elapsed = at - starts.pop(key)
                    if elapsed < 0 or not math.isfinite(elapsed):
                        raise ValueError("NATIVE_PROFILE_EVENT_ORDER")
                    samples[(task.settings["model"], task.purpose, "sdk." + family + ".duration_ms")].append(elapsed)
                else:
                    observed["unpaired_terminal"] += 1
            observed["unsettled_boundaries"] += len(starts)
    return samples, observed


def _profile(roots: list[Path], *, since_restart: bool = False) -> JsonObject:
    from civ_agent.operator_state import is_local
    from civ_agent.operator_state import projection as native_projection
    from civ_agent.operator_state import publication_basis as epoch_publication_basis
    from civ_agent.operator_state import published_game as published_managed_epoch
    from civ_agent.orchestration.contracts import Scope
    from civ_agent.orchestration.native_watch import operator_tasks
    from civ_agent.player_paths import PlayerPaths as ManagedPlayerPaths
    from civ_agent.watch_state import native_host_rows

    until = int(time.time() * 1000)
    windows, unavailable, recoveries, model_jobs = [], [], [], Counter()
    timings, by_model, observed = {}, defaultdict(list), Counter()
    for root in game_directories(roots):
        try:
            expected = "controller.json" if is_local(root) else "game.json"
            if not (root / "native-runtime" / expected).exists():
                unavailable.append({"game": str(root), "reason": "native_configuration_pending"})
                continue
            request, basis = published_managed_epoch(root), epoch_publication_basis(root)
            projection = native_projection(root)
            since = _restart_time(root) if since_restart else 0
            local_models, local_observed, local_jobs, local_timings, jobs = (
                defaultdict(list),
                Counter(),
                Counter(),
                {},
                (),
            )
            host = projection.result
            if host is not None:
                if since_restart:
                    unavailable.append({"game": str(root), "reason": "host_timings_have_no_launch_window"})
                else:
                    for name, row in host.hostTimings.items():
                        total = local_timings.setdefault(name, {"count": 0, "sumMs": 0, "maxMs": 0})
                        total.update(
                            count=total["count"] + row.calls,
                            sumMs=total["sumMs"] + row.elapsedMs,
                            maxMs=max(total["maxMs"], row.maxMs),
                        )
                _, jobs, _, _, _ = native_host_rows(
                    root,
                    request.authority.epoch,
                    host.matchId,
                    {seat.civilization for seat in request.seats} | {score.civilization for score in host.scores},
                )
                for job in jobs:
                    telemetry = job.get("telemetry")
                    if (
                        telemetry
                        and telemetry["phase"] == "COMPLETED"
                        and telemetry["startedAtMs"] >= since
                        and telemetry.get("endedAtMs") is not None
                        and telemetry["endedAtMs"] <= until
                    ):
                        local_jobs.update(
                            completed=1, elapsedMs=telemetry["elapsedMs"], modelRequests=telemetry["modelRequests"]
                        )
            for index, seat in enumerate(request.seats):
                paths = ManagedPlayerPaths(root, index, seat.civilization)
                if (paths.player, seat.session.game) != (seat.session.player, request.authority.game):
                    raise ValueError("NATIVE_PROFILE_PLAYER_SCOPE")
                scope = Scope(
                    game=request.authority.game,
                    player=paths.player,
                    epoch=request.authority.epoch,
                    activation="profile",
                )
                tasks = operator_tasks(paths.coordinator / "notebook/memory.sqlite3", scope, jobs)
                found, counts = asyncio.run(_sdk_profile(paths, scope, tasks, since, until))
                for key, values in found.items():
                    local_models[key].extend(values)
                local_observed.update(counts)
            if epoch_publication_basis(root) != basis:
                raise ValueError("NATIVE_PROFILE_PUBLICATION_CHANGED")
            windows.append(
                {"game": str(root), "epoch": request.authority.epoch, "sinceMs": since or None, "untilMs": until}
            )
            for key, values in local_models.items():
                by_model[key].extend(values)
            observed.update(local_observed)
            model_jobs.update(local_jobs)
            for name, row in local_timings.items():
                target = timings.setdefault(name, {"count": 0, "sumMs": 0, "maxMs": 0})
                target.update(
                    count=target["count"] + row["count"],
                    sumMs=target["sumMs"] + row["sumMs"],
                    maxMs=max(target["maxMs"], row["maxMs"]),
                )
        except (OSError, ValueError, KeyError, TypeError, sqlite3.Error, SQLAlchemyError, ContainerError) as error:
            unavailable.append(
                {"game": str(root), "reason": "native_profile_unreadable", "exceptionType": type(error).__name__}
            )
    grouped = defaultdict(list)
    for (_, _, metric), values in by_model.items():
        grouped[metric].extend(values)
    for row in timings.values():
        row["meanMs"] = row["sumMs"] / row["count"] if row["count"] else None
    return {
        "windows": windows,
        "recoveries": recoveries,
        "hostRoundTrips": {
            "available": False,
            "reason": "native_transport_round_trip_measurement_unavailable",
            **sample_summary([]),
        },
        "hostTimings": timings,
        "decisions": {
            "available": False,
            "reason": "native_classifier_completion_timestamps_unavailable",
            **sample_summary([]),
            "byModel": {},
            "fallbacks": {},
        },
        "llm": {
            "available": bool(grouped),
            "metrics": {name: sample_summary(values) for name, values in sorted(grouped.items())},
            "byModelAndPurpose": [
                {"model": model, "purpose": purpose, "metric": metric, **sample_summary(values)}
                for (model, purpose, metric), values in sorted(by_model.items())
            ],
            "observedBoundaries": dict(observed),
        },
        "modelJobs": dict(model_jobs),
        "unavailable": unavailable,
        "notes": [
            "SDK durations use recorded start and terminal StepEvents. They include tool execution or model-operation scheduling and transport; they are not provider inference, TTFT or network round trips.",
            "Started or interrupted boundaries without a terminal event remain unsettled, not zero-duration measurements. SDK model starts do not prove provider dispatch or billed usage.",
            "Native host timings are cumulative calls, elapsed and maximum durations; percentiles and per-launch slicing are unavailable.",
            "Only the selected local controller epoch or verified archived publication is measured.",
            "Native classifier completion times, inference-only latency and token streaming rates are unavailable; no legacy runner measurements are substituted.",
            "Concurrent durations overlap across jobs and games; they are not a partition of wall time.",
        ],
    }


def profile(roots: list[Path], *, since_restart: bool = False) -> JsonObject:
    from contextlib import ExitStack

    from civ_agent.operator_views import passive_root, remap_capture_paths

    with ExitStack() as stack:
        mapping, unavailable = [], []
        for root in game_directories(roots):
            try:
                mapping.append((stack.enter_context(passive_root(root)), root))
            except (OSError, ValueError, sqlite3.Error) as error:
                unavailable.append({"game": str(root), "reason": type(error).__name__})
        report = _profile([captured for captured, _ in mapping], since_restart=since_restart)
        report["unavailable"].extend(unavailable)
        return remap_capture_paths(report, mapping)
