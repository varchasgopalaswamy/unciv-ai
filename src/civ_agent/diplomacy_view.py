"""Operator presentation of shared diplomatic statements and native trade records."""

import json
from collections import Counter
from dataclasses import dataclass

from civ_agent.protocol import JsonObject
from civ_agent.terminal_output import visible_text


def item_text(clause):
    item = clause.get("item", {})
    kind, name, amount = item.get("type"), item.get("name", "Item"), item.get("amount", 1)
    if kind == "Gold":
        text = f"{amount} gold"
    elif kind == "Gold_Per_Turn":
        text = f"{amount} gold/turn"
    elif kind == "City":
        text = (clause.get("city") or {}).get("name", name)
    elif kind == "WarDeclaration":
        text = f"Declare war on {name}"
    elif kind == "PeaceProposal":
        text = f"Make peace with {name}"
    elif kind == "Introduction":
        text = f"Introduction to {name}"
    elif kind == "Treaty":
        text = name + (f" ({amount} gold each)" if name == "Research Agreement" else "")
    else:
        text = f"{amount} {name}" if amount != 1 else name
    duration = item.get("duration", -1)
    if isinstance(duration, int) and duration > 0:
        text += f" for {duration} turns"
    return visible_text(text)


def clauses_text(body):
    if friendship := body.get("friendship"):
        return [f"{friendship['name']} for {friendship['duration']} turns"]
    return [f"{c['from']} → {c['to']}: {item_text(c)}" for c in body.get("clauses", [])]


def trade_status(body):
    status = body.get("status", "Unknown")
    if status == "PENDING":
        return f"Awaiting {body.get('partner', 'response')}"
    return {
        "ENACTED": "Enacted",
        "SUPERSEDED": "Replaced by counteroffer",
        "DECLINED": "Declined",
        "WITHDRAWN": "Withdrawn",
        "EXPIRED": "Expired",
        "INVALID": "Failed validation",
    }.get(status, str(status).replace("_", " ").capitalize())


