"""Deterministic host-side barriers for the real HTTPS scheduler/service regression.

Only this test host waits to force the failing interleaving. Production model jobs
use delivered events and never wait for another player's reply.
"""

import copy

from tests.test_victory_snapshot import position


class ConcurrentTradeHost:
    def __init__(self, host, *, peace=False, settlement_delay=0):
        self.host = host
        self.peace = peace
        self.settlement_delay = settlement_delay
        self.events = {civ: [] for civ in host.agent_tokens}
        self.trade = None
        self.gameplay_waiting = False
        self.enacted = None
        self.writes = []
        self.reads = []
        host.mcp_override = self.tool

    def post(self, recipient, kind, body):
        stream = self.events[recipient]
        stream.append({"id": len(stream) + 1, "kind": kind, "body": body, "wake": True})
        self.host.changed.notify_all()

    def wait(self, predicate):
        assert self.host.changed.wait_for(predicate, timeout=15), "Concurrent trade test barrier timed out"

    def tool(self, name, args, token):
        host = self.host
        civ = next(
            (civ for civ, credential in host.agent_tokens.items() if token == credential),
            host.status()["activeCivilization"],
        )
        gameplay = token == host.player_token
        self.reads.append((civ, gameplay, name, args.get("op")))
        if name == "victory_progress":
            return position(host.handoff // 2, names=[])
        if name in {"research_details", "policy_details"}:
            return {"technologies" if name == "research_details" else "policies": []}
        if name == "player_turn":
            if civ == "Greece" and gameplay:
                assert self.enacted is not None
            return {
                "scenario": {"name": "CONCURRENT_TRADE_TEST", "peace": self.peace},
                "civilization": civ,
                "epoch": host.epoch,
                "turn": host.handoff // 2,
                "phase": host.phase,
                "canEndTurn": True,
                "rules": {"victoryTypes": []},
                "revision": "rev1",
                "turnReview": {"id": "review1"},
            }
        if name == "player_command":
            assert self.enacted is not None and self.trade is not None and self.trade["status"] == "ENACTED"
            return None  # The ordinary fake host commits the handoff.
        if name != "player_communication":
            return None
        op = args["op"]
        if op == "negotiation":
            if self.trade is not None and self.trade["status"] == "PENDING":
                self.gameplay_waiting = True
                host.changed.notify_all()
                if args.get("waitSeconds"):
                    self.wait(lambda: self.enacted is not None)
            return {"negotiation": {"active": self.trade is not None and self.trade["status"] == "PENDING"}}
        if op == "state":
            return {
                "epoch": host.epoch,
                "civilization": civ,
                "peaceVersion": 0,
                "context": {
                    "turn": host.handoff // 2,
                    "scenario": {"name": "CONCURRENT_TRADE_TEST", "peace": self.peace},
                },
                "offers": [],
                "consents": [],
                "outcomes": [],
                "trades": [] if self.trade is None else [copy.deepcopy(self.trade)],
            }
        if op == "trade_options":
            return {"partner": args["partner"], "mode": "SHARED", "give": [], "receive": []}
        if op == "read":
            stream = self.events[civ]
            host.changed.wait_for(lambda: len(stream) > args["cursor"], timeout=1)
            return {"events": copy.deepcopy(stream[args["cursor"] :]), "nextCursor": len(stream), "hasMore": False}
        if op == "trade_read":
            if civ == "Greece" and not gameplay:
                self.wait(lambda: self.gameplay_waiting)
                host.changed.wait_for(lambda: False, timeout=self.settlement_delay)
            return {"trade": copy.deepcopy(self.trade)}
        if op == "send":
            host.changed.notify_all()
            return {"sent": True}
        if op == "trade_offer":
            if self.peace:
                for side in ("give", "receive"):
                    assert [item for item in args[side] if item["type"] == "Treaty"] == [
                        {"type": "Treaty", "name": "Peace Treaty", "amount": 1, "duration": 10}
                    ]
            self.writes.append((civ, gameplay, op))
            version = 1 if self.trade is None else self.trade["version"] + 1
            self.trade = {
                "proposal": "d1",
                "version": version,
                "author": civ,
                "partner": args["partner"],
                "status": "PENDING",
                "mode": "SHARED",
                "clauses": [{"from": civ, "to": args["partner"], "item": item} for item in args["give"]]
                + [{"from": args["partner"], "to": civ, "item": item} for item in args["receive"]],
            }
            self.post(args["partner"], "trade", copy.deepcopy(self.trade))
            return {"trade": copy.deepcopy(self.trade)}
        if op == "trade_decide":
            assert self.gameplay_waiting
            assert civ == "Rome" and not gameplay and host.handoff == 0
            assert self.trade is not None and args["version"] == self.trade["version"] == 2
            self.writes.append((civ, gameplay, op))
            self.trade["status"] = "ENACTED"
            self.enacted = {"civilization": civ, "activeCivilization": "Rome", "handoff": host.handoff}
            self.trade["outcome"] = {"confirmation": self.enacted}
            host.changed.notify_all()
            return {"trade": copy.deepcopy(self.trade), "outcome": {"confirmation": self.enacted}}
        return None
