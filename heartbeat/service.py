"""The heartbeat service: two local-time checks, quiet hours, pause, notices, push, audit.

Checks (both at most once per local day, both held back in quiet hours):
  * plan_missing: the configured hour has passed and today has no saved plan.
  * checkin_gap:  the last home-chat check-in is at least N days old.

Everything persists in runtime/heartbeat.json, so a restart neither refires a
notice already sent today nor resets the check-in timer. The file is re-read on
every tick, so the web scheduler sees a pause made from the terminal, and the
other way round.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from config import HeartbeatSettings
from heartbeat import ntfy
from heartbeat.schedule import (
    checkin_gap_due,
    is_quiet_hours,
    local_time,
    plan_check_due,
    utc_now,
    whole_days_between,
)
from memory.jsonstore import JsonStore
from memory.plans import PlanStore
from tools.registry import AuditLog

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_HEARTBEAT_PATH = ROOT / "runtime" / "heartbeat.json"

KIND_PLAN = "plan_missing"
KIND_GAP = "checkin_gap"
PUSH_TITLES = {KIND_PLAN: "ORBIT: daily plan", KIND_GAP: "ORBIT: check-in"}

log = logging.getLogger("orbit.heartbeat")


def _iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _parse(text: str) -> datetime:
    return datetime.fromisoformat(text.replace("Z", "+00:00"))


def _notice_local_date(notice: dict[str, Any]) -> str | None:
    """Local-day key a notice was created on, or None if it cannot be parsed."""
    created = notice.get("created_at")
    if not isinstance(created, str):
        return None
    try:
        moment = _parse(created)
    except (TypeError, ValueError):
        return None
    return local_time(moment).date().isoformat()


def _notice_day(notice: dict[str, Any]) -> str | None:
    """The local-day key a notice belongs to, or None when it has no usable stamp."""
    return _notice_local_date(notice)


def _open_notices(notices: list[Any]) -> list[dict[str, Any]]:
    """Notices still shown to the person: not dismissed, not marked as a copy."""
    return [
        notice
        for notice in notices
        if isinstance(notice, dict)
        and notice.get("dismissed_at") is None
        and not notice.get("duplicate_of")
    ]


def _collapse_duplicate_notices(notices: list[Any]) -> int:
    """Mark exact repeats of one notice as copies; never delete a row.

    One condition — "no plan today", "no check-in for N days" — is one notice
    per local day. An earlier build could end up with the same notice stored
    twice (the PR #20 legacy import copied a notice in without its ``fired``
    marker, so the next tick opened a second identical one). The extra rows are
    kept and marked ``duplicate_of``, so nothing is lost and the person sees one
    card. Returns how many rows were marked.
    """
    canonical: dict[tuple[Any, Any, Any], str] = {}
    marked = 0
    for notice in notices:
        if not isinstance(notice, dict) or notice.get("dismissed_at") is not None:
            continue
        key = (notice.get("kind"), _notice_day(notice), notice.get("text"))
        first = canonical.get(key)
        if first is None:
            canonical[key] = str(notice.get("id") or "")
            continue
        if notice.get("duplicate_of"):
            continue
        notice["duplicate_of"] = first
        marked += 1
    return marked


def merge_imported_state(state: dict[str, Any], legacy: dict[str, Any]) -> dict[str, Any]:
    """Fold a pre-SQLite ``heartbeat.json`` into one account's state, once.

    The import used to copy the file over the row wholesale. That carried the
    legacy notices across but left today's ``fired`` markers behind whenever the
    old file had them under a different shape, so the first tick after import
    raised a second, identical notice. Here the notices are merged (one row per
    id), the markers are rebuilt from the notices themselves, and repeats are
    marked rather than removed.
    """
    merged = dict(state)
    for key in ("paused", "last_checkin_at"):
        if legacy.get(key) is not None:
            merged[key] = legacy[key]

    seen: set[str] = set()
    notices: list[Any] = []
    for notice in list(state.get("notices") or []) + list(legacy.get("notices") or []):
        if not isinstance(notice, dict):
            continue
        notice_id = notice.get("id")
        if isinstance(notice_id, str):
            if notice_id in seen:
                continue
            seen.add(notice_id)
        notices.append(dict(notice))
    merged["notices"] = notices

    fired = state.get("fired")
    fired = dict(fired) if isinstance(fired, dict) else {}
    # Every notice that survived the merge proves its condition already fired
    # that day. Keep the later day key; never move a marker back.
    for notice in notices:
        if notice.get("dismissed_at") is not None:
            continue
        kind, day = notice.get("kind"), _notice_day(notice)
        if isinstance(kind, str) and day is not None:
            fired[kind] = max(str(fired.get(kind) or ""), day)
    merged["fired"] = fired
    _collapse_duplicate_notices(notices)
    # Both state stores call ``change(state)`` and persist *that* object, so the
    # merge is applied in place as well as returned.
    state.update(merged)
    return state


def _empty_state() -> dict[str, Any]:
    return {"version": 1, "paused": False, "last_checkin_at": None, "fired": {}, "notices": []}


def _plan_text(plan_hour: int) -> str:
    return (
        f"It is past {plan_hour:02d}:00 and there is no plan saved for today yet. "
        "Tell Orbit your plan in chat to save it."
    )


def _gap_text(days: int) -> str:
    unit = "day" if days == 1 else "days"
    return f"No check-in for {days} {unit}. Send Orbit a message to pick things back up."


class Heartbeat:
    def __init__(
        self,
        *,
        settings: HeartbeatSettings,
        state_path: str | Path = DEFAULT_HEARTBEAT_PATH,
        plans: PlanStore | None = None,
        audit: AuditLog | None = None,
        state_store: Any | None = None,
    ) -> None:
        self.settings = settings
        self._store = state_store if state_store is not None else JsonStore(state_path, _empty_state)
        self._plans = plans if plans is not None else PlanStore()
        self._audit = audit if audit is not None else AuditLog()

    @property
    def state_path(self) -> Path:
        path = getattr(self._store, "path", None)
        if path is not None:
            return Path(path)
        return Path(self._store.database.path)

    # ----- reads -----
    def snapshot(self, now: datetime | None = None) -> dict[str, Any]:
        state = self._store.read()
        return {
            "paused": bool(state.get("paused")),
            "notices": [dict(n) for n in _open_notices(state.get("notices") or [])],
            "push_enabled": bool(self.settings.ntfy_topic),
            "plan_hour": self.settings.plan_hour,
            "checkin_gap_days": self.settings.checkin_gap_days,
            "quiet_hours": is_quiet_hours(local_time(now or utc_now())),
        }

    # ----- writes -----
    def record_checkin(self, now: datetime | None = None) -> None:
        moment = now or utc_now()

        def change(state: dict[str, Any]) -> None:
            state["last_checkin_at"] = _iso(moment)

        self._store.update(change)
        # Audited like every other heartbeat state change, so the Analytics view
        # can count check-ins from audit.log alone. It carries a timestamp and a
        # source, never message text.
        self._audit.record_event("heartbeat_checkin_recorded", source="chat_message")

    def set_paused(self, paused: bool, *, source: str) -> bool:
        def change(state: dict[str, Any]) -> None:
            state["paused"] = bool(paused)

        self._store.update(change)
        self._audit.record_event("heartbeat_paused" if paused else "heartbeat_resumed", source=source)
        return bool(paused)

    def dismiss(self, notice_id: str, *, source: str) -> bool:
        moment = utc_now()

        def change(state: dict[str, Any]) -> dict[str, Any] | None:
            for notice in state.get("notices", []):
                if notice["id"] == notice_id and notice.get("dismissed_at") is None:
                    notice["dismissed_at"] = _iso(moment)
                    return dict(notice)
            return None

        dismissed = self._store.update(change)
        if dismissed is None:
            return False
        self._audit.record_event(
            "heartbeat_notice_dismissed", notice_id=notice_id, kind=dismissed["kind"], source=source
        )
        return True

    def check_now(self, now: datetime | None = None) -> dict[str, Any]:
        """The person asked for a check directly. Quiet hours do not apply; once per day still does."""
        if self._store.read().get("paused"):
            return {"status": "paused", "fired": []}
        fired = self.tick(now, manual=True)
        return {"status": "fired" if fired else "nothing_due", "fired": fired}

    def tick(self, now: datetime | None = None, *, manual: bool = False) -> list[dict[str, Any]]:
        """Run both checks once. Returns the notices that fired on this tick."""
        moment = now or utc_now()
        local = local_time(moment)
        today_key = local.date().isoformat()
        has_plan = self._plans.has_plan(local.date())
        quiet = is_quiet_hours(local)

        def change(state: dict[str, Any]) -> list[dict[str, Any]]:
            # The first run starts the check-in timer, so a new install does not nag at once.
            if state.get("last_checkin_at") is None:
                state["last_checkin_at"] = _iso(moment)
            if state.get("paused") or (quiet and not manual):
                return []
            fired_today = state.setdefault("fired", {})
            if not isinstance(fired_today, dict):
                fired_today = {}
                state["fired"] = fired_today
            created: list[dict[str, Any]] = []
            notices = state.setdefault("notices", [])
            # Existing copies of one notice (from an older import) are marked,
            # not deleted, so a home screen already carrying two identical
            # cards settles to one on the next tick.
            _collapse_duplicate_notices(notices)
            # A condition may produce at most one open notice per kind per local
            # day. An older build (PR #20 legacy import) could copy a notice
            # into state without recording the matching ``fired`` marker, which
            # made a later tick raise a second, identical notice. Treat an open
            # notice that already exists for today as proof the condition fired,
            # so we never add a duplicate.
            def _open_notice_today(kind: str) -> bool:
                for notice in notices:
                    if (
                        notice.get("kind") == kind
                        and notice.get("dismissed_at") is None
                        and _notice_local_date(notice) == today_key
                    ):
                        return True
                return False

            if plan_check_due(local, has_plan, self.settings.plan_hour):
                if fired_today.get(KIND_PLAN) == today_key or _open_notice_today(KIND_PLAN):
                    fired_today[KIND_PLAN] = today_key
                else:
                    created.append(self._open(state, KIND_PLAN, _plan_text(self.settings.plan_hour), moment, manual))
                    fired_today[KIND_PLAN] = today_key
            last_checkin = _parse(state["last_checkin_at"])
            if checkin_gap_due(moment, last_checkin, self.settings.checkin_gap_days):
                if fired_today.get(KIND_GAP) == today_key or _open_notice_today(KIND_GAP):
                    fired_today[KIND_GAP] = today_key
                else:
                    days = whole_days_between(moment, last_checkin)
                    created.append(self._open(state, KIND_GAP, _gap_text(days), moment, manual))
                    fired_today[KIND_GAP] = today_key
            return created

        created = self._store.update(change)
        for notice in created:
            status, reason = self._push(notice)
            self._store.update(lambda s, nid=notice["id"], st=status: _set_push(s, nid, st))
            if reason is not None:
                self._audit.record_event("heartbeat_push_failed", notice_id=notice["id"], reason=reason)
            self._audit.record_event(
                "heartbeat_notice_fired", notice_id=notice["id"], kind=notice["kind"], manual=manual, push=status
            )
        return created

    # ----- helpers -----
    def _open(
        self, state: dict[str, Any], kind: str, text: str, moment: datetime, manual: bool
    ) -> dict[str, Any]:
        notice = {
            "id": f"hb-{uuid.uuid4().hex[:12]}",
            "kind": kind,
            "text": text,
            "created_at": _iso(moment),
            "dismissed_at": None,
            "push": "pending",
            "manual": manual,
        }
        state.setdefault("notices", []).append(notice)
        return dict(notice)

    def _push(self, notice: dict[str, Any]) -> tuple[str, str | None]:
        """Returns (status, failure reason). Status is off, sent or failed."""
        topic = self.settings.ntfy_topic
        if not topic:
            return "off", None
        try:
            ntfy.push(
                topic,
                server=self.settings.ntfy_server,
                title=PUSH_TITLES[notice["kind"]],
                message=notice["text"],
            )
        except ntfy.PushError as exc:
            log.warning("phone push failed for %s: %s", notice["id"], exc)
            return "failed", str(exc)
        return "sent", None


def _set_push(state: dict[str, Any], notice_id: str, status: str) -> None:
    for notice in state.get("notices", []):
        if notice["id"] == notice_id:
            notice["push"] = status
            return
