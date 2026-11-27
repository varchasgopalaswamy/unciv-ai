"""One systemd owner retains container and native launch identities across crashes."""

import copy
import json
import os
import subprocess
from types import SimpleNamespace
from typing import Any

import pytest

from civ_agent import container_supervisor as supervisor
from civ_agent import container_support as support
from civ_agent.game_config import GameConfig
from civ_agent.tls import write_private_json

CID = "a" * 64
IMAGE = "sha256:" + "b" * 64
OWNER = "123456789a"
LAUNCH = "123456789abc"


class ProcessOwner:
    def __init__(self, monkeypatch):
        self.service = support.ContainerService(
            "civ-" + OWNER + "-host",
            CID,
            IMAGE,
            OWNER,
            "civ-" + OWNER + "-host-" + LAUNCH + ".service",
        )
        self.container: dict[str, Any] = {
            "Id": CID,
            "Name": self.service.name,
            "Image": IMAGE,
            "Config": {"Labels": {support.LABEL: OWNER, support.SERVICE_LABEL: self.service.unit}},
            "HostConfig": {"RestartPolicy": {"Name": "no"}},
            "State": {"Running": True, "ExitCode": 0},
        }
        self.state = self.running()
        self.calls = []
        monkeypatch.setattr(support, "inspected", lambda _: copy.deepcopy(self.container))
        monkeypatch.setattr(support, "command", self.command)

    def running(self):
        start = "/usr/bin/podman start --attach --sig-proxy=false " + CID
        stop = "/usr/bin/podman stop --ignore --time 90 " + CID
        return {
            "LoadState": "loaded",
            "ActiveState": "active",
            "SubState": "running",
            "NRestarts": "0",
            "Description": "Civ container " + OWNER + " " + CID,
            "ExecStart": "{ path=/usr/bin/podman ; argv[]="
            + start
            + " ; ignore_errors=no ; pid=123 ; code=(null) ; status=0/0 }",
            "ExecStop": "{ path=/usr/bin/podman ; argv[]="
            + stop
            + " ; ignore_errors=no ; pid=0 ; code=(null) ; status=0/0 }",
        }

    def command(self, args, **kwargs):
        self.calls.append(args)
        if args == ["systemctl", "--version"]:
            return subprocess.CompletedProcess(args, 0, "systemd 259\n")
        if args[:3] == ["systemctl", "--user", "show"]:
            return subprocess.CompletedProcess(
                args, 0, "\n".join(key + "=" + value for key, value in self.state.items())
            )
        if args[:3] == ["systemctl", "--user", "stop"]:
            assert args[-1] == self.service.unit
            self.container["State"]["Running"] = False
            self.state = {"LoadState": "not-found"}
        elif args[0] == "systemd-run":
            self.state = self.running()
        else:
            raise AssertionError(args)
        return subprocess.CompletedProcess(args, 0, "")


def test_existing_cid_systemd_plan_has_one_bounded_restart_owner(monkeypatch, tmp_path):
    fake = ProcessOwner(monkeypatch)
    fake.state = {"LoadState": "not-found"}
    fake.container["State"]["Running"] = False
    support.start_container_service(fake.service, create=True, log_path=tmp_path / "private%worker.log")
    plan = next(args for args in fake.calls if args[0] == "systemd-run")
    assert plan[-5:] == ["/usr/bin/podman", "start", "--attach", "--sig-proxy=false", CID]
    assert "--property=RestartSec=2s" in plan and "--property=RestartSteps=2" in plan
    assert "--property=RestartMaxDelaySec=8s" in plan
    assert "--property=StartLimitIntervalSec=infinity" in plan and "--property=StartLimitBurst=4" in plan
    assert "--property=CollectMode=inactive" in plan
    assert "--property=StandardOutput=append:" + str(tmp_path / "private%%worker.log") in plan
    assert "--property=StandardError=append:" + str(tmp_path / "private%%worker.log") in plan
    assert not any(arg.startswith(("--restart", "--collect")) or "reset-failed" in arg for arg in plan)


