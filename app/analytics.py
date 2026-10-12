"""The Analytics view: what ``audit.log`` already recorded, read back.

Everything here comes from the audit log — no separate counter, no new store.
The log is JSON lines written by ``tools.registry.AuditLog`` and the heartbeat
service, so this module is a reader: it groups the last week of events into a
few plain counts and one descriptive sentence.

The framing is deliberately neutral. These are counts of what happened, not
scores: no streaks, no comparisons between days, no targets and no advice. A
quiet day is a quiet day.
"""

from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from providers.personas import model_persona_map, persona_for_model

# The window the view reports on. Seven days including today.
WINDOW_DAYS = 7
# Labels for the two ratings a message action can record.
RATING_LABELS = {"up": "Thumbs up", "down": "Thumbs down"}


@dataclass(frozen=True, slots=True)
class DayCount:
    """One day in the window, and how many messages were sent on it."""

    date: date
    label: str
    weekday: str
    count: int
    is_today: bool
    peak_count: int = 0

    @property
    def bar_percent(self) -> int:
        """Bar width relative to the busiest day in the window, as a reading aid."""
        if self.peak_count <= 0:
            return 0
        return max(4, round(self.count * 100 / self.peak_count)) if self.count else 0


@dataclass(frozen=True, slots=True)
class AnalyticsReport:
    """Everything the Analytics view renders, all of it derived from the log."""

    window_days: int
    log_path: str
    log_present: bool
    events_read: int
    messages_total: int
    messages_today: int
    messages_per_day: list[DayCount] = field(default_factory=list)
    models: list[tuple[str, str, int]] = field(default_factory=list)  # (model id, name, count)
    heartbeat_fired: int = 0
    heartbeat_dismissed: int = 0
    checkins: int = 0
    ratings_up: int = 0
    ratings_down: int = 0
    summary: str = ""
    notes: list[str] = field(default_factory=list)

    @property
    def has_data(self) -> bool:
        return bool(self.events_read)

    def as_dict(self) -> dict[str, Any]:
        return {
            "window_days": self.window_days,
            "log_path": self.log_path,
            "log_present": self.log_present,
            "events_read": self.events_read,
            "messages_total": self.messages_total,
            "messages_today": self.messages_today,
            "messages_per_day": [
                {"date": day.date.isoformat(), "count": day.count} for day in self.messages_per_day
            ],
            "models": [
                {"model": model, "name": name, "count": count}
                for model, name, count in self.models
            ],
            "heartbeat_fired": self.heartbeat_fired,
            "heartbeat_dismissed": self.heartbeat_dismissed,
            "checkins": self.checkins,
            "ratings": {"up": self.ratings_up, "down": self.ratings_down},
            "summary": self.summary,
            "notes": list(self.notes),
        }


def _parse_timestamp(raw: Any) -> datetime | None:
    """Read an audit line's timestamp. Naive values are treated as UTC."""
    if not isinstance(raw, str) or not raw:
        return None
    text = raw.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        moment = datetime.fromisoformat(text)
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment


def _read_events(path: Path, since: datetime) -> tuple[list[dict[str, Any]], int, bool]:
    """Return (events in the window, lines read, whether the file exists).

    A torn or hand-edited line is skipped rather than crashing the view: this is
    a reader of an append-only file that another process may be writing.
    """
    if not path.exists():
        return [], 0, False
    events: list[dict[str, Any]] = []
    read = 0
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict):
            continue
        read += 1
        moment = _parse_timestamp(event.get("timestamp"))
        if moment is None or moment >= since:
            events.append(event)
    return events, read, True


def _day_label(day: date, today: date) -> str:
    if day == today:
        return "Today"
    if day == today - timedelta(days=1):
        return "Yesterday"
    return day.strftime("%a")


