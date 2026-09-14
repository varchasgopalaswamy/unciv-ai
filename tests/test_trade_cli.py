import copy

import pytest

from civ_agent import cli
from civ_agent.commands import trade


@pytest.mark.parametrize("approve", [True, False])
def test_human_acceptance_pins_displayed_terms_and_requires_consent(tmp_path, monkeypatch, capsys, approve):
    displayed = {"proposal": "d1", "version": 1, "clauses": [{"from": "Greece", "to": "Rome", "item": {"amount": 20}}]}
    calls = []

    class Host:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            pass

        async def tool(self, name, args):
            calls.append((name, copy.deepcopy(args)))
            if name == "player_turn" or args.get("op") == "state":
                return {"epoch": "g1"}
            if args["op"] == "trade_read":
                return {"trade": copy.deepcopy(displayed)}
            assert args["op"] == "trade_decide" and args["version"] == 1
            return {"error": "STALE_REVISION"}

    def confirm(prompt):
        assert "exact trade terms" in prompt
        assert '"amount": 20' in capsys.readouterr().out
        displayed["version"] = 2
        return "y" if approve else "n"

    monkeypatch.setattr(trade, "player_credentials", lambda *args: ("Rome", "secret"))
    monkeypatch.setattr(trade, "endpoint_configuration", lambda *args: ("https://localhost:8765", None))
    monkeypatch.setattr(trade, "HostConnection", lambda *args: Host())
    monkeypatch.setattr("builtins.input", confirm)
    result = cli.main(["trade", "accept", "Rome", "--state-dir", str(tmp_path), "--proposal", "d1"])
    assert result == (1 if approve else 0)
    decisions = [args for _, args in calls if args.get("op") == "trade_decide"]
    assert len(decisions) == int(approve)
    assert "secret" not in capsys.readouterr().out
