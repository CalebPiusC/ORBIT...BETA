"""Message actions, the collapsed sidebar, and the Analytics view — all offline.

Rules under test:
- Every stored message gets an id, and the row renders it. The id is what a
  rating and a regeneration name, so both must survive a page reload.
- Thumbs up/down write one binary line to audit.log. No form, no reason, no
  second store; clicking the rating already showing does not re-send it.
- Regenerate works on any Orbit reply: the reply is rewritten in place, keeps its
  id and its position, later turns are left alone, and the model is asked for a
  genuinely different answer.
- The sidebar collapses to an icons-only rail, the choice persists across
  reloads, and every control keeps its tooltip.
- Analytics reads audit.log and nothing else: messages per day, which
  model/persona answered, heartbeat notices, check-ins, and ratings.
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from collections.abc import Iterator
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from unittest.mock import patch

from providers.base import ChatMessage, StreamChunk

from app.analytics import build_analytics
from app.server import create_app
from app.store import DEFAULT_USER_ID, ChatMessage as StoreChatMessage, Store
from providers.personas import model_persona_map, persona_for_model
from tools.registry import AuditLog

APP_JS = Path(__file__).resolve().parent.parent / "app" / "static" / "app.js"
LAYOUT = Path(__file__).resolve().parent.parent / "app" / "templates" / "layout.html"


def parse_sse(raw: str) -> list[tuple[str, str]]:
    events: list[tuple[str, str]] = []
    for block in raw.split("\n\n"):
        block = block.strip()
        if not block:
            continue
        event = "message"
        data_lines: list[str] = []
        for line in block.split("\n"):
            if line.startswith("event:"):
                event = line[6:].strip()
            elif line.startswith("data:"):
                data_lines.append(line[5:])
        events.append((event, "\n".join(data_lines)))
    return events


class ScriptedProvider:
    """Canned streaming provider that records what each turn was asked."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.reply_number = 0

    def title(self, user_text: str, answer: str) -> str:
        return ""

    def reply(self, history, system_prompt: str) -> str:
        return "A canned reply."

    def reply_stream(
        self,
        history,
        system_prompt: str,
        *,
        think: bool = False,
        report_attempts: bool = False,
        allow_tools: bool = False,
        thinking_level: str = "quick",
    ) -> Iterator[StreamChunk]:
        self.reply_number += 1
        self.calls.append(
            {
                "history": [message.content for message in history],
                "system_prompt": system_prompt,
                "think": think,
            }
        )
        yield StreamChunk(kind="answer", text=f"Reply {self.reply_number}")


class AppCase(unittest.TestCase):
    def setUp(self) -> None:
        self.store = Store()
        self.view = self.store.for_account(DEFAULT_USER_ID)
        self.provider = ScriptedProvider()
        self.temp_dir = tempfile.TemporaryDirectory()
        self.audit_path = Path(self.temp_dir.name) / "audit.log"
        self.addCleanup(self.temp_dir.cleanup)
        self.app = create_app(
            provider=self.provider,
            store=self.store,
            model_audit=AuditLog(self.audit_path),
        )
        self.client = self.app.test_client()

    def send(self, text: str) -> tuple[list[tuple[str, str]], str]:
        """Send one home message; return the SSE events and the stored reply id."""
        events = parse_sse(self.client.post("/api/chat", json={"message": text}).get_data(as_text=True))
        message_events = [data for name, data in events if name == "message"]
        self.assertTrue(message_events, "the stream must name the stored reply")
        return events, json.loads(message_events[-1])["id"]

    def audit_events(self) -> list[dict[str, Any]]:
        if not self.audit_path.exists():
            return []
        return [json.loads(line) for line in self.audit_path.read_text().splitlines()]