@dataclass(frozen=True)
class DiplomaticEvent:
    key: str
    epoch: str
    kind: str
    participants: tuple[str, ...]
    turn: int | None
    body: JsonObject
    order: int
    published_at: float

    @classmethod
    def from_record(cls, value, order, published_at):
        participants = value["participants"]
        if (
            not isinstance(value["body"], dict)
            or not isinstance(participants, list)
            or not all(isinstance(p, str) for p in participants)
        ):
            raise ValueError("Invalid diplomacy record")
        if value["turn"] is not None and type(value["turn"]) is not int:
            raise ValueError("Invalid diplomacy turn")
        return cls(
            value["id"],
            value["epoch"],
            value["kind"],
            tuple(sorted(participants)),
            value["turn"],
            value["body"],
            order,
            published_at,
        )

    @property
    def public(self):
        return self.kind == "message" and self.body.get("public") is True

    @property
    def conversation_key(self):
        return "public" if self.public else json.dumps(self.participants)

    @property
    def author(self):
        return self.body.get("author") or (self.participants[0] if self.participants else "Diplomacy")

    @property
    def visible(self):
        # The full confirmed outcome is already included in the enacted trade.
        return not (self.kind == "engine" and self.body.get("proposal"))

    @property
    def headline(self):
        b = self.body
        if self.kind == "message":
            target = "everyone" if self.public else ", ".join(p for p in self.participants if p != self.author)
            return f"{self.author} → {target}: {b.get('text', '')}"
        if self.kind == "trade":
            prefix = "Friendship" if b.get("friendship") else "Trade"
            if b.get("status") == "PENDING":
                action = "counteroffered" if b.get("version", 1) > 1 else "offered"
                return f"{self.author} {action}: {'; '.join(clauses_text(b))}. {trade_status(b)}."
            return f"{prefix} {trade_status(b).lower()}: {' ↔ '.join(self.participants)}. {'; '.join(clauses_text(b))}"
        if self.kind == "offer":
            return f"{self.author} proposed a social promise: {b.get('terms', '')}"
        if self.kind == "consent":
            decision = {
                "accept": "accepted",
                "withdraw": "withdrew",
                "reconsider": "reconsidered",
            }.get(b.get("decision", ""), b.get("decision", "responded to"))
            return f"{self.author} {decision} social promise {b.get('proposal', '')} v{b.get('version', '?')}."
        if self.kind == "agreement":
            return f"Agreement {' ↔ '.join(self.participants)}: {b.get('change', b.get('status', '')).replace('_', ' ').lower()} ({b.get('reason', '')})."
        return f"{self.author}: {str(b.get('action', 'Diplomatic action')).replace('_', ' ').lower()} — {' ↔ '.join(self.participants)}."

    def detail(self, previous=None):
        b = self.body
        lines = [self.headline]
        if self.kind == "trade":
            lines.append(f"Proposal {b.get('proposal', '?')} · version {b.get('version', '?')} · {trade_status(b)}")
            if b.get("mode") == "NATIVE_AI":
                lines.append(
                    "Native AI offer"
                    + ("; gameplay continues while awaiting a response." if b.get("status") == "PENDING" else ".")
                )
            if previous is not None and b.get("version") != previous.get("version"):
                old, new = Counter(clauses_text(previous)), Counter(clauses_text(b))
                lines.extend("Added: " + term for term in (new - old).elements())
                lines.extend("Removed: " + term for term in (old - new).elements())
            if b.get("consents"):
                lines.append("Decisions: " + "; ".join(f"{c['author']}: {c['decision']}" for c in b["consents"]))
            if b.get("failure"):
                lines.append("Reason: " + b["failure"])
            if b.get("status") == "ENACTED":
                lines.append("Confirmed by the game engine.")
        elif self.kind == "offer":
            lines.append(f"Social promise {b.get('proposal', '?')} · version {b.get('version', '?')}")
            if b.get("conditions"):
                lines.append("Conditions: " + b["conditions"])
            if b.get("expiresTurn") is not None:
                lines.append(f"Stated expiry: turn {b['expiresTurn']}")
        elif self.kind == "consent" and b.get("reason"):
            lines.append("Reason: " + b["reason"])
        return visible_text("\n".join(lines))


@dataclass(frozen=True)
class Conversation:
    key: str
    participants: tuple[str, ...]
    events: tuple[DiplomaticEvent, ...]

    @property
    def title(self):
        return "Public diplomacy" if self.key == "public" else " ↔ ".join(self.participants)

    @property
    def pending(self):
        latest = {e.body["proposal"]: e.body for e in self.events if e.kind == "trade"}
        return tuple(b for b in latest.values() if b.get("status") == "PENDING")

    @property
    def status(self):
        if self.pending:
            return "; ".join(trade_status(b) for b in self.pending)
        latest = next((e for e in reversed(self.events) if e.kind == "trade"), None)
        return trade_status(latest.body) if latest else "Conversation"

    def transcript(self):
        versions = {}
        blocks = []
        for event in self.events:
            proposal = event.body.get("proposal") if event.kind == "trade" else None
            blocks.append((event, event.detail(versions.get(proposal))))
            if proposal:
                versions[proposal] = event.body
        return blocks


def conversations(events, civilization=None, turn=None):
    groups = {}
    for event in sorted(events, key=lambda e: (e.turn if e.turn is not None else -1, e.order)):
        if event.visible:
            groups.setdefault(event.conversation_key, []).append(event)
    result = []
    for key, entries in groups.items():
        participants = tuple(sorted({p for e in entries for p in e.participants}))
        if civilization is not None and civilization not in participants:
            continue
        if turn is not None and not any(e.turn == turn for e in entries):
            continue
        result.append(Conversation(key, participants, tuple(entries)))
    return sorted(result, key=lambda c: (not bool(c.pending), -(c.events[-1].turn or 0), -c.events[-1].order))
