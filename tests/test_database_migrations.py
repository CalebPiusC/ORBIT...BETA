import sqlite3

import pytest

from app.database import Database


def test_migrations_apply_cleanly_to_empty_database(tmp_path):
    path = tmp_path / "fresh.sqlite3"
    db = Database(path)

    assert db.current_version == 2
    with db.connect() as connection:
        applied = connection.execute(
            "SELECT version, name FROM migrations ORDER BY version"
        ).fetchall()
        names = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = ?", ("table",)
            )
        }
    assert [(row[0], row[1]) for row in applied] == [
        (1, "001_initial_schema.sql"),
        (2, "002_account_integrity.sql"),
    ]
    expected = {
        "accounts",
        "conversations",
        "messages",
        "tasks",
        "projects",
        "tool_calls",
        "screen_state",
        "heartbeat_reminders",
        "analytics_data",
    }
    assert expected <= names

    with db.connect() as connection:
        assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        for table in expected - {"accounts"}:
            info = connection.execute(f"PRAGMA table_info({table})").fetchall()
            account_id = next(column for column in info if column[1] == "account_id")
            assert account_id[3] == 1, table
            foreign_keys = connection.execute(
                f"PRAGMA foreign_key_list({table})"
            ).fetchall()
            assert any(fk[2] == "accounts" and fk[3] == "account_id" for fk in foreign_keys), table

    with pytest.raises(sqlite3.IntegrityError):
        with db.connect() as connection:
            connection.execute(
                "INSERT INTO tasks(id, account_id) VALUES (?, ?)",
                ("invalid", "missing-account"),
            )

    # Reopening is idempotent and keeps one record per migration.
    reopened = Database(path)
    assert reopened.current_version == 2
    assert path.stat().st_mode & 0o077 == 0


def test_composite_foreign_keys_reject_cross_account_message_owner(tmp_path):
    db = Database(tmp_path / "ownership.sqlite3")
    with db.connect() as connection:
        connection.executemany(
            "INSERT INTO accounts(id) VALUES (?)", [("A",), ("B",)]
        )
        connection.execute(
            "INSERT INTO conversations(id, account_id) VALUES (?, ?)", ("conv-B", "B")
        )
    with pytest.raises(sqlite3.IntegrityError):
        with db.connect() as connection:
            connection.execute(
                "INSERT INTO messages(id, account_id, owner_id, conversation_id) "
                "VALUES (?, ?, ?, ?)",
                ("msg-A", "A", "conv-B", "conv-B"),
            )
