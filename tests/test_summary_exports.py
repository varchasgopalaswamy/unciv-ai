"""History exports reuse canonical evidence without trusting workspace contents."""

from unittest.mock import patch

from sqlalchemy import event

from civ_agent.memory_history import SummaryReader
from tests.domain_fixtures import create, setup


def test_explicit_export_after_rewind_uses_restored_evidence(tmp_path):
    memory = setup(tmp_path)
    create(memory, {"type": "note", "text": "Completed turn"})
    memory.finish_job("game", True)
    memory.start_job("next", "gameplay", {}, game_turn=5)
    note = create(memory, {"type": "note", "text": "At the boundary"}, "next")
    workspace = tmp_path / "workspace"
    memory.export(workspace)
    checkpoint = tmp_path / "checkpoint.json"
    checksum = memory.backup(checkpoint)
    completed = (workspace / "turn_4/summary.json").read_bytes()
    memory.patch("next", [{"op": "amend", "id": note, "fields": {"text": "ABANDONED FUTURE"}}])
    memory.finish_job("next", True)
    memory.export(workspace)
    (workspace / "turn_4/summary.json").write_text("PLAYER TAMPERED")
    memory.restore(checkpoint, checksum, "restored")
    with patch.object(SummaryReader, "build", autospec=True, side_effect=SummaryReader.build) as summaries:
        memory.export(workspace)
    assert {call.args[1:] for call in summaries.call_args_list} == {(4, None), (5, None)}
    assert (workspace / "turn_4/summary.json").read_bytes() == completed
    changed = (workspace / "turn_5/summary.json").read_text()
    assert "At the boundary" in changed and "ABANDONED FUTURE" not in changed
    # Explicit exports regenerate from canonical records in any destination.
    memory.restore(checkpoint, checksum, "restored")
    memory.export(tmp_path / "fresh-workspace")
    assert (tmp_path / "fresh-workspace/turn_5/summary.json").read_text() == changed


def test_export_ignores_damaged_generated_files(tmp_path):
    memory = setup(tmp_path)
    create(memory, {"type": "note", "text": "Canonical memory"})
    memory.finish_job("game", True)
    memory.export(tmp_path / "workspace")
    (tmp_path / "workspace/turn_4/summary.json").write_text("CORRUPTED CONTENT")
    with patch.object(SummaryReader, "build", autospec=True, side_effect=SummaryReader.build) as summaries:
        memory.export(tmp_path / "fresh-workspace")
    assert summaries.call_count == 1
    summary = (tmp_path / "fresh-workspace/turn_4/summary.json").read_text()
    assert "Canonical memory" in summary and "CORRUPTED CONTENT" not in summary


def test_batch_walks_ancestry_once_and_never_reads_gameplay_snapshots(tmp_path):
    memory = setup(tmp_path)
    create(memory, {"type": "note", "text": "First turn"})
    memory.finish_job("game", True)
    for turn in range(5, 15):
        job = f"turn-{turn}"
        memory.start_job(job, "gameplay", {}, game_turn=turn)
        create(memory, {"type": "note", "text": f"Turn {turn}"}, job)
        memory.finish_job(job, True)
    statements = []

    def observe(conn, cursor, statement, parameters, context, many):
        statements.append(statement)

    event.listen(memory.engine, "before_cursor_execute", observe)
    try:
        memory.export(tmp_path / "workspace")
    finally:
        event.remove(memory.engine, "before_cursor_execute", observe)
    assert sum("WITH RECURSIVE" in sql for sql in statements) == 1
    assert not any("jobs.snapshot" in sql for sql in statements)
    assert len(list((tmp_path / "workspace").glob("turn_*/summary.json"))) == 11
    assert "Turn 14" in (tmp_path / "workspace/turn_14/summary.json").read_text()
