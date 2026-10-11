"""Daily plans: one dated line each, kept in runtime/plans.json.

The heartbeat's daily-plan check asks whether today (Africa/Lagos) has an entry.
A plan is one short line of text. Nothing here summarizes or scores plans.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Any

from memory.jsonstore import JsonStore

DEFAULT_PLANS_PATH = Path(__file__).resolve().parents[1] / "runtime" / "plans.json"
MAX_PLAN_CHARS = 500


def validate_plan(text: object) -> str:
    if not isinstance(text, str):
        raise ValueError("A plan must be text.")
    cleaned = " ".join(text.split())
    if not cleaned:
        raise ValueError("A plan must not be empty.")
    if len(cleaned) > MAX_PLAN_CHARS:
        raise ValueError(f"A plan must be at most {MAX_PLAN_CHARS} characters.")
    return cleaned


class PlanStore:
    def __init__(self, path: str | Path = DEFAULT_PLANS_PATH) -> None:
        self._store = JsonStore(path, default=list)

    @property
    def path(self) -> Path:
        return self._store.path

    def add(self, text: str, on: date, saved_at: str) -> None:
        entry = {"date": on.isoformat(), "text": validate_plan(text), "saved_at": saved_at}

        def change(plans: Any) -> None:
            plans.append(entry)

        self._store.update(change)

    def for_date(self, on: date) -> list[dict[str, Any]]:
        return [p for p in self._store.read() if p.get("date") == on.isoformat()]

    def has_plan(self, on: date) -> bool:
        return bool(self.for_date(on))
