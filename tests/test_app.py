from __future__ import annotations

import json
import re
import tempfile
import unittest
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from providers.base import ChatMessage, StreamChunk

from app.server import create_app
from app.store import Connection, Store
from tools.registry import AuditLog


class FakeStreamingProvider:
    """Stand-in for GeminiProvider: returns canned streamed chunks, no network."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def reply(self, history, system_prompt: str) -> str:
        return "A canned one-shot reply."

    def reply_stream(
        self,
        history,
        system_prompt: str,
        *,
        think: bool = False,
        report_attempts: bool = False,
        thinking_level: str = "quick",
    ) -> Iterator[StreamChunk]:
        self.calls.append(
            {
                "history": list(history),
                "system_prompt": system_prompt,
                "think": think,
                "thinking_level": thinking_level,
            }
        )
        if think:
            yield StreamChunk(kind="thinking", text="I am checking the repository context.")
            yield StreamChunk(kind="answer", text="Here is my plan for the change.")
        else:
            yield StreamChunk(kind="answer", text="Hello from Orbit.")


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


class AppRoutesTests(unittest.TestCase):
    def setUp(self) -> None:
        self.store = Store()
        self.app = create_app(provider=FakeStreamingProvider(), store=self.store)
        self.client = self.app.test_client()

    def test_home_renders_live_data(self):
        resp = self.client.get("/")
        self.assertEqual(resp.status_code, 200)
        body = resp.get_data(as_text=True)
        self.assertIn("Caleb", body)
        self.assertIn("Good ", body)  # time-based greeting
        # Conversation history from the store.
        self.assertIn("Add order tracking to WhatsApp bot", body)
        # Mode toggle reflects default chat mode.
        self.assertIn('data-arena="mode-chat"', body)
        self.assertIn('data-mode="chat"', body)
        self.assertIn('id="currentConversationTitle"', body)
        self.assertIn("Your ideas, in orbit.", body)
        self.assertIn("Search chats", body)
        self.assertIn('data-open-settings', body)
        self.assertNotIn('class="plus-badge">ORBIT', body)

    def test_chat_and_agent_turn_prompts_include_lagos_time_context(self):
        home = self.client.post("/api/chat", json={"message": "What time is it?"})
        home.get_data(as_text=True)
        self.store.set_mode("agent")
        agent = self.client.post("/api/task/T1/stream", json={"initial": True})
        agent.get_data(as_text=True)
        provider = self.app.config["PROVIDER"]
        self.assertEqual(len(provider.calls), 2)
        for call in provider.calls:
            self.assertIn("Current date and time in Africa/Lagos:", call["system_prompt"])
            self.assertIn("Use this as the authoritative current time", call["system_prompt"])

    def test_active_conversation_title_is_shown_in_topbar(self):
        conversation = self.store.start_conversation()
        self.store.set_title(conversation.id, "Lagos release notes")
        body = self.client.get("/").get_data(as_text=True)
        self.assertIn('<span class="topbar-name">Lagos release notes</span>', body)

    def test_regenerate_replaces_only_the_last_home_reply(self):
        first = self.client.post("/api/chat", json={"message": "Check this answer again."})
        self.assertEqual(first.status_code, 200)
        first.get_data(as_text=True)
        self.assertEqual(len(self.store.home_thread), 2)

        second = self.client.post("/api/chat", json={"regenerate": True})
        self.assertEqual(second.status_code, 200)
        second.get_data(as_text=True)
        self.assertEqual(len(self.store.home_thread), 2)
        self.assertEqual([item.who for item in self.store.home_thread], ["You", "Orbit"])
        self.assertEqual(self.store.home_thread[0].body, "Check this answer again.")
        self.assertEqual(len(self.app.config["PROVIDER"].calls), 2)

    def test_favicon_and_wordmark_use_the_shared_svg_mark(self):
        body = self.client.get("/").get_data(as_text=True)
        mark_url = "/static/orbit-mark.svg"
        self.assertIn(f'<link rel="icon" type="image/svg+xml" href="{mark_url}" />', body)
        self.assertIn(f'<img class="brand-mark" src="{mark_url}" alt="" aria-hidden="true" />', body)

        mark = self.client.get(mark_url)
        self.assertEqual(mark.status_code, 200)
        svg = mark.get_data(as_text=True)
        mark.close()
        self.assertIn('viewBox="0 0 64 64"', svg)
        self.assertIn('rotate(25 32 32)', svg)
        self.assertIn('rotate(-30 32 32)', svg)

    def test_only_github_shown_as_connected(self):
        resp = self.client.get("/")
        body = resp.get_data(as_text=True)
        # GitHub is connected.
        self.assertIn("tool-pill connected", body)
        self.assertIn("GitHub", body)
        # Linear/Vercel are present but explicitly NOT connected (never faked active).
        self.assertIn("Linear", body)
        self.assertIn("Vercel", body)
        self.assertIn("tool-pill not-connected", body)
        # Exactly one active connection.
        self.assertIn("Connected apps · 1 active", body)
        # Sanity: there is no "connected" pill for Linear or Vercel.
        self.assertEqual(body.count("tool-pill connected"), 1)

    def test_task_renders_steps_changes_activity(self):
        resp = self.client.get("/task/T1")
        self.assertEqual(resp.status_code, 200)
        body = resp.get_data(as_text=True)
        self.assertIn("Add order tracking to WhatsApp bot", body)
        self.assertIn("Read repository context", body)  # a step
        self.assertIn("orders.py", body)  # a proposed change file
        self.assertIn("Running tests against the updated order lookup", body)  # activity
        # The approval gate note is present and verbatim.
        self.assertIn("No commits will be pushed without your explicit approval.", body)
        self.assertIn("No pushes without approval", body)
        self.assertIn("Orbit is working", body)

    def test_unknown_task_404(self):
        resp = self.client.get("/task/NOPE")
        self.assertEqual(resp.status_code, 404)

    def test_mode_toggle_sets_state(self):
        resp = self.client.post(
            "/api/mode", json={"mode": "agent"}, content_type="application/json"
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json()["mode"], "agent")
        self.assertEqual(self.store.mode, "agent")

    def test_mode_toggle_rejects_invalid(self):
        resp = self.client.post(
            "/api/mode", json={"mode": "banana"}, content_type="application/json"
        )
        self.assertEqual(resp.status_code, 400)

    def test_thinking_level_defaults_to_quick_and_can_be_changed(self):
        self.assertEqual(self.store.thinking_level, "quick")
        home = self.client.get("/").get_data(as_text=True)
        self.assertIn('data-thinking-level="quick" aria-pressed="true"', home)
        self.assertIn('data-thinking-level="deep" aria-pressed="false"', home)

        response = self.client.post("/api/thinking-level", json={"thinking_level": "deep"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json(), {"thinking_level": "deep"})
        self.assertEqual(self.store.thinking_level, "deep")
        task = self.client.get("/task/T1").get_data(as_text=True)
        self.assertIn('data-thinking-level="deep" aria-pressed="true"', task)

    def test_thinking_level_rejects_unknown_values(self):
        response = self.client.post("/api/thinking-level", json={"thinking_level": "extreme"})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.store.thinking_level, "quick")


class ApprovalGateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.store = Store()
        self.app = create_app(provider=FakeStreamingProvider(), store=self.store)
        self.client = self.app.test_client()

    def test_apply_before_approval_is_refused(self):
        resp = self.client.post(
            "/api/task/T1/apply", json={}, content_type="application/json"
        )
        self.assertEqual(resp.status_code, 403)
        self.assertFalse(resp.get_json()["approved"])
        self.assertIn("explicit approval", resp.get_json()["error"])

    def test_approve_requires_explicit_true(self):
        # Missing flag -> rejected.
        resp = self.client.post(
            "/api/task/T1/approve", json={}, content_type="application/json"
        )
        self.assertEqual(resp.status_code, 400)
        # Wrong value -> rejected.
        resp = self.client.post(
            "/api/task/T1/approve",
            json={"approve": "yes"},
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, 400)
        # Task must still be unapproved.
        self.assertFalse(self.store.get_task("T1").approved)

    def test_explicit_approve_then_apply_succeeds(self):
        resp = self.client.post(
            "/api/task/T1/approve",
            json={"approve": True},
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.get_json()["approved"])
        self.assertTrue(self.store.get_task("T1").approved)

        resp = self.client.post(
            "/api/task/T1/apply", json={}, content_type="application/json"
        )
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.get_json()["applied"])


class StreamingEndpointTests(unittest.TestCase):
    def setUp(self) -> None:
        self.store = Store()
        self.app = create_app(provider=FakeStreamingProvider(), store=self.store)
        self.client = self.app.test_client()

    def test_home_chat_streams_answer(self):
        resp = self.client.post(
            "/api/chat", json={"message": "Hello Orbit"}, content_type="application/json"
        )
        self.assertEqual(resp.status_code, 200)
        self.assertIn("text/event-stream", resp.content_type)
        events = parse_sse(resp.get_data(as_text=True))
        kinds = [e[0] for e in events]
        self.assertIn("answer", kinds)
        # "done" ends the reply. A naming event may follow it, never precede it.
        self.assertIn("done", kinds)
        self.assertEqual(kinds[kinds.index("done")], "done")
        self.assertTrue(all(k in ("done", "title") for k in kinds[kinds.index("done") :]))
        answer = "".join(d for k, d in events if k == "answer")
        self.assertIn("Hello from Orbit.", answer)
        # The reply was persisted to the home thread.
        self.assertTrue(any(m.is_orbit for m in self.store.home_thread))

    def test_task_stream_streams_thinking_then_answer(self):
        resp = self.client.post(
            "/api/task/T1/stream",
            json={"initial": True},
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, 200)
        events = parse_sse(resp.get_data(as_text=True))
        kinds = [e[0] for e in events]
        self.assertIn("thinking", kinds)
        self.assertIn("answer", kinds)
        self.assertEqual(kinds[0], "thinking")
        thinking = "".join(d for k, d in events if k == "thinking")
        self.assertIn("checking the repository", thinking)
        # Persisted as an Orbit chat message.
        task = self.store.get_task("T1")
        assert task is not None
        self.assertTrue(any(m.is_orbit for m in task.chat))

    def test_task_initial_response_can_be_regenerated_without_duplicate_messages(self):
        first = self.client.post("/api/task/T1/stream", json={"initial": True})
        first.get_data(as_text=True)
        task = self.store.get_task("T1")
        assert task is not None
        self.assertEqual(len(task.chat), 2)

        retry = self.client.post("/api/task/T1/stream", json={"regenerate": True})
        retry.get_data(as_text=True)
        self.assertEqual(len(task.chat), 2)
        self.assertEqual(sum(item.is_orbit for item in task.chat), 1)
        self.assertEqual(len(self.app.config["PROVIDER"].calls), 2)
        for call in self.app.config["PROVIDER"].calls:
            self.assertIn("Start the task:", call["history"][-1].content)

    def test_task_stream_requires_message_when_not_initial(self):
        resp = self.client.post(
            "/api/task/T1/stream", json={}, content_type="application/json"
        )
        self.assertEqual(resp.status_code, 400)

    def test_opening_turn_is_offered_once_not_once_per_refresh(self) -> None:
        """A refresh must not spend another model call to repeat itself.

        The opening update is generated on page load, so without this flag every
        reload would call the model again and append a duplicate message to the
        task thread.
        """
        body = self.client.get("/task/T1").get_data(as_text=True)
        self.assertIn("initialTurn: true", body)

        # The opening turn only completes once the client reads the stream, so
        # consume it here the way a browser would.
        streamed = self.client.post(
            "/api/task/T1/stream", json={"initial": True}, content_type="application/json"
        )
        self.assertIn("event: answer", streamed.get_data(as_text=True))
        task = self.store.get_task("T1")
        assert task is not None
        self.assertTrue(any(m.is_orbit for m in task.chat), "opening reply was persisted")

        body = self.client.get("/task/T1").get_data(as_text=True)
        self.assertIn("initialTurn: false", body)


class SilentFailureTests(unittest.TestCase):
    """A turn that produces nothing must not look like an idle UI.

    These are the two shapes the web chat took when it "just sat there": a
    provider that yielded no chunks, and a provider that raised before its
    first chunk. Both must reach the page as an event, not as silence.
    """

    def _events(self, provider: Any) -> tuple[list[tuple[str, str]], Store, Path]:
        store = Store()
        temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        audit_path = Path(temp_dir.name) / "audit.log"
        app = create_app(provider=provider, store=store, model_audit=AuditLog(audit_path))
        resp = app.test_client().post(
            "/api/chat", json={"message": "Hello Orbit"}, content_type="application/json"
        )
        self.assertEqual(resp.status_code, 200)
        return parse_sse(resp.get_data(as_text=True)), store, audit_path

    def test_empty_reply_is_reported_instead_of_streaming_nothing(self) -> None:
        class EmptyProvider:
            def reply_stream(self, history, system_prompt, *, think=False, report_attempts=False, thinking_level="quick"):
                return iter(())

        events, store, audit_path = self._events(EmptyProvider())
        kinds = [k for k, _ in events]
        self.assertIn("error", kinds)
        self.assertEqual(kinds[-1], "done")
        detail = "".join(d for k, d in events if k == "error")
        self.assertIn("empty reply", detail)
        self.assertFalse(any(m.is_orbit for m in store.home_thread))
        audit_events = [json.loads(line) for line in audit_path.read_text(encoding="utf-8").splitlines()]
        fallback = next(event for event in audit_events if event["event"] == "model_returned_nothing")
        self.assertEqual(fallback["reason"], "empty_stream")

    def test_provider_failure_before_first_chunk_reaches_the_ui(self) -> None:
        class BrokenProvider:
            def reply_stream(self, history, system_prompt, *, think=False, report_attempts=False, thinking_level="quick"):
                raise RuntimeError("GEMINI_API_KEY is not configured.")

        events, store, audit_path = self._events(BrokenProvider())
        kinds = [k for k, _ in events]
        self.assertIn("error", kinds)
        self.assertEqual(kinds[-1], "done")
        detail = "".join(d for k, d in events if k == "error")
        self.assertIn("GEMINI_API_KEY", detail)
        self.assertFalse(any(m.is_orbit for m in store.home_thread))
        raw_audit = audit_path.read_text(encoding="utf-8")
        audit_events = [json.loads(line) for line in raw_audit.splitlines()]
        failure = next(event for event in audit_events if event["event"] == "model_stream_failed")
        self.assertEqual(failure["reason"], "RuntimeError")
        self.assertNotIn("GEMINI_API_KEY", raw_audit)


class ChatBubbleLayoutTests(unittest.TestCase):
    """Speakers are told apart by position and marker, not by name labels.

    Rule (UI request, chat area): no "You" / "Orbit" text labels. User bubbles
    are right-aligned (class ``user``); Orbit replies are plain, unboxed text
    with the orbital mark in front as the only speaker marker.
    """

    def setUp(self) -> None:
        from app.store import ChatMessage as StoreChatMessage

        self.store = Store()
        self.app = create_app(provider=FakeStreamingProvider(), store=self.store)
        self.client = self.app.test_client()
        self.store.add_chat(
            "T1",
            StoreChatMessage(who="Orbit", time="09:42", body="On it.", is_orbit=True),
        )

    def test_task_side_chat_has_no_name_labels(self) -> None:
        body = self.client.get("/task/T1").get_data(as_text=True)
        self.assertNotIn('class="who"', body)
        self.assertNotIn("You · ", body)

    def test_each_message_is_its_own_row_with_a_time_line(self) -> None:
        body = self.client.get("/task/T1").get_data(as_text=True)
        # One row per message; the time sits under the bubble, not beside a label.
        self.assertRegex(
            body,
            r'(?s)<div class="chat-row orbit">\s*<div class="chat-bubble orbit">.*?</div>\s*'
            r'<div class="chat-meta">09:42</div>\s*</div>',
        )

    def test_user_messages_are_right_aligned_accent_bubbles(self) -> None:
        from app.store import ChatMessage as StoreChatMessage

        self.store.add_chat(
            "T1",
            StoreChatMessage(who="You", time="09:41", body="Add order status tracking", is_orbit=False),
        )
        body = self.client.get("/task/T1").get_data(as_text=True)
        self.assertIn('<div class="chat-row user">', body)
        self.assertIn('class="chat-bubble user"', body)
        self.assertRegex(body, r'<div class="chat-meta">09:41</div>')
        self.assertIn("Add order status tracking", body)

    def test_orbit_messages_are_unboxed_and_have_full_reply_width(self) -> None:
        css = (Path(__file__).resolve().parent.parent / "app" / "templates" / "layout.html").read_text(
            encoding="utf-8"
        )
        orbit = re.search(r"\.chat-bubble\.orbit\s*\{([^}]*)\}", css)
        self.assertIsNotNone(orbit)
        self.assertRegex(orbit.group(1), r"background:\s*transparent")
        self.assertRegex(orbit.group(1), r"border:\s*0")
        self.assertRegex(orbit.group(1), r"padding:\s*0")
        self.assertRegex(orbit.group(1), r"width:\s*100%")
        self.assertRegex(css, r"\.chat-row\.orbit\s*\{[^}]*width:\s*100%")
        self.assertRegex(css, r"\.chat-bubble\.orbit \.body\s*\{[^}]*white-space:\s*pre-wrap")

        user = re.search(r"\.chat-bubble\.user\s*\{([^}]*)\}", css)
        self.assertIsNotNone(user)
        self.assertIn("background: var(--accent)", user.group(1))

    def test_orbit_messages_are_left_aligned_with_orbital_mark_in_text(self) -> None:
        body = self.client.get("/task/T1").get_data(as_text=True)
        self.assertIn('class="chat-bubble orbit"', body)
        # The shared brand SVG is inside the body, before Orbit's text.
        self.assertRegex(body, r'<div class="body"><img class="orbit-icon"[^>]*><span class="reply">On it\.</span></div>')

    def test_script_builds_rows_and_time_lines(self) -> None:
        from pathlib import Path

        script = (Path(__file__).resolve().parent.parent / "app" / "static" / "app.js").read_text(
            encoding="utf-8"
        )
        self.assertNotIn('"who"', script)
        self.assertNotIn("You · just now", script)
        self.assertIn('"chat-row " + side', script)
        self.assertIn('"chat-meta"', script)
        self.assertIn('"chat-bubble " + side', script)
        # The home thread's pending check must look at rows, not bare bubbles.
        self.assertIn('".chat-row.orbit:last-child .body"', script)

    def test_voice_controls_record_and_use_the_ordinary_chat_send_handlers(self) -> None:
        home = self.client.get("/").get_data(as_text=True)
        task = self.client.get("/task/T1").get_data(as_text=True)
        script = (Path(__file__).resolve().parent.parent / "app" / "static" / "app.js").read_text(
            encoding="utf-8"
        )
        self.assertIn('id="homeVoice"', home)
        self.assertIn('id="taskVoice"', task)
        self.assertIn("navigator.mediaDevices.getUserMedia({ audio: true })", script)
        self.assertIn("new window.MediaRecorder(stream", script)
        self.assertIn('fetch("/api/voice/transcribe"', script)
        self.assertIn('form.append("audio", blob', script)
        self.assertIn("sendMessage({ voiceReply: true })", script)
        self.assertIn("sendSteer({ voiceReply: true })", script)
        self.assertIn('fetch("/api/voice/speech"', script)
        self.assertIn("body.appendChild(audio)", script)  # timestamp stays beneath text and player
        self.assertIn('window.MediaSource.isTypeSupported("audio/mpeg")', script)
        self.assertIn("response.blob()", script)  # playback fallback when MSE is absent


class ServerMessageTimeTests(unittest.TestCase):
    """Messages created by the server carry the real local time, not a placeholder."""

    def test_home_chat_stores_hh_mm_for_both_sides(self) -> None:
        store = Store()
        app = create_app(provider=FakeStreamingProvider(), store=store)
        resp = app.test_client().post(
            "/api/chat", json={"message": "Hello Orbit"}, content_type="application/json"
        )
        resp.get_data(as_text=True)
        self.assertEqual(len(store.home_thread), 2)
        for msg in store.home_thread:
            self.assertRegex(msg.time, r"^\d{2}:\d{2}$")
            self.assertNotEqual(msg.time, "now")
        self.assertEqual(store.home_thread[0].time, store.home_thread[1].time)

    def test_now_label_is_local_hh_mm(self) -> None:
        from datetime import datetime

        from app.store import now_label

        self.assertRegex(now_label(), r"^\d{2}:\d{2}$")
        self.assertEqual(now_label(), datetime.now().strftime("%H:%M"))


class DesignMockupSyncTests(unittest.TestCase):
    """design/orbit_mockup.html is the spec: its palette and chat markup must match the app."""

    ROOT = Path(__file__).resolve().parent.parent

    def _root_vars(self, text: str) -> dict[str, str]:
        block = re.search(r":root\s*\{(.*?)\}", text, re.S)
        self.assertIsNotNone(block, "no :root block found")
        return dict(re.findall(r"(--[\w-]+)\s*:\s*([^;]+);", block.group(1)))

    def test_accent_tokens_match_between_app_and_mockup(self) -> None:
        app_text = (self.ROOT / "app" / "templates" / "layout.html").read_text(encoding="utf-8")
        mock_text = (self.ROOT / "design" / "orbit_mockup.html").read_text(encoding="utf-8")
        app_vars, mock_vars = self._root_vars(app_text), self._root_vars(mock_text)
        for token in ("--accent", "--accent-bright", "--accent-dim", "--accent-wash", "--accent-soft"):
            self.assertIn(token, app_vars)
            self.assertEqual(
                mock_vars.get(token), app_vars[token].strip(), f"{token} differs between app and mockup"
            )

    def test_mockup_chat_uses_rows_and_has_no_name_labels(self) -> None:
        mock_text = (self.ROOT / "design" / "orbit_mockup.html").read_text(encoding="utf-8")
        self.assertNotIn('class="who"', mock_text)
        self.assertNotIn("You · ", mock_text)
        self.assertIn('class="chat-row user"', mock_text)
        self.assertIn('class="chat-row orbit"', mock_text)
        self.assertIn('class="chat-meta"', mock_text)


def _root_tokens(path: Path) -> dict[str, str]:
    text = path.read_text(encoding="utf-8")
    block = re.search(r":root\s*\{(.*?)\}", text, re.S)
    assert block is not None
    return {name: value.strip() for name, value in re.findall(r"(--[\w-]+)\s*:\s*([^;]+);", block.group(1))}


def _hex_luminance(color: str) -> float:
    """WCAG relative luminance of a #rrggbb colour."""
    channels = [int(color[i : i + 2], 16) / 255 for i in (1, 3, 5)]
    linear = [c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4 for c in channels]
    return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]


