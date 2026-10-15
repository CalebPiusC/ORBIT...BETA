"""Projects, and the dashboard state a person's projects put them in.

Two ideas do the work here.

**A project is a place to think, not a git clone.** What ORBIT keeps is the
project's identity and the *listing* of what came in — names, sizes, kinds. File
contents are not copied into state by any import path in this file, and every
import screen says so out loud. That is the honest version of "import your code"
for a beta, and it is also the safer one.

**There is one dashboard, and it adapts.** ``dashboard_state()`` reads an account
and returns one of three shapes — ``new``, ``projects`` or ``returning`` — and the
template renders whatever that shape actually contains. The states share a
skeleton and differ in what they lead with, because a person on day one and a
person on day thirty want opposite things from the same screen: day one wants a
short list of true first steps, day thirty wants to be back where they left off
within one click.

Imports are deliberately *just* a listing. Nothing here claims to have read code,
indexed a repository, or uploaded anything to a model, because it hasn't.
"""

from __future__ import annotations

import io
import json
import os
import re
import threading
import uuid
import zipfile
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from onboarding import content

MAX_LISTED_FILES = 400
MAX_ARCHIVE_BYTES = 20 * 1024 * 1024
MAX_ARCHIVE_ENTRIES = 4000
SKIP_DIRECTORIES = {
    ".git", "node_modules", "__pycache__", ".venv", "venv", "dist", "build",
    ".next", ".mypy_cache", ".pytest_cache", "target", ".idea", ".vscode",
}


def _is_secret_file(name: str) -> bool:
    """Credential files are never listed, in an archive or a folder.

    Nothing is uploaded either way, so this is not a security boundary — it is
    ORBIT refusing to print the name of a file whose contents are somebody's key.
    """
    lowered = name.lower()
    return lowered == ".env" or lowered.startswith(".env.") or lowered in {
        "id_rsa", "id_ed25519", ".npmrc", ".netrc", "credentials.json"
    }


def _is_skipped(part: str) -> bool:
    return part in SKIP_DIRECTORIES or _is_secret_file(part)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(moment: datetime | None = None) -> str:
    return (moment or _now()).isoformat(timespec="seconds")


