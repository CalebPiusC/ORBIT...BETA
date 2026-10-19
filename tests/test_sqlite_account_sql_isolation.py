import sqlite3

from app.database import Database
from app.sqlite_accounts import SQLiteAccountStore


def test_account_a_cannot_update_or_delete_account_b_rows_in_any_owned_table(tmp_path):
    db = Database(tmp_path / "owned.sqlite3")
    with db.connect() as connection:
        connection.executemany("INSERT INTO accounts(id) VALUES (?)", [("A",), ("B",)])
        connection.executemany(
            "INSERT INTO conversations(id, account_id) VALUES (?, ?)",
            [("conv-A", "A"), ("conv-B", "B")],
        )
        connection.executemany(
            "INSERT INTO messages(id, account_id, owner_id, conversation_id) VALUES (?, ?, ?, ?)",
            [("msg-A", "A", "conv-A", "conv-A"), ("msg-B", "B", "conv-B", "conv-B")],
        )
        connection.executemany(
            "INSERT INTO tasks(id, account_id) VALUES (?, ?)", [("task-A", "A"), ("task-B", "B")]
        )
        connection.execute(
            "INSERT INTO projects(id, account_id) VALUES (?, ?), (?, ?)",
            ("project-A", "A", "project-B", "B"),
        )
        connection.execute(
            "INSERT INTO tool_calls(id, account_id) VALUES (?, ?), (?, ?)",
            ("call-A", "A", "call-B", "B"),
        )
        connection.execute(
            "INSERT INTO screen_state(account_id) VALUES (?), (?)", ("A", "B")
        )
        connection.execute(
            "INSERT INTO heartbeat_reminders(id, account_id) VALUES (?, ?), (?, ?)",
            ("hb-A", "A", "hb-B", "B"),
        )
        connection.execute(
            "INSERT INTO analytics_data(id, account_id) VALUES (?, ?), (?, ?)",
            ("analytics-A", "A", "analytics-B", "B"),
        )
        connection.execute(
            "INSERT INTO plans(id, account_id, plan_date, text, saved_at) VALUES (?, ?, ?, ?, ?), (?, ?, ?, ?, ?)",
            ("plan-A", "A", "2026-10-19", "A", "now", "plan-B", "B", "2026-10-19", "B", "now"),
        )
        connection.execute(
            "INSERT INTO facts(id, account_id, fact) VALUES (?, ?, ?), (?, ?, ?)",
            ("fact-A", "A", "A fact", "fact-B", "B", "B fact"),
        )
        connection.execute(
            "INSERT INTO notes(id, account_id, created_at, content) VALUES (?, ?, ?, ?), (?, ?, ?, ?)",
            ("note-A", "A", "now", "A note", "note-B", "B", "now", "B note"),
        )

    cases = [
        ("conversations", "id", "conv-B", "payload"),
        ("messages", "id", "msg-B", "payload"),
        ("tasks", "id", "task-B", "payload"),
        ("projects", "id", "project-B", "payload"),
        ("tool_calls", "id", "call-B", "payload"),
        ("heartbeat_reminders", "id", "hb-B", "payload"),
        ("analytics_data", "id", "analytics-B", "payload"),
        ("plans", "id", "plan-B", "text"),
        ("facts", "id", "fact-B", "fact"),
        ("notes", "id", "note-B", "content"),
    ]
    with db.connect() as connection:
        for table, id_column, foreign_id, value_column in cases:
            # The account-scoped read predicate used by the production stores
            # must not expose a known row owned by the other account.
            read = connection.execute(
                f"SELECT {id_column} FROM {table} WHERE account_id = ? AND {id_column} = ?",
                ("A", foreign_id),
            )
            assert read.fetchone() is None, table
            updated = connection.execute(
                f"UPDATE {table} SET {value_column} = ? WHERE account_id = ? AND {id_column} = ?",
                ("attempted", "A", foreign_id),
            )
            deleted = connection.execute(
                f"DELETE FROM {table} WHERE account_id = ? AND {id_column} = ?",
                ("A", foreign_id),
            )
            assert updated.rowcount == 0, table
            assert deleted.rowcount == 0, table
            assert connection.execute(
                f"SELECT 1 FROM {table} WHERE account_id = ? AND {id_column} = ?",
                ("B", foreign_id),
            ).fetchone(), table

        assert connection.execute(
            "SELECT account_id FROM screen_state WHERE account_id = ? AND account_id = ?", ("A", "B")
        ).fetchone() is None
        updated_state = connection.execute(
            "UPDATE screen_state SET payload = ? WHERE account_id = ? AND account_id = ?",
            ("attempted", "A", "B"),
        )
        deleted_state = connection.execute(
            "DELETE FROM screen_state WHERE account_id = ? AND account_id = ?", ("A", "B")
        )
        assert updated_state.rowcount == 0
        assert deleted_state.rowcount == 0
        assert connection.execute(
            "SELECT 1 FROM screen_state WHERE account_id = ?", ("B",)
        ).fetchone()


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