class MessageIdTests(AppCase):
    def test_stored_messages_get_unique_ids_and_the_row_renders_them(self) -> None:
        conv = self.view.start_conversation()
        self.view.append_message(
            conv.id, StoreChatMessage(who="You", time="09:41", body="First question", is_orbit=False)
        )
        self.view.append_message(
            conv.id, StoreChatMessage(who="Orbit", time="09:42", body="First answer", is_orbit=True)
        )
        ids = [message.id for message in self.view.home_thread]
        self.assertEqual(len(set(ids)), 2)
        self.assertTrue(all(message_id.startswith("m-") for message_id in ids))

        body = self.client.get("/").get_data(as_text=True)
        for message_id in ids:
            self.assertIn(f'data-message-id="{message_id}"', body)

    def test_a_live_turn_names_the_stored_reply_over_sse(self) -> None:
        events, message_id = self.send("Hello")
        names = [name for name, _data in events]
        self.assertIn("message", names)
        # The id line arrives before "done", so a client that saw "done" has it.
        self.assertLess(names.index("message"), names.index("done"))
        self.assertEqual(self.view.home_thread[-1].id, message_id)

    def test_task_chat_messages_carry_ids_too(self) -> None:
        events = parse_sse(
            self.client.post("/api/task/T1/stream", json={"initial": True}).get_data(as_text=True)
        )
        stored_id = json.loads([data for name, data in events if name == "message"][-1])["id"]
        self.assertEqual(self.view.get_task("T1").chat[-1].id, stored_id)

    def test_find_message_spans_conversations_and_tasks(self) -> None:
        _events, home_id = self.send("Home question")
        task_events = parse_sse(
            self.client.post("/api/task/T1/stream", json={"initial": True}).get_data(as_text=True)
        )
        task_id = json.loads([data for name, data in task_events if name == "message"][-1])["id"]

        self.assertEqual(self.view.find_message(home_id)[0], self.view.active_conversation_id)
        self.assertEqual(self.view.find_message(task_id)[0], "T1")
        self.assertIsNone(self.view.find_message("m-does-not-exist"))
        self.assertIsNone(self.view.find_message(""))


class RatingTests(AppCase):
    def test_a_rating_is_one_binary_audit_line_against_the_message(self) -> None:
        _events, message_id = self.send("Rate me")

        response = self.client.post(f"/api/messages/{message_id}/rating", json={"rating": "up"})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json(), {"message_id": message_id, "rating": "up"})
        [event] = [item for item in self.audit_events() if item["event"] == "message_rated"]
        self.assertEqual(event["message_id"], message_id)
        self.assertEqual(event["rating"], "up")
        self.assertEqual(event["surface"], "home")
        self.assertEqual(event["owner_id"], self.view.active_conversation_id)
        self.assertIn("timestamp", event)
        # A rating is a signal, not a conversation: no message text is recorded.
        self.assertNotIn("Rate me", json.dumps(event))
        self.assertNotIn("Reply 1", json.dumps(event))

    def test_both_ratings_can_be_recorded_for_one_message(self) -> None:
        _events, message_id = self.send("Rate me twice")
        self.client.post(f"/api/messages/{message_id}/rating", json={"rating": "up"})
        self.client.post(f"/api/messages/{message_id}/rating", json={"rating": "down"})

        rated = [item for item in self.audit_events() if item["event"] == "message_rated"]
        self.assertEqual([item["rating"] for item in rated], ["up", "down"])

    def test_a_rating_must_be_up_or_down(self) -> None:
        _events, message_id = self.send("Rate me")
        for payload in ({"rating": "meh"}, {"rating": 1}, {}, {"rating": None}):
            with self.subTest(payload=payload):
                response = self.client.post(f"/api/messages/{message_id}/rating", json=payload)
                self.assertEqual(response.status_code, 400)
        self.assertEqual([e for e in self.audit_events() if e["event"] == "message_rated"], [])

    def test_an_unknown_message_id_is_refused_rather_than_logged(self) -> None:
        response = self.client.post("/api/messages/m-not-a-message/rating", json={"rating": "up"})
        self.assertEqual(response.status_code, 404)
        self.assertEqual([e for e in self.audit_events() if e["event"] == "message_rated"], [])

    def test_a_task_reply_rating_is_tagged_as_a_task(self) -> None:
        events = parse_sse(
            self.client.post("/api/task/T1/stream", json={"initial": True}).get_data(as_text=True)
        )
        message_id = json.loads([data for name, data in events if name == "message"][-1])["id"]

        self.client.post(f"/api/messages/{message_id}/rating", json={"rating": "down"})

        [event] = [item for item in self.audit_events() if item["event"] == "message_rated"]
        self.assertEqual(event["surface"], "task")
        self.assertEqual(event["owner_id"], "T1")


