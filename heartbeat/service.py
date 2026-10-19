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
            "notices": [dict(n) for n in state.get("notices", []) if n.get("dismissed_at") is None],
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
            created: list[dict[str, Any]] = []
            if plan_check_due(local, has_plan, self.settings.plan_hour) and fired_today.get(KIND_PLAN) != today_key:
                created.append(self._open(state, KIND_PLAN, _plan_text(self.settings.plan_hour), moment, manual))
                fired_today[KIND_PLAN] = today_key
            last_checkin = _parse(state["last_checkin_at"])
            if checkin_gap_due(moment, last_checkin, self.settings.checkin_gap_days) and fired_today.get(KIND_GAP) != today_key:
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
