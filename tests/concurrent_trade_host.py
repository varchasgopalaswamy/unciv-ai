"""Deterministic host-side barriers for the real HTTPS scheduler/service regression.

Only this test host waits to force the failing interleaving. Production model jobs
use delivered events and never wait for another player's reply.
"""

import copy


class ConcurrentTradeHost:
    def __init__(self, host):
        self.host = host
        self.events = {civ: [] for civ in host.agent_tokens}
        self.trade = None
        self.greek_planning = False
        self.counter_ready = False
        self.recovered = False
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
        if name == "player_turn":
            if civ == "Greece" and gameplay and host.handoff == 1:
                self.greek_planning = True
                host.changed.notify_all()
            return {
                "testConcurrentTrade": True,
                "civilization": civ,
                "epoch": host.epoch,
                "turn": host.handoff // 2,
                "phase": host.phase,
                "revision": "rev1",
                "turnReview": {"id": "review1"},
            }
        if name == "player_command" and civ == "Greece" and host.handoff == 1:
            self.wait(lambda: self.enacted is not None)
            return None  # The ordinary fake host commits the handoff.
        if name != "player_communication":
            return None
        op = args["op"]
        if op == "state":
            return {
                "testConcurrentTrade": True,
                "epoch": host.epoch,
                "civilization": civ,
                "peaceVersion": 0,
                "context": {"turn": host.handoff // 2, "scenario": {"name": "CONCURRENT_TRADE_TEST"}},
                "offers": [],
                "consents": [],
                "outcomes": [],
                "trades": [] if self.trade is None else [copy.deepcopy(self.trade)],
            }
        if op == "read":
            stream = self.events[civ]
            host.changed.wait_for(lambda: len(stream) > args["cursor"], timeout=1)
            return {"events": copy.deepcopy(stream[args["cursor"] :]), "nextCursor": len(stream), "hasMore": False}
        if op == "trade_read":
            if civ == "Greece" and not gameplay and not self.counter_ready:
                self.wait(lambda: self.greek_planning)
            if civ == "Greece" and gameplay:
                self.wait(lambda: self.counter_ready)
            return {"trade": copy.deepcopy(self.trade)}
        if op == "send":
            if args["text"] == "Counteroffer prepared":
                self.counter_ready = True
            if args["text"] == "Reconsidered after memory conflict":
                self.recovered = True
            host.changed.notify_all()
            return {"sent": True}
        if op == "trade_offer":
            self.writes.append((civ, gameplay, op))
            version = 1 if self.trade is None else self.trade["version"] + 1
            self.trade = {
                "proposal": "d1",
                "version": version,
                "author": civ,
                "partner": args["partner"],
                "status": "PENDING",
                "clauses": [{"from": civ, "to": args["partner"], "item": item} for item in args["give"]]
                + [{"from": args["partner"], "to": civ, "item": item} for item in args["receive"]],
            }
            self.post(args["partner"], "trade", copy.deepcopy(self.trade))
            return {"trade": copy.deepcopy(self.trade)}
        if op == "trade_decide":
            self.wait(lambda: self.recovered)
            assert civ == "Rome" and not gameplay and host.handoff == 1
            assert self.trade is not None and args["version"] == self.trade["version"] == 2
            self.writes.append((civ, gameplay, op))
            self.trade["status"] = "ENACTED"
            self.enacted = {"civilization": civ, "activeCivilization": "Greece", "handoff": host.handoff}
            host.changed.notify_all()
            return {"trade": copy.deepcopy(self.trade), "outcome": {"confirmation": self.enacted}}
        return None
