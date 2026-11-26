"""Managed controller configuration stays scoped and never invokes the old runner."""

import pytest

from civ_agent.game_config import GameConfig


def test_cli_rejects_limit_decrease_before_rewriting_saved_configuration(tmp_path):
    from civ_agent.commands import game

    root = tmp_path / "game"
    config = GameConfig(limits={"max_seconds": 60}).resolved()
    game.initialize(root, config, tmp_path / "auth", images={"runtime": "image"}, validation={"maxTurns": 10})
    paths = [root / "resolved.json", root / "container.json"]
    before = {path: path.read_bytes() for path in paths}
    arguments = game.parser(action="resume").parse_args(["--max-seconds", "59", "--port", "12000"])
    with pytest.raises(ValueError, match="decrease"):
        game.resume_configuration(root, arguments)
    assert {path: path.read_bytes() for path in paths} == before
    increased = game.parser(action="resume").parse_args(["--max-seconds", "90"])
    assert game.resume_configuration(root, increased).limits.max_seconds == 90
