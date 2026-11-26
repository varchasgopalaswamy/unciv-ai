"""The live coordinator can import without the archived recovery-task system."""

import subprocess
import sys


def test_current_task_identity_does_not_load_archived_recovery_tasks_or_worker_rpc():
    script = """
import importlib.abc
import sys

class RetiredExecution(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path, target=None):
        if fullname in {
            'civ_agent.orchestration.recovery_tasks',
            'civ_agent.orchestration.gateway_transport',
        }:
            raise AssertionError('retired execution dependency: ' + fullname)

sys.meta_path.insert(0, RetiredExecution())
from civ_agent.orchestration import coordinator, effort, task_identity
assert coordinator.TurnRoleTask is task_identity.TurnRoleTask
assert effort.TurnRoleTask is task_identity.TurnRoleTask
assert effort.task_identity_sha256 is task_identity.task_identity_sha256
"""
    result = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stdout + result.stderr