@pytest.mark.parametrize("problem", ["cid", "image", "owner", "unit", "podman-restart", "exec"])
def test_foreign_or_changed_process_owner_never_mutates_a_service(monkeypatch, problem):
    fake = ProcessOwner(monkeypatch)
    if problem == "cid":
        fake.container["Id"] = "c" * 64
    elif problem == "image":
        fake.container["Image"] = "sha256:" + "c" * 64
    elif problem in {"owner", "unit"}:
        key = support.LABEL if problem == "owner" else support.SERVICE_LABEL
        fake.container["Config"]["Labels"][key] = "foreign"
    elif problem == "podman-restart":
        fake.container["HostConfig"]["RestartPolicy"]["Name"] = "always"
    else:
        fake.state["ExecStart"] = fake.state["ExecStart"].replace("--sig-proxy=false", "--sig-proxy=true")
    with pytest.raises(support.ContainerError):
        support.start_container_service(fake.service)
    assert not any(args[0] == "systemd-run" or args[:3] == ["systemctl", "--user", "stop"] for args in fake.calls)


@pytest.mark.parametrize("active", ["active", "activating"])
def test_observation_during_process_recovery_does_not_create_or_start_again(monkeypatch, active):
    fake = ProcessOwner(monkeypatch)
    fake.state.update(ActiveState=active, SubState="auto-restart", NRestarts="2")
    fake.container["State"]["Running"] = False
    support.start_container_service(fake.service)
    assert all(args[:3] == ["systemctl", "--user", "show"] for args in fake.calls)


def test_exhausted_restart_allowance_requires_explicit_operator_action(monkeypatch):
    fake = ProcessOwner(monkeypatch)
    fake.state.update(ActiveState="failed", Result="start-limit-hit", NRestarts="4")
    with pytest.raises(support.ContainerError, match="restart allowance"):
        support.start_container_service(fake.service)
    assert all(args[:3] == ["systemctl", "--user", "show"] for args in fake.calls)


def test_explicit_stop_stops_systemd_first_and_is_idempotent_after_collection(monkeypatch):
    fake = ProcessOwner(monkeypatch)
    support.stop_container_service(fake.service)
    support.stop_container_service(fake.service)
    stops = [args for args in fake.calls if args[:3] == ["systemctl", "--user", "stop"]]
    assert stops == [["systemctl", "--user", "stop", fake.service.unit]]
    assert not fake.container["State"]["Running"]


@pytest.mark.parametrize("collected", [False, True])
def test_explicit_broker_replacement_waits_for_exact_old_unit_before_removal(monkeypatch, collected):
    fake = ProcessOwner(monkeypatch)
    original = fake.command
    clock = [0.0]
    stopped = False

    def command(args, **kwargs):
        nonlocal stopped
        if args[:3] == ["systemctl", "--user", "stop"]:
            stopped = True
            result = original(args, **kwargs)
            fake.state = fake.running()
            fake.state.update(ActiveState="inactive", SubState="dead")
            return result
        if stopped and collected and clock[0] >= 1:
            fake.state = {"LoadState": "not-found"}
        return original(args, **kwargs)

    monkeypatch.setattr(support, "command", command)
    monkeypatch.setattr(
        support, "time", SimpleNamespace(monotonic=lambda: clock[0], sleep=lambda _: clock.__setitem__(0, clock[0] + 1))
    )
    if collected:
        support.stop_container_service(fake.service, collect=True)
        assert clock[0] >= 1
    else:
        with pytest.raises(support.ContainerError, match="unit is still loaded"):
            support.stop_container_service(fake.service, collect=True)
        assert clock[0] == 5
    assert not fake.container["State"]["Running"]
    assert all(args[0] == "systemctl" for args in fake.calls)


