"""Infrastructure rejoins retain native authority; integrity failures stay visible."""

import asyncio
import copy
import errno
import socket
import ssl
import subprocess
import sys
import time
import urllib.error
import urllib.request
from types import SimpleNamespace

import pytest

from civ_agent.container_runtime import run_with_native_host
from civ_agent.operator_connection import JsonEndpoint, OperatorError
from civ_agent.orchestration.managed_host import ManagedHost
from tests.test_managed_game import Operator, request


@pytest.mark.parametrize(
    "cause,expected",
    [
        (TimeoutError(), "TRANSPORT_TIMEOUT"),
        (ConnectionRefusedError(), "TRANSPORT_UNAVAILABLE"),
        (ConnectionResetError(), "TRANSPORT_UNAVAILABLE"),
        (socket.gaierror(), "TRANSPORT_UNAVAILABLE"),
        (EOFError(), "TRANSPORT_UNAVAILABLE"),
        (OSError(errno.EHOSTUNREACH, "private endpoint"), "TRANSPORT_UNAVAILABLE"),
        (ssl.SSLCertVerificationError("private certificate"), None),
        (ssl.SSLError("private handshake"), None),
        (OSError(errno.EACCES, "private path"), None),
        (urllib.error.URLError("unknown private cause"), None),
    ],
)
def test_only_positive_network_failures_receive_transport_codes(cause, expected):
    class Opener(urllib.request.OpenerDirector):
        def open(self, *_args, **_kwargs):
            raise urllib.error.URLError(cause)

    endpoint = JsonEndpoint("https://localhost:12345/operator", "private-token", None)
    endpoint.opener = Opener()
    with pytest.raises(OperatorError) as caught:
        endpoint.call("status")
    assert caught.value.code == expected and caught.value.status is None
    assert "private endpoint" not in str(caught.value)
    assert "private-token" not in str(caught.value)


def test_reacquisition_fact_is_local_validated_and_held_until_native_pause():
    async def run():
        operator = Operator(auto_finish=False)
        operator.status.update(leaseId="shared-lease", lease_reacquired=True)
        owner = ManagedHost(request().authority, operator)
        try:
            initial = await owner.poll(request().authority)
            assert not initial.lease_reacquired
            assert "lease_reacquired" not in initial.model_dump(mode="json")
            operator.status["leaseId"] = None
            assert (await owner.poll(request().authority)).lease_reacquired
            assert (await owner.poll(request().authority)).lease_reacquired
            await owner.pause(request().authority)
            assert not (await owner.poll(request().authority)).lease_reacquired
            assert [op for op, _ in operator.calls].count("acquire") == 1
        finally:
            await owner.close()

    asyncio.run(run())


def test_unexpected_native_process_exit_closes_controller_and_fails_owner():
    async def run():
        entered, closed = asyncio.Event(), asyncio.Event()

        class Controller:
            async def run(self):
                entered.set()
                try:
                    await asyncio.Future()
                finally:
                    closed.set()

        process = subprocess.Popen([sys.executable, "-c", "raise SystemExit(7)"])
        try:
            with pytest.raises(RuntimeError, match="NATIVE_HOST_PROCESS_EXITED:7"):
                await asyncio.wait_for(run_with_native_host(Controller(), process), 3)
            assert entered.is_set() and closed.is_set() and process.poll() == 7
        finally:
            process.wait(timeout=3)

    asyncio.run(run())


def test_normal_controller_completion_does_not_misclassify_intentional_host_shutdown():
    async def run():
        process = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(10)"])
        try:

            async def complete():
                return 0

            assert await run_with_native_host(SimpleNamespace(run=complete), process) == 0
            assert process.poll() is None
        finally:
            process.terminate()
            process.wait(timeout=3)

    asyncio.run(run())


@pytest.mark.parametrize("body", [b"not json", b"[]"])
def test_malformed_native_response_has_no_transport_recovery_code(body):
    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            pass

        def read(self, _maximum):
            return body

    class Opener(urllib.request.OpenerDirector):
        def open(self, *_args, **_kwargs):
            return Response()

    endpoint = JsonEndpoint("https://localhost:12345/operator", "private-token", None)
    endpoint.opener = Opener()
    with pytest.raises(OperatorError) as caught:
        endpoint.call("status")
    assert caught.value.code is None


class RestartingOperator(Operator):
    def __init__(self):
        super().__init__(finish=False, auto_finish=False)
        self.offline_until = 0.0

    def call(self, operation, **fields):
        if time.monotonic() < self.offline_until:
            raise OperatorError("synthetic host connection refused", "TRANSPORT_UNAVAILABLE")
        # Native acquire is idempotent for its original owner, including two
        # concurrent activity retries which observed the expired lease.
        if operation == "acquire" and self.status["leaseId"] == fields["leaseId"]:
            self.calls.append((operation, copy.deepcopy(fields)))
            return {"status": copy.deepcopy(self.status), "playerToken": "private-human-activation-token"}
        return super().call(operation, **fields)
