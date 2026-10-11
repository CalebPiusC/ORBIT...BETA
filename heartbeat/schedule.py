"""The heartbeat's clock rules. Pure functions: no files, no network, no threads.

All local-time decisions use Africa/Lagos. Quiet hours run from 22:00 to 08:00
local time. A check is due at most once per local day (the service enforces that).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from config import HEARTBEAT_TIMEZONE

LAGOS = ZoneInfo(HEARTBEAT_TIMEZONE)
QUIET_START_HOUR = 22  # 22:00 local
QUIET_END_HOUR = 8  # 08:00 local


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def local_time(moment: datetime) -> datetime:
    if moment.tzinfo is None:
        raise ValueError("Heartbeat times must be timezone-aware.")
    return moment.astimezone(LAGOS)


def is_quiet_hours(local: datetime) -> bool:
    """True from 22:00 up to, but not including, 08:00 local time."""
    return local.hour >= QUIET_START_HOUR or local.hour < QUIET_END_HOUR


def plan_check_due(local: datetime, has_plan_today: bool, plan_hour: int) -> bool:
    """The daily-plan check is due once the configured hour has started and today has no plan."""
    return local.hour >= plan_hour and not has_plan_today


def checkin_gap_due(now: datetime, last_checkin: datetime, gap_days: int) -> bool:
    return now - last_checkin >= timedelta(days=gap_days)


def whole_days_between(now: datetime, earlier: datetime) -> int:
    return int((now - earlier).total_seconds() // 86400)
