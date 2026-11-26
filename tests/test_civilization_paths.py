"""Civilization paths preserve names without admitting directory traversal."""

from urllib.parse import unquote

import pytest

from civ_agent.civilization_paths import TurnArchiveError, civilization_directory_name


def test_civilization_names_are_readable_reversible_and_collision_free():
    names = ["Rome", "Greece", "../Rome", "a/b", "a\\b", "a%2Fb", ".", "..", "Éire", "Éire", "中国", "New World"]
    encoded = [civilization_directory_name(name) for name in names]
    assert encoded[:2] == ["Rome", "Greece"]
    assert [unquote(name) for name in encoded] == names
    assert len(encoded) == len(set(encoded))
    for name in encoded:
        assert "/" not in name and "\\" not in name and name not in (".", "..")
    for name in ("", None, "x" * 256, "\ud800"):
        with pytest.raises(TurnArchiveError):
            civilization_directory_name(name)
