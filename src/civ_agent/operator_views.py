"""Runtime publications and passive, independently committed operator captures."""

import asyncio
import fcntl
import hashlib
import json
import os
import shutil
import socket
import struct
import tempfile
import time
from collections import OrderedDict
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass, replace
from functools import lru_cache
from itertools import islice
from pathlib import Path
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter

from civ_agent.local_performance import LocalPerformance
from civ_agent.service_io import ServiceIO
from civ_agent.step_store_notifications import CommittedStepStore
from civ_agent.tls import TLSConfigError, private_directory, regular_file, write_private_json

_PUBLICATION_INTERVAL = 2.0


class ViewGeneration(BaseModel):
    version: Literal[1] = 1
    generation: str = Field(pattern=r"^[a-f0-9]{32}$")
    game: str
    epoch: str
    started_at: float
    captured_at: float
    consistency: Literal["independent_committed_captures", "coordinated_stopped"] = "independent_committed_captures"
    sources: dict[str, str] = Field(default_factory=dict)
    errors: list[str] = Field(default_factory=list)
    civilization: str | None = None


class ViewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    operation: Literal["refresh", "capture"]
    civilization: str | None = Field(default=None, min_length=1, max_length=128)


def _json(path):
    return json.loads(regular_file(path, private=True).read_bytes())


@contextmanager
def quiescent(root):
    """Exclude controller/supervisor ownership without creating source lock files."""
    with ExitStack() as stack:
        for name in ("native-runtime/controller.lock", ".supervisor.lock", ".launch.lock"):
            path = root / name
            if path.exists():
                stream = stack.enter_context(path.open("rb"))
                try:
                    fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    raise ValueError("OPERATOR_STOPPED_RUNTIME_REQUIRED") from None
        from civ_agent.operator_state import is_local

        if is_local(root):
            path = root / "container-status.json"
            status = _json(path).get("status") if path.exists() else None
            if status not in {"stopped", "paused", "failed", "finished", "budget_exhausted", "completed"}:
                raise ValueError("OPERATOR_STOPPED_DEPLOYMENT_REQUIRED")
        # Retired runtime archives have no execution owner. Current deployments
        # require both exclusive lifecycle locks and terminal supervisor state.
        yield