def _contrast(a: str, b: str) -> float:
    hi, lo = sorted((_hex_luminance(a), _hex_luminance(b)), reverse=True)
    return (hi + 0.05) / (lo + 0.05)


class PaletteAndBubbleTests(unittest.TestCase):
    """Dark-blue accent, readable text on it, and the bubble markup the UI relies on."""

    ROOT = Path(__file__).resolve().parent.parent

    def setUp(self) -> None:
        self.tokens = _root_tokens(self.ROOT / "app" / "templates" / "layout.html")
        self.store = Store()
        self.client = create_app(provider=FakeStreamingProvider(), store=self.store).test_client()

    def test_accent_is_dark_blue_not_copper(self) -> None:
        for token in ("--accent", "--accent-dim", "--accent-bright"):
            hex_value = self.tokens[token]
            red, green, blue = (int(hex_value[i : i + 2], 16) for i in (1, 3, 5))
            self.assertGreater(blue, red, f"{token} must be blue, not warm/copper")
            self.assertGreater(blue, green, f"{token} must be blue, not warm/copper")
        self.assertLess(_hex_luminance(self.tokens["--accent"]), 0.1, "--accent must be dark")

    def test_text_on_the_accent_bubble_is_readable(self) -> None:
        self.assertGreaterEqual(_contrast(self.tokens["--text"], self.tokens["--accent"]), 4.5)

    def test_accent_labels_are_readable_on_the_navy_surfaces(self) -> None:
        for surface in ("--bg", "--bg-raised", "--bg-card"):
            self.assertGreaterEqual(
                _contrast(self.tokens["--accent-bright"], self.tokens[surface]), 4.5, surface
            )

    def test_home_thread_renders_every_orbit_message_with_orbital_mark(self) -> None:
        from app.store import ChatMessage as StoreChatMessage

        self.store.start_conversation()
        self.store.home_thread.extend(
            [
                StoreChatMessage(who="You", time="09:41", body="First question", is_orbit=False),
                StoreChatMessage(who="Orbit", time="09:41", body="First answer", is_orbit=True),
                StoreChatMessage(who="Orbit", time="09:43", body="Second answer", is_orbit=True),
            ]
        )
        body = self.client.get("/").get_data(as_text=True)
        self.assertIn('<div class="chat-row user">', body)
        self.assertEqual(body.count('<div class="chat-row orbit">'), 2)
        # Every Orbit body starts with the shared logo mark, not only while streaming.
        self.assertEqual(
            len(re.findall(r'<div class="body"><img class="orbit-icon"[^>]*><span class="reply">', body, re.S)),
            2,
        )
        self.assertRegex(body, r'<div class="chat-meta">09:43</div>')

    def test_no_speaker_name_labels_on_either_screen(self) -> None:
        for path in ("/", "/task/T1"):
            body = self.client.get(path).get_data(as_text=True)
            self.assertNotIn("YOU:", body, path)
            self.assertNotIn("ORBIT:", body, path)
            self.assertNotIn('class="who"', body, path)


