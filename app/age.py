"""Relative time for card footers and update lines ("Updated 12 min ago").

Worked out from the store's UTC ISO timestamps. Under a week it speaks in
minutes, hours or days; past that it shows the date ("Oct 12"), which stays
true after any length of time. A timestamp we cannot parse renders nothing
rather than a wrong time.
"""

from __future__ import annotations

from datetime import datetime, timezone


def human_age(iso: str, *, now: datetime | None = None) -> str:
    """"12 min ago" / "2 h ago" / "Yesterday" / "3 days ago" / "Oct 12"."""
    try:
        stamp = datetime.fromisoformat(iso)
    except (TypeError, ValueError):
        return ""
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    moment = now or datetime.now(timezone.utc)
    seconds = max(0.0, (moment - stamp).total_seconds())
    if seconds < 60:
        return "just now"
    minutes = int(seconds // 60)
    if minutes < 60:
        return f"{minutes} min ago"
    hours = minutes // 60
    if hours < 24:
        return f"{hours} h ago"
    days = int(seconds // 86400)
    if days <= 1:
        return "Yesterday"
    if days < 7:
        return f"{days} days ago"
    return stamp.strftime("%b %d")
