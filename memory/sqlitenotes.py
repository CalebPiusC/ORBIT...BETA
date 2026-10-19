"""Account-scoped notes in SQLite."""
from __future__ import annotations

import uuid
from app.database import Database


class SQLiteNoteStore:
    def __init__(self, database: Database, account_id: str) -> None:
        self.database = database
        self.account_id = account_id

    def add(self, content: str, created_at: str) -> str:
        if not isinstance(content, str) or not content.strip():
            raise ValueError("content must not be empty")
        if "\n" in content or "\r" in content:
            raise ValueError("content must be a single line")
        note_id = f"note-{uuid.uuid4().hex}"
        with self.database.connect() as db:
            db.execute("INSERT OR IGNORE INTO accounts(id) VALUES (?)", (self.account_id,))
            db.execute(
                "INSERT INTO notes(id, account_id, created_at, content) VALUES (?, ?, ?, ?)",
                (note_id, self.account_id, created_at, content),
            )
        return note_id

    def list(self) -> list[dict[str, str]]:
        with self.database.connect() as db:
            rows = db.execute(
                "SELECT id, created_at, content FROM notes WHERE account_id = ? ORDER BY rowid",
                (self.account_id,),
            ).fetchall()
        return [dict(id=row[0], created_at=row[1], content=row[2]) for row in rows]