class RegenerateTests(AppCase):
    def test_any_reply_can_be_regenerated_in_place_not_only_the_newest(self) -> None:
        _events, first_reply_id = self.send("First question")
        self.send("Second question")
        before = [(message.who, message.body, message.id) for message in self.view.home_thread]

        response = self.client.post(
            "/api/chat", json={"regenerate": True, "message_id": first_reply_id}
        )
        self.assertEqual(response.status_code, 200)
        response.get_data(as_text=True)  # drain the stream

        after = [(message.who, message.body, message.id) for message in self.view.home_thread]
        self.assertEqual(len(after), len(before))
        # The regenerated reply keeps its position and its id; the turns after it
        # are untouched, and no duplicate turn appears.
        self.assertEqual([row[0] for row in after], [row[0] for row in before])
        self.assertEqual([row[2] for row in after], [row[2] for row in before])
        self.assertEqual(after[1][1], "Reply 3")
        self.assertEqual(after[3][1], before[3][1])

    def test_regenerating_asks_for_a_different_answer_from_the_same_turn(self) -> None:
        _events, reply_id = self.send("Explain the diff")
        first_call = self.provider.calls[-1]

        self.client.post("/api/chat", json={"regenerate": True, "message_id": reply_id}).get_data(
            as_text=True
        )

        regenerated_call = self.provider.calls[-1]
        # Same conversation up to that reply, so the question being answered is
        # the same one; the prompt is what asks for a different answer.
        self.assertEqual(regenerated_call["history"], first_call["history"])
        self.assertNotIn("genuinely differently", first_call["system_prompt"])
        self.assertIn("genuinely differently", regenerated_call["system_prompt"])
        self.assertIn("Do not repeat your previous reply", regenerated_call["system_prompt"])

    def test_regenerating_an_older_reply_does_not_send_what_came_after_it(self) -> None:
        _events, first_reply_id = self.send("First question")
        self.send("Second question")

        self.client.post("/api/chat", json={"regenerate": True, "message_id": first_reply_id}).get_data(
            as_text=True
        )

        self.assertEqual(self.provider.calls[-1]["history"], ["First question"])

    def test_the_newest_reply_still_regenerates_without_an_id(self) -> None:
        self.send("First question")
        before = len(self.view.home_thread)

        response = self.client.post("/api/chat", json={"regenerate": True})

        self.assertEqual(response.status_code, 200)
        response.get_data(as_text=True)
        self.assertEqual(len(self.view.home_thread), before)  # replaced, not appended
        self.assertEqual(self.view.home_thread[-1].body, "Reply 2")

    def test_a_user_message_or_unknown_id_is_refused(self) -> None:
        self.send("First question")
        user_id = self.view.home_thread[0].id

        as_user = self.client.post("/api/chat", json={"regenerate": True, "message_id": user_id})
        as_unknown = self.client.post(
            "/api/chat", json={"regenerate": True, "message_id": "m-nothing"}
        )

        self.assertEqual(as_user.status_code, 409)
        self.assertEqual(as_unknown.status_code, 409)

    def test_a_regenerated_reply_still_names_its_id_over_sse(self) -> None:
        _events, reply_id = self.send("First question")

        events = parse_sse(
            self.client.post(
                "/api/chat", json={"regenerate": True, "message_id": reply_id}
            ).get_data(as_text=True)
        )

        self.assertEqual(json.loads([data for name, data in events if name == "message"][-1])["id"], reply_id)

    def test_a_task_reply_can_be_regenerated_by_id(self) -> None:
        events = parse_sse(
            self.client.post("/api/task/T1/stream", json={"initial": True}).get_data(as_text=True)
        )
        first_id = json.loads([data for name, data in events if name == "message"][-1])["id"]
        self.client.post("/api/task/T1/stream", json={"message": "Keep going"}).get_data(as_text=True)
        before = [(message.who, message.id) for message in self.view.get_task("T1").chat]

        response = self.client.post(
            "/api/task/T1/stream", json={"regenerate": True, "message_id": first_id}
        )
        self.assertEqual(response.status_code, 200)
        response.get_data(as_text=True)

        after = [(message.who, message.id) for message in self.view.get_task("T1").chat]
        self.assertEqual(after, before)  # replaced in place, nothing appended
        # The reply that carried that id is the one that changed, and the turns
        # after it are untouched.
        rewritten = next(m for m in self.view.get_task("T1").chat if m.id == first_id)
        self.assertEqual(rewritten.body, f"Reply {self.provider.reply_number}")
        self.assertEqual(after[-1], before[-1])


