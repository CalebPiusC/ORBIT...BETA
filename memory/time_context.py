"""Authoritative Lagos-local date/time context for every model turn."""

from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

LAGOS_ZONE = ZoneInfo("Africa/Lagos")


def current_lagos_time(now: datetime | None = None) -> str:
    """Format the current moment in Africa/Lagos, or a supplied aware instant."""
    moment = now if now is not None else datetime.now(timezone.utc)
    if moment.tzinfo is None or moment.utcoffset() is None:
        raise ValueError("now must be timezone-aware")
    local = moment.astimezone(LAGOS_ZONE)
    return (
        f"Current date and time in Africa/Lagos: "
        f"{local:%A, %d %B %Y at %I:%M:%S %p} WAT (UTC+01:00)."
    )


def with_current_time(system_prompt: str, now: datetime | None = None) -> str:
    """Append an authoritative, freshly computed local date/time to a prompt."""
    if not isinstance(system_prompt, str):
        raise TypeError("system_prompt must be a string")
    context = current_lagos_time(now)
    base = system_prompt.rstrip()
    return (
        f"{base}\n\n[Current date/time context]\n{context}\n"
        "Use this as the authoritative current time for time-sensitive questions."
    )