def test_unsupported_systemd_does_not_silently_drop_restart_bounds(monkeypatch):
    fake = ProcessOwner(monkeypatch)
    fake.state = {"LoadState": "not-found"}
    fake.container["State"]["Running"] = False
    original = fake.command

    def command(args, **kwargs):
        if args == ["systemctl", "--version"]:
            return subprocess.CompletedProcess(args, 0, "systemd 253\n")
        return original(args, **kwargs)

    monkeypatch.setattr(support, "command", command)
    with pytest.raises(support.ContainerError, match="254"):
        support.start_container_service(fake.service, create=True)
    assert not any(args[0] == "systemd-run" for args in fake.calls)


@pytest.mark.parametrize("replace", [False, True])
def test_failed_unit_allowance_resets_only_for_explicit_owned_replacement(monkeypatch, replace):
    fake = ProcessOwner(monkeypatch)
    fake.container["State"]["Running"] = False
    fake.state.update(ActiveState="failed", SubState="failed", NRestarts="4")
    original = fake.command

    def command(args, **kwargs):
        if args[:3] == ["systemctl", "--user", "reset-failed"]:
            assert args[-1] == fake.service.unit and not fake.container["State"]["Running"]
            assert ["systemctl", "--user", "stop", fake.service.unit] in fake.calls
            fake.calls.append(args)
            fake.state = {"LoadState": "not-found"}
            return subprocess.CompletedProcess(args, 0, "")
        result = original(args, **kwargs)
        if args[:3] == ["systemctl", "--user", "stop"]:
            fake.state = fake.running()
            fake.state.update(ActiveState="failed", SubState="failed", NRestarts="4")
        return result

    monkeypatch.setattr(support, "command", command)
    support.stop_container_service(fake.service, collect=replace)
    resets = [args for args in fake.calls if args[:3] == ["systemctl", "--user", "reset-failed"]]
    assert resets == ([["systemctl", "--user", "reset-failed", fake.service.unit]] if replace else [])
    if replace:
        assert fake.state == {"LoadState": "not-found"}
    else:
        assert fake.state["NRestarts"] == "4"


def test_failed_foreign_unit_never_gets_a_replacement_reset(monkeypatch):
    fake = ProcessOwner(monkeypatch)
    fake.container["Config"]["Labels"][support.LABEL] = "foreign"
    fake.state.update(ActiveState="failed", SubState="failed", NRestarts="4")
    with pytest.raises(support.ContainerError):
        support.stop_container_service(fake.service, collect=True)
    assert not any(
        args[:3] in (["systemctl", "--user", "stop"], ["systemctl", "--user", "reset-failed"]) for args in fake.calls
    )


def test_explicit_supervisor_resume_collects_failed_owned_unit_after_reset(monkeypatch, tmp_path):
    unit = "civ-game-" + OWNER
    write_private_json(
        tmp_path / "container.json",
        {
            "format": 2,
            "runtime": "pydantic-local-v1",
            "id": OWNER,
            "supervision": {"backend": "systemd-existing-cid-v1", "unit": unit},
        },
    )
    state = {"LoadState": "loaded", "ActiveState": "failed", "NRestarts": "4"}
    commands = []

    def command(args, **kwargs):
        commands.append(args)
        if args[:3] == ["systemctl", "--user", "reset-failed"]:
            assert commands[-2] == ["systemctl", "--user", "stop", unit]
            state.update(LoadState="not-found")

    monkeypatch.setattr(support, "require_restart_systemd", lambda: None)
    monkeypatch.setattr(
        support, "unit_state", lambda selected: dict(state) if selected == unit else pytest.fail(selected)
    )
    monkeypatch.setattr(support, "command", command)
    assert support.launch_background(tmp_path, "game") == unit
    assert commands[0] == ["systemctl", "--user", "stop", unit]
    assert commands[1] == ["systemctl", "--user", "reset-failed", unit]
    assert commands[2][0] == "systemd-run"


