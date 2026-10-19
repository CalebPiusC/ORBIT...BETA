"""Account-scoped daily plan rows in SQLite."""
from __future__ import annotations

import uuid
from datetime import date
from typing import Any

from app.database import Database
from memory.plans import validate_plan


class SQLitePlanStore:
    def __init__(self, database: Database, account_id: str) -> None:
        self.database = database
        self.account_id = account_id

    def add(self, text: str, on: date, saved_at: str) -> None:
        cleaned = validate_plan(text)
        with self.database.connect() as db:
            db.execute("INSERT OR IGNORE INTO accounts(id) VALUES (?)", (self.account_id,))
            db.execute(
                "INSERT INTO plans(id, account_id, plan_date, text, saved_at) VALUES (?, ?, ?, ?, ?)",
                (f"plan-{uuid.uuid4().hex}", self.account_id, on.isoformat(), cleaned, saved_at),
            )

    def for_date(self, on: date) -> list[dict[str, Any]]:
        with self.database.connect() as db:
            rows = db.execute(
                "SELECT plan_date, text, saved_at FROM plans "
                "WHERE account_id = ? AND plan_date = ? ORDER BY rowid",
                (self.account_id, on.isoformat()),
            ).fetchall()
        return [{"date": row[0], "text": row[1], "saved_at": row[2]} for row in rows]

    def has_plan(self, on: date) -> bool:
        with self.database.connect() as db:
            row = db.execute(
                "SELECT 1 FROM plans WHERE account_id = ? AND plan_date = ? LIMIT 1",
                (self.account_id, on.isoformat()),
            ).fetchone()
        return row is not None
