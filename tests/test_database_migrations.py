import os
import sqlite3

import pytest

from app.database import Database

# Windows has no POSIX mode bits: there st_mode is a read-only flag and
# 0o077 is always 0, so a mode assertion would pass without proving anything.
# Database's chmod calls are best-effort by design, so they are checked only
# where the operating system reports modes.
POSIX_MODES = os.name == "posix"


def test_migrations_apply_cleanly_to_empty_database(tmp_path):
    path = tmp_path / "fresh.sqlite3"
    db = Database(path)

    assert db.current_version == 10
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
        (3, "003_credentials_consistency.sql"),
        (4, "004_account_plans.sql"),
        (5, "005_account_records.sql"),
        (6, "006_account_tombstones.sql"),
        (7, "007_import_markers.sql"),
        (8, "008_facts_notes.sql"),
        (9, "009_project_account_keys.sql"),
        (10, "010_account_parent_guards.sql"),
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
        "plans",
        "data_imports",
        "facts",
        "notes",
    }
    assert expected <= names

    with db.connect() as connection:
        assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert connection.execute("PRAGMA busy_timeout").fetchone()[0] == 10000
        assert connection.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
        for table in expected - {"accounts", "data_imports"}:
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
    with pytest.raises(sqlite3.IntegrityError):
        with db.connect() as connection:
            connection.execute(
                "INSERT INTO accounts(id, email_verification_token_hash) VALUES (?, ?)",
                ("bad-token-account", "hash-without-expiry"),
            )
    with pytest.raises(sqlite3.IntegrityError):
        with db.connect() as connection:
            connection.execute(
                "INSERT INTO accounts(id, payload) VALUES (?, ?)",
                ("plaintext-account", '{"password":"not allowed"}'),
            )

    with db.connect() as first_connection, db.connect() as second_connection:
        # One connection per caller, never a shared one across request threads.
        assert first_connection is not second_connection
    assert db.open_connection_count == 0

    # Reopening is idempotent and keeps one record per migration.
    reopened = Database(path)
    assert reopened.current_version == 10
    if POSIX_MODES:
        assert path.stat().st_mode & 0o077 == 0
    backup = path.with_name(path.name + ".pre-migration.bak")
    assert backup.is_file()
    if POSIX_MODES:
        assert backup.stat().st_mode & 0o077 == 0


def test_a_connection_is_closed_when_its_block_ends(tmp_path):
    """``with sqlite3.connect(...)`` does not close; Database.connect must."""
    db = Database(tmp_path / "closed.sqlite3")
    assert db.open_connection_count == 0

    with db.connect() as connection:
        assert connection.execute("SELECT 1").fetchone()[0] == 1
        assert db.open_connection_count == 1

    assert db.open_connection_count == 0
    with pytest.raises(sqlite3.ProgrammingError):
        # Closed, not merely unreferenced: the object refuses to work.
        connection.execute("SELECT 1")


def test_store_operations_leave_no_connection_open(tmp_path):
    """After real store work the file is free, so Windows can delete it.

    The dashboard tests failed on Windows while removing their temp directory:
    every store call had left its connection open, and an open handle makes the
    file undeletable there. POSIX happily unlinks an open file, so only the
    platform where the check means something performs it; the connection count
    is the check that holds everywhere.
    """
    from app.sqlite_accounts import SQLiteAccountStore
    from app.sqlite_projects import SQLiteProjectStore
    from app.sqlite_store import SQLiteStore
    from app.store import ActionCard, ChatMessage

    path = tmp_path / "released.sqlite3"
    store = SQLiteStore(str(path))
    database = store.database
    accounts = SQLiteAccountStore(database, iterations=1_000)
    projects = SQLiteProjectStore(database)

    view = store.for_account("release-check")
    conversation = view.start_conversation()
    view.append_message(
        conversation.id,
        ChatMessage(
            who="Orbit",
            time="09:00",
            body="Add this line to your notes",
            is_orbit=True,
            action=ActionCard(id="act-release", summary="Review the proposal"),
        ),
    )
    view.create_task("Release the database file")
    assert view.pending_approvals()
    assert view.recent_activity()

    project = projects.for_account("release-check").create(
        name="Released", description="", source="new"
    )
    assert project.id

    account = accounts.register_email("Release Check", "release@example.test", "password-123")
    assert account["id"]

    assert database.open_connection_count == 0
    database.close()  # belt and braces; must already be a no-op
    assert database.open_connection_count == 0

    if os.name == "nt":
        # Windows refuses to delete a file with a live handle.
        path.unlink()
        assert not path.exists()


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


def test_project_task_and_dependency_parents_cannot_cross_accounts(tmp_path):
    db = Database(tmp_path / "parent-ownership.sqlite3")
    with db.connect() as connection:
        connection.executemany(
            "INSERT INTO accounts(id) VALUES (?)", [("A",), ("B",)]
        )
        connection.execute(
            "INSERT INTO projects(id, account_id) VALUES (?, ?)", ("project-B", "B")
        )
        connection.execute(
            "INSERT INTO tasks(id, account_id) VALUES (?, ?)", ("task-B", "B")
        )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO tasks(id, account_id, project_id) VALUES (?, ?, ?)",
                ("task-A", "A", "project-B"),
            )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO tasks(id, account_id, builds_on_id) VALUES (?, ?, ?)",
                ("task-A", "A", "task-B"),
            )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO conversations(id, account_id, project_id) VALUES (?, ?, ?)",
                ("conv-A", "A", "project-B"),
            )
