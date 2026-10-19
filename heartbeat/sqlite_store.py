"""Per-account heartbeat state backed by the shared SQLite database."""
from __future__ import annotations

import contextvars
import hashlib
import json
import threading
from typing import Any, Callable

from app.database import Database
from heartbeat.service import Heartbeat, _empty_state
from memory.sqliteplans import SQLitePlanStore
from tools.registry import AuditLog


class _AccountHeartbeatState:
    def __init__(self, database: Database, account_id: str) -> None:
        self.database = database
        self.account_id = account_id
        self.row_id = "hb-state-" + hashlib.sha256(account_id.encode()).hexdigest()
        self._lock = threading.RLock()

    def read(self) -> dict[str, Any]:
        with self.database.connect() as db:
            row = db.execute(
                "SELECT payload FROM heartbeat_reminders WHERE account_id = ? AND id = ?",
                (self.account_id, self.row_id),
            ).fetchone()
        return json.loads(row[0]) if row is not None else _empty_state()

    def update(self, change: Callable[[dict[str, Any]], Any]) -> Any:
        with self._lock:
            state = self.read()
            result = change(state)
            payload = json.dumps(state, ensure_ascii=False, separators=(",", ":"))
            with self.database.connect() as db:
                db.execute("INSERT OR IGNORE INTO accounts(id) VALUES (?)", (self.account_id,))
                db.execute(
                    "INSERT INTO heartbeat_reminders(id, account_id, payload) VALUES (?, ?, ?) "
                    "ON CONFLICT(id) DO UPDATE SET payload = excluded.payload "
                    "WHERE heartbeat_reminders.account_id = excluded.account_id",
                    (self.row_id, self.account_id, payload),
                )
            return result


class SQLiteHeartbeatManager:
    """Factory and scheduler target for account-bound Heartbeat instances."""

    def __init__(self, *, settings: Any, database: Database) -> None:
        self.settings = settings
        self.database = database
        self._instances: dict[str, Heartbeat] = {}
        self._lock = threading.Lock()
        self._context_account: contextvars.ContextVar[str | None] = contextvars.ContextVar(
            "orbit_heartbeat_account", default=None
        )

    def account_context(self, account_id: str):
        manager = self
        class _Binding:
            def __enter__(self):
                self.token = manager._context_account.set(account_id)
                return self

            def __exit__(self, *_exc):
                manager._context_account.reset(self.token)
        return _Binding()

    def _current(self) -> Heartbeat:
        account_id = self._context_account.get()
        if not account_id:
            raise ValueError("heartbeat tool operation needs an account context")
        return self.for_account(account_id)

    def set_paused(self, paused: bool, *, source: str) -> bool:
        return self._current().set_paused(paused, source=source)

    def add(self, text, on, saved_at: str) -> None:
        self._plans_for_current().add(text, on, saved_at)

    def for_date(self, on):
        return self._plans_for_current().for_date(on)

    def has_plan(self, on) -> bool:
        return self._plans_for_current().has_plan(on)

    def _plans_for_current(self):
        account_id = self._context_account.get()
        if not account_id:
            raise ValueError("plan tool operation needs an account context")
        return SQLitePlanStore(self.database, account_id)

    def for_account(self, account_id: str) -> Heartbeat:
        if not isinstance(account_id, str) or not account_id.strip():
            raise ValueError("account_id is required for heartbeat access")
        with self._lock:
            heartbeat = self._instances.get(account_id)
            if heartbeat is None:
                audit = AuditLog(database=self.database, account_id=account_id)
                heartbeat = Heartbeat(
                    settings=self.settings,
                    state_store=_AccountHeartbeatState(self.database, account_id),
                    plans=SQLitePlanStore(self.database, account_id),
                    audit=audit,
                )
                self._instances[account_id] = heartbeat
            return heartbeat

    def tick(self) -> list[dict[str, Any]]:
        """Tick each registered account; state is fetched independently."""
        with self.database.connect() as db:
            account_ids = [
                row[0] for row in db.execute("SELECT id FROM accounts WHERE is_active = 1 ORDER BY id").fetchall()
            ]
        fired: list[dict[str, Any]] = []
        for account_id in account_ids:
            fired.extend(self.for_account(account_id).tick())
        return fired
