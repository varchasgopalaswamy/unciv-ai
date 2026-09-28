import asyncio
import time

from textual.widgets import DataTable, RichLog, Static, TabbedContent

from civ_agent.diplomacy_view import clauses_text, conversations
from civ_agent.match_watch import MatchWatch
from civ_agent.monitoring import MonitorStore, path_for
from civ_agent.watch_state import WatchReader
from tests.test_match_watch import fixture, log_text, put_state


def publish(root, id, kind, body, *, participants=("Rome", "Greece"), epoch="g1", turn=2):
    MonitorStore(path_for(root)).put(
        "diplomacy",
        f"{epoch}/{id}",
        {
            "id": id,
            "epoch": epoch,
            "kind": kind,
            "participants": list(participants),
            "turn": turn,
            "body": body,
        },
        epoch=epoch,
        game_turn=turn,
    )


def terms(author="Rome", partner="Greece", version=1, status="PENDING"):
    return {
        "proposal": "trade1",
        "author": author,
        "partner": partner,
        "version": version,
        "status": status,
        "deadlineMillis": int(time.time() * 1000) + 300_000,
        "mode": "SHARED",
        "clauses": [
            {
                "from": "Rome",
                "to": "Greece",
                "item": {"type": "Luxury_Resource", "name": "Spices", "amount": 1, "duration": 30},
            },
            {
                "from": "Greece",
                "to": "Rome",
                "item": {
                    "type": "Gold_Per_Turn",
                    "name": "Gold per turn",
                    "amount": 3 if version == 2 else 2,
                    "duration": 30,
                },
            },
        ],
        "consents": [{"author": author, "decision": "accept", "turn": 2}],
    }


def diplomatic_fixture(root):
    fixture(root)
    publish(root, "m1", "message", {"author": "Rome", "text": "Would you trade for Spices?", "public": False})
    first = terms()
    publish(root, "v1", "trade", first)
    publish(root, "m2", "message", {"author": "Greece", "text": "I can offer 3 gold per turn.", "public": False})
    publish(root, "replaced", "trade", {**first, "status": "SUPERSEDED"})
    counter = terms("Greece", "Rome", 2)
    publish(root, "v2", "trade", counter)
    publish(
        root,
        "p1",
        "offer",
        {
            "author": "France",
            "proposal": "promise",
            "version": 1,
            "terms": "We may avoid settling near your border.",
            "conditions": "If our lands remain safe",
        },
        participants=("France", "Rome"),
    )
    publish(
        root,
        "m3",
        "message",
        {"author": "Greece", "text": "Separate French discussion", "public": False},
        participants=("Greece", "France"),
    )
    put_state(root, "diplomacy", {"epoch": "g1", "activeNegotiation": counter})
    return counter


def test_reader_keeps_threads_and_outcomes_separate_and_reads_without_writing(tmp_path):
    counter = diplomatic_fixture(tmp_path)
    reader = WatchReader(tmp_path)
    before = path_for(tmp_path).read_bytes()
    snapshot = reader.read()
    assert snapshot.diplomacy_available and snapshot.negotiation == counter
    groups = conversations(snapshot.diplomacy, "Rome")
    assert len(groups) == 2 and groups[0].participants == ("Greece", "Rome")
    assert groups[0].status == "Awaiting Rome"
    text = "\n".join(detail for _, detail in groups[0].transcript())
    assert "Added: Greece → Rome: 3 gold/turn for 30 turns" in text
    assert "Removed: Greece → Rome: 2 gold/turn for 30 turns" in text
    assert "Separate French discussion" not in text
    assert "Social promise" in groups[1].transcript()[0][1]
    assert reader.read() == snapshot and path_for(tmp_path).read_bytes() == before
    failed = {**counter, "status": "INVALID", "failure": "Insufficient gold"}
    publish(tmp_path, "failed", "trade", failed)
    put_state(tmp_path, "diplomacy", {"epoch": "g1", "activeNegotiation": None})
    updated = reader.read()
    thread = next(c for c in conversations(updated.diplomacy) if c.key == groups[0].key)
    assert updated.negotiation is None and not thread.pending
    assert "Reason: Insufficient gold" in thread.transcript()[-1][1]
    assert any("Insufficient gold" in entry.text for entry in updated.activity)
    assert "Insufficient gold" in updated.activity[-1].text
    assert updated.activity[-1].time == ""  # Publication time is not a claimed message timestamp.
    # A rewind's projection contains only communications retained in that branch.
    put_state(tmp_path, "run", {"epoch": "g2"})
    put_state(tmp_path, "diplomacy", {"epoch": "g2", "activeNegotiation": None})
    publish(tmp_path, "retained", "message", {"author": "Rome", "text": "Retained message"}, epoch="g2")
    restored = reader.read()
    assert len(restored.diplomacy) == 1 and restored.diplomacy[0].body["text"] == "Retained message"
    assert not restored.negotiation


