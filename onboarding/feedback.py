"""Beta feedback: suggestions, bug reports, and everything else.

Two rules shape this file.

* **Submitting never costs you your work.** The panel posts in place and the form
  keeps its draft until the server confirms. There is no navigation, no reload,
  and no "are you sure you want to leave this page".
* **Attached context is visible and removable.** What gets sent alongside a
  report is exactly the list the person can see in the form before sending —
  route, project name, viewport, browser, and whether a message was in flight.
  Nothing hidden is added on the server side.

Statuses are honest too: in this build there is no tracker behind the scenes, so
every entry says **Received** and the screen says plainly that status changes are
not automated yet. A fake "In review → Planned → Shipped" ladder would be a lie
with better typography.
"""

from __future__ import annotations

import json
import os
import re
import threading
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from onboarding.projects import humanize_when

KINDS = ("suggestion", "bug", "general")
MAX_BODY = 4000
MAX_SUBJECT = 140


def _html_escape(text: str) -> str:
    return (
        str(text)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


@dataclass
class FeedbackEntry:
    id: str
    account_id: str
    kind: str
    subject: str
    body: str
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat(timespec="seconds"))
    context: dict[str, Any] = field(default_factory=dict)
    status: str = "received"
    reference: str = ""

    @property
    def created_human(self) -> str:
        return humanize_when(self.created_at)

    @property
    def kind_label(self) -> str:
        return {"suggestion": "Feature suggestion", "bug": "Bug report", "general": "General feedback"}.get(
            self.kind, "Feedback"
        )

    @property
    def status_label(self) -> str:
        return {"received": "Received", "archived": "Archived"}.get(self.status, self.status.title())

    @property
    def context_lines(self) -> list[str]:
        labels = {
            "surface": "Screen",
            "route": "Route",
            "project": "Project",
            "viewport": "Window",
            "browser": "Browser",
            "activity": "State",
            "account": "Account",
            "build": "Build",
        }
        lines: list[str] = []
        for key, value in (self.context or {}).items():
            if value in ("", None, [], {}):
                continue
            lines.append(f"{labels.get(key, key.title())}: {value}")
        return lines

    def as_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["created_human"] = self.created_human
        payload["kind_label"] = self.kind_label
        payload["status_label"] = self.status_label
        return payload


class FeedbackStore:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._lock = threading.RLock()
        self._entries: list[FeedbackEntry] = []
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
                    self._entries = []
                    self._loaded = True
                    return
                for raw in document.get("entries", []):
                    known = {name for name in FeedbackEntry.__dataclass_fields__}
                    self._entries.append(FeedbackEntry(**{k: v for k, v in raw.items() if k in known}))
            self._loaded = True

    def _save(self) -> None:
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            payload = {"version": 1, "entries": [asdict(entry) for entry in self._entries]}
            temporary = self.path.with_suffix(self.path.suffix + ".tmp")
            temporary.write_text(json.dumps(payload, indent=2), encoding="utf-8")
            os.replace(temporary, self.path)

    # ----- reads -----
    def for_account(self, account_id: str, *, limit: int = 25) -> list[FeedbackEntry]:
        self._ensure_loaded()
        items = [entry for entry in self._entries if entry.account_id == account_id]
        return sorted(items, key=lambda entry: entry.created_at, reverse=True)[:limit]

    def count_for_account(self, account_id: str) -> int:
        self._ensure_loaded()
        return len([entry for entry in self._entries if entry.account_id == account_id])

    def get(self, entry_id: str, account_id: str) -> FeedbackEntry | None:
        self._ensure_loaded()
        for entry in self._entries:
            if entry.id == entry_id and entry.account_id == account_id:
                return entry
        return None

    # ----- writes -----
    def submit(
        self,
        *,
        account_id: str,
        kind: str,
        body: str,
        subject: str = "",
        context: dict[str, Any] | None = None,
    ) -> tuple[FeedbackEntry | None, list[str]]:
        """Validate, then keep. Returns (entry, problems)."""
        self._ensure_loaded()
        problems: list[str] = []
        if kind not in KINDS:
            problems.append("Choose what kind of feedback this is.")
        text = (body or "").strip()
        if len(text) < 12:
            problems.append("A little more detail, please — twelve characters or so.")
        if len(text) > MAX_BODY:
            problems.append(f"Keep it under {MAX_BODY} characters.")
        heading = (subject or "").strip() or first_line(text)
        if len(heading) > MAX_SUBJECT:
            heading = heading[:MAX_SUBJECT].rstrip() + "…"
        if problems:
            return None, problems

        entry = FeedbackEntry(
            id=f"f-{uuid.uuid4().hex[:10]}",
            account_id=account_id,
            kind=kind,
            subject=heading or "Feedback",
            body=_html_escape(text),
            context={key: value for key, value in (context or {}).items() if value not in ("", None)},
            reference="ORB-" + uuid.uuid4().hex[:6].upper(),
        )
        with self._lock:
            self._entries.append(entry)
            self._save()
        return entry, []


def first_line(text: str) -> str:
    for line in (text or "").splitlines():
        clean = line.strip()
        if clean:
            return re.sub(r"\s+", " ", clean)[:MAX_SUBJECT]
    return ""


def build_context(
    *,
    surface: str,
    route: str,
    account_email: str,
    project_name: str = "",
    user_agent: str = "",
    viewport: str = "",
    activity: str = "",
    build: str = "beta",
) -> dict[str, Any]:
    """The context block offered to the person, in the order they see it."""
    return {
        "surface": surface,
        "route": route,
        "project": project_name,
        "activity": activity,
        "viewport": viewport,
        "browser": _browser_from(user_agent),
        "build": build,
        "account": account_email,
    }


def _browser_from(user_agent: str) -> str:
    ua = (user_agent or "").lower()
    if not ua:
        return ""
    for needle, label in (
        ("edg/", "Edge"), ("opr/", "Opera"), ("chrome/", "Chrome"),
        ("firefox/", "Firefox"), ("safari/", "Safari"),
    ):
        if needle in ua:
            return label
    return "Unknown browser"


CONTEXT_KEYS_ALLOWED = (
    "surface", "route", "project", "activity", "viewport", "browser", "build", "account",
)


def sanitize_context(raw: Any) -> dict[str, Any]:
    """Accept only the keys the form showed, and keep the values short."""
    if not isinstance(raw, dict):
        return {}
    clean: dict[str, Any] = {}
    for key in CONTEXT_KEYS_ALLOWED:
        value = raw.get(key)
        if isinstance(value, str) and value.strip():
            clean[key] = value.strip()[:160]
    return clean