class AgentScrollbarTests(unittest.TestCase):
    """Scrollbars are hidden on the Agent panels, but those panels still scroll."""

    ROOT = Path(__file__).resolve().parent.parent
    PANELS = (".task-work", ".task-chat-log", ".home-thread", ".step-row", ".activity-section", ".change-list")

    def _css(self, name: str) -> str:
        text = (self.ROOT / name).read_text(encoding="utf-8")
        return re.sub(r"/\*.*?\*/", "", text, flags=re.S)  # comments would bleed into selectors

    def _rules_for(self, css: str, selector: str) -> list[str]:
        """Every declaration block whose selector list names this panel exactly."""
        blocks = re.findall(r"([^{}]+)\{([^{}]*)\}", css)
        found = []
        for head, body in blocks:
            names = [part.strip() for part in head.split(",")]
            if any(re.fullmatch(re.escape(selector) + r"(::-webkit-scrollbar)?", n) for n in names):
                found.append(body)
        return found

    def test_every_panel_hides_its_scrollbar_in_all_three_engines(self) -> None:
        for name in ("app/templates/layout.html", "design/orbit_mockup.html"):
            css = self._css(name)
            self.assertIn("scrollbar-width: none", css, name)
            self.assertIn("-ms-overflow-style: none", css, name)
            self.assertRegex(css, r"::-webkit-scrollbar\s*\{\s*display:\s*none;", name)
            for panel in self.PANELS:
                self.assertTrue(
                    any("scrollbar-width: none" in body for body in self._rules_for(css, panel)),
                    f"{name}: {panel} lacks scrollbar-width: none",
                )
                self.assertTrue(
                    any(re.search(r"-ms-overflow-style:\s*none", body) for body in self._rules_for(css, panel)),
                    f"{name}: {panel} lacks -ms-overflow-style: none",
                )
                webkit = [b for b in self._rules_for(css, panel + "::-webkit-scrollbar")]
                self.assertTrue(webkit, f"{name}: {panel} lacks a ::-webkit-scrollbar rule")

    def test_scrolling_is_not_disabled_on_the_scroll_containers(self) -> None:
        css = self._css("app/templates/layout.html")
        for panel in (".task-work", ".task-chat-log", ".home-thread"):
            bodies = self._rules_for(css, panel)
            self.assertTrue(bodies, panel)
            for body in bodies:
                self.assertNotRegex(body, r"overflow(-[xy])?:\s*hidden", panel)
        self.assertRegex(self._rules_for(css, ".task-work")[0], r"overflow-y:\s*auto")
        self.assertRegex(self._rules_for(css, ".task-chat-log")[0], r"overflow-y:\s*auto")

    def test_the_proposed_changes_list_is_a_named_panel_in_the_markup(self) -> None:
        body = self.client_body()
        self.assertRegex(body, r'<div class="change-list">\s*<div class="change-row">')

    def client_body(self) -> str:
        store = Store()
        return create_app(provider=FakeStreamingProvider(), store=store).test_client().get("/task/T1").get_data(as_text=True)


