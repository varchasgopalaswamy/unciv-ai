"""Native imports stay independent of the removable Codex runtime and fixtures."""

import subprocess
import sys


def test_native_launch_and_domain_fixtures_import_without_legacy_runtime():
    script = """
import importlib.abc
import sys

blocked = {
    'civ_agent.agent_service', 'civ_agent.agent_settings', 'civ_agent.providers',
    'civ_agent.turn_archive', 'civ_agent.scheduler', 'civ_agent.agent_history', 'civ_agent.runner_history',
    'civ_agent.provider_gate', 'civ_agent.structured_decisions',
    'civ_agent.chat_runtime', 'civ_agent.review_runtime', 'civ_agent.economy_runtime',
    'tests.test_agent_service', 'tests.test_economy_roles',
    'tests.test_proposal_gateway', 'tests.test_structured_memory',
    'tests.test_economy_optimization', 'tests.test_economy_schedule',
    'temporalio', 'pydantic_ai.durable_exec.temporal',
    'civ_agent.orchestration.player_agent', 'civ_agent.orchestration.managed_runtime',
}

class NoLegacyRuntime(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname in blocked:
            raise AssertionError('Native import reached removable runtime: ' + fullname)

sys.meta_path.insert(0, NoLegacyRuntime())
import civ_agent.commands.game
import civ_agent.commands.history
import civ_agent.commands.runtime
import civ_agent.container_supervisor
import civ_agent.orchestration.coordinator
import civ_agent.player_paths
import civ_agent.runtime.agents
import civ_agent.runtime.controller
import civ_agent.orchestration.managed_player
import civ_agent.orchestration.providers
import tests.domain_fixtures
assert not blocked.intersection(sys.modules)
"""
    result = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