class SidebarCollapseTests(AppCase):
    def test_every_screen_has_the_collapse_toggle_and_the_analytics_entry(self) -> None:
        for path in ("/", "/task/T1", "/analytics"):
            with self.subTest(path=path):
                body = self.client.get(path).get_data(as_text=True)
                self.assertIn('id="sidebarToggle"', body)
                self.assertIn('aria-label="Collapse sidebar"', body)
                self.assertIn('id="analyticsLink"', body)
                self.assertIn('href="/analytics"', body)

    def test_collapsed_controls_keep_a_tooltip_with_the_full_label(self) -> None:
        body = self.client.get("/").get_data(as_text=True)
        # Without the text label, the tooltip is what names the control.
        self.assertIn('id="newConvo" title="New conversation"', body)
        self.assertIn('title="Projects"', body)
        self.assertIn('title="Analytics"', body)
        self.assertIn('title="Agents"', body)
        # The workspace tile names the single workspace it represents.
        self.assertIn('class="workspace-tile" title="Personal space"', body)

    def test_the_choice_persists_across_reloads_and_applies_before_paint(self) -> None:
        layout = LAYOUT.read_text(encoding="utf-8")
        script = APP_JS.read_text(encoding="utf-8")
        self.assertIn('orbit.sidebar.collapsed', layout)  # restored before first paint
        self.assertIn("sidebar-collapsed-pending", layout)
        self.assertIn('var SIDEBAR_STORAGE_KEY = "orbit.sidebar.collapsed";', script)
        self.assertIn("localStorage.setItem(SIDEBAR_STORAGE_KEY", script)
        self.assertIn("localStorage.getItem(SIDEBAR_STORAGE_KEY", script)
        # A blocked localStorage (private mode) must not break the toggle.
        self.assertIn("catch (_error)", script)

    def test_collapsed_state_is_icons_only_with_a_scrollable_history(self) -> None:
        layout = LAYOUT.read_text(encoding="utf-8")
        self.assertIn(".sidebar--collapsed", layout)
        self.assertIn(".sidebar-scroll { flex: 1; min-height: 0; overflow-y: auto;", layout)
        # The collapsed rail hides the labels, not the controls themselves.
        self.assertIn(".sidebar--collapsed .sidebar-label,", layout)
        self.assertNotIn(".sidebar--collapsed .new-convo { display: none", layout)

    def test_the_toggle_updates_pressed_state_and_tooltips(self) -> None:
        script = APP_JS.read_text(encoding="utf-8")
        self.assertIn('aria-pressed", collapsed ? "true" : "false"', script)
        self.assertIn('collapsed ? "Expand sidebar" : "Collapse sidebar"', script)


