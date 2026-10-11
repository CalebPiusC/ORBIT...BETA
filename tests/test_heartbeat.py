"""Heartbeat: local-time checks, quiet hours, once-per-day, persistence, pause, push, audit.

Offline and temp-path only. Nothing here touches runtime/heartbeat.json, runtime/plans.json
or audit.log. Push tests run against a local HTTP server, never ntfy.sh.
"""

from __future__ import annotations

import json
import tempfile
import threading
import unittest
from datetime import date, datetime
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from config import HeartbeatSettings
from heartbeat import ntfy
from heartbeat.schedule import is_quiet_hours, plan_check_due
from heartbeat.scheduler import run_scheduler
from heartbeat.service import KIND_GAP, KIND_PLAN, Heartbeat
from memory.jsonstore import JsonStore
from memory.plans import PlanStore, validate_plan
from tools import ProposedAction, build_tool_registry
from tools.registry import AuditLog

LAGOS = ZoneInfo("Africa/Lagos")
TOPIC = "orbit-test-7f3c9a1e"


def at(day: int, hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 10, day, hour, minute, tzinfo=LAGOS)


class RecordingGate:
    def __init__(self, approved: bool) -> None:
        self.approved = approved
        self.actions: list[ProposedAction] = []

    def confirm(self, action: ProposedAction) -> bool:
        self.actions.append(action)
        return self.approved


