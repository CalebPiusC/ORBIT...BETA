"""SQLite database bootstrap and ordered schema migrations.

Importing this module does not create a database. Production launchers construct
:class:`Database` during startup; in-memory app factories do not.
"""

from __future__ import annotations

import os
import shutil
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

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
        self._live: set[sqlite3.Connection] = set()
        self._live_lock = threading.Lock()
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            self.path.parent.chmod(0o700)
        except OSError:
            pass
        self._backup_before_migration()
        self.migrate()
        self._secure_file(self.path)
        with self.connect() as connection:
            connection.execute("PRAGMA journal_mode = WAL")

    @staticmethod
    def _secure_file(path: Path) -> None:
        try:
            path.chmod(0o600)
        except OSError:
            pass

    def _backup_before_migration(self) -> None:
        # Keep a byte-for-byte backup alongside an existing database before
        # touching its schema. Never create a meaningless backup of a new DB.
        if self.path.is_file() and self.path.stat().st_size:
            backup = self.path.with_name(self.path.name + ".pre-migration.bak")
            shutil.copy2(self.path, backup)
            self._secure_file(backup)

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        """Open a connection for one block, then close it.

        ``with sqlite3.connect(...) as connection`` closes nothing: it only
        commits or rolls back on the way out, and the file handle stays alive
        until garbage collection. Every store here opens a connection per
        operation, so that pattern leaked one handle per call. On Windows a
        live handle locks the database file, which is why a temporary
        directory holding it failed to delete after the dashboard tests.

        So: a fresh connection per caller (never shared across request
        threads), committed on success, rolled back on error, and always
        closed when the block ends.
        """
        connection = sqlite3.connect(self.path, timeout=10.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 10000")
        with self._live_lock:
            self._live.add(connection)
        try:
            yield connection
            connection.commit()
        except BaseException:
            try:
                connection.rollback()
            except sqlite3.Error:
                pass
            raise
        finally:
            with self._live_lock:
                self._live.discard(connection)
            connection.close()

    @property
    def open_connection_count(self) -> int:
        """Connections this database has open right now; zero between operations."""
        with self._live_lock:
            return len(self._live)

    def close(self) -> None:
        """Close any connection still open. A no-op between operations.

        The per-operation context manager already closes everything; this is
        the belt-and-braces exit for a shutdown hook, and it lets a test prove
        the file is releasable before deleting it on Windows.
        """
        with self._live_lock:
            live = list(self._live)
            self._live.clear()
        for connection in live:
            try:
                connection.close()
            except sqlite3.Error:
                pass

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
                    pending = ""
                    for line in migration.read_text(encoding="utf-8").splitlines(True):
                        pending += line
                        if sqlite3.complete_statement(pending):
                            if pending.strip():
                                connection.execute(pending)
                            pending = ""
                    if pending.strip():
                        raise RuntimeError(f"Incomplete SQL in migration {migration.name}")
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