class AlwaysVisibleActionsTests(unittest.TestCase):
    def test_response_actions_are_not_hover_only(self) -> None:
        layout = LAYOUT.read_text(encoding="utf-8")
        # The old rule hid the row entirely until hover; a phone cannot hover.
        self.assertNotIn("opacity: 0;", layout.split(".message-actions")[1].split("}")[0])
        self.assertIn("opacity: 0.55", layout)
        self.assertIn(".chat-row.orbit:hover .message-actions", layout)

    def test_the_four_actions_exist_on_every_orbit_row(self) -> None:
        store = Store()
        view = store.for_account(DEFAULT_USER_ID)
        conversation = view.start_conversation()
        view.append_message(
            conversation.id,
            StoreChatMessage(who="Orbit", time="09:42", body="An answer", is_orbit=True),
        )
        client = create_app(store=store, provider=ScriptedProvider()).test_client()
        body = client.get("/").get_data(as_text=True)
        for action in ("copy", "regenerate", "feedback-up", "feedback-down"):
            self.assertIn(f'data-message-action="{action}"', body)

    def test_the_client_posts_a_rating_but_does_not_re_send_the_active_one(self) -> None:
        script = APP_JS.read_text(encoding="utf-8")
        self.assertIn('"/api/messages/" + encodeURIComponent(id) + "/rating"', script)
        self.assertIn('action === "feedback-up" ? "up" : "down"', script)
        # Clicking the rating already showing is a change of mind, not a rating.
        self.assertIn("if (pressed) {", script)

    def test_the_client_sends_the_message_id_when_regenerating(self) -> None:
        script = APP_JS.read_text(encoding="utf-8")
        self.assertIn("{ regenerate: true, message_id: id }", script)
        self.assertIn("messageId: id,", script)
        self.assertIn('setMessageId(row, JSON.parse(data).id)', script)
        self.assertIn('setMessageId(orbit.row, JSON.parse(data).id)', script)


