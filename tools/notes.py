"""The first local note-writing tool, registered through the shared wrapper."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from app.database import Database
from memory.sqlitenotes import SQLiteNoteStore
from tools.registry import ToolRegistry, current_account_id

DEFAULT_NOTES_PATH = Path(__file__).resolve().parents[1] / "notes.txt"


def register_write_note(
    registry: ToolRegistry,
    *,
    notes_path: str | Path = DEFAULT_NOTES_PATH,
    database: Database | None = None,
) -> None:
    destination = Path(notes_path)

    @registry.register(
        name="write_note",
        description=(
            "Append one user-approved, timestamped line to the local notes.txt. "
            "Use the exact one-line text in content; do not rewrite it."
        ),
        parameters={
            "type": "object",
            "properties": {
                "content": {
                    "type": "string",
                    "description": "The exact one-line note text to append; do not include newlines.",
                }
            },
            "required": ["content"],
        },
    )
    def write_note(content: str) -> str:
        if not isinstance(content, str):
            raise TypeError("content must be a string.")
        if not content.strip():
            raise ValueError("content must not be empty.")
        if "\n" in content or "\r" in content:
            raise ValueError("content must be a single line.")

        timestamp = (
            datetime.now(timezone.utc)
            .isoformat(timespec="seconds")
            .replace("+00:00", "Z")
        )
        if database is not None:
            account_id = current_account_id()
            if not account_id:
                raise ValueError("account_id is required to write a note")
            SQLiteNoteStore(database, account_id).add(content, timestamp)
        else:
            destination.parent.mkdir(parents=True, exist_ok=True)
            with destination.open("a", encoding="utf-8", newline="") as note_file:
                note_file.write(f"{timestamp}\t{content}\n")
        return f"Note appended at {timestamp}."