class RecordingProvider:
    """Yields the given pieces as answers, recording how many have left so far."""

    def __init__(self, pieces: list[str]) -> None:
        self.pieces = pieces
        self.produced = 0
        self.calls: list[dict[str, Any]] = []

    def reply(self, history, system_prompt: str) -> str:
        return "".join(self.pieces)

    def reply_stream(self, history, system_prompt: str, *, think: bool = False, report_attempts: bool = False, thinking_level: str = "quick"):
        self.calls.append({"think": think, "report_attempts": report_attempts, "thinking_level": thinking_level})
        if report_attempts:
            yield StreamChunk(kind="status", text="Asking Titan…")
        for piece in self.pieces:
            self.produced += 1
            yield StreamChunk(kind="answer", text=piece)


def events_as_they_arrive(client, url: str, body: dict, provider: RecordingProvider):
    """Read a streamed response piece by piece; return (event, pieces produced at arrival)."""
    resp = client.post(url, json=body, buffered=False)
    buffer = ""
    seen: list[tuple[str, int]] = []
    for piece in resp.response:
        buffer += piece.decode("utf-8") if isinstance(piece, bytes) else piece
        while "\n\n" in buffer:
            block, buffer = buffer.split("\n\n", 1)
            for event, _data in parse_sse(block + "\n\n"):
                seen.append((event, provider.produced))
    return seen