class AnalyticsModuleTests(unittest.TestCase):
    """The reader: what it counts, what it ignores, and how it says it."""

    def setUp(self) -> None:
        self.now = datetime(2026, 10, 12, 9, 0, tzinfo=timezone.utc)
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.path = Path(self.temp_dir.name) / "audit.log"

    def write(self, *events: dict[str, Any]) -> None:
        self.path.write_text("\n".join(json.dumps(event) for event in events) + "\n", encoding="utf-8")

    def stamp(self, days_ago: int = 0, hour: int = 9) -> str:
        moment = (self.now - timedelta(days=days_ago)).replace(hour=hour)
        return moment.isoformat().replace("+00:00", "Z")

    def test_counts_messages_models_reminders_checkins_and_ratings(self) -> None:
        self.write(
            {"timestamp": self.stamp(0, 9), "event": "chat_message_received", "surface": "home"},
            {"timestamp": self.stamp(0, 10), "event": "chat_message_received", "surface": "task"},
            {"timestamp": self.stamp(1, 10), "event": "chat_message_received", "surface": "home"},
            {"timestamp": self.stamp(0, 9), "event": "model_answered", "model": "titan-model"},
            {"timestamp": self.stamp(0, 9), "event": "model_answered", "model": "titan-model"},
            {"timestamp": self.stamp(0, 9), "event": "model_answered", "model": "some-model"},
            {"timestamp": self.stamp(0, 11), "event": "heartbeat_notice_fired", "kind": "plan"},
            {"timestamp": self.stamp(0, 12), "event": "heartbeat_notice_dismissed", "kind": "plan"},
            {"timestamp": self.stamp(0, 13), "event": "heartbeat_checkin_recorded"},
            {"timestamp": self.stamp(0, 13), "event": "message_rated", "rating": "up"},
            {"timestamp": self.stamp(0, 13), "event": "message_rated", "rating": "down"},
            {"timestamp": self.stamp(0, 13), "event": "message_rated", "rating": "sideways"},
        )
        with patch.dict(os.environ, {"GEMINI_MODEL": "titan-model"}):
            report = build_analytics(self.path, now=self.now)

        self.assertEqual(report.messages_total, 3)
        self.assertEqual(report.messages_today, 2)
        self.assertEqual(len(report.messages_per_day), 7)
        self.assertEqual([day.count for day in report.messages_per_day][-2:], [1, 2])
        self.assertEqual(report.models, [("titan-model", "Titan", 2), ("some-model", "some-model", 1)])
        self.assertEqual((report.heartbeat_fired, report.heartbeat_dismissed), (1, 1))
        self.assertEqual(report.checkins, 1)
        self.assertEqual((report.ratings_up, report.ratings_down), (1, 1))

    def test_event_persona_keeps_context_specific_fallback_names(self) -> None:
        self.write(
            {
                "timestamp": self.stamp(),
                "event": "model_answered",
                "model": "shared-gemini-model",
                "persona": "Titan",
            },
            {
                "timestamp": self.stamp(),
                "event": "model_answered",
                "model": "shared-gemini-model",
                "persona": "Europa",
            },
        )

        report = build_analytics(self.path, now=self.now)

        self.assertEqual(
            report.models,
            [("shared-gemini-model", "Europa", 1), ("shared-gemini-model", "Titan", 1)],
        )

    def test_events_outside_the_window_and_broken_lines_are_ignored(self) -> None:
        self.path.write_text(
            "\n".join(
                [
                    "{not json",
                    json.dumps({"timestamp": "2026-09-01T09:00:00Z", "event": "chat_message_received"}),
                    "",
                    json.dumps({"timestamp": self.stamp(0), "event": "chat_message_received"}),
                    json.dumps({"no_timestamp": True, "event": "chat_message_received"}),
                    json.dumps({"timestamp": "not-a-time", "event": "chat_message_received"}),
                ]
            )
            + "\n",
            encoding="utf-8",
        )

        report = build_analytics(self.path, now=self.now)

        self.assertEqual(report.messages_total, 1)
        self.assertTrue(report.log_present)

    def test_a_missing_log_is_reported_as_having_nothing_to_count(self) -> None:
        report = build_analytics(self.path, now=self.now)

        self.assertFalse(report.log_present)
        self.assertFalse(report.has_data)
        self.assertEqual(report.messages_total, 0)
        self.assertEqual(report.summary, "No messages in the last seven days.")
        self.assertTrue(any("No audit.log yet" in note for note in report.notes))

    def test_the_summary_is_descriptive_only(self) -> None:
        self.write(
            *[
                {"timestamp": self.stamp(0, 9), "event": "chat_message_received"}
                for _ in range(12)
            ],
            {"timestamp": self.stamp(0, 9), "event": "model_answered", "model": "titan-model"},
        )
        with patch.dict(os.environ, {"GEMINI_MODEL": "titan-model"}):
            report = build_analytics(self.path, now=self.now)

        self.assertEqual(report.summary, "12 messages today; 12 in the last 7 days, mostly Titan (1 of 12).")
        # No streaks, no comparisons, no targets: the words that would make a
        # quiet day read as a failure are simply not in the view.
        lowered = report.summary.lower()
        for word in ("streak", "target", "goal", "best", "worst", "more than", "less than"):
            self.assertNotIn(word, lowered)

    def test_bars_are_relative_to_the_busiest_day_in_the_window(self) -> None:
        self.write(
            *[{"timestamp": self.stamp(0, 9), "event": "chat_message_received"} for _ in range(4)],
            {"timestamp": self.stamp(1, 9), "event": "chat_message_received"},
        )
        report = build_analytics(self.path, now=self.now)

        days = {day.label: day for day in report.messages_per_day}
        self.assertEqual(days["Today"].bar_percent, 100)
        self.assertEqual(days["Yesterday"].bar_percent, 25)
        self.assertEqual(days["Tue"].bar_percent, 0)


