"""Managed launches use durable infrastructure and isolated native player workers."""

from civ_agent import container_supervisor as supervisor
from civ_agent.tls import write_private


def values(arguments, option):
    return [arguments[index + 1] for index, value in enumerate(arguments[:-1]) if value == option]


def mounts(arguments):
    return {
        target: (source, mode) for source, target, mode in (value.split(":") for value in values(arguments, "--volume"))
    }


class FakeProcess:
    def __init__(self, arguments, *, stdout, stderr):
        name = arguments[-1]
        if name.endswith("-coordinator"):
            stdout.write("COORDINATOR-READY\n")
        elif name.endswith("-worker"):
            stdout.write("PLAYER-WORKER-READY\n")
        stdout.flush()

    def wait(self, timeout=None):
        return 0

    def poll(self):
        return 0


def install_process_owner(fake, monkeypatch):
    """Bind the native launch fixture to exact-CID ownership without host I/O."""
    monkeypatch.setattr(supervisor, "podman", fake)
    monkeypatch.setattr(
        supervisor,
        "inspected",
        lambda name: next(
            (value for key, value in fake.containers.items() if key == name or value.get("Id") == name), None
        ),
    )

    def start_service(service, **kwargs):
        fake("start", service.name)
        if path := kwargs.get("log_path"):
            ready = (
                "COORDINATOR-READY"
                if service.name.endswith("-coordinator")
                else "PLAYER-WORKER-READY"
                if service.name.endswith("-worker")
                else "STARTED"
            )
            write_private(path, (ready + "\n").encode())

    monkeypatch.setattr(supervisor, "start_container_service", start_service)
    monkeypatch.setattr(
        supervisor, "stop_container_service", lambda service: fake("stop", "--time", "90", service.name)
    )
    monkeypatch.setattr(
        supervisor,
        "verify_container_service",
        lambda service, **_: (fake.containers[service.name], {"ActiveState": "active"}),
    )
