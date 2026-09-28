"""Inspect stored records independently of the conversation's selected context."""

from civ_agent.memory_store import RecordStore


def stored_records(memory):
    with memory.transaction() as db:
        return RecordStore(db).all()
