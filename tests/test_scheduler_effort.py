"""Configured defaults survive adaptive activations and pre-fix recovery state."""

import copy
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from civ_agent.protocol import JsonObject
from civ_agent.scheduler import RunnerError, Scheduler

BASE = {"provider": "openai", "model": "gpt-6-luna", "reasoningEffort": "medium"}


def report(effort="low"):
    return {
        "effectiveSettings": {**BASE, "reasoningEffort": effort},
        "configuredSettings": dict(BASE),
        "decisionsSettings": {
            "decisions_enabled": True,
            "decisions_effort_enabled": True,
            "decisions_effort_levels": ["low", "medium", "high", "xhigh", "max"],
        },
        "effortSelection": {
            "version": "reasoning-effort-v1",
            "reason": "selected" if effort != "medium" else "not_standout",
            "applied": effort != "medium",
            "defaultEffort": "medium",
            "selectedEffort": effort,
            "candidate": effort,
        },
    }


@pytest.fixture
def scheduler(tmp_path):
    value = Scheduler(SimpleNamespace(root=tmp_path, save=Mock()), None, {}, {}, color="never")
    yield value
    value.finalizer.shutdown()


@pytest.mark.parametrize("legacy_effort", [None, "low", "medium", "high"])
def test_each_activation_pins_its_effort_without_changing_defaults(scheduler, legacy_effort):
    session = {} if legacy_effort is None else {"effectiveSettings": {**BASE, "reasoningEffort": legacy_effort}}
    for effort in ("low", "high", "medium", "low"):
        flight: JsonObject = {"civilization": "Rome"}
        assert scheduler.record_settings(report(effort), flight, session)
        assert session["configuredSettings"] == session["effectiveSettings"] == BASE
        assert flight["effectiveSettings"]["reasoningEffort"] == effort
        assert not scheduler.record_settings(report(effort), flight, session)


@pytest.mark.parametrize("field,value", [("model", "another-model"), ("provider", "openrouter")])
def test_provider_and_model_changes_are_still_rejected(scheduler, field, value):
    session = {}
    scheduler.record_settings(report(), {"civilization": "Rome"}, session)
    status = report()
    status["effectiveSettings"][field] = status["configuredSettings"][field] = value
    with pytest.raises(RunnerError, match="provider or model"):
        scheduler.record_settings(status, {"civilization": "Rome"}, session)
    with pytest.raises(RunnerError, match="provider or model"):
        scheduler.record_settings({**report(), "effectiveSettings": status["effectiveSettings"]}, {}, {})


def test_effort_changes_do_not_require_scheduler_selection_metadata(scheduler):
    flight: JsonObject = {"civilization": "Rome"}
    session = {}
    for effort in ("low", "high", "medium", "max"):
        value = {"effectiveSettings": {**BASE, "reasoningEffort": effort}, "configuredSettings": dict(BASE)}
        assert scheduler.record_settings(value, flight, session)
        assert flight["effectiveSettings"]["reasoningEffort"] == effort
        assert session["configuredSettings"] == BASE


def test_configured_effort_remains_controlled_by_the_service(scheduler):
    session = {}
    scheduler.record_settings(report(), {"civilization": "Rome"}, session)
    value = report("high")
    value["configuredSettings"]["reasoningEffort"] = "high"
    assert scheduler.record_settings(value, {"civilization": "Rome"}, session)
    assert session["configuredSettings"]["reasoningEffort"] == "high"


def test_legacy_services_can_change_effort_without_new_status_fields(scheduler):
    value, session = {"effectiveSettings": dict(BASE)}, {}
    scheduler.record_settings(value, {"civilization": "Rome"}, session)
    value = copy.deepcopy(value)
    value["effectiveSettings"]["reasoningEffort"] = "low"
    assert scheduler.record_settings(value, {"civilization": "Rome"}, session)


@pytest.mark.parametrize("field,value", [("provider", "unknown"), ("model", "bad model"), ("reasoningEffort", 3)])
def test_invalid_settings_are_rejected(scheduler, field, value):
    status = report()
    status["effectiveSettings"][field] = value
    with pytest.raises(RunnerError, match="invalid model or reasoning settings"):
        scheduler.record_settings(status, {}, {})
