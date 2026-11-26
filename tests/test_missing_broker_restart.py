"""Only explicit broker replacement may retire a proven missing physical owner."""

import hashlib
import json
import subprocess
from copy import deepcopy
from types import SimpleNamespace

import pytest

from civ_agent import cli
from civ_agent import container_supervisor as supervisor
from civ_agent import container_support as support
from civ_agent.commands import runtime
from civ_agent.decisions_ledger import DecisionsLedger, requests
from civ_agent.decisions_policy import DecisionsSettings
from civ_agent.decisions_tasks import request_body
from civ_agent.native_broker_contracts import NativeBrokerScope
from civ_agent.orchestration.contracts import NativeAuxiliaryTask, digest
from civ_agent.tls import write_private_json
from tests.test_broker_restart import configure
from tests.test_broker_restart import fake_runtime as fake_runtime

IMAGE = "sha256:" + "2" * 64


def missing_owner(fake, monkeypatch, role="auth", *, loaded=False):
    configure(fake.root, role)
    name = fake.existing(role)
    saved = json.loads((fake.root / "container.json").read_bytes())
    service = support.ContainerService.load(saved["services"][name])
    del fake.containers[name]
    state = (
        {
            "LoadState": "loaded",
            "ActiveState": "failed",
            "Description": "Civ container auth " + service.cid,
            "ExecStart": "{ path=/usr/bin/podman ; argv[]=/usr/bin/podman start --attach --sig-proxy=false "
            + service.cid
            + " ; ignore_errors=no ; pid=0 ; code=(null) ; status=0/0 }",
            "ExecStop": "{ path=/usr/bin/podman ; argv[]=/usr/bin/podman stop --ignore --time 90 "
            + service.cid
            + " ; ignore_errors=no ; pid=0 ; code=(null) ; status=0/0 }",
        }
        if loaded
        else {"LoadState": "not-found"}
    )
    case = SimpleNamespace(
        service=service, state=state, commands=[], absence={}, stopped=False, collect=True, clock=0.0, before_stop=None
    )

    def units(unit):
        assert unit == service.unit
        return deepcopy(case.state)

    def command(arguments, **options):
        case.commands.append(arguments)
        if arguments == ["systemctl", "--user", "reset-failed", service.unit]:
            assert not options and case.stopped
            case.state["ActiveState"] = "inactive"
            return subprocess.CompletedProcess(arguments, 0, "")
        assert arguments == ["systemctl", "--user", "stop", service.unit]
        assert options == {"timeout": 110}
        case.stopped = True
        if case.before_stop is not None:
            case.before_stop()
        if case.collect:
            case.state = {"LoadState": "not-found"}
        return subprocess.CompletedProcess(arguments, 0, "")

    def podman(*arguments, **options):
        if arguments[:2] != ("container", "exists"):
            return fake(*arguments, **options)
        fake.calls.append((arguments, options))
        target = arguments[2]
        code = case.absence.get(target)
        if code is None:
            code = 0 if any(target in {key, value["Id"]} for key, value in fake.containers.items()) else 1
        return subprocess.CompletedProcess(arguments, code, "")

    monkeypatch.setattr(support, "unit_state", units)
    monkeypatch.setattr(supervisor, "command", command)
    monkeypatch.setattr(support, "command", command)
    monkeypatch.setattr(supervisor, "podman", podman)
    return case


def authority(fake):
    write_private_json(fake.root / "clients.json", {"seat": "existing-auth-client"})
    write_private_json(fake.root / "operator/decisions-client.json", {"token": "synthetic-operator"})
    write_private_json(
        fake.root / "decisions-clients.json",
        {"seat": "existing-client", "operator-health": hashlib.sha256(b"synthetic-operator").hexdigest()},
    )
    settings = DecisionsSettings(decisions_enabled=True, decisions_backend="luna", additional_decisions_budget_usd=1)
    ledger = DecisionsLedger(fake.root)
    try:
        ledger.register("seat", "game", "Rome", settings.model_dump(), native_player="p0")
        body = request_body(
            {
                "player": "Rome",
                "militaryReview": {
                    "baselineTurn": 0,
                    "currentTurn": 1,
                    "nextReviewTurn": 5,
                    "baseline": {},
                    "current": {},
                },
            },
            settings,
        )
        assert (
            ledger.reserve(
                "seat",
                "a" * 64,
                "b" * 64,
                body,
                native_scope=NativeBrokerScope(game="game", player="p0", epoch="e1"),
                native_task=NativeAuxiliaryTask(kind="military_triage", key=digest("original-task")),
            )
            is None
        )
        with ledger.engine.connect() as db:
            row = dict(db.execute(requests.select()).mappings().one())
        assert row["input_tokens"] is None and json.loads(row["result"])["pending"]
        return row
    finally:
        ledger.engine.dispose()


