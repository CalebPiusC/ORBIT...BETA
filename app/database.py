"""SQLite database bootstrap and ordered schema migrations.

Importing this module does not create a database. Construct :class:`Database`
when database access is explicitly needed; the application is not wired to it
in this step.
"""

from __future__ import annotations

import os
import shutil
import sqlite3
from pathlib import Path

MIGRATIONS_DIR = Path(__file__).with_name("migrations")


def default_database_path() -> Path:
    configured = os.environ.get("ORBIT_DB_PATH")
    if configured:
        return Path(configured).expanduser().resolve()
    return (Path.home() / ".orbit" / "orbit.sqlite3").resolve()


class Database:
    """SQLite connection factory with automatic, backed-up migrations."""

    def __init__(self, path: str | os.PathLike[str] | None = None) -> None:
        self.path = Path(path).expanduser().resolve() if path is not None else default_database_path()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._backup_before_migration()
        self.migrate()

    def _backup_before_migration(self) -> None:
        # Keep a byte-for-byte backup alongside an existing database before
        # touching its schema. Never create a meaningless backup of a new DB.
        if self.path.is_file() and self.path.stat().st_size:
            backup = self.path.with_name(self.path.name + ".pre-migration.bak")
            shutil.copy2(self.path, backup)

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    def migrate(self) -> None:
        with self.connect() as connection:
            connection.execute(
                "CREATE TABLE IF NOT EXISTS migrations "
                "(version INTEGER PRIMARY KEY NOT NULL, name TEXT NOT NULL, "
                "applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)"
            )
            applied = {
                row[0]
                for row in connection.execute("SELECT version FROM migrations").fetchall()
            }
            for migration in sorted(MIGRATIONS_DIR.glob("[0-9]*_*.sql")):
                prefix = migration.name.split("_", 1)[0]
                version = int(prefix)
                if version in applied:
                    continue
                # Scripts are trusted, checked-in DDL. Execute each as one
                # transaction, and record its version only after it succeeds.
                connection.execute("BEGIN IMMEDIATE")
                try:
                    statements = migration.read_text(encoding="utf-8").split(";")
                    for statement in statements:
                        if statement.strip():
                            connection.execute(statement)
                    connection.execute(
                        "INSERT INTO migrations(version, name) VALUES (?, ?)",
                        (version, migration.name),
                    )
                    connection.commit()
                except Exception:
                    connection.rollback()
                    raise

    @property
    def current_version(self) -> int:
        with self.connect() as connection:
            row = connection.execute("SELECT MAX(version) FROM migrations").fetchone()
            return int(row[0] or 0)
