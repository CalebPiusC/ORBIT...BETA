"""SQLite persistence adapter for the account-scoped in-memory Store API.

The existing Store remains the default, fast in-memory implementation. This
adapter loads and flushes only the requested account's rows, always using
account_id predicates for owned data.
"""
from __future__ import annotations

import pickle
import threading
import uuid
from typing import Any

from app.database import Database
from app.store import AccountView, Store


class SQLiteStore(Store):
    """Store-compatible SQLite-backed implementation."""

    def __init__(self, database_path: str | None = None) -> None:
        super().__init__()
        self.database = Database(database_path)
        self._loaded_accounts: set[str] = set()
        self._lock = threading.RLock()

    def for_account(self, account_id: str) -> "SQLiteAccountView":
        account_id = self._validate_account(account_id)
        self._load_account(account_id)
        return SQLiteAccountView(self, account_id)

    @staticmethod
    def _validate_account(account_id: str) -> str:
        from app.store import _require_account_id
        return _require_account_id(account_id)

    @staticmethod
    def _dump(value: Any) -> bytes:
        return pickle.dumps(value, protocol=pickle.HIGHEST_PROTOCOL)

    @staticmethod
    def _load(blob: bytes) -> Any:
        return pickle.loads(blob)

    def _load_account(self, account_id: str) -> None:
        if account_id in self._loaded_accounts:
            return
        with self.database.connect() as db:
            db.execute("INSERT OR IGNORE INTO accounts(id) VALUES (?)", (account_id,))
            conv_rows = db.execute(
                "SELECT id, payload FROM conversations WHERE account_id = ? ORDER BY rowid",
                (account_id,),
            ).fetchall()
            task_rows = db.execute(
                "SELECT id, payload FROM tasks WHERE account_id = ? ORDER BY rowid",
                (account_id,),
            ).fetchall()
            message_rows = db.execute(
                "SELECT id, owner_id, conversation_id, task_id, payload "
                "FROM messages WHERE account_id = ? ORDER BY rowid",
                (account_id,),
            ).fetchall()
            ui_row = db.execute(
                "SELECT payload FROM screen_state WHERE account_id = ?", (account_id,)
            ).fetchone()
        for row in conv_rows:
            conv = self._load(row["payload"])
            self.conversations = [item for item in self.conversations if item.id != conv.id]
            self.conversations.append(conv)
            self.threads[conv.id] = []
        for row in task_rows:
            task = self._load(row["payload"])
            task.chat = []
            self.tasks[task.id] = task
        for row in message_rows:
            message = self._load(row["payload"])
            if row["conversation_id"] is not None:
                self.threads.setdefault(row["conversation_id"], []).append(message)
            elif row["task_id"] is not None:
                task = self.tasks.get(row["task_id"])
                if task is not None:
                    task.chat.append(message)
        if ui_row is not None:
            self._ui[account_id] = self._load(ui_row["payload"])
        self._loaded_accounts.add(account_id)
        # Seed the legacy demo task the same way the memory Store does.
        self._flush_account(account_id)

    def _flush_account(self, account_id: str) -> None:
        conversations = [c for c in self.conversations if c.user_id == account_id]
        tasks = [t for t in self.tasks.values() if t.user_id == account_id]
        with self.database.connect() as db:
            db.execute("INSERT OR IGNORE INTO accounts(id) VALUES (?)", (account_id,))
            db.execute("DELETE FROM messages WHERE account_id = ?", (account_id,))
            db.execute("DELETE FROM conversations WHERE account_id = ?", (account_id,))
            db.execute("DELETE FROM tasks WHERE account_id = ?", (account_id,))
            db.execute("DELETE FROM screen_state WHERE account_id = ?", (account_id,))
            for conv in conversations:
                db.execute(
                    "INSERT INTO conversations(id, account_id, payload) VALUES (?, ?, ?)",
                    (conv.id, account_id, self._dump(conv)),
                )
            for task in tasks:
                db.execute(
                    "INSERT INTO tasks(id, account_id, payload) VALUES (?, ?, ?)",
                    (task.id, account_id, self._dump(task)),
                )
            for conv in conversations:
                for message in self.threads.get(conv.id, []):
                    db.execute(
                        "INSERT OR IGNORE INTO messages(id, account_id, owner_id, conversation_id, payload) "
                        "VALUES (?, ?, ?, ?, ?)",
                        (message.id or f"m-{uuid.uuid4().hex}", account_id, conv.id, conv.id, self._dump(message)),
                    )
            for task in tasks:
                for message in task.chat:
                    db.execute(
                        "INSERT OR IGNORE INTO messages(id, account_id, owner_id, task_id, payload) "
                        "VALUES (?, ?, ?, ?, ?)",
                        (message.id or f"m-{uuid.uuid4().hex}", account_id, task.id, task.id, self._dump(message)),
                    )
            db.execute(
                "INSERT INTO screen_state(account_id, payload) VALUES (?, ?) "
                "ON CONFLICT(account_id) DO UPDATE SET payload = excluded.payload",
                (account_id, self._dump(self._ui_for(account_id))),
            )


class SQLiteAccountView:
    """AccountView proxy that flushes mutations at each account operation."""
    def __init__(self, store: SQLiteStore, account_id: str) -> None:
        object.__setattr__(self, "_store", store)
        object.__setattr__(self, "_account_id", account_id)
        object.__setattr__(self, "_view", AccountView(store, account_id))

    def __getattr__(self, name: str) -> Any:
        store = object.__getattribute__(self, "_store")
        account_id = object.__getattribute__(self, "_account_id")
        view = object.__getattribute__(self, "_view")
        value = getattr(view, name)
        if not callable(value):
            # A live list/model may have been mutated since the previous
            # property read; flush before handing it out again.
            with store._lock:
                store._flush_account(account_id)
                return value

        def call(*args: Any, **kwargs: Any) -> Any:
            # Serialize in-process access to shared in-memory objects while
            # using a distinct SQLite connection for every flush.
            with store._lock:
                store._flush_account(account_id)
                result = value(*args, **kwargs)
                store._flush_account(account_id)
                return result
        return call

    def __setattr__(self, name: str, value: Any) -> None:
        view = object.__getattribute__(self, "_view")
        store = object.__getattribute__(self, "_store")
        with store._lock:
            setattr(view, name, value)
            store._flush_account(object.__getattribute__(self, "_account_id"))
