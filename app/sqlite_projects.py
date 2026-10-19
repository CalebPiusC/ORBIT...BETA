"""SQLite-backed, account-scoped projects."""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from typing import Any

from app.database import Database
from app.projects import (
    AccountProjects,
    LIVE_SOURCES,
    MAX_DESCRIPTION_CHARS,
    MAX_NAME_CHARS,
    Project,
    ProjectStore,
    SourceUnavailable,
    STATUS_LABELS,
    SOURCE_LABELS,
    _utcnow_iso,
)


class SQLiteProjectStore(ProjectStore):
    def __init__(self, database: Database) -> None:
        self.database = database

    @property
    def persistent(self) -> bool:
        return True

    def for_account(self, account_id: str) -> AccountProjects:
        if not isinstance(account_id, str) or not account_id.strip():
            raise ValueError("account_id is required: project access is always scoped to an account.")
        return AccountProjects(self, account_id)

    def list_for(self, user_id: str, *, query: str = "", status: str | None = None) -> list[Project]:
        with self.database.connect() as db:
            rows = db.execute(
                "SELECT payload FROM projects WHERE account_id = ? ORDER BY rowid",
                (user_id,),
            ).fetchall()
        projects = [Project.from_dict(json.loads(row[0])) for row in rows]
        if status is not None:
            projects = [p for p in projects if p.status == status]
        needle = " ".join((query or "").split()).lower()
        if needle:
            projects = [
                p for p in projects
                if needle in " ".join([p.name, p.description, p.summary, *p.languages]).lower()
            ]
        projects.sort(key=lambda p: p.updated_at or p.created_at, reverse=True)
        return projects

    def get_for(self, project_id: str, user_id: str) -> Project | None:
        with self.database.connect() as db:
            row = db.execute(
                "SELECT payload FROM projects WHERE account_id = ? AND id = ?",
                (user_id, project_id),
            ).fetchone()
        return Project.from_dict(json.loads(row[0])) if row else None

    def create(self, *, user_id: str, name: str, description: str = "", source: str = "new", status: str = "building") -> Project:
        if source not in SOURCE_LABELS:
            raise ValueError(f"unknown project source: {source!r}")
        if source not in LIVE_SOURCES:
            raise SourceUnavailable(source)
        if status not in STATUS_LABELS:
            raise ValueError(f"unknown project status: {status!r}")
        clean_name = " ".join((name or "").split())
        if not clean_name:
            raise ValueError("a project needs a name.")
        if len(clean_name) > MAX_NAME_CHARS:
            raise ValueError(f"keep the name under {MAX_NAME_CHARS} characters.")
        clean_description = " ".join((description or "").split())
        if len(clean_description) > MAX_DESCRIPTION_CHARS:
            raise ValueError(f"keep the description under {MAX_DESCRIPTION_CHARS} characters.")
        stamp = _utcnow_iso()
        project = Project(
            id=f"p-{uuid.uuid4().hex[:10]}", user_id=user_id, name=clean_name,
            description=clean_description, status=status, source=source,
            created_at=stamp, updated_at=stamp,
        )
        with self.database.connect() as db:
            db.execute("INSERT OR IGNORE INTO accounts(id) VALUES (?)", (user_id,))
            db.execute(
                "INSERT INTO projects(id, account_id, payload) VALUES (?, ?, ?)",
                (project.id, user_id, json.dumps(project.as_dict(), ensure_ascii=False)),
            )
        return project

    def _mutate(self, project_id: str, user_id: str, mutate) -> Project | None:
        with self.database.connect() as db:
            row = db.execute(
                "SELECT payload FROM projects WHERE account_id = ? AND id = ?",
                (user_id, project_id),
            ).fetchone()
            if row is None:
                return None
            raw = json.loads(row[0])
            mutate(raw)
            raw["updated_at"] = _utcnow_iso()
            db.execute(
                "UPDATE projects SET payload = ? WHERE account_id = ? AND id = ?",
                (json.dumps(raw, ensure_ascii=False), user_id, project_id),
            )
        return Project.from_dict(raw)

    def import_legacy_data(self, projects: list[dict[str, Any]], *, source: str) -> None:
        with self.database.connect() as db:
            if db.execute("SELECT 1 FROM data_imports WHERE source = ?", (source,)).fetchone():
                return
        for raw in projects:
            try:
                project = Project.from_dict(raw)
            except (KeyError, TypeError, ValueError):
                continue
            with self.database.connect() as db:
                db.execute("INSERT OR IGNORE INTO accounts(id) VALUES (?)", (project.user_id,))
                db.execute(
                    "INSERT OR IGNORE INTO projects(id, account_id, payload) VALUES (?, ?, ?)",
                    (project.id, project.user_id, json.dumps(project.as_dict(), ensure_ascii=False)),
                )
        with self.database.connect() as db:
            db.execute("INSERT OR IGNORE INTO data_imports(source) VALUES (?)", (source,))
