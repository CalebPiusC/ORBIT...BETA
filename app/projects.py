"""Projects: the unit a person manages, imports, and opens a workspace for.

The Projects page answers "what do I have?" from this module's data, never
from template text. Two honesty rules are enforced here, not displayed:

  * Only ``source="new"`` creates a project today. Every other entry point the
    product brief lists (GitHub, GitLab, Bitbucket, a local folder, a ZIP or
    file upload) is offered in the creation dialog as a real, discoverable
    choice but is NOT connected. Asking for one raises ``SourceUnavailable``
    and the route answers with an honest notice — the same rule the sidebar
    connections follow (nothing is connected until the integration is real),
    applied to imports.
  * ``summary`` exists to hold an AI-generated project summary — which means
    it is written only by the Main Agent, once a workspace conversation
    produced one. Until then a project shows the description its owner typed,
    or nothing. No card ever renders an AI-flavoured sentence no model
    produced.

Persistence follows the repo convention: JSON under ``runtime/`` through
``memory.jsonstore.JsonStore`` (atomic write, file lock, a corrupt file raises
instead of silently resetting somebody's projects). ``ProjectStore()`` with no
path is in-memory, which is what ``create_app()`` and tests use, so importing
or testing the app never touches ``runtime/`` — ``run_app`` passes the real
path, the same split the heartbeat service uses.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from memory.jsonstore import JsonStore

# The brief's status filter, verbatim. Keys are stored values; labels are what
# the filter chips and badges render.
ProjectStatus = Literal["live", "building", "archived", "idea"]
STATUS_LABELS: dict[str, str] = {
    "live": "Live",
    "building": "Build in Progress",
    "archived": "Archived",
    "idea": "Saved Ideas",
}
STATUSES: tuple[str, ...] = tuple(STATUS_LABELS)

# Every way the brief says a project can enter ORBIT. Only "new" is buildable
# today; the rest are visible choices that answer honestly when picked.
SOURCE_LABELS: dict[str, str] = {
    "new": "New project",
    "github": "GitHub repository",
    "gitlab": "GitLab repository",
    "bitbucket": "Bitbucket repository",
    "local": "Local folder",
    "zip": "ZIP or files",
}
IMPORT_SOURCES: tuple[str, ...] = ("github", "gitlab", "bitbucket", "local", "zip")
LIVE_SOURCES: frozenset[str] = frozenset({"new"})

MAX_NAME_CHARS = 80
MAX_DESCRIPTION_CHARS = 600

DATA_VERSION = 1


class SourceUnavailable(Exception):
    """Raised when a project is created from an entry point that is not connected."""

    def __init__(self, source: str) -> None:
        self.source = source
        label = SOURCE_LABELS.get(source, source)
        super().__init__(f"{label} import is not connected yet.")


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass
class Project:
    id: str
    user_id: str
    name: str
    description: str
    status: str
    source: str
    languages: list[str] = field(default_factory=list)
    summary: str = ""  # AI-generated; empty until the Main Agent writes one
    created_at: str = ""
    updated_at: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "user_id": self.user_id,
            "name": self.name,
            "description": self.description,
            "status": self.status,
            "source": self.source,
            "languages": list(self.languages),
            "summary": self.summary,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    @staticmethod
    def from_dict(data: dict[str, Any]) -> Project:
        # Tolerate missing optional keys so a future field does not strand the
        # projects somebody already has on disk.
        languages = data.get("languages") or []
        return Project(
            id=str(data["id"]),
            user_id=str(data["user_id"]),
            name=str(data["name"]),
            description=str(data.get("description") or ""),
            status=str(data.get("status") or "building"),
            source=str(data.get("source") or "new"),
            languages=[str(item) for item in languages],
            summary=str(data.get("summary") or ""),
            created_at=str(data.get("created_at") or ""),
            updated_at=str(data.get("updated_at") or ""),
        )

    def card(self) -> dict[str, Any]:
        """What a Projects-page card (and the JSON responses) show."""
        return {
            "id": self.id,
            "name": self.name,
            "description": self.description,
            "status": self.status,
            "status_label": STATUS_LABELS.get(self.status, self.status),
            "source": self.source,
            "source_label": SOURCE_LABELS.get(self.source, self.source),
            "languages": list(self.languages),
            "summary": self.summary,
            "updated_at": self.updated_at,
        }


def _empty() -> dict[str, Any]:
    return {"version": DATA_VERSION, "projects": []}


class ProjectStore:
    """The user's projects, persisted as JSON or held in memory.

    All reads go through ``list_for`` / ``get_for`` with a ``user_id``, same
    scoping seam as ``Store.conversations_for`` / ``tasks_for``: there is one
    user today, and the seam is what keeps it from being a rewrite later.
    """

    def __init__(self, path: str | Path | None = None) -> None:
        self._json: JsonStore | None = None if path is None else JsonStore(path, default=_empty)
        self._memory: dict[str, Any] = _empty()

    @property
    def persistent(self) -> bool:
        """True when a file backs the store, so restarts keep the projects."""
        return self._json is not None

    # ----- raw data access: file-backed when a path was given -----
    def _read(self) -> dict[str, Any]:
        if self._json is None:
            return self._memory
        return self._json.read()

    def _update(self, change):
        if self._json is None:
            result = change(self._memory)
            return result
        return self._json.update(change)

    # ----- queries -----
    def list_for(
        self,
        user_id: str,
        *,
        query: str = "",
        status: str | None = None,
    ) -> list[Project]:
        """The user's projects, most recently updated first.

        ``status`` filters to exactly one of the brief's four buckets (an
        unknown status matches nothing rather than everything — a filter that
        silently stops filtering reads as data loss). ``query`` matches name,
        description, summary and language names, case-insensitively.
        """
        projects = [
            Project.from_dict(raw)
            for raw in self._read().get("projects", [])
            if raw.get("user_id") == user_id
        ]
        if status is not None:
            projects = [p for p in projects if p.status == status]
        needle = " ".join((query or "").split()).lower()
        if needle:
            projects = [
                p
                for p in projects
                if needle in " ".join(
                    [p.name, p.description, p.summary, *p.languages]
                ).lower()
            ]
        projects.sort(key=lambda p: p.updated_at or p.created_at, reverse=True)
        return projects

    def counts_for(self, user_id: str) -> dict[str, int]:
        """Per-status counts for the filter chips, including zeroes."""
        counts = {status: 0 for status in STATUSES}
        for project in self.list_for(user_id):
            if project.status in counts:
                counts[project.status] += 1
        return counts

    def get_for(self, project_id: str, user_id: str) -> Project | None:
        for raw in self._read().get("projects", []):
            if raw.get("id") == project_id and raw.get("user_id") == user_id:
                return Project.from_dict(raw)
        return None

    # ----- mutations -----
    def create(
        self,
        *,
        user_id: str,
        name: str,
        description: str = "",
        source: str = "new",
        status: str = "building",
    ) -> Project:
        """Create a project. Only ``source="new"`` is a working entry point."""
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
            raise ValueError(
                f"keep the description under {MAX_DESCRIPTION_CHARS} characters."
            )
        stamp = _utcnow_iso()
        project = Project(
            id=f"p-{uuid.uuid4().hex[:10]}",
            user_id=user_id,
            name=clean_name,
            description=clean_description,
            status=status,
            source=source,
            created_at=stamp,
            updated_at=stamp,
        )

        def _add(data: dict[str, Any]) -> Project:
            data.setdefault("projects", []).append(project.as_dict())
            return project

        return self._update(_add)

    def _mutate(self, project_id: str, user_id: str, mutate) -> Project | None:
        """Apply ``mutate`` to one stored project dict; touch updated_at."""

        def _apply(data: dict[str, Any]) -> Project | None:
            for raw in data.get("projects", []):
                if raw.get("id") == project_id and raw.get("user_id") == user_id:
                    mutate(raw)
                    raw["updated_at"] = _utcnow_iso()
                    return Project.from_dict(raw)
            return None

        return self._update(_apply)

    def set_status(self, project_id: str, user_id: str, status: str) -> Project | None:
        if status not in STATUS_LABELS:
            raise ValueError(f"unknown project status: {status!r}")
        return self._mutate(project_id, user_id, lambda raw: raw.__setitem__("status", status))

    def set_summary(self, project_id: str, user_id: str, summary: str) -> Project | None:
        """Record the AI-generated summary. Called with model output only —
        this is the seam that keeps "AI-generated" from meaning "we made one
        up at render time".
        """
        text = (summary or "").strip()
        if not text:
            raise ValueError("an empty summary would pretend none exists.")
        return self._mutate(project_id, user_id, lambda raw: raw.__setitem__("summary", text))