def _summarize(
    *, messages_today: int, messages_total: int, top_model: str | None, top_model_count: int
) -> str:
    """One neutral, descriptive sentence about the window.

    Descriptive only: it says what the counts are. It never judges them.
    """
    if not messages_total:
        return "No messages in the last seven days."
    today = f"{messages_today} message{'s' if messages_today != 1 else ''} today"
    total = f"{messages_total} in the last {WINDOW_DAYS} days"
    if top_model and top_model_count:
        share = f", mostly {top_model} ({top_model_count} of {messages_total})"
        return f"{today}; {total}{share}."
    return f"{today}; {total}."


def build_analytics(
    audit_path: str | Path,
    *,
    now: datetime | None = None,
    window_days: int = WINDOW_DAYS,
) -> AnalyticsReport:
    """Read the audit log and return the counts the Analytics view shows."""
    path = Path(audit_path)
    moment = now or datetime.now(timezone.utc)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    start_day = moment.date() - timedelta(days=window_days - 1)
    since = datetime.combine(start_day, datetime.min.time(), tzinfo=timezone.utc)
    events, lines_read, present = _read_events(path, since)

    messages_by_day: Counter[str] = Counter()
    models: Counter[tuple[str, str | None]] = Counter()
    ratings: Counter[str] = Counter()
    fired = dismissed = checkins = 0

    for event in events:
        name = event.get("event")
        stamp = _parse_timestamp(event.get("timestamp"))
        day_key = stamp.date().isoformat() if stamp else None
        if name == "chat_message_received":
            if day_key:
                messages_by_day[day_key] += 1
        elif name == "model_answered":
            model = event.get("model")
            persona = event.get("persona")
            if isinstance(model, str) and model:
                explicit_persona = persona if isinstance(persona, str) and persona else None
                models[(model, explicit_persona)] += 1
        elif name == "message_rated":
            rating = event.get("rating")
            if rating in RATING_LABELS:
                ratings[rating] += 1
        elif name == "heartbeat_notice_fired":
            fired += 1
        elif name == "heartbeat_notice_dismissed":
            dismissed += 1
        elif name == "heartbeat_checkin_recorded":
            checkins += 1

    days: list[DayCount] = []
    peak = max(messages_by_day.values(), default=0)
    for offset in range(window_days):
        day = start_day + timedelta(days=offset)
        days.append(
            DayCount(
                date=day,
                label=_day_label(day, moment.date()),
                weekday=day.strftime("%a"),
                count=messages_by_day.get(day.isoformat(), 0),
                is_today=day == moment.date(),
                peak_count=peak,
            )
        )

    names = model_persona_map()
    ordered_models = sorted(
        models.items(),
        key=lambda item: (-item[1], item[0][0], item[0][1] or ""),
    )
    model_rows = [
        (model, persona or persona_for_model(model, names), count)
        for (model, persona), count in ordered_models
    ]

    messages_total = sum(messages_by_day.values())
    messages_today = messages_by_day.get(moment.date().isoformat(), 0)
    top_model = model_rows[0][1] if model_rows else None
    top_count = model_rows[0][2] if model_rows else 0

    notes: list[str] = []
    if not present:
        notes.append(
            "No audit.log yet, so there is nothing to count. It is written on the first "
            "message, tool call, rating or reminder."
        )
    elif not events:
        notes.append("audit.log has no events in this window yet.")

    return AnalyticsReport(
        window_days=window_days,
        log_path=str(path),
        log_present=present,
        events_read=lines_read,
        messages_total=messages_total,
        messages_today=messages_today,
        messages_per_day=days,
        models=model_rows,
        heartbeat_fired=fired,
        heartbeat_dismissed=dismissed,
        checkins=checkins,
        ratings_up=ratings.get("up", 0),
        ratings_down=ratings.get("down", 0),
        summary=_summarize(
            messages_today=messages_today,
            messages_total=messages_total,
            top_model=top_model,
            top_model_count=top_count,
        ),
        notes=notes,
    )