def humanize_when(raw: str) -> str:
    """“just now” / “2 hours ago” / “12 Oct”. Honest about which it is."""
    try:
        moment = datetime.fromisoformat(raw)
    except (TypeError, ValueError):
        return "recently"
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    delta = _now() - moment
    seconds = delta.total_seconds()
    if seconds < 90:
        return "just now"
    if seconds < 3600:
        return f"{int(seconds // 60)} min ago"
    if seconds < 86400:
        hours = int(seconds // 3600)
        return f"{hours} hour{'s' if hours != 1 else ''} ago"
    if seconds < 86400 * 7:
        days = int(seconds // 86400)
        return f"{days} day{'s' if days != 1 else ''} ago"
    return moment.strftime("%d %b")


def describe_files(files: list[dict[str, Any]]) -> str:
    if not files:
        return "No files listed yet"
    count = len(files)
    return f"{count} file{'s' if count != 1 else ''} listed"


@dataclass
class Project:
    id: str
    account_id: str
    name: str
    kind: str  # orbit | github | gitlab | bitbucket | folder | zip | files | idea
    created_at: str = field(default_factory=_iso)
    last_opened: str = field(default_factory=_iso)
    description: str = ""
    idea: str = ""
    primary_language: str = ""
    template: str = ""
    options: list[str] = field(default_factory=list)
    files: list[dict[str, Any]] = field(default_factory=list)
    plan: dict[str, Any] = field(default_factory=dict)
    remote: dict[str, Any] = field(default_factory=dict)
    opened_count: int = 0
    archived: bool = False
    notes: str = ""

    # ----- derived, for templates -----
    @property
    def kind_label(self) -> str:
        return content.PROJECT_KIND_LABELS.get(self.kind, ("Project", self.kind))[0]

    @property
    def kind_slug(self) -> str:
        return content.PROJECT_KIND_LABELS.get(self.kind, ("Project", "orbit"))[1]

    @property
    def is_imported(self) -> bool:
        return self.kind in ("github", "gitlab", "bitbucket", "folder", "zip", "files")

    @property
    def origin_line(self) -> str:
        if self.kind == "github" and self.remote.get("repo"):
            return f"{self.remote.get('owner', '')}/{self.remote['repo']}".strip("/")
        if self.remote.get("path"):
            return str(self.remote["path"])
        if self.remote.get("archive"):
            return str(self.remote["archive"])
        return self.kind_label

    @property
    def file_summary(self) -> str:
        return describe_files(self.files)

    @property
    def last_opened_human(self) -> str:
        return humanize_when(self.last_opened)

    @property
    def created_human(self) -> str:
        return humanize_when(self.created_at)

    @property
    def top_level(self) -> list[str]:
        """Root-level names only, for a readable project card."""
        names: list[str] = []
        for item in self.files:
            path = str(item.get("path", ""))
            if not path:
                continue
            head = path.split("/")[0]
            if head not in names:
                names.append(head)
        return names[:6]

    def as_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload.update(
            {
                "kind_label": self.kind_label,
                "origin_line": self.origin_line,
                "file_summary": self.file_summary,
                "last_opened_human": self.last_opened_human,
            }
        )
        return payload


# --------------------------------------------------------------------------- #
# File listing helpers — every one of them reads names, never contents
# --------------------------------------------------------------------------- #

def list_uploaded_files(storage_files: Any) -> list[dict[str, Any]]:
    """Names and sizes from ``<input type="file">`` uploads, capped and sorted."""
    listed: list[dict[str, Any]] = []
    for item in storage_files or []:
        filename = str(getattr(item, "filename", "") or "").strip()
        if not filename:
            continue
        listed.append({"path": _safe_path(filename), "size": _stream_size(item), "kind": "file"})
    return listed[:MAX_LISTED_FILES]


def _stream_size(storage: Any) -> int:
    try:
        position = storage.stream.tell()
        storage.stream.seek(0, os.SEEK_END)
        size = storage.stream.tell()
        storage.stream.seek(position)
        return int(size)
    except (AttributeError, OSError, ValueError):
        return 0


def _safe_path(raw: str) -> str:
    """A display path with no traversal and no absolute roots."""
    text = str(raw).replace("\\", "/")
    parts = [part for part in text.split("/") if part not in ("", ".", "..")]
    return "/".join(parts)[:240]


def list_zip_entries(payload: bytes) -> tuple[list[dict[str, Any]], str]:
    """Read an archive's *listing*. Returns (files, error_message).

    Nothing is extracted: a name from an archive is untrusted input, and the only
    safe thing to do with it here is display it.
    """
    if not payload:
        return [], "That file is empty. Choose the archive you downloaded from your provider."
    if len(payload) > MAX_ARCHIVE_BYTES:
        return [], f"That archive is larger than {MAX_ARCHIVE_BYTES // (1024 * 1024)} MB. Try a folder or a few files instead."
    try:
        with zipfile.ZipFile(io.BytesIO(payload)) as archive:
            raw_entries = archive.infolist()
    except zipfile.BadZipFile:
        return [], "That doesn't look like a ZIP archive. Compressed .tar.gz files need to be unzipped first."
    except (OSError, ValueError) as error:
        return [], f"That archive could not be read ({type(error).__name__})."

    files: list[dict[str, Any]] = []
    for entry in raw_entries:
        if entry.is_dir():
            continue
        path = _safe_path(entry.filename)
        if not path or any(_is_skipped(part) for part in path.split("/")):
            continue
        files.append({"path": path, "size": int(entry.file_size), "kind": "file"})
        if len(files) >= MAX_LISTED_FILES:
            break

    if not files:
        return [], "That archive has no files to list at the top level. Check it is the right download."
    return sorted(files, key=lambda item: item["path"]), ""


def list_folder_paths(paths: Any) -> list[dict[str, Any]]:
    """Root-relative listing for a folder chosen with ``webkitdirectory``.

    Every path the browser hands over starts with the chosen folder's own name
    ("myapp/src/main.py"). That name is already used as the project name
    (``folder_name_from_paths``), so it is stripped here instead of being printed
    again on every row.
    """
    entries: list[str] = []
    roots: set[str] = set()
    for raw in paths or []:
        clean = _safe_path(str(raw))
        if not clean:
            continue
        head, _, rest = clean.partition("/")
        roots.add(head)
        entries.append(rest or head)
    if len(roots) != 1:
        # More than one top-level folder in the pick, so nothing can be treated
        # as *the* root: show the paths exactly as the browser named them.
        entries = [clean for clean in (_safe_path(str(raw)) for raw in paths or []) if clean]

    files: list[dict[str, Any]] = []
    seen: set[str] = set()
    for path in entries:
        parts = [part for part in path.split("/") if part]
        if not parts or any(_is_skipped(part) for part in parts):
            continue
        listed = "/".join(parts)
        if listed in seen:
            continue
        seen.add(listed)
        files.append({"path": listed, "size": 0, "kind": "file"})
        if len(files) >= MAX_LISTED_FILES:
            break
    return files


def folder_name_from_paths(paths: Any) -> str:
    for raw in paths or []:
        head = _safe_path(str(raw)).split("/")[0]
        if head:
            return head[:60]
    return ""


def detect_language(files: list[dict[str, Any]]) -> str:
    """A guess from extensions, capped at the one we are most sure about."""
    counts: dict[str, int] = {}
    extension_map = {
        ".py": "Python", ".js": "JavaScript", ".jsx": "JavaScript", ".ts": "TypeScript",
        ".tsx": "TypeScript", ".dart": "Dart", ".go": "Go", ".rs": "Rust", ".java": "Java",
        ".kt": "Kotlin", ".swift": "Swift", ".cs": "C#", ".php": "PHP", ".rb": "Ruby",
        ".sql": "SQL", ".html": "HTML & CSS", ".css": "HTML & CSS", ".scss": "HTML & CSS",
        ".vue": "Vue", ".svelte": "Svelte", ".sh": "Shell",
    }
    for item in files:
        path = str(item.get("path", ""))
        suffix = Path(path).suffix.lower()
        label = extension_map.get(suffix)
        if label:
            counts[label] = counts.get(label, 0) + 1
    if not counts:
        return ""
    return max(counts.items(), key=lambda pair: pair[1])[0]


def clean_project_name(raw: str, fallback: str = "") -> str:
    text = re.sub(r"\s+", " ", str(raw or "")).strip()
    text = text.strip("/\\")[:60]
    return text or fallback or "Untitled project"


# --------------------------------------------------------------------------- #
# Project store
# --------------------------------------------------------------------------- #

@dataclass
class DashboardState:
    """The one dashboard's shape. The template renders exactly what is here."""

    kind: str  # new | projects | returning
    heading: str
    subheading: str
    resume: Project | None = None
    projects: list[Project] = field(default_factory=list)
    recent_conversations: list[Any] = field(default_factory=list)
    recommendations: list[str] = field(default_factory=list)
    getting_started: list[dict[str, Any]] = field(default_factory=list)
    show_getting_started: bool = False
    entry_points: list[dict[str, Any]] = field(default_factory=list)
    hint: dict[str, str] | None = None
    has_provider: bool = False


class ProjectStore:
    """Projects in one JSON document, same atomic-write pattern as accounts."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._lock = threading.RLock()
        self._projects: dict[str, Project] = {}
        self._loaded = False

    def _ensure_loaded(self) -> None:
        if self._loaded:
            return
        with self._lock:
            if self._loaded:
                return
            if self.path.exists():
                try:
                    document = json.loads(self.path.read_text(encoding="utf-8"))
                except (json.JSONDecodeError, OSError):
                    # A corrupt project file must not stop the workspace opening.
                    # Projects are append-only records; losing the index loses no
                    # code, so the honest move is to start a fresh index.
                    self._projects = {}
                    self._loaded = True
                    return
                for raw in document.get("projects", []):
                    known = {name for name in Project.__dataclass_fields__}
                    self._projects[raw["id"]] = Project(**{k: v for k, v in raw.items() if k in known})
            self._loaded = True

    def _save(self) -> None:
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            payload = {"version": 1, "projects": [asdict(project) for project in self._projects.values()]}
            temporary = self.path.with_suffix(self.path.suffix + ".tmp")
            temporary.write_text(json.dumps(payload, indent=2), encoding="utf-8")
            os.replace(temporary, self.path)

    # ----- reads -----
    def for_account(self, account_id: str, *, include_archived: bool = False) -> list[Project]:
        self._ensure_loaded()
        items = [
            project
            for project in self._projects.values()
            if project.account_id == account_id and (include_archived or not project.archived)
        ]
        return sorted(items, key=lambda project: project.last_opened, reverse=True)

    def get(self, project_id: str) -> Project | None:
        self._ensure_loaded()
        return self._projects.get(project_id)

    def owned_by(self, project_id: str, account_id: str) -> Project | None:
        project = self.get(project_id)
        return project if project is not None and project.account_id == account_id else None

    def latest_for_account(self, account_id: str) -> Project | None:
        items = self.for_account(account_id)
        return items[0] if items else None

    def count_for_account(self, account_id: str) -> int:
        return len(self.for_account(account_id))

    # ----- writes -----
    def create(
        self,
        *,
        account_id: str,
        name: str,
        kind: str,
        description: str = "",
        idea: str = "",
        primary_language: str = "",
        template: str = "",
        options: list[str] | None = None,
        files: list[dict[str, Any]] | None = None,
        plan: dict[str, Any] | None = None,
        remote: dict[str, Any] | None = None,
    ) -> Project:
        self._ensure_loaded()
        project = Project(
            id=f"p-{uuid.uuid4().hex[:10]}",
            account_id=account_id,
            name=clean_project_name(name),
            kind=kind,
            description=description.strip()[:280],
            idea=idea.strip()[:4000],
            primary_language=primary_language.strip()[:40],
            template=template.strip()[:40],
            options=list(options or []),
            files=list(files or []),
            plan=dict(plan or {}),
            remote=dict(remote or {}),
        )
        with self._lock:
            self._projects[project.id] = project
            self._save()
        return project

    def touch(self, project: Project) -> Project:
        project.last_opened = _iso()
        project.opened_count += 1
        with self._lock:
            self._projects[project.id] = project
            self._save()
        return project

    def update(self, project: Project, **changes: Any) -> Project:
        known = {name for name in Project.__dataclass_fields__}
        for key, value in changes.items():
            if key in known:
                setattr(project, key, value)
        with self._lock:
            self._projects[project.id] = project
            self._save()
        return project

    def delete(self, project: Project) -> None:
        with self._lock:
            self._projects.pop(project.id, None)
            self._save()

    # ----- guided setup -----
    def guided_plan(self, *, template: str, language: str, options: list[str], name: str) -> dict[str, Any]:
        """The file plan a guided setup produces.

        This is a plan, not a repository: ORBIT lists the files it *would* create
        and waits for the person to ask for them. Nothing here claims a folder
        exists on anyone's machine.
        """
        language = (language or "").strip() or "Plain text"
        entry = {
            "web": ("index.html", "src/app.js", "src/styles.css"),
            "api": ("src/server.py", "src/routes.py", "tests/test_routes.py"),
            "cli": ("src/main.py", "src/commands.py", "tests/test_commands.py"),
            "library": ("src/__init__.py", "src/core.py", "tests/test_core.py"),
            "data": ("analysis/load.py", "analysis/report.py", "data/.gitkeep"),
            "other": ("README.md", "src/main.py"),
        }.get(template, ("README.md", "src/main.py"))

        planned = [
            {
                "path": "README.md",
                "why": f"Says what {name} is, how to run it, and what is not built yet.",
                "kind": "doc",
            }
        ]
        planned += [{"path": path, "why": f"Starting point for a {template or 'small'} project in {language}.", "kind": "code"} for path in entry]
        if "tests" in options:
            planned.append({"path": "tests/test_smoke.py", "why": "One failing test, so 'make it pass' is the first real task.", "kind": "test"})
        if "env" in options:
            planned.append({"path": ".env.example", "why": "The settings this project needs, with placeholders — never real values.", "kind": "config"})
        if "ci" in options:
            planned.append({"path": ".github/workflows/tests.yml", "why": "Runs the tests on every push.", "kind": "ci"})
        if "gitignore" in options:
            planned.append({"path": ".gitignore", "why": f"The usual {language} ignores: caches, virtualenvs, secrets.", "kind": "config"})
        return {
            "generated_by": "local planner",
            "template": template,
            "language": language,
            "files": planned,
            "status": "planned",
            "note": "Planned, not created. Ask ORBIT in Chat mode to write any of these.",
        }

    # ----- idea planning -----
    def local_idea_plan(self, idea: str, *, experience: str = "", languages: list[str] | None = None) -> dict[str, Any]:
        """A deterministic outline used when no model is reachable.

        It is labeled ``local planner`` everywhere it appears. A plan that looks
        like the model's work but isn't is exactly the kind of quiet lie this
        project keeps having to remove.
        """
        text = re.sub(r"\s+", " ", idea or "").strip()
        words = [word.strip(".,:;!?()") for word in text.split(" ") if len(word) > 3]
        subject = " ".join(words[:9]) or "the thing you described"
        stack = ", ".join((languages or [])[:3]) or "the stack you prefer"
        return {
            "generated_by": "local planner",
            "headline": f"A first pass at {subject}",
            "summary": (
                "This outline was produced by ORBIT's local planner, not by a model — "
                "no model was reachable when you asked. It is a checklist to react to."
            ),
            "goals": [
                "Decide what the smallest useful version does for one person.",
                "Get that version running end to end before adding a second feature.",
                "Write down the one thing that must not break.",
            ],
            "milestones": [
                {"name": "Shape", "detail": "Two or three sentences on who it is for and what it does first."},
                {"name": "Spine", "detail": f"The thinnest path through {stack}: input, work, output."},
                {"name": "Guardrails", "detail": "One test that fails if the core path breaks."},
                {"name": "Second step", "detail": "The next feature, chosen after using the first."},
            ],
            "first_tasks": [
                "Write the one-sentence description of the smallest version.",
                "List the three things it definitely needs.",
                "Name the two things you will not build yet.",
                "Set up the project skeleton and run it once.",
            ],
            "risks": [
                "Scope: the interesting version is usually three times too big.",
                "Unstated assumptions about who uses it.",
            ],
        }

    # ----- import sources -----
    def import_github(
        self,
        *,
        account_id: str,
        repo: str,
        owner: str = "",
        default_branch: str = "main",
        tree: list[dict[str, Any]] | None = None,
        access_mode: str = "token",
    ) -> Project:
        return self.create(
            account_id=account_id,
            name=repo,
            kind="github",
            description=f"Imported from GitHub ({owner + '/' if owner else ''}{repo}) — file listing only.",
            files=tree or [],
            primary_language=detect_language(tree or []),
            remote={
                "repo": repo,
                "owner": owner,
                "branch": default_branch,
                "access_mode": access_mode,
                "imported_at": _iso(),
                "contents_copied": False,
            },
        )

    def import_archive(self, *, account_id: str, archive_name: str, files: list[dict[str, Any]]) -> Project:
        return self.create(
            account_id=account_id,
            name=Path(archive_name).stem or "Imported archive",
            kind="zip",
            description="ZIP archive — the file listing was read; the archive itself was not kept.",
            files=files,
            primary_language=detect_language(files),
            remote={"archive": archive_name, "contents_copied": False, "imported_at": _iso()},
        )

    def import_folder(self, *, account_id: str, folder: str, files: list[dict[str, Any]]) -> Project:
        return self.create(
            account_id=account_id,
            name=folder or "Local folder",
            kind="folder",
            description="Local folder — names were read in your browser. Nothing was uploaded.",
            files=files,
            primary_language=detect_language(files),
            remote={"path": folder, "contents_copied": False, "imported_at": _iso()},
        )

    def import_files(self, *, account_id: str, name: str, files: list[dict[str, Any]]) -> Project:
        return self.create(
            account_id=account_id,
            name=name or "Selected files",
            kind="files",
            description="Individual files — names and sizes only.",
            files=files,
            primary_language=detect_language(files),
            remote={"contents_copied": False, "imported_at": _iso()},
        )


# --------------------------------------------------------------------------- #
# The adaptive dashboard
# --------------------------------------------------------------------------- #

def recommendations_for(preferences: dict[str, Any], *, limit: int = 3) -> list[str]:
    if not preferences.get("use_preferences_for_suggestions", True):
        return []
    lines: list[str] = []
    for value in [*(preferences.get("languages") or []), *(preferences.get("interests") or [])]:
        hint = content.DASHBOARD_RECOMMENDATIONS.get(str(value))
        if hint and hint not in lines:
            lines.append(hint)
    for value in preferences.get("custom_languages") or []:
        lines.append(f"You added “{value}” — ask ORBIT for a starting point in it.")
    if not lines:
        lines.append(content.DEFAULT_RECOMMENDATION)
    return lines[:limit]


def visible_hint(account: Any, where: str) -> dict[str, str] | None:
    """The one contextual hint still allowed to show on this surface."""
    dismissed = set((account.onboarding or {}).get("dismissed_hints", []))
    for hint in content.HINTS:
        if hint["where"] == where and hint["id"] not in dismissed:
            return dict(hint)
    return None


def getting_started_steps(account: Any, *, project_count: int, conversation_count: int, store: Any = None) -> list[dict[str, Any]]:
    """The checklist, with each step's completion read from real state."""
    thread_sent = bool(getattr(store, "home_thread", None)) or conversation_count > 0
    done = {
        "account": True,
        "verify": bool(account.verified),
        "workspace": bool(account.onboarding.get("tour_seen")),
        "project": project_count > 0,
        "first-turn": thread_sent,
    }
    steps: list[dict[str, Any]] = []
    for step in content.GETTING_STARTED_STEPS:
        steps.append(
            {
                "id": step["id"],
                "label": step["label"],
                "href": step["href"],
                "done": bool(done.get(step["id"])),
            }
        )
    return steps


def dashboard_state(
    account: Any,
    *,
    projects: list[Project],
    conversations: list[Any],
    latest_conversation: Any = None,
    store: Any = None,
    github_connected: bool = False,
) -> DashboardState:
    """One dashboard, three shapes, chosen by what the account actually has."""
    preferences = account.preferences or {}
    recommendations = recommendations_for(preferences)
    steps = getting_started_steps(
        account,
        project_count=len(projects),
        conversation_count=len(conversations),
        store=store,
    )
    completed = len([step for step in steps if step["done"]])
    show_getting_started = (
        not account.onboarding.get("getting_started_dismissed") and completed < len(steps)
    )
    entry_points = [point.as_dict() for point in content.PROJECT_ENTRY_POINTS]
    if projects:
        latest = projects[0]
        return DashboardState(
            kind="projects",
            heading=f"Welcome back, {account.first_name}.",
            subheading=(
                f"{len(projects)} project{'s' if len(projects) != 1 else ''} in your workspace. "
                "Pick up where you left off, or start something new."
            ),
            resume=latest,
            projects=projects,
            recent_conversations=list(conversations)[:4],
            recommendations=recommendations,
            getting_started=steps,
            show_getting_started=show_getting_started,
            entry_points=entry_points,
            hint=visible_hint(account, "home"),
            has_provider=github_connected,
        )

    if conversations:
        return DashboardState(
            kind="returning",
            heading=f"Welcome back, {account.first_name}.",
            subheading="Your recent conversations are below — resume one, or turn it into a project.",
            projects=list(projects),
            recent_conversations=list(conversations)[:5],
            recommendations=recommendations,
            getting_started=steps,
            show_getting_started=show_getting_started,
            entry_points=entry_points,
            hint=visible_hint(account, "home"),
            has_provider=github_connected,
        )

    return DashboardState(
        kind="new",
        heading=f"Welcome, {account.first_name}.",
        subheading=(
            "Nothing to set up. Pick a starting point, or just say hello below — "
            "ORBIT will ask for whatever it is missing."
        ),
        projects=[],
        recent_conversations=[],
        recommendations=recommendations,
        getting_started=steps,
        show_getting_started=show_getting_started,
        entry_points=entry_points,
        hint=visible_hint(account, "home"),
        has_provider=github_connected,
    )


# One import, one row in the Projects overview: this layer's richer kinds, in
# the workspace's own vocabulary for where a project came from.
KIND_TO_SOURCE: dict[str, str] = {
    "orbit": "new",
    "idea": "new",
    "github": "github",
    "gitlab": "gitlab",
    "bitbucket": "bitbucket",
    "folder": "local",
    "zip": "zip",
    "files": "zip",
}


def publish_project(app: Any, project: Project) -> None:
    """Give the workspace's project store the project this layer just recorded.

    The onboarding layer reads the import — a repository reference, a ZIP
    listing, a folder walk — and keeps the fuller record. The workspace keeps
    its own store, and that is what the Projects overview, the rail, ``/create``
    and the project page read. Publishing takes the same id into it, so the
    project somebody just imported is openable from the place they will look
    for it, and there is no second list that disagrees with the first.
    """
    store = app.config.get("PROJECTS")
    if store is None:
        return
    status = "archived" if project.archived else ("idea" if project.kind == "idea" else "building")
    store.adopt(
        project_id=project.id,
        user_id=project.account_id,
        name=project.name,
        description=project.description,
        source=KIND_TO_SOURCE.get(project.kind, "new"),
        status=status,
        languages=[project.primary_language] if project.primary_language else [],
        remote=dict(project.remote),
        files=list(project.files),
        notes=project.notes,
    )


class PublishingProjectStore:
    """This layer's project ledger, kept in step with the workspace's own store.

    Every creation that goes through here — a quick project, a guided plan, an
    import from a provider, a folder, a ZIP or a set of files — is published
    into the workspace's store as well. Reads and edits pass straight through.
    """

    CREATING = ("create", "import_github", "import_archive", "import_folder", "import_files")
    REFRESHING = ("update",)

    def __init__(self, inner: ProjectStore, app: Any) -> None:
        self._inner = inner
        self._app = app

    def __getattr__(self, name: str) -> Any:
        attribute = getattr(self._inner, name)
        if not callable(attribute):
            return attribute
        if name in self.CREATING:

            def creating(*args: Any, **kwargs: Any) -> Any:
                result = attribute(*args, **kwargs)
                if isinstance(result, Project):
                    publish_project(self._app, result)
                return result

            return creating
        if name in self.REFRESHING:

            def refreshing(*args: Any, **kwargs: Any) -> Any:
                result = attribute(*args, **kwargs)
                project = result if isinstance(result, Project) else (args[0] if args else None)
                store = self._app.config.get("PROJECTS")
                if store is not None and isinstance(project, Project):
                    # The import screens finish describing a project a moment
                    # after creating it (access granted, what the walk found).
                    store.refresh_import(
                        project.id,
                        project.account_id,
                        remote=dict(project.remote),
                        files=list(project.files),
                        notes=project.notes,
                    )
                return result

            return refreshing
        return attribute
