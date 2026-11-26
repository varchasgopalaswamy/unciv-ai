"""Human native API commands select only a validated private player credential."""

import json

import pytest

from civ_agent.player_credentials import PlayerCredentialError, player_credentials
from civ_agent.tls import write_private_json


def players(tmp_path):
    path = tmp_path / "players.json"
    write_private_json(
        path,
        {
            "players": [
                {"civilizationId": "Rome", "token": "A" * 43},
                {"civilizationId": "Greece", "token": "B" * 43, "controller": "HUMAN"},
            ]
        },
    )
    return path


def test_native_player_selection_is_case_insensitive_and_does_not_change_credentials(tmp_path):
    path = players(tmp_path)
    before = path.read_bytes()
    assert player_credentials(path, "rOmE") == ("Rome", "A" * 43)
    assert player_credentials(path, "Greece") == ("Greece", "B" * 43)
    assert path.read_bytes() == before


@pytest.mark.parametrize(
    "fault", ["missing", "unknown", "permissions", "duplicate-token", "duplicate-name", "invalid-json", "oversized"]
)
def test_native_credentials_fail_closed_without_exposing_token_content(tmp_path, fault):
    path = players(tmp_path)
    requested = "Rome"
    if fault == "missing":
        path.unlink()
    elif fault == "unknown":
        requested = "Unknown"
    elif fault == "permissions":
        path.chmod(0o644)
    elif fault == "invalid-json":
        path.write_text("BROKEN " + "A" * 43)
    elif fault == "oversized":
        path.write_text("A" * 65537)
    else:
        value = json.loads(path.read_text())
        key = "token" if fault == "duplicate-token" else "civilizationId"
        value["players"][1][key] = value["players"][0][key]
        write_private_json(path, value)
    with pytest.raises(PlayerCredentialError) as error:
        player_credentials(path, requested)
    assert "A" * 43 not in str(error.value) and "B" * 43 not in str(error.value)
    assert error.value.__cause__ is None
