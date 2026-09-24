"""Private assessments, separate from command validity and observation history.

The caller owns the transaction. Gameplay is the sole notebook writer; other
jobs append immutable contributions for a subsequent gameplay activation.
"""

from civ_agent.memory_records import MEMORY_TYPES, MemoryContentError, amended_content, public_record
from civ_agent.memory_store import RecordStore


class Notebook:
    def __init__(self, db, snapshot, job, purpose, turn, game_turn):
        self.store = RecordStore(db, turn)
        self.snapshot, self.job, self.purpose = snapshot, job, purpose
        observed_turns = [
            r["body"].get("turn", r["body"].get("context", {}).get("turn"))
            for key, r in snapshot["records"].items()
            if key in snapshot.get("delivered", {}) and r["category"] == "observation"
        ]
        self.basis = {
            "job": job,
            "purpose": purpose,
            "turn": game_turn
            if game_turn is not None
            else max((v for v in observed_turns if isinstance(v, int)), default=None),
        }

    def update(self, change):
        key = change.get("id")
        if self.purpose != "gameplay" and change["op"] != "create":
            return {
                "id": key,
                "accepted": False,
                "reason": "NOTEBOOK_READ_ONLY",
                "instruction": "Append a contribution with create. The next gameplay job owns notebook revisions.",
            }
        old = self.store.all(keys=[key]).get(key) if key else None
        if change["op"] != "create":
            if (
                not old
                or old["category"] not in MEMORY_TYPES | {"contribution"}
                or (old["category"] == "contribution" and key not in self.snapshot.get("contributionIds", []))
            ):
                return {"id": key, "accepted": False, "reason": "UNKNOWN_MEMORY"}
            expected = self.snapshot.get("delivered", {}).get(key)
            if expected != old["version"]:
                return {
                    "id": key,
                    "accepted": False,
                    "reason": "MEMORY_CONFLICT",
                    "recoverable": True,
                    "recovery": {
                        "reads": [{"tool": "memory_read", "arguments": {"ids": [key], "includeRetired": True}}]
                    },
                    "instruction": "Read the current entry before editing it. The proposed edit was not applied.",
                }
            if old["body"].get("state") == "retired":
                return {"id": key, "accepted": False, "reason": "RETIRED_MEMORY"}
            if old["category"] == "contribution" and change["op"] != "retire":
                return {
                    "id": key,
                    "accepted": False,
                    "reason": "IMMUTABLE_CONTRIBUTION",
                    "instruction": "Use this contribution to create or amend a notebook entry, then retire it when handled.",
                }
        try:
            if change["op"] == "retire":
                assert old is not None
                body = {
                    **old["body"],
                    "state": "retired",
                    "retirementReason": change["reason"],
                    "assessment": change.get("assessment", ""),
                    "updatedTurn": self.store.turn,
                }
                category = old["category"]
            else:
                if change["op"] == "amend":
                    assert old is not None
                    proposed = amended_content(old["body"], change["fields"])
                else:
                    proposed = change["content"]
                content = self.store.content(proposed, self.snapshot, old["body"] if old else None)
                category = content["type"] if self.purpose == "gameplay" else "contribution"
                if category == "soul" and any(
                    k != key and r["body"].get("state") == "active"
                    for k, r in self.store.all(categories={"soul"}).items()
                ):
                    raise ValueError("SOUL_EXISTS: amend the existing principles")
                body = {
                    **content,
                    "createdTurn": old["body"]["createdTurn"] if old else self.store.turn,
                    "updatedTurn": self.store.turn,
                    "state": "active",
                    "basis": self.basis,
                    "_seenNotice": self.snapshot["records"].get(key, {}).get("body", {}).get("_noticeVersion", 0),
                }
                key = key or self.store.handle()
            version = self.store.write(key, category, body, self.job)
            if category == "belief" and old is not None:
                self.store.flag(keys=[key], reason=f"Belief {key} changed")
            record = {"version": version, "category": category, "body": body}
            # A successful edit acknowledges notices actually delivered to this
            # conversation. Later notices remain pending, including during reflection.
            if category in MEMORY_TYPES:
                notices = (
                    self.snapshot.get("notices", {})
                    if self.snapshot.get("memoryOnly")
                    else self.store.all(categories={"notice"}, project=False)
                )
                self.store.project_notices(key, record, notices)
            self.store.deliver(self.snapshot, {key: record})
            if category == "contribution":
                ids = self.snapshot.setdefault("contributionIds", [])
                if key not in ids:
                    ids.append(key)
            result = {
                "id": key,
                "accepted": True,
                "contribution": category == "contribution",
                "version": version,
                "record": public_record(key, record),
            }
            if body.get("unverifiedSources"):
                result["warnings"] = [
                    {
                        "code": "UNVERIFIED_REFERENCES",
                        "sources": body["unverifiedSources"],
                        "instruction": "Saved as an assessment. These references were not delivered to this job and are not verified evidence.",
                    }
                ]
            return result
        except MemoryContentError as error:
            return {"id": key, "accepted": False, "reason": str(error), "recoverable": True, **error.details}
        except ValueError as error:
            return {"id": key, "accepted": False, "reason": str(error)}
