from datetime import date

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
from app.actions import PendingActions
from tools.confirmation import DeferredConfirmationGate
from tools.registry import AuditLog, ToolRegistry
from app.store import ChatMessage


def _settings():
    return HeartbeatSettings(plan_hour=10, checkin_gap_days=3, interval_seconds=60)


def _registry():
    gate = DeferredConfirmationGate()
    registry = ToolRegistry(gate)

    @registry.register(name="test_tool", description="A test action.", parameters={"type": "object", "properties": {}})
    def test_tool():
        return "done"

    return registry, gate


def test_restart_restores_each_data_kind_for_two_accounts_without_cross_leaks(tmp_path):
    path = tmp_path / "all-data.sqlite3"
    database = Database(path)
    accounts = SQLiteAccountStore(database, iterations=1_000)
    a = accounts.register_email("Ada", "ada@restart.test", "password1")
    b = accounts.register_email("Grace", "grace@restart.test", "password2")
    ids = {"A": a["id"], "B": b["id"]}

    store = SQLiteStore(str(path))
    projects = SQLiteProjectStore(database)
    registry, gate = _registry()
    actions = PendingActions(registry, gate, database=database)
    heartbeat = SQLiteHeartbeatManager(settings=_settings(), database=database)
    today = date.today()
    record_ids = {}

    for label, account_id in ids.items():
        view = store.for_account(account_id)
        view.set_mode("agent")
        view.set_thinking_level("deep")
        conversation = view.start_conversation()
        view.append_message(
            conversation.id,
            ChatMessage(who="You", time="10:00", body=f"hello {label}", is_orbit=False),
        )
        task = view.create_task(f"task for {label}")
        view.add_chat(
            task.id,
            ChatMessage(who="You", time="10:01", body=f"task message {label}", is_orbit=False),
        )
        project = projects.for_account(account_id).create(name=f"Project {label}")
        SQLiteFactStore(database, account_id).add(f"Fact {label}")
        SQLiteNoteStore(database, account_id).add(f"Note {label}", "2026-10-19T10:00:00Z")
        SQLitePlanStore(database, account_id).add(f"Plan {label}", today, "now")
        heartbeat.for_account(account_id).set_paused(label == "A", source="test")
        AuditLog(database=database, account_id=account_id).record_event(
            "chat_message_received", surface="home"
        )
        action = actions.propose("test_tool", {}, account_id=account_id)
        record_ids[label] = {
            "conversation": conversation.id,
            "task": task.id,
            "project": project.id,
            "action": action.id,
        }

    # Drop references to the live store/services and reconstruct them as a
    # fresh process would after closing the database.
    del store, projects, heartbeat, accounts, database
    reopened_db = Database(path)
    reopened_store = SQLiteStore(str(path))
    reopened_projects = SQLiteProjectStore(reopened_db)
    reopened_heartbeat = SQLiteHeartbeatManager(settings=_settings(), database=reopened_db)
    reopened_registry, reopened_gate = _registry()
    reopened_actions = PendingActions(reopened_registry, reopened_gate, database=reopened_db)

    for label, account_id in ids.items():
        view = reopened_store.for_account(account_id)
        data = record_ids[label]
        assert view.mode == "agent"
        assert view.thinking_level == "deep"
        assert view.get_conversation(data["conversation"]) is not None
        assert [m.body for m in view.thread(data["conversation"])] == [f"hello {label}"]
        assert view.get_task(data["task"]).chat[0].body == f"task message {label}"
        assert [p.name for p in reopened_projects.for_account(account_id).list()] == [f"Project {label}"]
        assert SQLiteFactStore(reopened_db, account_id).load() == [f"Fact {label}"]
        assert SQLiteNoteStore(reopened_db, account_id).list()[0]["content"] == f"Note {label}"
        assert SQLitePlanStore(reopened_db, account_id).has_plan(today)
        assert reopened_heartbeat.for_account(account_id).snapshot()["paused"] == (label == "A")
        assert build_analytics(reopened_db, account_id=account_id).messages_total == 1
        assert reopened_actions.owner_of(data["action"], account_id=account_id) == account_id
        outcome = reopened_actions.decide(data["action"], approved=True, account_id=account_id)
        assert outcome.result == "done"

    # The SQLite boundary itself scopes every row type; A's predicates cannot
    # see B's records even when the row IDs are known.
    with reopened_db.connect() as db:
        for table, row_id in (
            ("conversations", record_ids["B"]["conversation"]),
            ("tasks", record_ids["B"]["task"]),
            ("projects", record_ids["B"]["project"]),
            ("tool_calls", record_ids["B"]["action"]),
        ):
            assert db.execute(
                f"SELECT 1 FROM {table} WHERE account_id = ? AND id = ?",
                (ids["A"], row_id),
            ).fetchone() is None
