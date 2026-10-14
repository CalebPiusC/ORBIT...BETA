"""Local suggestion-capturing tool, registered through the shared gate and audit wrapper.

Like ``write_note``, nothing here runs until the person explicitly approves it.
A declined call writes nothing, and every call (including declines) is audited.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from tools.registry import ToolRegistry

DEFAULT_SUGGESTIONS_PATH = Path(__file__).resolve().parents[1] / "suggestions.txt"


def register_send_suggestion(
    registry: ToolRegistry,
    *,
    suggestions_path: str | Path = DEFAULT_SUGGESTIONS_PATH,
) -> None:
    destination = Path(suggestions_path)

    @registry.register(
        name="send_suggestion",
        description=(
            "Append one user-approved, timestamped suggestion line to the local suggestions.txt. "
            "Use the exact one-line text in content; do not rewrite it."
        ),
        parameters={
            "type": "object",
            "properties": {
                "content": {
                    "type": "string",
                    "description": "The exact one-line suggestion text to append; do not include newlines.",
                }
            },
            "required": ["content"],
        },
    )
    def send_suggestion(content: str) -> str:
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
        destination.parent.mkdir(parents=True, exist_ok=True)
        with destination.open("a", encoding="utf-8", newline="") as suggestion_file:
            suggestion_file.write(f"{timestamp}\t{content}\n")
        return f"Suggestion appended at {timestamp}."
