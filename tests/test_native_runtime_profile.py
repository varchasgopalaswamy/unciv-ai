"""Native operator timing uses SDK event evidence, never process telemetry."""

import asyncio
import json
from datetime import UTC, datetime, timedelta

import pytest
from pydantic_ai_harness.step_persistence import StepEvent

from civ_agent.commands import profile as command
from civ_agent.runtime_profile import profile
from civ_agent.tls import write_private_json
from tests.native_watch_fixtures import fixture, history, persistent_files, task


def measured(root):
    fixture(root)
    request = task(root, phase="COMPLETED")

    async def write():
        store = await history(root, request)
        stamp = datetime(2026, 11, 5, 12, tzinfo=UTC)
        for kind, step, elapsed, call in (
            ("model_request_completed", 0, 500, None),
            ("tool_call_started", 1, 100, "tool"),
            ("tool_call_completed", 1, 200, "tool"),
            ("model_request_started", 2, 600, None),
        ):
            await store.append_event(
                StepEvent(
                    run_id=request.scope.activation,
                    agent_name="player_role_v1",
                    kind=kind,
                    step_index=step,
                    tool_call_id=call,
                    timestamp=stamp + timedelta(milliseconds=elapsed),
                    error="PRIVATE raw provider exception" if kind == "model_request_completed" else None,
                )
            )

    asyncio.run(write())
    return request


def test_native_timing_pairs_only_real_recorded_boundaries_and_preserves_host_measurements(tmp_path):
    measured(tmp_path)
    before = persistent_files(tmp_path)
    report = profile([tmp_path])
    assert not report["unavailable"]
    assert report["llm"]["metrics"]["sdk.model_request.duration_ms"]["meanMs"] == 500
    assert report["llm"]["metrics"]["sdk.tool_call.duration_ms"]["sumMs"] == 100
    assert report["llm"]["observedBoundaries"]["unsettled_boundaries"] == 1
    assert report["hostTimings"]["accounting.commit"] == {"count": 20, "sumMs": 123, "maxMs": 20, "meanMs": 6.15}
    assert report["hostRoundTrips"]["available"] is False and report["decisions"]["available"] is False
    assert report["modelJobs"]["completed"] == 1
    assert "PRIVATE" not in json.dumps(report) and "Repeated startup" not in json.dumps(report)
    assert before == persistent_files(tmp_path)


def test_native_since_restart_uses_only_explicit_timestamp_and_cannot_slice_host_totals(tmp_path):
    measured(tmp_path)
    stamp = datetime(2026, 11, 5, 12, tzinfo=UTC).timestamp() * 1000
    write_private_json(tmp_path / "container.json", {"launchStartedAtMs": int(stamp + 550)})
    report = profile([tmp_path], since_restart=True)
    assert report["windows"][0]["sinceMs"] == stamp + 550
    assert report["llm"]["metrics"] == {} and report["llm"]["observedBoundaries"]["unsettled_boundaries"] == 1
    assert report["hostTimings"] == {} and report["unavailable"][0]["reason"] == "host_timings_have_no_launch_window"
    write_private_json(tmp_path / "container.json", {})
    missing = profile([tmp_path], since_restart=True)
    assert missing["windows"] == [] and missing["unavailable"]


def test_profile_publication_change_discards_current_game_samples(tmp_path, monkeypatch):
    measured(tmp_path)
    from civ_agent.orchestration import managed_epochs

    actual = managed_epochs.epoch_publication_basis
    calls = 0

    def changed(root):
        nonlocal calls
        calls += 1
        return actual(root) if calls == 1 else None

    monkeypatch.setattr(managed_epochs, "epoch_publication_basis", changed)
    report = profile([tmp_path])
    assert report["windows"] == [] and report["llm"]["metrics"] == {} and report["hostTimings"] == {}
    assert report["unavailable"]


def test_native_profile_cli_explains_unavailable_measurements_and_keeps_json(tmp_path, capsys):
    measured(tmp_path)
    args = command.parser().parse_args(["--state-dir", str(tmp_path)])
    assert command.run(args) == 0
    text = capsys.readouterr().out
    assert "sdk.model_request.duration_ms" in text and "not provider inference" in text
    assert "runner metrics" not in text and "Rebuild" not in text
    args.json = True
    assert command.run(args) == 0
    assert json.loads(capsys.readouterr().out)["llm"]["available"]


@pytest.mark.parametrize("identifier", ["../foreign", "..", "", "/absolute"])
def test_native_profile_refuses_unsafe_batch_members(tmp_path, identifier):
    write_private_json(tmp_path / "batch.json", {"format": 2, "runs": [{"id": identifier}]})
    with pytest.raises(ValueError):
        profile([tmp_path])