class RouteStreamingTests(unittest.TestCase):
    PIECES = ["Hel", "lo ", "there, ", "Caleb."]

    def _client(self, provider, mode: str = "chat"):
        store = Store()
        store.set_mode(mode)
        return create_app(provider=provider, store=store).test_client()

    def test_home_answer_reaches_the_client_piece_by_piece(self) -> None:
        provider = RecordingProvider(self.PIECES)
        seen = events_as_they_arrive(self._client(provider), "/api/chat", {"message": "hi"}, provider)
        answers = [produced for event, produced in seen if event == "answer"]
        self.assertEqual(len(answers), len(self.PIECES))
        self.assertLess(answers[0], len(self.PIECES))  # first piece left before the end

    def test_task_answer_reaches_the_client_piece_by_piece(self) -> None:
        provider = RecordingProvider(self.PIECES)
        seen = events_as_they_arrive(
            self._client(provider), "/api/task/T1/stream", {"message": "steer"}, provider
        )
        answers = [produced for event, produced in seen if event == "answer"]
        self.assertEqual(len(answers), len(self.PIECES))
        self.assertLess(answers[0], len(self.PIECES))

    def test_selected_deep_level_is_passed_to_both_chat_routes(self) -> None:
        for url, payload in (
            ("/api/chat", {"message": "deep question"}),
            ("/api/task/T1/stream", {"message": "deep steering"}),
        ):
            provider = RecordingProvider(self.PIECES)
            store = Store()
            store.set_thinking_level("deep")
            client = create_app(provider=provider, store=store).test_client()
            client.post(url, json=payload).get_data(as_text=True)
            self.assertEqual(provider.calls[0]["thinking_level"], "deep")

    def test_chat_mode_never_asks_for_status_lines(self) -> None:
        provider = RecordingProvider(self.PIECES)
        seen = events_as_they_arrive(self._client(provider), "/api/chat", {"message": "hi"}, provider)
        self.assertNotIn("status", [event for event, _ in seen])
        self.assertEqual(provider.calls, [{"think": False, "report_attempts": False, "thinking_level": "quick"}])

    def test_agent_mode_asks_for_status_lines_and_shows_them(self) -> None:
        provider = RecordingProvider(self.PIECES)
        seen = events_as_they_arrive(
            self._client(provider, mode="agent"), "/api/task/T1/stream", {"message": "steer"}, provider
        )
        self.assertEqual(provider.calls, [{"think": True, "report_attempts": True, "thinking_level": "quick"}])
        self.assertIn("status", [event for event, _ in seen])
        self.assertEqual(seen[0][0], "status")

    def test_chat_mode_task_view_is_unlabeled(self) -> None:
        provider = RecordingProvider(self.PIECES)
        seen = events_as_they_arrive(
            self._client(provider, mode="chat"), "/api/task/T1/stream", {"message": "steer"}, provider
        )
        self.assertEqual(provider.calls, [{"think": True, "report_attempts": False, "thinking_level": "quick"}])
        self.assertNotIn("status", [event for event, _ in seen])


if __name__ == "__main__":
    unittest.main()
