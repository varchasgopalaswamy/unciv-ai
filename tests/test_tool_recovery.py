"""Recovery reports expose schema constraints without copying rejected inputs."""

import json

import pytest
from pydantic import ValidationError

from civ_agent.orchestration.authored_plan_tools import CityPlanIntent
from civ_agent.schemas import argument_feedback


def test_invalid_city_reason_reports_allowed_schema_keys_without_echoing_rejected_content():
    with pytest.raises(ValidationError) as caught:
        CityPlanIntent.model_validate({"city": "city1", "reasons": {"PRIVATE_KEY": "PRIVATE_VALUE"}})
    feedback = argument_feedback(caught.value, CityPlanIntent.model_json_schema())
    assert feedback["allowedValues"] == [
        {"fields": ["reasons"], "values": ["focus", "growth", "citizens", "specialists", "construction", "purchases"]}
    ]
    assert "PRIVATE" not in json.dumps(feedback)
    assert "construction" in feedback["instruction"]