class PushServer:
    """Local stand-in for ntfy: records every POST it receives."""

    def __init__(self) -> None:
        self.posts: list[dict[str, Any]] = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802
                length = int(self.headers.get("Content-Length", "0"))
                outer.posts.append(
                    {
                        "path": self.path,
                        "title": self.headers.get("Title"),
                        "body": self.rfile.read(length).decode("utf-8"),
                    }
                )
                self.send_response(200)
                self.end_headers()

            def log_message(self, *args: Any) -> None:
                return

        self.httpd = HTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def close(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()


class HeartbeatTestCase(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.state_path = self.root / "heartbeat.json"
        self.audit_path = self.root / "audit.log"
        self.plans_path = self.root / "plans.json"

    def settings(self, **overrides: Any) -> HeartbeatSettings:
        values: dict[str, Any] = {
            "plan_hour": 10,
            "checkin_gap_days": 2,
            "interval_seconds": 60.0,
            "ntfy_topic": "",
            "ntfy_server": "https://ntfy.sh",
        }
        values.update(overrides)
        return HeartbeatSettings(**values)

    def heartbeat(self, **overrides: Any) -> Heartbeat:
        return Heartbeat(
            settings=self.settings(**overrides),
            state_path=self.state_path,
            plans=PlanStore(self.plans_path),
            audit=AuditLog(self.audit_path),
        )

    def audit_events(self) -> list[dict[str, Any]]:
        if not self.audit_path.exists():
            return []
        return [json.loads(line) for line in self.audit_path.read_text(encoding="utf-8").splitlines()]

    def state(self) -> dict[str, Any]:
        return json.loads(self.state_path.read_text(encoding="utf-8"))


class ScheduleRulesTests(unittest.TestCase):
    def test_quiet_hours_run_from_22_to_08_local(self) -> None:
        self.assertFalse(is_quiet_hours(at(11, 21, 59)))
        self.assertTrue(is_quiet_hours(at(11, 22, 0)))
        self.assertTrue(is_quiet_hours(at(11, 0, 30)))
        self.assertTrue(is_quiet_hours(at(11, 7, 59)))
        self.assertFalse(is_quiet_hours(at(11, 8, 0)))

    def test_plan_check_waits_for_the_configured_hour(self) -> None:
        self.assertFalse(plan_check_due(at(11, 9, 59).astimezone(LAGOS), False, 10))
        self.assertTrue(plan_check_due(at(11, 10, 0).astimezone(LAGOS), False, 10))
        self.assertFalse(plan_check_due(at(11, 15, 0).astimezone(LAGOS), True, 10))

    def test_plan_text_validation(self) -> None:
        self.assertEqual(validate_plan("  Ship the  heartbeat \n today "), "Ship the heartbeat today")
        with self.assertRaises(ValueError):
            validate_plan("   ")
        with self.assertRaises(ValueError):
            validate_plan("x" * 501)
        with self.assertRaises(ValueError):
            validate_plan(None)

    def test_naive_times_are_refused(self) -> None:
        from heartbeat.schedule import local_time

        with self.assertRaises(ValueError):
            local_time(datetime(2026, 10, 11, 10, 0))


class DailyPlanCheckTests(HeartbeatTestCase):
    def test_missing_plan_fires_after_the_hour_and_only_once_a_day(self) -> None:
        hb = self.heartbeat()
        self.assertEqual(hb.tick(at(11, 9, 30)), [])
        fired = hb.tick(at(11, 10, 5))
        self.assertEqual([n["kind"] for n in fired], [KIND_PLAN])
        self.assertEqual(hb.tick(at(11, 12, 0)), [])
        self.assertEqual(hb.tick(at(11, 18, 0)), [])
        # A new local day gets its own check.
        self.assertEqual([n["kind"] for n in hb.tick(at(12, 10, 0))], [KIND_PLAN])

    def test_a_saved_plan_suppresses_the_check(self) -> None:
        plans = PlanStore(self.plans_path)
        plans.add("Write the report", date(2026, 10, 11), saved_at="2026-10-11T08:00:00Z")
        hb = self.heartbeat()
        self.assertEqual(hb.tick(at(11, 11, 0)), [])

    def test_a_plan_for_yesterday_does_not_count(self) -> None:
        PlanStore(self.plans_path).add("Yesterday only", date(2026, 10, 10), saved_at="x")
        fired = self.heartbeat().tick(at(11, 11, 0))
        self.assertEqual([n["kind"] for n in fired], [KIND_PLAN])


class QuietHoursTests(HeartbeatTestCase):
    def test_automatic_checks_are_held_during_quiet_hours(self) -> None:
        hb = self.heartbeat()
        self.assertEqual(hb.tick(at(11, 23, 0)), [])
        self.assertEqual(hb.tick(at(11, 3, 0)), [])
        # Held, not dropped: at 08:00 the plan hour (10:00) has not started yet,
        # so the first check that is both out of quiet hours and due is at 10:00.
        self.assertEqual(hb.tick(at(11, 8, 0)), [])
        self.assertEqual([n["kind"] for n in hb.tick(at(11, 10, 0))], [KIND_PLAN])

    def test_direct_check_bypasses_quiet_hours_but_not_once_per_day(self) -> None:
        hb = self.heartbeat()
        first = hb.check_now(at(11, 23, 30))
        self.assertEqual(first["status"], "fired")
        self.assertEqual([n["kind"] for n in first["fired"]], [KIND_PLAN])
        self.assertEqual(hb.check_now(at(11, 23, 45))["status"], "nothing_due")
        # The next local day gets its own check, once the hour passes.
        self.assertEqual([n["kind"] for n in hb.tick(at(12, 10, 0))], [KIND_PLAN])

    def test_direct_check_is_audited_as_manual(self) -> None:
        self.heartbeat().check_now(at(11, 23, 30))
        fired = [e for e in self.audit_events() if e["event"] == "heartbeat_notice_fired"]
        self.assertEqual(len(fired), 1)
        self.assertTrue(fired[0]["manual"])


class CheckInGapTests(HeartbeatTestCase):
    def test_first_run_starts_the_timer_instead_of_nagging(self) -> None:
        hb = self.heartbeat()
        self.assertEqual(hb.tick(at(11, 9, 0)), [])
        self.assertIsNotNone(self.state()["last_checkin_at"])

    def test_gap_fires_once_the_configured_days_pass(self) -> None:
        hb = self.heartbeat(checkin_gap_days=2)
        hb.record_checkin(at(9, 12, 0))
        plans = PlanStore(self.plans_path)
        plans.add("plan", date(2026, 10, 10), saved_at="x")
        plans.add("plan", date(2026, 10, 11), saved_at="x")
        # One day and 23 hours is not yet two days.
        self.assertEqual(hb.tick(at(11, 11, 0)), [])
        fired = hb.tick(at(11, 12, 0))
        self.assertEqual([n["kind"] for n in fired], [KIND_GAP])
        self.assertIn("2 days", fired[0]["text"])
        self.assertEqual(hb.tick(at(11, 15, 0)), [])

    def test_a_check_in_resets_the_gap(self) -> None:
        hb = self.heartbeat(checkin_gap_days=2)
        hb.record_checkin(at(9, 12, 0))
        hb.record_checkin(at(11, 9, 0))
        PlanStore(self.plans_path).add("plan", date(2026, 10, 12), saved_at="x")
        self.assertEqual(hb.tick(at(12, 11, 0)), [])


class RestartPersistenceTests(HeartbeatTestCase):
    def test_restart_does_not_refire_today(self) -> None:
        self.heartbeat().tick(at(11, 10, 5))
        self.assertEqual(self.heartbeat().tick(at(11, 16, 0)), [])

    def test_restart_does_not_reset_the_checkin_timer(self) -> None:
        hb = self.heartbeat(checkin_gap_days=2)
        hb.record_checkin(at(9, 12, 0))
        restarted = self.heartbeat(checkin_gap_days=2)
        PlanStore(self.plans_path).add("plan", date(2026, 10, 11), saved_at="x")
        fired = restarted.tick(at(11, 12, 0))
        self.assertEqual([n["kind"] for n in fired], [KIND_GAP])


class PauseTests(HeartbeatTestCase):
    def test_paused_heartbeat_fires_nothing_even_for_a_direct_check(self) -> None:
        hb = self.heartbeat()
        hb.set_paused(True, source="settings")
        self.assertEqual(hb.tick(at(11, 11, 0)), [])
        self.assertEqual(hb.check_now(at(11, 11, 0))["status"], "paused")

    def test_pause_and_resume_are_audited_and_visible_to_other_instances(self) -> None:
        hb = self.heartbeat()
        hb.set_paused(True, source="settings")
        self.assertTrue(self.heartbeat().snapshot()["paused"])
        self.heartbeat().set_paused(False, source="tool")
        self.assertFalse(hb.snapshot()["paused"])
        events = [e["event"] for e in self.audit_events()]
        self.assertEqual(events, ["heartbeat_paused", "heartbeat_resumed"])
        self.assertEqual(self.audit_events()[0]["source"], "settings")


class DismissAndAuditTests(HeartbeatTestCase):
    def test_dismiss_hides_the_notice_and_is_audited_once(self) -> None:
        hb = self.heartbeat()
        notice = hb.tick(at(11, 10, 5))[0]
        self.assertEqual(len(hb.snapshot()["notices"]), 1)
        self.assertTrue(hb.dismiss(notice["id"], source="card"))
        self.assertEqual(hb.snapshot()["notices"], [])
        self.assertFalse(hb.dismiss(notice["id"], source="card"))
        dismissed = [e for e in self.audit_events() if e["event"] == "heartbeat_notice_dismissed"]
        self.assertEqual(len(dismissed), 1)
        self.assertEqual(dismissed[0]["notice_id"], notice["id"])

    def test_firing_is_audited_without_message_text(self) -> None:
        self.heartbeat().tick(at(11, 10, 5))
        fired = [e for e in self.audit_events() if e["event"] == "heartbeat_notice_fired"]
        self.assertEqual(len(fired), 1)
        self.assertEqual(fired[0]["kind"], KIND_PLAN)
        self.assertEqual(fired[0]["push"], "off")
        self.assertNotIn("text", fired[0])


class PhonePushTests(HeartbeatTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.server = PushServer()
        self.addCleanup(self.server.close)

    def test_notice_posts_plain_text_to_the_topic(self) -> None:
        hb = self.heartbeat(ntfy_topic=TOPIC, ntfy_server=self.server.url)
        hb.tick(at(11, 10, 5))
        self.assertEqual(len(self.server.posts), 1)
        post = self.server.posts[0]
        self.assertEqual(post["path"], f"/{TOPIC}")
        self.assertEqual(post["title"], "ORBIT: daily plan")
        self.assertIn("no plan saved for today", post["body"])
        self.assertEqual(self.state()["notices"][0]["push"], "sent")

    def test_quiet_hours_send_no_push(self) -> None:
        hb = self.heartbeat(ntfy_topic=TOPIC, ntfy_server=self.server.url)
        hb.tick(at(11, 23, 0))
        self.assertEqual(self.server.posts, [])

    def test_a_failed_push_is_recorded_and_the_topic_never_leaks(self) -> None:
        dead = "http://127.0.0.1:9"  # nothing listens on the discard port
        hb = self.heartbeat(ntfy_topic=TOPIC, ntfy_server=dead)
        fired = hb.tick(at(11, 10, 5))
        self.assertEqual(len(fired), 1)
        self.assertEqual(self.state()["notices"][0]["push"], "failed")
        raw_audit = self.audit_path.read_text(encoding="utf-8")
        self.assertIn("heartbeat_push_failed", raw_audit)
        self.assertNotIn(TOPIC, raw_audit)
        self.assertNotIn(TOPIC, self.state_path.read_text(encoding="utf-8"))

    def test_push_refuses_a_missing_topic_and_non_ascii_title(self) -> None:
        with self.assertRaises(ntfy.PushError):
            ntfy.push("", server="https://ntfy.sh", title="t", message="m")
        with self.assertRaises(ValueError):
            ntfy.push(TOPIC, server=self.server.url, title="Orbit ✓", message="m")


class StateFileTests(HeartbeatTestCase):
    def test_corrupt_state_raises_instead_of_resetting(self) -> None:
        self.state_path.write_text("{not json", encoding="utf-8")
        with self.assertRaisesRegex(RuntimeError, "not valid JSON"):
            self.heartbeat().tick(at(11, 10, 5))
        self.assertEqual(self.state_path.read_text(encoding="utf-8"), "{not json")

    def test_writes_are_atomic_and_leave_no_temp_file(self) -> None:
        self.heartbeat().tick(at(11, 10, 5))
        self.assertTrue(self.state_path.exists())
        self.assertFalse(self.state_path.with_name("heartbeat.json.tmp").exists())

    def test_a_failed_change_writes_nothing(self) -> None:
        store = JsonStore(self.root / "s.json", default=dict)
        store.update(lambda d: d.__setitem__("a", 1))

        def bad(d: dict[str, Any]) -> None:
            d["a"] = 2
            raise KeyError("boom")

        with self.assertRaises(KeyError):
            store.update(bad)
        self.assertEqual(store.read(), {"a": 1})

    def test_plan_store_round_trip(self) -> None:
        plans = PlanStore(self.plans_path)
        plans.add("Ship heartbeat", date(2026, 10, 11), saved_at="2026-10-11T09:00:00Z")
        self.assertTrue(plans.has_plan(date(2026, 10, 11)))
        self.assertFalse(plans.has_plan(date(2026, 10, 12)))


class SchedulerTests(HeartbeatTestCase):
    def test_scheduler_keeps_going_after_a_failed_tick(self) -> None:
        calls: list[int] = []
        stop = threading.Event()

        class Flaky:
            def tick(self) -> None:
                calls.append(1)
                if len(calls) == 1:
                    raise RuntimeError("one bad read")
                if len(calls) >= 3:
                    stop.set()

        run_scheduler(Flaky(), interval_seconds=0.001, stop=stop)
        self.assertEqual(len(calls), 3)

    def test_scheduler_is_a_daemon_thread(self) -> None:
        from heartbeat.scheduler import start_scheduler

        hb = self.heartbeat()
        thread, stop_event = start_scheduler(hb, interval_seconds=3600)
        try:
            self.assertTrue(thread.daemon)
            self.assertEqual(thread.name, "orbit-heartbeat")
        finally:
            stop_event.set()
            thread.join(timeout=5)
        self.assertFalse(thread.is_alive())


class PlanAndPauseToolTests(HeartbeatTestCase):
    def test_save_plan_writes_a_dated_entry_only_after_approval(self) -> None:
        gate = RecordingGate(approved=True)
        registry = build_tool_registry(
            confirmation_gate=gate,
            audit_log_path=self.audit_path,
            notes_path=self.root / "notes.txt",
            facts_path=self.root / "facts.txt",
            plans_path=self.plans_path,
            heartbeat_path=self.state_path,
        )
        outcome = registry.dispatch("save_plan", {"plan": "Finish the heartbeat"})
        self.assertEqual(outcome.execution, "succeeded")
        entries = json.loads(self.plans_path.read_text(encoding="utf-8"))
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["text"], "Finish the heartbeat")
        self.assertRegex(entries[0]["date"], r"^\d{4}-\d{2}-\d{2}$")

    def test_declined_save_plan_writes_nothing_and_is_audited(self) -> None:
        registry = build_tool_registry(
            confirmation_gate=RecordingGate(approved=False),
            audit_log_path=self.audit_path,
            notes_path=self.root / "notes.txt",
            facts_path=self.root / "facts.txt",
            plans_path=self.plans_path,
            heartbeat_path=self.state_path,
        )
        outcome = registry.dispatch("save_plan", {"plan": "Nope"})
        self.assertEqual(outcome.confirmation, "declined")
        self.assertFalse(self.plans_path.exists())
        self.assertEqual(self.audit_events()[-1]["tool_name"], "save_plan")

    def test_pause_and_resume_tools_go_through_the_gate(self) -> None:
        gate = RecordingGate(approved=True)
        registry = build_tool_registry(
            confirmation_gate=gate,
            audit_log_path=self.audit_path,
            notes_path=self.root / "notes.txt",
            facts_path=self.root / "facts.txt",
            plans_path=self.plans_path,
            heartbeat_path=self.state_path,
        )
        self.assertEqual(registry.dispatch("pause_heartbeat", {}).execution, "succeeded")
        self.assertTrue(self.heartbeat().snapshot()["paused"])
        self.assertEqual(registry.dispatch("resume_heartbeat", {}).execution, "succeeded")
        self.assertFalse(self.heartbeat().snapshot()["paused"])
        self.assertEqual([a.tool_name for a in gate.actions], ["pause_heartbeat", "resume_heartbeat"])

    def test_declined_pause_leaves_reminders_running(self) -> None:
        registry = build_tool_registry(
            confirmation_gate=RecordingGate(approved=False),
            audit_log_path=self.audit_path,
            notes_path=self.root / "notes.txt",
            facts_path=self.root / "facts.txt",
            plans_path=self.plans_path,
            heartbeat_path=self.state_path,
        )
        registry.dispatch("pause_heartbeat", {})
        self.assertFalse(self.heartbeat().snapshot()["paused"])


class HeartbeatRoutesTests(HeartbeatTestCase):
    def client(self, hb: Heartbeat | None) -> Any:
        from app.server import create_app
        from app.store import Store
        from tests.test_app import FakeStreamingProvider

        app = create_app(provider=FakeStreamingProvider(), store=Store(), heartbeat=hb)
        return app.test_client()

    def test_routes_report_503_without_a_running_heartbeat(self) -> None:
        client = self.client(None)
        self.assertEqual(client.get("/api/heartbeat").status_code, 503)
        self.assertEqual(client.post("/api/heartbeat/pause", json={"paused": True}).status_code, 503)

    def test_pause_route_accepts_only_literal_booleans(self) -> None:
        hb = self.heartbeat()
        client = self.client(hb)
        self.assertEqual(client.post("/api/heartbeat/pause", json={"paused": "yes"}).status_code, 400)
        self.assertEqual(client.post("/api/heartbeat/pause", json={}).status_code, 400)
        self.assertFalse(hb.snapshot()["paused"])
        response = client.post("/api/heartbeat/pause", json={"paused": True})
        self.assertEqual(response.status_code, 200)
        self.assertTrue(hb.snapshot()["paused"])
        self.assertEqual(self.audit_events()[-1]["source"], "settings")

    def test_dismiss_route_404s_for_unknown_ids(self) -> None:
        client = self.client(self.heartbeat())
        self.assertEqual(client.post("/api/heartbeat/notices/hb-missing/dismiss").status_code, 404)

    def test_home_shows_the_open_notice_and_chat_counts_as_checkin(self) -> None:
        hb = self.heartbeat()
        hb.tick(at(11, 10, 5))
        client = self.client(hb)
        html = client.get("/").get_data(as_text=True)
        self.assertIn("no plan saved for today", html)

        before = self.state()["last_checkin_at"]
        client.post("/api/chat", json={"message": "hello"})
        self.assertNotEqual(self.state()["last_checkin_at"], before)

    def test_home_still_renders_when_the_state_file_is_corrupt(self) -> None:
        self.state_path.write_text("{broken", encoding="utf-8")
        client = self.client(self.heartbeat())
        response = client.get("/")
        self.assertEqual(response.status_code, 200)


if __name__ == "__main__":
    unittest.main()