def test_supervisor_replacement_retains_launch_controls_and_existing_container(monkeypatch, tmp_path):
    fake = ProcessOwner(monkeypatch)
    write_private_json(tmp_path / "resolved.json", GameConfig().resolved().model_dump())
    write_private_json(
        tmp_path / "container.json",
        {
            "format": 2,
            "runtime": "pydantic-local-v1",
            "id": OWNER,
            "auth_root": str(tmp_path.parent / "separate-auth"),
            "launch": LAUNCH,
            "servicesReady": True,
            "services": [fake.service.record()],
        },
    )
    (tmp_path / "launches" / LAUNCH).mkdir(parents=True)
    for filename in ("control/start.json", "control/request.json", "runtime.json"):
        write_private_json(tmp_path / filename, {"retained": filename})
    before = {path: path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()}
    run = supervisor.GameRun(tmp_path, rejoin=True)
    logs = []
    monkeypatch.setattr(run, "follow_log", logs.append)
    monkeypatch.setattr(run, "setup", lambda: pytest.fail("Automatic rejoin must not run setup"))
    run.reattach()
    assert run.record["launch"] == LAUNCH and run.names == [fake.service.name] and logs == run.names
    assert all(path.read_bytes() == value for path, value in before.items())
    assert not any(args[0] == "systemd-run" for args in fake.calls)


@pytest.mark.parametrize("status,expected", [("failed", 78), ("paused", 0), ("budget_exhausted", 0), ("completed", 0)])
def test_supervisor_exit_preserves_native_terminal_policy(monkeypatch, tmp_path, status, expected):
    write_private_json(tmp_path / "container.json", {"format": 2, "runtime": "pydantic-local-v1", "id": OWNER})
    monkeypatch.delenv("CIV_SUPERVISOR_UNIT", raising=False)
    monkeypatch.setattr("sys.argv", ["supervisor", "game", "--state-dir", str(tmp_path)])

    class Game:
        def __init__(self, root, event, *, rejoin):
            assert root == tmp_path and rejoin is False

        def run(self):
            return status

    monkeypatch.setattr(supervisor, "GameRun", Game)
    assert supervisor.main() == expected


def test_interrupted_bootstrap_cannot_create_missing_services_on_automatic_rejoin(monkeypatch, tmp_path):
    fake = ProcessOwner(monkeypatch)
    write_private_json(tmp_path / "resolved.json", GameConfig().resolved().model_dump())
    write_private_json(
        tmp_path / "container.json",
        {
            "format": 2,
            "runtime": "pydantic-local-v1",
            "id": OWNER,
            "auth_root": str(tmp_path.parent / "auth"),
            "launch": LAUNCH,
            "servicesReady": False,
            "services": [fake.service.record()],
        },
    )
    (tmp_path / "launches" / LAUNCH).mkdir(parents=True)
    before = (tmp_path / "container.json").read_bytes()
    run = supervisor.GameRun(tmp_path, rejoin=True)
    with pytest.raises(support.ContainerError, match="startup was interrupted"):
        run.reattach()
    assert run.names == [fake.service.name] and (tmp_path / "container.json").read_bytes() == before
    assert not fake.calls


@pytest.mark.parametrize("current,backoff,expected", [(False, False, False), (True, True, False), (True, False, True)])
def test_terminal_projection_belongs_to_current_controller_process(monkeypatch, tmp_path, current, backoff, expected):
    fake = ProcessOwner(monkeypatch)
    run = object.__new__(supervisor.GameRun)
    run.root, run.record = tmp_path, {"launch": LAUNCH}
    run.controller, run.services = fake.service.name, [fake.service]
    fake.container["State"]["StartedAt"] = "2024-03-09T16:00:00Z"
    fake.state["ActiveState"] = "activating" if backoff else "active"
    write_private_json(tmp_path / "runtime.json", {"phase": "stopped", "launch": LAUNCH, "runner_status": "paused"})
    stamp = 1710000001 if current else 1709999999
    os.utime(tmp_path / "runtime.json", (stamp, stamp))
    result = run.current_runtime([fake.container])
    assert (result is not None) is expected


