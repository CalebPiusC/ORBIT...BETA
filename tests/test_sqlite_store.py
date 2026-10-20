import sqlite3

from app.sqlite_store import SQLiteStore
from app.store import ChatMessage


def test_data_and_screen_state_survive_store_restart(tmp_path):
    path = tmp_path / "store.sqlite3"
    first = SQLiteStore(str(path))
    account = first.for_account("restart-account")
    conversation = account.start_conversation()
    account.append_message(
        conversation.id,
        ChatMessage(who="You", time="10:00", body="persist me", is_orbit=False),
    )
    account.set_mode("agent")
    account.set_thinking_level("deep")

    restarted = SQLiteStore(str(path)).for_account("restart-account")
    restored = restarted.get_conversation(conversation.id)
    assert restored is not None
    assert [message.body for message in restarted.thread(conversation.id)] == ["persist me"]
    assert restarted.mode == "agent"
    assert restarted.thinking_level == "deep"


def test_account_scoped_sql_cannot_read_update_or_delete_other_account_rows(tmp_path):
    path = tmp_path / "isolation.sqlite3"
    store = SQLiteStore(str(path))
    account_a = store.for_account("A")
    account_b = store.for_account("B")
    conv_a = account_a.start_conversation()
    conv_b = account_b.start_conversation()
    account_a.append_message(
        conv_a.id, ChatMessage(who="You", time="10:00", body="A", is_orbit=False)
    )
    account_b.append_message(
        conv_b.id, ChatMessage(who="You", time="10:01", body="B", is_orbit=False)
    )

    # Adapter queries are scoped by account_id; a foreign identifier is not
    # visible or mutable through the other account's scoped predicate.
    with store.database.connect() as db:
        assert db.execute(
            "SELECT id FROM conversations WHERE account_id = ? AND id = ?",
            ("A", conv_b.id),
        ).fetchone() is None
        assert db.execute(
            "SELECT id FROM messages WHERE account_id = ? AND owner_id = ?",
            ("A", conv_b.id),
        ).fetchone() is None
        updated = db.execute(
            "UPDATE conversations SET payload = ? WHERE account_id = ? AND id = ?",
            (b"tampered", "A", conv_b.id),
        )
        deleted = db.execute(
            "DELETE FROM conversations WHERE account_id = ? AND id = ?",
            ("A", conv_b.id),
        )
        assert updated.rowcount == 0
        assert deleted.rowcount == 0

    assert account_a.get_conversation(conv_b.id) is None
    assert account_a.thread(conv_b.id) == []
    assert account_a.replace_message(conv_b.id, "not-a-message", "x") is False

    with store.database.connect() as db:
        row = db.execute(
            "SELECT payload FROM conversations WHERE account_id = ? AND id = ?",
            ("B", conv_b.id),
        ).fetchone()
        assert row is not None
        assert db.execute(
            "SELECT COUNT(*) FROM messages WHERE account_id = ?", ("B",)
        ).fetchone()[0] == 1


def test_pending_approvals_and_activity_survive_a_restart(tmp_path):
    """The dashboard roll-ups read the SQLite store, not just the memory one."""
    from app.store import ActionCard, ChatMessage

    path = tmp_path / "dashboard.sqlite3"
    first = SQLiteStore(str(path))
    account = first.for_account("dash-account")
    conversation = account.start_conversation()
    account.append_message(
        conversation.id,
        ChatMessage(
            who="Orbit",
            time="09:12",
            body="Add this line to your notes: keep going",
            is_orbit=True,
            action=ActionCard(id="act-sqlite", summary="Add this line to your notes: keep going"),
        ),
    )
    project_task = account.create_task("Build the reading list")

    restarted = SQLiteStore(str(path)).for_account("dash-account")
    pending = restarted.pending_approvals()
    assert [item["summary"] for item in pending] == ["Add this line to your notes: keep going"]
    activity = restarted.recent_activity(limit=10)
    assert activity[0]["task"] == project_task.title
    assert activity[0]["text"] == "Task created from your request."
    assert len(activity) == 1


def test_dashboard_rollups_never_read_another_accounts_rows(tmp_path):
    from app.store import ActionCard, ChatMessage

    store = SQLiteStore(str(tmp_path / "rollups.sqlite3"))
    a = store.for_account("A")
    b = store.for_account("B")
    conv_a = a.start_conversation()
    a.append_message(
        conv_a.id,
        ChatMessage(
            who="Orbit",
            time="09:00",
            body="A proposal",
            is_orbit=True,
            action=ActionCard(id="act-a", summary="Alpha-only proposal"),
        ),
    )
    task_a = a.create_task("Alpha-only task")
    task_b = b.create_task("Beta-only task")

    assert [item["summary"] for item in a.pending_approvals()] == ["Alpha-only proposal"]
    assert b.pending_approvals() == []
    assert [item["task"] for item in a.recent_activity()] == [task_a.title]
    assert [item["task"] for item in b.recent_activity()] == [task_b.title]
    assert all(item["task"] != task_b.title for item in a.recent_activity())
    assert all(item["task"] != task_a.title for item in b.recent_activity())