def diplomacy_text(app):
    return "\n".join(line.text for line in app.query_one("#diplomacy", RichLog).lines)


def test_tui_shows_negotiations_outside_player_turn_and_filters_both_participants(tmp_path):
    counter = diplomatic_fixture(tmp_path)

    async def run():
        app = MatchWatch(tmp_path, refresh=60)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            assert "counteroffered" in log_text(app) and not app.verbose
            banner = app.query_one("#negotiation", Static)
            assert banner.display and "Awaiting Rome" in str(banner.render())
            # Greece is the recipient of Rome's initial proposal and is off turn.
            app.civilization = "Greece"
            app.render_records()
            assert "Would you trade for Spices?" in log_text(app)
            assert "France proposed a social promise" not in log_text(app)
            await pilot.press("d")
            await pilot.pause()
            assert app.query_one(TabbedContent).active == "diplomacy-tab"
            assert app.query_one("#conversations", DataTable).row_count == 2
            assert "Would you trade for Spices?" in diplomacy_text(app)
            assert "Added: Greece → Rome: 3 gold/turn" in diplomacy_text(app)
            await pilot.resize_terminal(80, 24)
            await pilot.pause()
            assert app.query_one("#diplomacy", RichLog).size.height >= 3
            await pilot.press("space")
            publish(tmp_path, "success", "trade", {**counter, "status": "ENACTED"})
            publish(tmp_path, "outcome", "engine", {"proposal": "trade1", "action": "trade"})
            put_state(tmp_path, "diplomacy", {"epoch": "g1", "activeNegotiation": None})
            await app.poll_records()
            assert banner.display
            await pilot.press("space")
            await pilot.pause()
            assert not banner.display
            assert diplomacy_text(app).count("Confirmed by the game engine.") == 1
            app.query_one(TabbedContent).active = "activity-tab"
            await pilot.pause()
            assert "Trade enacted" in log_text(app)
            await pilot.press("q")

    asyncio.run(run())


def test_literal_message_display_and_public_conversation_filtering(tmp_path):
    fixture(tmp_path)
    publish(
        tmp_path,
        "public",
        "message",
        {"author": "Rome", "public": True, "text": "[bold]literal[/bold]\x1b[2J"},
        participants=("Rome", "Greece", "France"),
    )
    view = WatchReader(tmp_path).read()
    group = conversations(view.diplomacy, "France")[0]
    assert group.title == "Public diplomacy"
    assert group.transcript()[0][1] == "Rome → everyone: [bold]literal[/bold]"
    assert conversations(view.diplomacy, "Egypt") == []


def test_trade_terms_distinguish_native_actions_and_show_friendship_duration():
    assert clauses_text({"friendship": {"name": "Declaration of Friendship", "duration": 30}}) == [
        "Declaration of Friendship for 30 turns"
    ]
    assert clauses_text(
        {
            "clauses": [
                {"from": "Rome", "to": "Greece", "item": {"type": "WarDeclaration", "name": "Egypt"}},
                {"from": "Greece", "to": "Rome", "item": {"type": "PeaceProposal", "name": "France"}},
            ]
        }
    ) == ["Rome → Greece: Declare war on Egypt", "Greece → Rome: Make peace with France"]