def test_same_launch_supervisor_restart_selects_observation_not_fresh_setup(monkeypatch, tmp_path):
    unit = "civ-game-" + OWNER
    record = {
        "format": 2,
        "runtime": "pydantic-local-v1",
        "id": OWNER,
        "launch": LAUNCH,
        "supervision": {"backend": "systemd-existing-cid-v1", "unit": unit},
    }
    write_private_json(tmp_path / "container.json", record)
    before = (tmp_path / "container.json").read_bytes()
    monkeypatch.setenv("CIV_SUPERVISOR_UNIT", unit)
    monkeypatch.setattr(supervisor, "unit_state", lambda _: {"NRestarts": "1"})
    monkeypatch.setattr("sys.argv", ["supervisor", "game", "--state-dir", str(tmp_path)])
    observed = []

    class Game:
        def __init__(self, root, event, *, rejoin):
            observed.append(rejoin)

        def run(self):
            return "paused"

    monkeypatch.setattr(supervisor, "GameRun", Game)
    assert supervisor.main() == 0 and observed == [True]
    assert (tmp_path / "container.json").read_bytes() == before


def test_atomic_status_replacement_cannot_lend_new_mtime_to_old_stopped_body(monkeypatch, tmp_path):
    fake = ProcessOwner(monkeypatch)
    run = object.__new__(supervisor.GameRun)
    run.root, run.record = tmp_path, {"launch": LAUNCH}
    run.controller, run.services = fake.service.name, [fake.service]
    fake.container["State"]["StartedAt"] = "2024-03-09T16:00:00Z"
    path = tmp_path / "runtime.json"
    write_private_json(path, {"phase": "stopped", "launch": LAUNCH, "runner_status": "paused"})
    os.utime(path, (1709999999, 1709999999))
    original = json.loads
    replaced = False

    def decode(value, *args, **kwargs):
        nonlocal replaced
        result = original(value, *args, **kwargs)
        if isinstance(result, dict) and result.get("phase") == "stopped" and not replaced:
            replaced = True
            write_private_json(path, {"phase": "running", "launch": LAUNCH})
            os.utime(path, (1710000001, 1710000001))
        return result

    monkeypatch.setattr(supervisor.json, "loads", decode)
    assert run.current_runtime([fake.container]) is None and replaced
    assert json.loads(path.read_bytes())["phase"] == "running"


def test_batch_replacement_rejoins_running_seats_once_and_starts_only_pending(monkeypatch, tmp_path):
    write_private_json(
        tmp_path / "batch.json",
        {"format": 2, "parallel": 2, "runs": [{"id": "old", "status": "running"}, {"id": "new", "status": "pending"}]},
    )
    started = []

    class Game:
        def __init__(self, root, event, *, rejoin=False):
            started.append((root.name, rejoin))

        def run(self):
            return "completed"

    monkeypatch.setattr(supervisor, "GameRun", Game)
    monkeypatch.setattr(supervisor, "batch_summary", lambda *args: None)
    supervisor.run_batch(tmp_path, supervisor.threading.Event(), rejoin=True)
    assert sorted(started) == [("new", False), ("old", True)]


def test_explicit_supervisor_relaunch_joins_old_unit_collection_before_new_owner(monkeypatch, tmp_path):
    unit = "civ-game-" + OWNER
    write_private_json(
        tmp_path / "container.json",
        {
            "format": 2,
            "runtime": "pydantic-local-v1",
            "id": OWNER,
            "supervision": {"backend": "systemd-existing-cid-v1", "unit": unit},
        },
    )
    states = iter(
        [
            {"LoadState": "loaded", "ActiveState": "failed"},
            {"LoadState": "loaded", "ActiveState": "inactive"},
            {"LoadState": "not-found"},
        ]
    )
    commands = []
    monkeypatch.setattr(support, "require_restart_systemd", lambda: None)
    monkeypatch.setattr(support, "unit_state", lambda value: next(states))
    monkeypatch.setattr(support, "command", lambda args, **kwargs: commands.append(args))
    assert support.launch_background(tmp_path, "game") == unit
    assert commands[0] == ["systemctl", "--user", "stop", unit]
    assert commands[1][0] == "systemd-run"
    assert "--collect" not in commands[1] and "--property=RestartPreventExitStatus=78" in commands[1]