def _file_sha256(path: Path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def _copy_capture_file(source: Path, target: Path, *, verify):
    shutil.copyfile(source, target)
    target.chmod(0o600)
    checksum = _file_sha256(target)
    if verify and _file_sha256(source) != checksum:
        raise ValueError("OPERATOR_SOURCE_CHANGED")
    return checksum


def capture_tree(root: Path, destination: Path, *, live=False, civilization=None):
    """Only the runtime owner may back up live databases; passive callers copy frozen bytes."""
    from civ_agent.orchestration.history_exports import copy_native_database

    sources = {}
    files = [root / name for name in ("container.json", "resolved.json", "container-status.json")]
    for directory in ("native-runtime", "runner", "coordinator", "player-stores"):
        base = root / directory
        if base.exists():
            files.extend(path for path in base.rglob("*") if path.suffix in {".json", ".sqlite3", ".sqlite"})
    for source in sorted(set(files)):
        relative = source.relative_to(root)
        if civilization is not None and relative.parts[0] in {"coordinator", "player-stores"}:
            from civ_agent.civilization_paths import civilization_directory_name

            if len(relative.parts) < 2 or relative.parts[1] != civilization_directory_name(civilization):
                continue
        if not source.is_file():
            continue
        regular_file(source, private=True)
        if not source.stat().st_mode & 0o400:
            raise TLSConfigError("OPERATOR_PRIVATE_SOURCE_UNREADABLE")
        target = private_directory(destination / relative.parent) / relative.name
        database = source.suffix in {".sqlite", ".sqlite3"}
        if database and live:
            copy_native_database(source, target)
            checksum = _file_sha256(target)
        else:
            checksum = _copy_capture_file(source, target, verify=False)
            if database:
                wal = source.with_name(source.name + "-wal")
                if wal.exists():
                    _copy_capture_file(wal, target.with_name(target.name + "-wal"), verify=True)
        target.chmod(0o600)
        info = source.stat()
        os.utime(target, ns=(info.st_atime_ns, info.st_mtime_ns))
        if not live and _file_sha256(source) != checksum:
            raise ValueError("OPERATOR_SOURCE_CHANGED")
        sources[str(relative)] = checksum
    return sources


@contextmanager
def passive_root(root: Path, *, coordinated=False, civilization=None):
    """Read a published capture, or prove quiescence and work entirely in scratch."""
    root = Path(root)
    pointer = root / "operator-views/capture.json"
    if (
        pointer.exists()
        and not coordinated
        and (not (root / "operator-views/owner.sock").exists() or time.time() - pointer.stat().st_mtime < 2)
    ):
        generation = ViewGeneration.model_validate(_json(pointer))
        path = root / "operator-views" / generation.generation / "stores"
        from civ_agent.operator_state import published_game

        current = published_game(root)
        if (generation.game, generation.epoch) != (current.authority.game, current.authority.epoch):
            raise ValueError("OPERATOR_CAPTURE_EPOCH_CONFLICT")
        if path.is_dir() and generation.civilization == civilization:
            with tempfile.TemporaryDirectory(prefix="published-operator-capture-") as scratch:
                copied = Path(scratch) / "stores"
                shutil.copytree(path, copied)
                yield copied
            return
    if (root / "operator-views/owner.sock").exists() and not coordinated:
        request_capture(root, civilization)
        with passive_root(root, civilization=civilization) as captured:
            yield captured
        return
    with quiescent(root), tempfile.TemporaryDirectory(prefix="operator-capture-") as scratch:
        captured = Path(scratch)
        started = time.time()
        sources = capture_tree(root, captured, civilization=civilization)
        write_private_json(
            captured / "capture-metadata.json",
            {
                "started_at": started,
                "captured_at": time.time(),
                "sources": sources,
                "consistency": "coordinated_stopped" if coordinated else "independent_committed_captures",
            },
        )
        yield captured


def request_capture(root: Path, civilization=None):
    with socket.socket(socket.AF_UNIX) as connection:
        connection.settimeout(60)
        connection.connect(str(root / "operator-views/owner.sock"))
        connection.sendall(
            (ViewRequest(operation="capture", civilization=civilization).model_dump_json() + "\n").encode()
        )
        response = b""
        while b"\n" not in response and len(response) < 4096:
            response += connection.recv(4096)
        if json.loads(response).get("ok") is not True:
            raise ValueError("OPERATOR_CAPTURE_UNAVAILABLE")


@dataclass(frozen=True)
class ActivityRun:
    run_id: str
    civilization: str
    turn: int
    purpose: str
    native_job: str


@lru_cache(maxsize=1)
def _watch_snapshot_adapter():
    from civ_agent.watch_state import WatchSnapshot

    return TypeAdapter(WatchSnapshot)


class OperatorPublisher:
    def __init__(self, root, state, *, players=None):
        self.root, self.state = root, state
        self.players = players or {}
        self.directory = private_directory(root / "operator-views")
        self.refresh = asyncio.Event()
        self.reader = None
        self.io = ServiceIO()
        self.lock = asyncio.Lock()
        self.server = None
        self.requests: set[asyncio.Task] = set()
        self.performance = LocalPerformance()
        self._activity_cache = OrderedDict()
        self._activity_epoch = None
        self._run_index: tuple[ActivityRun, ...] = ()
        self._subscriptions = []
        self._lag_timer = None

    async def start(self):
        loop = asyncio.get_running_loop()
        for player in self.players.values():
            steps = getattr(player, "steps", None)
            if isinstance(steps, CommittedStepStore):

                def refresh_activity(_run):
                    loop.call_soon_threadsafe(self.refresh.set)

                self._subscriptions.append(steps.subscribe(refresh_activity))
        self._schedule_lag(loop)
        path = self.directory / "owner.sock"
        path.unlink(missing_ok=True)
        self.server = await asyncio.start_unix_server(self._request, path=path, limit=4096)
        path.chmod(0o600)

    def _schedule_lag(self, loop):
        expected = loop.time() + 0.5

        def measured():
            self.performance.timing("eventLoopLag", loop.time() - expected)
            self._schedule_lag(loop)

        self._lag_timer = loop.call_at(expected, measured)

    async def close(self):
        if self._lag_timer is not None:
            self._lag_timer.cancel()
        for unsubscribe in self._subscriptions:
            unsubscribe()
        self._subscriptions.clear()
        if self.server is not None:
            self.server.close()
            await self.server.wait_closed()
        for task in list(self.requests):
            task.cancel()
        await asyncio.gather(*list(self.requests), return_exceptions=True)
        (self.directory / "owner.sock").unlink(missing_ok=True)
        await self.io.close()

    async def _request(self, reader, writer):
        task = asyncio.current_task()
        if task is not None:
            self.requests.add(task)
        try:
            peer = writer.get_extra_info("socket").getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12)
            if struct.unpack("3i", peer)[1] != os.getuid():
                raise ValueError("OPERATOR_OWNER_REQUIRED")
            request = ViewRequest.model_validate_json(await asyncio.wait_for(reader.readline(), 5))
            if request.civilization is not None and request.civilization not in {
                s["civilization"] for s in self.state.current.seats
            }:
                raise ValueError("OPERATOR_SELECTION_INVALID")
            if request.operation == "refresh":
                self.refresh.set()
            else:
                await self.publish(capture=True, civilization=request.civilization)
            writer.write(b'{"ok":true}\n')
        except Exception:
            writer.write(b'{"ok":false}\n')
        finally:
            try:
                await writer.drain()
            finally:
                writer.close()
                await writer.wait_closed()
                if task is not None:
                    self.requests.discard(task)

    async def publish(self, *, capture=False, civilization=None):
        async with self.lock:
            state = self.state.current
            generation = ViewGeneration(
                generation=uuid4().hex,
                game=state.game,
                epoch=state.epoch,
                civilization=civilization,
                started_at=time.time(),
                captured_at=time.time(),
            )
            directory = await self.io.call(self._prepare_generation, generation, capture)
            if directory is None:
                return
            view = None
            try:
                if capture:
                    generation.sources = await self.io.call(
                        capture_tree,
                        self.root,
                        directory / "stores",
                        live=True,
                        civilization=civilization,
                    )
                else:
                    from civ_agent.watch_state import WatchReader

                    if self.reader is None:
                        self.reader = await self.io.call(WatchReader, self.root, owner=True, include_history=False)
                    started = time.monotonic()
                    view = await self.io.call(self.reader.read)
                    self.performance.timing("watchRead", time.monotonic() - started)
                    started = time.monotonic()
                    activity = await self._activity()
                    self.performance.timing("activity", time.monotonic() - started)
                    view = replace(
                        view,
                        activity=tuple(
                            item for purpose, item in activity if purpose not in {"economy", "military", "review"}
                        ),
                        specialists=tuple(
                            replace(
                                specialist,
                                activity=tuple(
                                    item
                                    for purpose, item in activity
                                    if purpose == specialist.role and item.civilization == specialist.civilization
                                ),
                            )
                            for specialist in view.specialists
                        ),
                    )
                    generation.sources = {
                        "monitoring_revision": str((self.reader.host_cache or {}).get("revision", 0)),
                        "controller_updated_at": state.updated_at.isoformat(),
                    }
                    if view.errors:
                        raise ValueError("OPERATOR_VIEW_UNAVAILABLE")
                generation.captured_at = time.time()
                # Durable presentation writes and retention cleanup must not
                # block inference, tool dispatch, or native lease renewals.
                # ServiceIO also settles an accepted publication on cancellation.
                started = time.monotonic()
                await self.io.call(self._commit_generation, directory, generation, capture, view)
                self.performance.timing("publicationCommit", time.monotonic() - started)
                await self.io.call(self._publish_performance, generation)
            except Exception as error:
                await self.io.call(
                    write_private_json,
                    self.directory / "availability.json",
                    {"epoch": state.epoch, "error": type(error).__name__, "time": time.time()},
                )
                raise

    def _publish_performance(self, generation):
        from civ_agent.decision_contracts import decision_contract_cache_metrics
        from civ_agent.local_performance import memory_usage

        try:
            write_private_json(
                self.directory / "performance.json",
                {
                    "game": generation.game,
                    "epoch": generation.epoch,
                    "captured_at": generation.captured_at,
                    "publication": self.performance.snapshot(),
                    "decisionContracts": decision_contract_cache_metrics(),
                    "memory": memory_usage(),
                },
            )
        except OSError:
            pass  # Optional local metrics cannot change a committed publication.

    def _prepare_generation(self, generation, capture):
        pointer = self.directory / ("capture.json" if capture else "current.json")
        if capture and pointer.exists():
            prior = ViewGeneration.model_validate(_json(pointer))
            if (prior.game, prior.epoch, prior.civilization) == (
                generation.game,
                generation.epoch,
                generation.civilization,
            ) and time.time() - prior.captured_at < 2:
                return None
        return private_directory(self.directory / generation.generation)

    def _commit_generation(self, directory, generation, capture, view):
        if capture:
            write_private_json(directory / "stores/capture-metadata.json", generation.model_dump(mode="json"))
        else:
            write_private_json(directory / "view.json", _watch_snapshot_adapter().dump_python(view, mode="json"))
        write_private_json(directory / "generation.json", generation.model_dump(mode="json"))
        pointer = self.directory / ("capture.json" if capture else "current.json")
        previous = _json(pointer) if pointer.exists() else None
        write_private_json(pointer, generation.model_dump(mode="json"))
        if previous:
            write_private_json(self.directory / ("capture-previous.json" if capture else "previous.json"), previous)
        keep = {generation.generation}
        for name in ("current.json", "previous.json", "capture.json", "capture-previous.json"):
            if (self.directory / name).exists():
                keep.add(ViewGeneration.model_validate(_json(self.directory / name)).generation)
        for old in self.directory.iterdir():
            if old.is_dir() and old.name not in keep:
                shutil.rmtree(old)

    async def _activity(self):
        """Read bounded, recorded SDK run IDs through the runtime's public stores.

        Full histories are exported on demand. No conversation IDs are guessed,
        and recurring refreshes never copy the complete SDK database.
        """
        from civ_agent.orchestration.history_exports import native_run_scope
        from civ_agent.orchestration.job_contracts import NativeJob, host_job_id

        state = self.state.current
        scope_identity = (state.game, state.epoch)
        if self._activity_epoch != scope_identity:
            self._activity_cache.clear()
            self._activity_epoch = scope_identity
        owned = {}
        attempts = [*state.completed_attempts[-2:], *([state.active_attempt] if state.active_attempt else [])]
        for attempt in attempts:
            for role in attempt.roles[-32:]:
                if role.run_id:
                    owned[role.run_id] = (attempt.civilization, attempt.turn, role)
        # Native restart inventory contains outstanding permits, not the full
        # history. Read only bounded tails; never copy the controller archive.
        native_tail = tuple(islice(reversed(state.native_jobs.values()), 32))[::-1]
        for role in [*state.recent_roles[-32:], *native_tail]:
            if role.run_id and role.native_job:
                player_id = role.native_job["scope"]["player"]
                civilization = next(
                    (name for name, player in self.players.items() if player.session.player == player_id), None
                )
                if civilization:
                    owned.setdefault(role.run_id, (civilization, state.turn, role))
        self._run_index = tuple(
            ActivityRun(run_id, civilization, turn, role.purpose, json.dumps(role.native_job, sort_keys=True))
            for run_id, (civilization, turn, role) in list(owned.items())[-32:]
            if role.native_job is not None
        )
        entries = []
        for selected in self._run_index:
            run_id, civilization, turn = selected.run_id, selected.civilization, selected.turn
            player = self.players.get(civilization)
            if player is None:
                continue
            run = await player.steps.get_run(run_id=run_id)
            if run is None:
                continue
            scope = native_run_scope(run, state.game, player.session.player)
            native = NativeJob.model_validate_json(selected.native_job)
            if scope.epoch != state.epoch or run.metadata.get("native_job_id") != host_job_id(native):
                raise ValueError("OPERATOR_SDK_RUN_BINDING_CONFLICT")
            turn = int(run.metadata.get("game_turn", turn))
            if turn < 0:
                raise ValueError("OPERATOR_SDK_TURN_INVALID")
            observed = player.steps if isinstance(player.steps, CommittedStepStore) else None
            token = observed.snapshot_token(run_id) if observed is not None else None
            cache_key = (scope_identity, player.session.player, selected, token)
            cached = self._activity_cache.get(run_id)
            if token is not None and cached is not None and cached[0] == cache_key:
                self.performance.count("activityCacheHits")
                self._activity_cache.move_to_end(run_id)
                entries.extend(cached[1])
                continue
            self.performance.count("activityCacheMisses")
            started = time.monotonic()
            snapshot = await player.steps.latest_snapshot(run_id=run_id, include_interrupted=True)
            self.performance.timing("snapshotRestore", time.monotonic() - started)
            if snapshot is None:
                continue
            if (snapshot.run_id, snapshot.agent_name, snapshot.conversation_id) != (
                run.run_id,
                run.agent_name,
                run.conversation_id,
            ):
                raise ValueError("OPERATOR_SDK_SNAPSHOT_BINDING_CONFLICT")
            started = time.monotonic()
            rendered = await self.io.call(
                self._snapshot_activity, snapshot, run_id, civilization, turn, selected.purpose
            )
            self.performance.timing("snapshotRender", time.monotonic() - started)
            rendered = tuple(sorted(rendered, key=lambda entry: entry[1].sort_key)[-600:])
            if observed is not None and token is not None and observed.snapshot_token(run_id) == token:
                identity = (snapshot.idempotency_key, snapshot.step_index, snapshot.state, snapshot.timestamp)
                self._activity_cache[run_id] = (cache_key, rendered, identity)
                self._activity_cache.move_to_end(run_id)
                while len(self._activity_cache) > 32:
                    self._activity_cache.popitem(last=False)
            entries.extend(rendered)
        entries.sort(key=lambda entry: entry[1].sort_key)
        return entries[-600:]

    @staticmethod
    def _snapshot_activity(snapshot, run_id, civilization, turn, purpose):
        from civ_agent.orchestration.native_watch import MAX_TEXT, _command_summary, _part
        from civ_agent.terminal_output import visible_text
        from civ_agent.watch_state import Activity

        entries = []
        for index, message in enumerate(snapshot.messages):
            if message.run_id not in {None, run_id}:
                continue
            stamp = message.timestamp.isoformat() if message.timestamp else ""
            for number, part in enumerate(message.parts):
                summary = _command_summary(part)
                detail = _part(part)
                for kind, value in (("summary", (*summary, False) if summary else None), ("message", detail)):
                    if value is None:
                        continue
                    label, content, verbose = value
                    entries.append(
                        (
                            purpose,
                            Activity(
                                f"{run_id}:{kind}:{index}:{number}",
                                civilization,
                                turn,
                                stamp,
                                label,
                                visible_text(content)[:MAX_TEXT],
                                verbose,
                                published_at=snapshot.timestamp.timestamp(),
                            ),
                        )
                    )
        return entries

    async def run(self):
        next_publication = 0.0
        while True:
            # Commit notifications can arrive in bursts. Preserve the existing
            # polling ceiling instead of serializing a new view for each write.
            delay = next_publication - asyncio.get_running_loop().time()
            if delay > 0:
                await asyncio.sleep(delay)
            self.refresh.clear()
            try:
                await self.publish()
            except Exception:
                pass  # Observational failure retains the last valid generation.
            next_publication = asyncio.get_running_loop().time() + _PUBLICATION_INTERVAL
            try:
                await asyncio.wait_for(self.refresh.wait(), _PUBLICATION_INTERVAL)
            except TimeoutError:
                pass


def remap_capture_paths(value, mapping):
    """Temporary capture locations never become durable operator identities."""
    if isinstance(value, dict):
        return {key: remap_capture_paths(item, mapping) for key, item in value.items()}
    if isinstance(value, list):
        return [remap_capture_paths(item, mapping) for item in value]
    if isinstance(value, str):
        for captured, source in mapping:
            if value == str(captured):
                return str(source)
    return value