def protected(fake):
    return {
        str(path.relative_to(fake.root)): path.read_bytes()
        for path in fake.root.rglob("*")
        if path.is_file()
        and path.name not in {"container.json", "ready.json", "decisions-ready.json", "decisions-preflight.json"}
    }


@pytest.mark.parametrize("role", ["auth", "decisions"])
@pytest.mark.parametrize("loaded", [False, True])
def test_explicit_missing_owner_replacement_preserves_native_unknown_and_credentials(
    fake_runtime, monkeypatch, role, loaded
):
    fake = fake_runtime
    case = missing_owner(fake, monkeypatch, role, loaded=loaded)
    unknown = authority(fake)
    before = protected(fake)
    ensure = supervisor.ensure_auth if role == "auth" else supervisor.ensure_decisions
    ensure(fake.root, IMAGE, restart=True)
    assert protected(fake) == before
    saved = json.loads((fake.root / "container.json").read_bytes())["services"][case.service.name]
    replacement = support.ContainerService.load(saved)
    assert replacement.name == case.service.name and replacement.owner == "auth" and replacement.image == IMAGE
    assert replacement.cid != case.service.cid and replacement.unit == case.service.unit
    assert fake.containers[replacement.name]["State"]["Running"]
    assert case.stopped == loaded and len(case.commands) == int(loaded)
    assert [(args[2], options) for args, options in fake.calls if args[:2] == ("container", "exists")] == [
        (case.service.cid, {"check": False}),
        (case.service.name, {"check": False}),
    ] * 2
    assert not any(args[0] in {"stop", "rm"} for args, _ in fake.calls)
    ledger = DecisionsLedger(fake.root)
    try:
        with ledger.engine.connect() as db:
            assert dict(db.execute(requests.select()).mappings().one()) == unknown
    finally:
        ledger.engine.dispose()


