import sqlite3

from app.database import Database


def test_migrations_apply_cleanly_to_empty_database(tmp_path):
    path = tmp_path / "fresh.sqlite3"
    db = Database(path)

    assert db.current_version == 1
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
    assert [(row[0], row[1]) for row in applied] == [(1, "001_initial_schema.sql")]
    assert {
        "accounts",
        "conversations",
        "messages",
        "tasks",
        "projects",
        "tool_calls",
        "screen_state",
        "heartbeat_reminders",
        "analytics_data",
    } <= names

    with sqlite3.connect(path) as connection:
        for table in sorted(names - {"accounts", "migrations", "sqlite_sequence"}):
            info = connection.execute(f"PRAGMA table_info({table})").fetchall()
            account_id = next(column for column in info if column[1] == "account_id")
            assert account_id[3] == 1, table
            foreign_keys = connection.execute(f"PRAGMA foreign_key_list({table})").fetchall()
            assert any(fk[2] == "accounts" and fk[3] == "account_id" for fk in foreign_keys), table

    # Reopening is idempotent and keeps one migration record.
    Database(path)
    assert db.current_version == 1
