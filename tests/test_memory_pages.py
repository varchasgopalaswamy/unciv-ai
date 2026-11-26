"""Large notebook reads retain exact, versioned content without oversized turns."""

import asyncio

import pytest

from civ_agent.civilization_state import StateConflict
from civ_agent.memory_pages import READ_BUDGET, memory_page
from civ_agent.memory_store import RecordStore
from civ_agent.strict_schema import encoded
from tests.domain_fixtures import managed, setup


def test_large_search_and_read_fit_a_maximum_decision_batch(tmp_path):
    async def run():
        async with managed(tmp_path) as m:
            m.memory.observe("large", {"text": "Greece " * 190000, "nested": {"_evidence": "private"}}, m.main.id)
            raw = m.memory.search_memory(m.main.id, {"query": "Greece"})
            assert len(encoded(raw)) > 1048576
            assert any(tool["name"] == "memory_page" for tool in await m.gateway.tools(m.main))
            results = [
                await m.gateway.call(m.main, "memory_search", {"id": "observation:large"}, f"large-search-{i}")
                for i in range(12)
            ]
            for result in results:
                assert [record["id"] for record in result["records"]] == []
                assert [ref["id"] for ref in result["recordPages"]] == ["observation:large"]
            assert len(encoded(results).encode()) < 1048576
            read = await m.gateway.call(m.main, "memory_read", {"ids": ["observation:large"]}, "large-read")
            assert not read["records"] and read["recordPages"][0]["id"] == "observation:large"
            ref = read["recordPages"][0]
            page = await m.gateway.call(
                m.main, "memory_page", {"id": ref["id"], "version": ref["version"], "path": "/body/text"}, "page"
            )
            assert page["value"] == ("Greece " * 190000)[: page["nextOffset"]]
            assert page["hasMore"] and len(encoded(read).encode()) < READ_BUDGET
            assert await m.gateway.call(m.main, "memory_read", {"ids": ["observation:large"]}, "large-read") == read
            for index, (id, path, expected) in enumerate(
                [
                    ("observation:large", "/missing", "INVALID_MEMORY_PATH"),
                    ("observation:large", "/body/nested/_evidence", "INVALID_MEMORY_PATH"),
                    ("unread", "", "SOURCE_NOT_DELIVERED"),
                ]
            ):
                rejected = await m.gateway.call(m.main, "memory_page", {"id": id, "path": path}, f"bad-page-{index}")
                assert rejected["error"] == expected and rejected["recoverable"] and not rejected["applied"]

    asyncio.run(run())


def test_pages_preserve_nested_values_and_unicode_strings():
    text = 'é🦊"\\\n' * 1000
    record = {"body": {"a/b~c": [{"index": i, "text": text} for i in range(25)]}}
    pointer = "/body/a~1b~0c"
    first = memory_page(record, id="large", version=3, path=pointer)
    rest = memory_page(record, id="large", version=3, path=pointer, offset=first["nextOffset"])
    assert first["hasMore"] and not rest["hasMore"]
    assert [entry["path"] for entry in first["entries"] + rest["entries"]] == [f"{pointer}/{i}" for i in range(25)]
    output, offset = "", 0
    while True:
        page = memory_page(record, id="large", version=3, path=f"{pointer}/24/text", offset=offset)
        assert len(encoded(page).encode()) < READ_BUDGET
        output += page["value"]
        offset = page["nextOffset"]
        if not page["hasMore"]:
            break
    assert output == text
    for invalid in ("/missing", f"{pointer}/-"):
        with pytest.raises(ValueError, match="INVALID_MEMORY_PATH"):
            memory_page(record, id="large", version=3, path=invalid)


def test_pages_pin_delivered_versions_and_historical_evidence(tmp_path):
    memory = setup(tmp_path)
    try:
        memory.observe("large", {"text": "old " * 30000}, "game")
        memory.search_memory("game", {"id": "observation:large"})
        # Another job updates the source. A page continues the delivered version.
        memory.start_job("other", "communication", {}, game_turn=4)
        memory.observe("large", {"text": "new " * 30000}, "other")
        version, old = memory.delivered_memory_record("game", "observation:large")
        assert version == 1 and old["body"]["text"].startswith("old ")
        memory.refresh_memory("game", ["observation:large"])
        assert memory.delivered_memory_record("game", "observation:large")[0] == 2
        historical = memory.search_memory("game", {"id": "observation:large", "history": True})
        alias = historical["records"][-1]["evidence"]
        version, old = memory.delivered_memory_record("game", alias)
        assert version == 1 and old["body"]["text"].startswith("old ")
        with pytest.raises(StateConflict, match="SOURCE_NOT_DELIVERED"):
            memory.delivered_memory_record("game", "not-delivered")
        with memory.transaction() as db:
            RecordStore(db).write("routing:private", "routing", {"secret": "INTERNAL"})
        memory.refresh_memory("game", ["routing:private"])
        with pytest.raises(StateConflict, match="SOURCE_NOT_DELIVERED"):
            memory.delivered_memory_record("game", "routing:private")
    finally:
        memory.engine.dispose()
