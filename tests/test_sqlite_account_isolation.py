import sqlite3
from datetime import date

import pytest

from app.actions import PendingActions
from app.analytics import build_analytics
from app.database import Database
from app.sqlite_accounts import SQLiteAccountStore
from app.sqlite_projects import SQLiteProjectStore
from app.sqlite_store import SQLiteStore
from config import HeartbeatSettings
from heartbeat.sqlite_store import SQLiteHeartbeatManager
from memory.sqlitefacts import SQLiteFactStore
from memory.sqliteplans import SQLitePlanStore
from memory.sqlitenotes import SQLiteNoteStore
from app.store import ChatMessage
from tools.confirmation import DeferredConfirmationGate
from tools.registry import AuditLog, ToolRegistry


def test_account_a_isolated_from_b_through_real_persistent_store_methods(tmp_path):
    """Exercise the production adapters; no hand-written tenant queries here."""
    database = Database(tmp_path / "owned.sqlite3")
    store = SQLiteStore(str(database.path))
    a = store.for_account("A")
    b = store.for_account("B")

    # B creates persisted conversation/message/task/screen-state records.
    b.set_mode("agent")
    b.set_thinking_level("deep")
    conversation = b.start_conversation()
    message = ChatMessage(who="You", time="10:00", body="B message", is_orbit=False)
    b.append_message(conversation.id, message)
    task = b.create_task("B task")
    task_message = ChatMessage(who="You", time="10:01", body="B task message", is_orbit=False)
    b.add_chat(task.id, task_message)

    # B writes each other account-owned data kind through its actual adapter.
    projects = SQLiteProjectStore(database)
    project_b = projects.for_account("B").create(name="B project")
    facts_b = SQLiteFactStore(database, "B")
    facts_b.add("B fact")
    notes_b = SQLiteNoteStore(database, "B")
    notes_b.add("B note", "now")
    today = date(2026, 10, 19)
    plans_b = SQLitePlanStore(database, "B")
    plans_b.add("B plan", today, "now")
    heartbeat = SQLiteHeartbeatManager(
        settings=HeartbeatSettings(plan_hour=10, checkin_gap_days=3, interval_seconds=60),
        database=database,
    )
    heartbeat.for_account("B").set_paused(True, source="test")
    AuditLog(database=database, account_id="B").record_event(
        "chat_message_received", surface="test"
    )

    gate = DeferredConfirmationGate()
    registry = ToolRegistry(gate)

    @registry.register(name="test_tool", description="Test action", parameters={"type": "object", "properties": {}})
    def test_tool():
        return "done"

    actions = PendingActions(registry, gate, database=database)
    action_b = actions.propose("test_tool", {}, account_id="B")

    # Reads through A's bound production interfaces behave as not found/empty.
    assert a.get_conversation(conversation.id) is None
    assert a.conversations_for() == []
    assert a.thread(conversation.id) == []
    assert a.find_message(message.id) is None
    assert a.get_task(task.id) is None
    assert a.tasks_for() == []
    assert a.mode == "chat"
    assert a.thinking_level == "quick"
    projects_a = projects.for_account("A")
    assert projects_a.get(project_b.id) is None
    assert projects_a.list() == []
    assert SQLiteFactStore(database, "A").load() == []
    assert SQLiteNoteStore(database, "A").list() == []
    assert SQLitePlanStore(database, "A").for_date(today) == []
    assert not SQLitePlanStore(database, "A").has_plan(today)
    assert heartbeat.for_account("A").snapshot()["paused"] is False
    assert build_analytics(database, account_id="A").messages_total == 0
    assert actions.owner_of(action_b.id, account_id="A") is None

    # Real mutators invoked through A's view cannot change B's conversations,
    # messages, tasks, project, screen state, or pending action.
    a.set_title(conversation.id, "tampered")
    a.append_message(
        conversation.id,
        ChatMessage(who="A", time="10:02", body="unauthorized", is_orbit=False),
    )
    assert not a.replace_message(conversation.id, message.id, "tampered")
    assert a.set_done(task.id) is None
    a.add_chat(task.id, ChatMessage(who="A", time="10:03", body="unauthorized", is_orbit=False))
    assert projects_a.set_status(project_b.id, "archived") is None
    assert projects_a.set_summary(project_b.id, "tampered") is None
    a.set_mode("chat")
    a.set_thinking_level("quick")
    assert b.get_conversation(conversation.id).title != "tampered"
    assert [item.body for item in b.thread(conversation.id)] == ["B message"]
    assert b.get_task(task.id).done is False
    assert [item.body for item in b.get_task(task.id).chat] == ["B task message"]
    assert b.mode == "agent"
    assert b.thinking_level == "deep"
    assert projects.for_account("B").get(project_b.id).status == "building"
    assert projects.for_account("B").get(project_b.id).summary == ""

    # Facts, notes, plans, reminders and analytics expose only owner-scoped
    # append/read APIs. A's writes go to A; they do not alter B's records.
    facts_a = SQLiteFactStore(database, "A")
    assert facts_a.remove("B fact") == 0
    facts_a.add("A fact")
    assert facts_b.load() == ["B fact"]
    notes_a = SQLiteNoteStore(database, "A")
    notes_a.add("A note", "now")
    assert [row["content"] for row in notes_b.list()] == ["B note"]
    plans_a = SQLitePlanStore(database, "A")
    plans_a.add("A plan", today, "now")
    assert [row["text"] for row in plans_b.for_date(today)] == ["B plan"]
    heartbeat.for_account("A").set_paused(False, source="test")
    assert heartbeat.for_account("B").snapshot()["paused"] is True
    AuditLog(database=database, account_id="A").record_event(
        "chat_message_received", surface="test"
    )
    assert build_analytics(database, account_id="B").messages_total == 1
    with pytest.raises(KeyError):
        actions.decide(action_b.id, approved=True, account_id="A")
    assert actions.owner_of(action_b.id, account_id="B") == "B"
    assert actions.decide(action_b.id, approved=True, account_id="B").result == "done"


def test_removing_account_a_does_not_mutate_account_b_record(tmp_path):
    database = Database(tmp_path / "accounts.sqlite3")
    accounts = SQLiteAccountStore(database, iterations=1_000)
    a = accounts.register_email("Ada", "a@owned.test", "password1")
    b = accounts.register_email("Grace", "b@owned.test", "password2")
    accounts.delete_account(a["id"])
    assert accounts.by_id(a["id"]) is None
    assert accounts.by_id(b["id"])["email"] == "b@owned.test"
    assert accounts.check_password(b["id"], "password2")


def test_insert_with_unknown_account_is_rejected_and_storage_permissions_are_private(tmp_path):
    db_path = tmp_path / "missing" / "nested" / "store.sqlite3"
    db = Database(db_path)
    assert db_path.is_file()
    assert db_path.parent.is_dir()
    assert db_path.parent.stat().st_mode & 0o077 == 0
    assert db_path.stat().st_mode & 0o077 == 0

    with sqlite3.connect(db_path) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        try:
            connection.execute(
                "INSERT INTO notes(id, account_id, created_at, content) VALUES (?, ?, ?, ?)",
                ("bad", "missing", "now", "not accepted"),
            )
        except sqlite3.IntegrityError:
            pass
        else:
            raise AssertionError("a row with a nonexistent account_id was accepted")