def test_explicit_restart_cli_recovers_removed_decisions_owner_without_losing_ledger(fake_runtime, monkeypatch, capsys):
    fake = fake_runtime
    case = missing_owner(fake, monkeypatch, "decisions", loaded=True)
    unknown = authority(fake)
    before = protected(fake)
    monkeypatch.setattr(runtime, "resolve_image", lambda _: IMAGE)
    assert cli.main(["auth", "restart", "--auth-dir", str(fake.root), "--image", "reviewed:runtime"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["auth"]["status"] == "not_configured" and report["decisions"]["status"] == "ready"
    assert report["decisions"]["container"] == case.service.name and case.stopped
    assert protected(fake) == before
    ledger = DecisionsLedger(fake.root)
    try:
        with ledger.engine.connect() as db:
            assert dict(db.execute(requests.select()).mappings().one()) == unknown
    finally:
        ledger.engine.dispose()


def test_normal_ensure_does_not_retire_or_recreate_missing_owner(fake_runtime, monkeypatch):
    fake = fake_runtime
    case = missing_owner(fake, monkeypatch)
    before = (fake.root / "container.json").read_bytes()
    monkeypatch.setattr(support, "inspected", fake.inspected)
    monkeypatch.setattr(supervisor, "start_container_service", support.start_container_service)
    with pytest.raises(supervisor.ContainerError, match="ownership differs"):
        supervisor.ensure_auth(fake.root, case.service.image)
    assert (fake.root / "container.json").read_bytes() == before
    assert not fake.calls and not case.commands


@pytest.mark.parametrize("target", ["cid", "name"])
@pytest.mark.parametrize("code", [0, 125])
def test_missing_inspection_is_not_absence_without_both_exact_exists_one(fake_runtime, monkeypatch, target, code):
    fake = fake_runtime
    case = missing_owner(fake, monkeypatch, loaded=True)
    case.absence[case.service.cid if target == "cid" else case.service.name] = code
    before = (fake.root / "container.json").read_bytes()
    with pytest.raises(supervisor.ContainerError, match="absence could not be confirmed"):
        supervisor.ensure_auth(fake.root, IMAGE, restart=True)
    assert (fake.root / "container.json").read_bytes() == before
    assert not case.commands and not any(args[0] == "create" for args, _ in fake.calls)


@pytest.mark.parametrize("field", ["name", "owner", "unit", "image", "targetImage"])
def test_unowned_or_unpinned_missing_descriptor_is_never_retired(fake_runtime, monkeypatch, field):
    fake = fake_runtime
    case = missing_owner(fake, monkeypatch)
    path = fake.root / "container.json"
    saved = json.loads(path.read_bytes())
    if field != "targetImage":
        saved["services"][case.service.name][field] = {
            "name": "civ-foreign-auth",
            "owner": "foreign",
            "unit": "civ-foreign-auth.service",
            "image": "unverified:tag",
        }[field]
        write_private_json(path, saved)
    before = path.read_bytes()
    with pytest.raises(supervisor.ContainerError):
        supervisor.ensure_auth(fake.root, "unverified:tag" if field == "targetImage" else IMAGE, restart=True)
    assert path.read_bytes() == before and not fake.calls and not case.commands


@pytest.mark.parametrize("field", ["Description", "ExecStart", "ExecStop"])
def test_foreign_unit_metadata_is_not_stopped(fake_runtime, monkeypatch, field):
    fake = fake_runtime
    case = missing_owner(fake, monkeypatch, loaded=True)
    case.state[field] = case.state[field].replace(case.service.cid, "f" * 64)
    before = (fake.root / "container.json").read_bytes()
    with pytest.raises(supervisor.ContainerError, match="Systemd process ownership differs"):
        supervisor.ensure_auth(fake.root, IMAGE, restart=True)
    assert not case.commands and (fake.root / "container.json").read_bytes() == before
    assert not any(args[0] == "create" for args, _ in fake.calls)


def test_uncollected_unit_preserves_descriptor_after_one_explicit_failed_state_reset(fake_runtime, monkeypatch):
    fake = fake_runtime
    case = missing_owner(fake, monkeypatch, loaded=True)
    case.collect = False
    monkeypatch.setattr(
        support,
        "time",
        SimpleNamespace(
            monotonic=lambda: case.clock, sleep=lambda seconds: setattr(case, "clock", case.clock + seconds)
        ),
    )
    before = (fake.root / "container.json").read_bytes()
    with pytest.raises(supervisor.ContainerError, match="unit is still loaded"):
        supervisor.ensure_auth(fake.root, IMAGE, restart=True)
    assert case.stopped and case.clock >= 5
    assert (fake.root / "container.json").read_bytes() == before
    assert not any(args[0] in {"create", "rm"} for args, _ in fake.calls)
    assert case.commands == [
        ["systemctl", "--user", "stop", case.service.unit],
        ["systemctl", "--user", "reset-failed", case.service.unit],
    ]


def test_reappearing_name_after_owned_unit_stop_is_not_replaced(fake_runtime, monkeypatch):
    fake = fake_runtime
    case = missing_owner(fake, monkeypatch, loaded=True)
    case.before_stop = lambda: case.absence.update({case.service.name: 0})
    before = (fake.root / "container.json").read_bytes()
    with pytest.raises(supervisor.ContainerError, match="absence could not be confirmed"):
        supervisor.ensure_auth(fake.root, IMAGE, restart=True)
    assert case.stopped and (fake.root / "container.json").read_bytes() == before
    assert not any(args[0] == "create" for args, _ in fake.calls)


def test_changed_descriptor_after_stop_is_not_overwritten(fake_runtime, monkeypatch):
    fake = fake_runtime
    case = missing_owner(fake, monkeypatch, loaded=True)
    path = fake.root / "container.json"

    def change():
        saved = json.loads(path.read_bytes())
        saved["services"][case.service.name]["cid"] = "f" * 64
        write_private_json(path, saved)

    case.before_stop = change
    with pytest.raises(supervisor.ContainerError, match="descriptor changed"):
        supervisor.ensure_auth(fake.root, IMAGE, restart=True)
    assert json.loads(path.read_bytes())["services"][case.service.name]["cid"] == "f" * 64
    assert not any(args[0] == "create" for args, _ in fake.calls)
