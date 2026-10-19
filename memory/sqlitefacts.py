"""Account-scoped fact memory in SQLite."""
from __future__ import annotations

import uuid
from typing import Any

from app.database import Database
from memory.facts import FactStore, build_system_prompt, validate_fact
from tools.registry import current_account_id


class SQLiteFactStore(FactStore):
    def __init__(self, database: Database, account_id: str | None = None) -> None:
        self.database = database
        self.account_id = account_id

    @property
    def persistent(self) -> bool:
        return True

    def _owner(self) -> str:
        account_id = self.account_id or current_account_id()
        if not account_id:
            raise ValueError("account_id is required to access facts")
        return account_id

    def load(self) -> list[str]:
        account_id = self._owner()
        with self.database.connect() as db:
            rows = db.execute(
                "SELECT fact FROM facts WHERE account_id = ? ORDER BY rowid",
                (account_id,),
            ).fetchall()
        return [row[0] for row in rows]

    def system_prompt(self, base: str) -> str:
        return build_system_prompt(base, self.load())

    def add(self, fact: str) -> bool:
        cleaned = validate_fact(fact)
        account_id = self._owner()
        with self.database.connect() as db:
            db.execute("INSERT OR IGNORE INTO accounts(id) VALUES (?)", (account_id,))
            cursor = db.execute(
                "INSERT OR IGNORE INTO facts(id, account_id, fact) VALUES (?, ?, ?)",
                (f"fact-{uuid.uuid4().hex}", account_id, cleaned),
            )
        return cursor.rowcount > 0

    def remove(self, fact: str) -> int:
        if not isinstance(fact, str) or not fact.strip():
            from memory.facts import FactError
            raise FactError("fact must be a non-empty string.")
        with self.database.connect() as db:
            cursor = db.execute(
                "DELETE FROM facts WHERE account_id = ? AND fact = ? COLLATE NOCASE",
                (self._owner(), fact.strip()),
            )
        return cursor.rowcount
