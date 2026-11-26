"""A real stop signal flushes the local controller and joins its native child."""

import json
import os
import signal
import subprocess
import sys
import time

import pytest

from civ_agent.container_support import identity
from civ_agent.game_config import GameConfig
from civ_agent.tls import write_private_json

SCRIPT = r'''
import asyncio
import json
import sys
from pathlib import Path
from types import SimpleNamespace

from civ_agent import container_runtime
from civ_agent.execution_policy import legacy_execution_policy
from civ_agent.game_config import ResolvedGameConfig
from civ_agent.runtime import controller as controller_module
from civ_agent.runtime.state import ControllerStore

root = Path(sys.argv[1])
container_runtime.ROOT = root
container_runtime.ASSETS = root / 'assets'
container_runtime.ASSETS.mkdir()
container_runtime.PROVENANCE = root / 'provenance.json'

save = ControllerStore.save
async def recording_save(self, *args, **kwargs):
    try:
        return await save(self, *args, **kwargs)
    except asyncio.CancelledError:
        (root / 'interrupted-save.json').write_text(json.dumps({
            'mode': self.current.mode, 'cancellations': asyncio.current_task().cancelling(),
        }))
        raise
ControllerStore.save = recording_save

def prepare(_config):
    (root / 'endpoints.json').write_text(json.dumps({'caFile': str(root / 'ca.pem')}))
    return root / 'tls.json'

native_script = r"""
import os
import signal
import sys
import time
from pathlib import Path
root = Path(sys.argv[1])
(root / 'native.pid').write_text(str(os.getpid()))
def stopped(*_):
    (root / 'native-stopped').write_text('joined')
    raise SystemExit(0)
signal.signal(signal.SIGTERM, stopped)
print('MCP-READY:https://game:8765/mcp', flush=True)
while True:
    time.sleep(1)
"""

container_runtime.prepare = prepare
container_runtime.native = lambda *_args: [sys.executable, '-u', '-c', native_script, str(root)]

class Operator:
    def call(self, name, **_fields):
        with (root / 'native-calls.jsonl').open('a') as log:
            log.write(json.dumps({'name': name}) + '\n')
        return {'status': {
            'epoch': 'g1', 'turn': 0, 'handoff': 0, 'phase': 'PAUSED',
            'activeController': 'AGENT', 'activeCivilization': 'Rome',
            'serverInstance': 'synthetic-native', 'modelInvocations': 0,
        }}

class Player:
    def __init__(self, game):
        self.root = SimpleNamespace(civilization='Rome', routing=None)
        self.session = SimpleNamespace(
            game=game, player='p0', settings=SimpleNamespace(execution_policy=legacy_execution_policy()),
        )
        self.tasks = set()
    async def hold_communication(self):
        pass
    async def release_communication(self):
        pass
    async def begin(self, _attempt):
        pass
    async def run_role(self, _purpose, _context):
        (root / 'role-running').write_text('ready')
        try:
            await asyncio.Event().wait()
        finally:
            (root / 'role-cancelled').write_text('cancelled')
    async def run_roles(self, context):
        async with asyncio.TaskGroup() as group:
            for purpose in ('gameplay', 'economy', 'military'):
                group.create_task(self.run_role(purpose, context))
    async def close(self):
        (root / 'player-closed').write_text('closed')

async def run_controller(_root, **_kwargs):
    config = ResolvedGameConfig.model_validate_json((root / 'resolved.json').read_text())
    manifest = json.loads((root / 'container.json').read_text())
    controller = controller_module.LocalController(
        root, config, manifest, Operator(), players={'Rome': Player(manifest['id'])},
    )
    async def background(_player):
        await asyncio.Event().wait()
    controller._background = background
    return await controller.run()

controller_module.run_controller = run_controller
raise SystemExit(container_runtime.controller())
'''


@pytest.mark.parametrize("stop_signal", [signal.SIGTERM, signal.SIGINT])
def test_stop_signal_flushes_pause_barrier_and_cancels_then_reaps_native_child(tmp_path, stop_signal):
    config = GameConfig.model_validate(
        {
            "game": {"civilizations": ["Rome", "Greece"]},
            "players": [{"name": "Player"}, {"name": "Native", "controller": "built_in_ai"}],
            "agents": {"model": "scripted-v1"},
            "runtime": {"scripted_models": True},
        }
    ).resolved()
    root = tmp_path / "game"
    manifest = {**identity(root), "launch": "signal-fixture", "launchResumeRequested": False}
    write_private_json(root / "resolved.json", config.model_dump(mode="json"))
    write_private_json(root / "container.json", manifest)
    write_private_json(root / "control/start.json", {"launch": manifest["launch"]})
    write_private_json(root / "provenance.json", {"source": {"revision": "fixture"}, "engine": {"revision": "fixture"}})
    helper = tmp_path / "signal_child.py"
    helper.write_text(SCRIPT)
    log = tmp_path / "controller.log"
    with log.open("w") as output:
        process = subprocess.Popen([sys.executable, str(helper), str(root)], stdout=output, stderr=output)
        try:
            deadline = time.monotonic() + 20
            while not (root / "role-running").exists():
                if process.poll() is not None or time.monotonic() > deadline:
                    raise AssertionError(log.read_text())
                time.sleep(0.02)
            process.send_signal(stop_signal)
            assert process.wait(timeout=20) == 130, log.read_text()
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=5)
    status = json.loads((root / "native-runtime/controller.json").read_text())
    assert status["mode"] == "PAUSED" and status["reason"] == "OPERATOR_STOP"
    assert not (root / "interrupted-save.json").exists()
    calls = [json.loads(line)["name"] for line in (root / "native-calls.jsonl").read_text().splitlines()]
    assert calls.count("barrier") >= 2
    assert (root / "role-cancelled").exists() and (root / "player-closed").exists()
    assert (root / "native-stopped").exists()
    native_pid = int((root / "native.pid").read_text())
    try:
        os.kill(native_pid, 0)
    except ProcessLookupError:
        pass
    else:
        raise AssertionError("The controller left its synthetic native child alive")