class PersonaNamingTests(unittest.TestCase):
    def test_configured_models_map_to_their_names_and_unknowns_keep_their_id(self) -> None:
        with patch.dict(
            os.environ,
            {
                "OPENROUTER_MODEL_1": "vendor/free-one:free",
                "OPENROUTER_MODEL_2": "vendor/free-two:free",
                "OPENROUTER_MODEL_3": "vendor/free-three:free",
                "GEMINI_MODEL": "titan-model",
                "GEMINI_FALLBACK_MODEL": "europa-model",
            },
        ):
            mapping = model_persona_map()

        self.assertEqual(mapping["vendor/free-one:free"], "Io")
        self.assertEqual(mapping["vendor/free-two:free"], "Ganymede")
        self.assertEqual(mapping["vendor/free-three:free"], "Callisto")
        self.assertEqual(mapping["titan-model"], "Titan")
        self.assertEqual(mapping["europa-model"], "Europa")
        self.assertEqual(persona_for_model("some/other", mapping), "some/other")

    def test_no_openrouter_models_configured_means_no_openrouter_names(self) -> None:
        with patch.dict(
            os.environ,
            {
                "OPENROUTER_MODEL_1": "",
                "OPENROUTER_MODEL_2": "",
                "OPENROUTER_MODEL_3": "",
                "GEMINI_MODEL": "titan-model",
            },
            clear=True,
        ):
            mapping = model_persona_map()

        self.assertNotIn("Io", mapping.values())
        self.assertNotIn("Ganymede", mapping.values())
        self.assertNotIn("Callisto", mapping.values())


class AnalyticsRouteTests(AppCase):
    def test_the_view_renders_the_counts_from_the_log(self) -> None:
        _events, message_id = self.send("Hello Orbit")
        self.client.post(f"/api/messages/{message_id}/rating", json={"rating": "up"})

        response = self.client.get("/analytics")

        self.assertEqual(response.status_code, 200)
        body = response.get_data(as_text=True)
        self.assertIn("Messages per day", body)
        self.assertIn("Which model answered", body)
        self.assertIn("Reminders", body)
        self.assertIn("Message ratings", body)
        self.assertIn("1 message today", body)
        # Thumbs up counted from the line the rating wrote.
        self.assertIn("<span class=\"key\">Thumbs up</span><span class=\"value\">1</span>", body)

    def test_every_sent_message_is_audited_for_the_daily_count(self) -> None:
        self.send("One")
        self.send("Two")

        received = [e for e in self.audit_events() if e["event"] == "chat_message_received"]

        self.assertEqual(len(received), 2)
        self.assertEqual([event["surface"] for event in received], ["home", "home"])
        # The text of the message is never part of the record.
        self.assertNotIn("One", json.dumps(received))
        self.assertNotIn("Two", json.dumps(received))

    def test_a_task_message_is_audited_as_a_task(self) -> None:
        self.client.post("/api/task/T1/stream", json={"message": "Keep going"}).get_data(as_text=True)

        [received] = [e for e in self.audit_events() if e["event"] == "chat_message_received"]

        self.assertEqual(received["surface"], "task")
        self.assertEqual(received["conversation_id"], "T1")

    def test_a_checkin_is_audited_when_a_message_counts_as_one(self) -> None:
        class FakeHeartbeat:
            def __init__(self) -> None:
                self.checkins = 0
                self.settings = type("S", (), {"interval_seconds": 60.0})()

            def record_checkin(self) -> None:
                self.checkins += 1

        heartbeat = FakeHeartbeat()
        app = create_app(
            provider=ScriptedProvider(),
            store=Store(),
            heartbeat=heartbeat,
            model_audit=AuditLog(self.audit_path),
        )
        app.test_client().post("/api/chat", json={"message": "Checking in"}).get_data(as_text=True)

        self.assertEqual(heartbeat.checkins, 1)


if __name__ == "__main__":
    unittest.main()
