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
        self.assertIn("<title>orbit</title>", svg)

    def test_ui_loads_space_grotesk_and_inter_as_css_custom_properties(self):
        body = self.client.get("/").get_data(as_text=True)
        self.assertIn("fonts.googleapis.com/css2?family=Inter:wght@400;500;600", body)
        self.assertIn("family=Space+Grotesk:wght@500;600;700", body)
        self.assertIn("--font-display: 'Space Grotesk'", body)
        self.assertIn("--font-body: 'Inter'", body)
        self.assertIn("font-family: var(--font-body)", body)
        self.assertIn(".brand-word { font-family: var(--font-display)", body)
        self.assertIn("h1, h2, h3, h4,", body)
        self.assertRegex(body, r"\.sidebar-brand \{[^}]*font-family: var\(--font-display\)")

    def test_visual_wordmark_is_always_lowercase_orbit(self):
        home = self.client.get("/").get_data(as_text=True)
        self.assertIn("<title>orbit — Home</title>", home)
        self.assertIn('<span class="brand-word">orbit</span>', home)
        self.assertNotIn('<span class="brand-word">ORBIT</span>', home)
        self.assertNotIn('<span class="brand-word">Orbit</span>', home)
        self.assertIn('content: "orbit"', home)
        analytics = self.client.get("/analytics").get_data(as_text=True)
        self.assertIn("<title>orbit — Analytics</title>", analytics)
        missing = self.client.get("/task/NOPE").get_data(as_text=True)
        self.assertIn("<title>orbit — Task not found</title>", missing)

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

    def test_task_panel_is_planned_from_the_request(self):
        """The checklist comes from this request's wording, not a fixed workflow."""
        resp = self.client.get("/task/T1")
        self.assertEqual(resp.status_code, 200)
        body = resp.get_data(as_text=True)
        self.assertIn("Add order tracking to WhatsApp bot", body)
        # Steps are derived from the request: its own clause, and a verify step
        # because it asks for a change and mentions no tests.
        self.assertIn("Find the code for a status field", body)
        self.assertIn("Plan how to add a status field", body)
        self.assertIn("Summarize it for your review", body)
        self.assertNotIn("Read repository context", body)  # the old fixed step list
        # Files come from the request text and say where the name came from.
        self.assertIn("orders.py", body)
        self.assertIn("your request", body)
        # The approval gate note is present and verbatim.
        self.assertIn("No commits will be pushed without your explicit approval.", body)
        self.assertIn("No pushes without approval", body)

    def test_a_task_that_has_not_run_has_no_panel(self):
        """No run means no panel: no standing empty checklist on the screen."""
        from app.store import ChatMessage as StoreChatMessage

        task = self.store.get_task("T1")
        assert task is not None
        task.chat.append(StoreChatMessage(who="Orbit", time="09:42", body="On it.", is_orbit=True))
        body = self.client.get("/task/T1").get_data(as_text=True)
        self.assertIn('<section class="task-panel" id="taskPanel" aria-live="polite" hidden>', body)
        self.assertIn('<div class="task-start" id="taskStart">', body)
        # The start control is real, and the seeded fake numbers are gone.
        self.assertIn('id="startTaskBtn"', body)
        for fake in ("6 files scanned", "38 → 61", "Running tests against the updated order lookup"):
            self.assertNotIn(fake, body)

    def test_a_planned_run_is_not_reported_as_in_progress(self):
        """The panel can draw its checklist before the stream starts. It must not
        say IN PROGRESS until something is actually running."""
        created = self.client.post("/api/tasks", json={"request": "Fix the crash in checkout.py."}).get_json()
        body = self.client.get("/task/" + created["id"]).get_data(as_text=True)
        self.assertIn("taskRunState\">NOT STARTED", body)
        self.assertIn('id="workingIndicator" hidden', body)
        self.assertIn("currentOperation\">Starting…", body)
        # The plan is still there to look at — it just is not claimed as done.
        self.assertIn("Find the code for the crash", body)

    def test_the_working_indicator_only_appears_while_a_run_is_in_flight(self):
        """A status that says Orbit is working when it is idle is not a status."""
        from app.store import ChatMessage as StoreChatMessage

        task = self.store.get_task("T1")
        assert task is not None
        task.chat.append(StoreChatMessage(who="Orbit", time="09:42", body="On it.", is_orbit=True))
        body = self.client.get("/task/T1").get_data(as_text=True)
        self.assertIn('id="workingIndicator" hidden', body)
        self.assertNotIn(">Orbit is working<", body)

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
        # The panel's opening state leads, so the client can draw the plan
        # before the first word arrives; narration still precedes the answer.
        self.assertEqual(kinds[0], "task_state")
        self.assertLess(kinds.index("thinking"), kinds.index("answer"))
        thinking = "".join(d for k, d in events if k == "thinking")
        self.assertIn("checking the repository", thinking)
        # Persisted as an Orbit chat message.
        task = self.store.get_task("T1")
        assert task is not None
        self.assertTrue(any(m.is_orbit for m in task.chat))
        # The panel's state is derived from this run, not seeded: the narration
        # mentions reading the repository, so a step moved and says why.
        states = [json.loads(d) for k, d in events if k == "task_state"]
        self.assertTrue(states, "the task stream must report panel state")
        final = states[-1]
        self.assertEqual(final["status"], "idle")
        self.assertIn("Waiting for your input", final["operation"])
        moved = [s for s in final["steps"] if s["status"] != "pending"]
        self.assertTrue(moved, "narration that reads the code should move a step")
        self.assertTrue(moved[0]["evidence"], "a moved step keeps the line that moved it")
        self.assertTrue(final["activity"], "the run's real events are reported")

    def test_a_task_run_marks_no_step_without_evidence(self):
        """A turn that narrates nothing leaves every step pending."""

        class Blank:
            calls: list[dict] = []

            def reply_stream(self, history, system_prompt, **kwargs):
                Blank.calls.append({"think": kwargs.get("think")})
                yield StreamChunk(kind="answer", text="Nothing to narrate.")

        app = create_app(provider=Blank(), store=self.store)
        events = parse_sse(
            app.test_client().post("/api/task/T1/stream", json={"initial": True}).get_data(as_text=True)
        )
        states = [json.loads(d) for k, d in events if k == "task_state"]
        final = states[-1]
        self.assertEqual([s["status"] for s in final["steps"]], ["pending"] * len(final["steps"]))
        # Files the request named are still listed; nothing Orbit never said was
        # added to them.
        self.assertEqual({f["source"] for f in final["files"]}, {"request"})

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
        # Stored messages also carry the id their row actions act on.
        self.assertRegex(
            body,
            r'(?s)<div class="chat-row orbit"(?: data-message-id="[^"]*")?>\s*'
            r'<div class="chat-bubble orbit">.*?</div>\s*'
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
        self.assertIn('id="homeVoiceExpand"', home)
        self.assertIn('id="voiceChatView"', home)
        self.assertIn('id="voiceChatBack"', home)
        self.assertIn('id="voiceViewRings"', home)
        self.assertEqual(home.count('id="homeVoice"'), 1)
        self.assertIn("voiceViewMic.appendChild(voiceButton)", script)
        self.assertIn("voiceHomeParent.insertBefore(voiceButton, voiceHomeNextSibling)", script)
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
        # A voice failure shows the server's specific reason beside the stable line,
        # not only the generic sentence.
        self.assertIn("function voiceFailureText(payload, fallback)", script)
        self.assertEqual(script.count('voiceFailureText(payload, "'), 2)  # transcribe + speech

    def test_focused_voice_view_reuses_existing_orbit_ring_state_styles(self) -> None:
        root = Path(__file__).resolve().parent.parent
        home = self.client.get("/").get_data(as_text=True)
        script = (root / "app" / "static" / "app.js").read_text(encoding="utf-8")
        styles = (root / "app" / "templates" / "layout.html").read_text(encoding="utf-8")
        self.assertIn('aria-label="Back to text chat"', home)
        self.assertIn('aria-modal="true"', home)
        self.assertIn("voiceChatBack.focus()", script)
        self.assertIn('event.key === "Escape"', script)
        self.assertIn('event.key !== "Tab"', script)
        self.assertIn('voiceViewRings.setAttribute("data-state", state || "idle")', script)
        for state in ("idle", "listening", "thinking", "speaking"):
            self.assertIn('data-state="' + state + '"', styles)
        # All four focused states reuse the existing orbital turn keyframe.
        self.assertIn("animation: orbit-ring-turn 20s linear infinite", styles)
        self.assertIn("animation: orbit-ring-turn 1.6s linear infinite", styles)
        self.assertIn("animation: orbit-ring-turn 8s linear infinite", styles)
        self.assertIn('reportPlayback("speaking", "Speaking…"', script)


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
    PANELS = (".task-work", ".task-chat-log", ".home-thread", ".activity-section")

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

    def test_the_agent_panel_is_a_named_panel_in_the_markup(self) -> None:
        body = self.client_body()
        self.assertRegex(body, r'<section class="task-panel" id="taskPanel"[^>]*>\s*<div class="panel-section">')
        self.assertIn('<ol class="checklist" id="stepChecklist">', body)

    def client_body(self) -> str:
        store = Store()
        return create_app(provider=FakeStreamingProvider(), store=store).test_client().get("/task/T1").get_data(as_text=True)


class CodeStreamingProvider:
    """A provider whose reply contains a code block — the one signal that earns follow-up chips."""

    PIECES = [
        "Here is the helper:\n\n```python\ndef status_for(order):\n    return order.get('status')\n```\n"
    ]

    def reply(self, history, system_prompt: str) -> str:
        return "".join(self.PIECES)

    def reply_stream(self, history, system_prompt: str, **kwargs):
        for piece in self.PIECES:
            yield StreamChunk(kind="answer", text=piece)


def _script() -> str:
    return (Path(__file__).resolve().parent.parent / "app" / "static" / "app.js").read_text(encoding="utf-8")


def _layout() -> str:
    return (Path(__file__).resolve().parent.parent / "app" / "templates" / "layout.html").read_text(encoding="utf-8")


class WelcomeScreenTests(unittest.TestCase):
    """The welcome screen belongs to a genuinely empty conversation, and only to that."""

    def setUp(self) -> None:
        self.store = Store()
        self.app = create_app(provider=FakeStreamingProvider(), store=self.store)
        self.client = self.app.test_client()

    def test_an_empty_conversation_shows_the_welcome_screen(self) -> None:
        body = self.client.get("/").get_data(as_text=True)
        self.assertIn('<div class="welcome" id="welcome">', body)
        self.assertIn("Your ideas, in orbit.", body)
        self.assertIn("Think it. Build it. Let’s work on it together.", body)

    def test_the_three_shortcuts_are_real_prompts_that_send(self) -> None:
        from app.starters import WELCOME_STARTERS

        body = self.client.get("/").get_data(as_text=True)
        self.assertEqual([s.label for s in WELCOME_STARTERS], ["Build something", "Explore an idea", "Review my work"])
        import html

        rendered = html.unescape(body)  # Jinja escapes the apostrophes in the prompts
        self.assertEqual(rendered.count('class="welcome-action" data-starter="'), 3)
        for starter in WELCOME_STARTERS:
            self.assertIn(starter.label, rendered)
            # The prompt is on the button, so a click sends a real turn.
            self.assertIn(starter.prompt, rendered)
        # And a click goes through the composer, not a decorative handler.
        self.assertIn('sendPrompt(button.getAttribute("data-starter"))', _script())

    def test_a_returning_conversation_with_history_never_shows_it(self) -> None:
        self.client.post("/api/chat", json={"message": "Hello Orbit"}).get_data(as_text=True)
        self.assertTrue(self.store.home_thread, "the turn was stored")
        body = self.client.get("/").get_data(as_text=True)
        self.assertNotIn("Your ideas, in orbit.", body)
        self.assertNotIn("Think it. Build it.", body)
        self.assertNotIn('id="welcome"', body)

    def test_the_orbit_rings_turn_slowly_idle_and_speed_up_once_orbit_responds(self) -> None:
        css = _layout()
        script = _script()
        self.assertRegex(css, r"\.rings-slow img \{ animation: orbit-ring-turn 20s")
        self.assertRegex(css, r"\.rings-fast img \{ animation: orbit-ring-turn 1\.6s")
        # Same mark, faster: the responding state re-times the spin rather than
        # swapping in a second icon.
        self.assertRegex(css, r"\.welcome\.is-responding \.welcome-rings img \{ animation: orbit-ring-turn 1\.6s")
        self.assertIn('welcome.classList.add("is-responding")', script)
        self.assertIn('turnStatus.bar.classList.add("no-rings")', script)


class FollowupChipTests(unittest.TestCase):
    """Chips appear only when the reply gives them something real to act on."""

    def setUp(self) -> None:
        self.store = Store()
        self.app = create_app(provider=CodeStreamingProvider(), store=self.store)
        self.client = self.app.test_client()

    def test_a_reply_with_code_offers_chips_after_the_reply_lands(self) -> None:
        events = parse_sse(self.client.post("/api/chat", json={"message": "write it"}).get_data(as_text=True))
        kinds = [k for k, _ in events]
        self.assertIn("followups", kinds)
        self.assertLess(kinds.index("done"), kinds.index("followups"), "chips never hold up the reply")
        chips = json.loads([d for k, d in events if k == "followups"][0])
        self.assertEqual([c["label"] for c in chips], ["Explain this code", "Find potential bugs", "Improve implementation"])

    def test_a_plain_reply_offers_no_chips_at_all(self) -> None:
        app = create_app(provider=FakeStreamingProvider(), store=Store())
        events = parse_sse(app.test_client().post("/api/chat", json={"message": "hi"}).get_data(as_text=True))
        self.assertNotIn("followups", [k for k, _ in events])

    def test_a_failed_turn_offers_no_chips(self) -> None:
        class Broken:
            def reply_stream(self, history, system_prompt, **kwargs):
                raise RuntimeError("boom")
                yield  # pragma: no cover - keeps this a generator

        app = create_app(provider=Broken(), store=Store())
        events = parse_sse(app.test_client().post("/api/chat", json={"message": "hi"}).get_data(as_text=True))
        self.assertIn("error", [k for k, _ in events])
        self.assertNotIn("followups", [k for k, _ in events])

    def test_chips_are_rendered_above_the_composer_on_page_load(self) -> None:
        self.client.post("/api/chat", json={"message": "write it"}).get_data(as_text=True)
        body = self.client.get("/").get_data(as_text=True)
        self.assertIn('<div class="followup-chips" id="followupChips"', body)
        self.assertIn('data-followup="Walk me through the code you just wrote', body)
        self.assertNotIn('id="followupChips" aria-label="Follow-up suggestions" hidden', body)

    def test_the_row_is_hidden_not_empty_when_there_is_nothing_to_suggest(self) -> None:
        app = create_app(provider=FakeStreamingProvider(), store=Store())
        body = app.test_client().get("/").get_data(as_text=True)
        self.assertIn('id="followupChips" aria-label="Follow-up suggestions" hidden', body)

    def test_chips_clear_the_moment_a_new_turn_starts(self) -> None:
        script = _script()
        self.assertIn("renderFollowups(followupsBox, [])", script)
        self.assertIn('sendPrompt(chip.getAttribute("data-followup"))', script)


class AgentTaskCreationTests(unittest.TestCase):
    """A task starts from a request typed in the UI, and the panel plans from it."""

    def setUp(self) -> None:
        self.store = Store()
        self.app = create_app(provider=FakeStreamingProvider(), store=self.store)
        self.client = self.app.test_client()

    def test_a_task_is_created_from_the_request_text(self) -> None:
        resp = self.client.post("/api/tasks", json={"request": "Add a status field to orders.py."})
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        task = self.store.get_task(data["id"])
        assert task is not None
        self.assertEqual(task.description, "Add a status field to orders.py.")
        self.assertEqual(task.title, "Add a status field to orders.py.")
        self.assertIsNone(task.run, "a task that has not run has no panel state")
        self.assertEqual([a.text for a in task.activity], ["Task created from your request."])

    def test_an_empty_request_is_refused(self) -> None:
        for payload in ({}, {"request": ""}, {"request": "   "}, {"request": None}):
            resp = self.client.post("/api/tasks", json=payload)
            self.assertEqual(resp.status_code, 400, payload)

    def test_the_new_task_plans_its_checklist_from_that_request(self) -> None:
        created = self.client.post(
            "/api/tasks", json={"request": "Fix the crash in checkout.py when the cart is empty."}
        ).get_json()
        body = self.client.get("/task/" + created["id"]).get_data(as_text=True)
        self.assertIn("Find the code for the crash", body)
        self.assertIn("Reproduce the failure", body)
        self.assertIn("checkout.py", body)
        # Not the old fixed workflow, and no invented numbers.
        self.assertNotIn("Read repository context", body)
        self.assertNotIn("6 files scanned", body)

    def test_each_task_gets_its_own_id(self) -> None:
        first = self.client.post("/api/tasks", json={"request": "One."}).get_json()["id"]
        second = self.client.post("/api/tasks", json={"request": "Two."}).get_json()["id"]
        self.assertNotEqual(first, second)
        self.assertEqual(len(self.store.tasks_for()), 3)  # the seeded task plus these two


class TurnStatusLabelTests(unittest.TestCase):
    """The generating label says what is actually happening — nothing it does not know."""

    def test_the_label_only_claims_states_the_stream_can_prove(self) -> None:
        script = _script()
        self.assertIn('"Thinking…"', script)
        self.assertIn('"Still thinking…"', script)  # a long wait says so, it does not pretend
        self.assertIn('"Reading the file…"', script)
        self.assertIn('"Calling a tool…"', script)
        self.assertIn('"Writing your answer…"', script)
        self.assertIn('"Working through it…"', script)
        self.assertIn("function describeThinkingStep(text)", script)

    def test_task_stream_varies_operation_by_what_narration_is_doing(self) -> None:
        class StepNarratingProvider:
            def reply_stream(
                self,
                history,
                system_prompt: str,
                *,
                think: bool = False,
                report_attempts: bool = False,
                thinking_level: str = "quick",
            ):
                yield StreamChunk(kind="thinking", text="I am reading orders.py to locate the status field.")
                yield StreamChunk(kind="thinking", text="Calling a tool to inspect the state.")
                yield StreamChunk(kind="answer", text="Done.")

        client = create_app(provider=StepNarratingProvider(), store=Store()).test_client()
        events = parse_sse(
            client.post("/api/task/T1/stream", json={"initial": True}).get_data(as_text=True)
        )
        operations = [
            json.loads(data)["operation"]
            for event_name, data in events
            if event_name == "task_state"
        ]
        self.assertIn("Thinking…", operations)
        self.assertIn("Reading the file…", operations)
        self.assertIn("Calling a tool…", operations)
        self.assertIn("Writing your answer…", operations)

    def test_streaming_cursor_is_inline_after_on_reply_text_node_and_removed_when_done(self) -> None:
        root = Path(__file__).resolve().parent.parent
        layout = (root / "app" / "templates" / "layout.html").read_text(encoding="utf-8")
        mockup = (root / "design" / "orbit_mockup.html").read_text(encoding="utf-8")
        script = _script()
        self.assertRegex(
            layout,
            r"\.chat-bubble \.reply\.streaming::after\s*\{\s*content:\s*\"▍\";\s*display:\s*inline;",
        )
        self.assertRegex(
            mockup,
            r"\.chat-bubble \.reply\.streaming::after\s*\{\s*content:\s*\"▍\";\s*display:\s*inline;",
        )
        self.assertIn('reply.classList.add("streaming")', script)
        self.assertIn('reply.classList.remove("streaming")', script)
        self.assertIn('orbit.reply.classList.add("streaming")', script)
        self.assertIn('orbit.reply.classList.remove("streaming")', script)

    def test_turn_status_indicator_is_anchored_inside_message_list_never_above_it(self) -> None:
        home = create_app(provider=FakeStreamingProvider(), store=Store()).test_client().get("/").get_data(as_text=True)
        task = create_app(provider=FakeStreamingProvider(), store=Store()).test_client().get("/task/T1").get_data(as_text=True)
        script = _script()
        self.assertLess(home.index('id="homeThread"'), home.index('id="turnStatusBar"'))
        self.assertLess(task.index('id="taskChatLog"'), task.index('id="taskTurnStatus"'))
        self.assertIn("turnStatus.placeAfter(userRow)", script)
        self.assertIn("turnStatus.placeBefore(row)", script)
        self.assertIn("turnStatus.placeBefore(orbit.row)", script)

    def test_sending_three_messages_in_a_row_maintains_strict_chronological_order(self) -> None:
        import subprocess

        class ThreeTurnProvider(FakeStreamingProvider):
            def __init__(self) -> None:
                super().__init__()
                self.turn = 0

            def reply_stream(self, history, system_prompt: str, **kwargs) -> Iterator[StreamChunk]:
                self.turn += 1
                t = self.turn
                yield StreamChunk(kind="thinking", text=f"Reading app/server.py for turn {t}.")
                yield StreamChunk(kind="answer", text=f"Reply {t} part A, ")
                yield StreamChunk(kind="answer", text=f"part B.")

        client = create_app(provider=ThreeTurnProvider(), store=Store()).test_client()
        sse_turns = [
            client.post("/api/chat", json={"message": f"Message {i}"}).get_data(as_text=True)
            for i in range(1, 4)
        ]

        node_driver = r"""
const fs = require("fs");
const vm = require("vm");
const { TextEncoder, TextDecoder } = require("util");

const sseTurns = JSON.parse(process.argv[1]);
const appJs = fs.readFileSync("app/static/app.js", "utf8");

class ClassList {
  constructor(el) { this.el = el; this.set = new Set(); }
  _sync() { this.el._className = Array.from(this.set).join(" "); }
  add(...names) { names.forEach(n => n && this.set.add(n)); this._sync(); }
  remove(...names) { names.forEach(n => this.set.delete(n)); this._sync(); }
  toggle(name, force) {
    const shouldAdd = force !== undefined ? !!force : !this.set.has(name);
    if (shouldAdd) this.set.add(name); else this.set.delete(name);
    this._sync();
    return shouldAdd;
  }
  contains(name) { return this.set.has(name); }
}

class Element {
  constructor(tag, id = "", classes = "") {
    this.tagName = tag.toUpperCase();
    this.id = id;
    this.children = [];
    this.parentNode = null;
    this.attributes = {};
    this.dataset = {};
    this.listeners = {};
    this.style = {};
    this.hidden = false;
    this.disabled = false;
    this.value = "";
    this._textContent = "";
    this.scrollTop = 0;
    this.scrollHeight = 100;
    this.classList = new ClassList(this);
    if (classes) classes.split(/\s+/).forEach(c => c && this.classList.add(c));
  }
  set className(v) {
    this._className = v || "";
    this.classList.set = new Set((v || "").split(/\s+/).filter(Boolean));
  }
  get className() { return Array.from(this.classList.set).join(" "); }
  set textContent(v) {
    this._textContent = String(v ?? "");
    this.children = [];
  }
  get textContent() {
    if (this.children.length === 0) return this._textContent;
    return this._textContent + this.children.map(c => c.textContent).join("");
  }
  set innerHTML(v) {
    this._textContent = String(v ?? "");
    this.children = [];
  }
  get innerHTML() { return this.textContent; }
  get nextSibling() {
    if (!this.parentNode) return null;
    const idx = this.parentNode.children.indexOf(this);
    return idx >= 0 && idx + 1 < this.parentNode.children.length ? this.parentNode.children[idx + 1] : null;
  }
  appendChild(child) {
    if (child.parentNode) child.parentNode.removeChild(child);
    child.parentNode = this;
    this.children.push(child);
    return child;
  }
  removeChild(child) {
    const idx = this.children.indexOf(child);
    if (idx >= 0) {
      this.children.splice(idx, 1);
      child.parentNode = null;
    }
    return child;
  }
  insertBefore(newNode, refNode) {
    if (!refNode) return this.appendChild(newNode);
    if (newNode.parentNode) newNode.parentNode.removeChild(newNode);
    const idx = this.children.indexOf(refNode);
    if (idx < 0) return this.appendChild(newNode);
    newNode.parentNode = this;
    this.children.splice(idx, 0, newNode);
    return newNode;
  }
  remove() { if (this.parentNode) this.parentNode.removeChild(this); }
  setAttribute(k, v) {
    this.attributes[k] = String(v);
    if (k.startsWith("data-")) {
      const camel = k.slice(5).replace(/-([a-z])/g, (_, c) => c.toUpperCase());
      this.dataset[camel] = String(v);
    }
  }
  getAttribute(k) { return this.attributes[k] ?? null; }
  addEventListener(type, fn) { (this.listeners[type] = this.listeners[type] || []).push(fn); }
  dispatchEvent(evt) { (this.listeners[evt.type] || []).forEach(fn => fn.call(this, evt)); }
  focus() {}
  matches(sel) {
    if (sel.startsWith("#")) return this.id === sel.slice(1);
    if (sel.startsWith(".")) return sel.slice(1).split(".").every(c => this.classList.contains(c));
    return this.tagName.toLowerCase() === sel.toLowerCase();
  }
  closest(sel) {
    let cur = this;
    while (cur) {
      if (cur.matches && cur.matches(sel)) return cur;
      cur = cur.parentNode;
    }
    return null;
  }
  querySelector(sel) { return this.querySelectorAll(sel)[0] || null; }
  querySelectorAll(sel) {
    const out = [];
    const visit = (node) => {
      for (const child of node.children) {
        if (matchComplex(child, sel)) out.push(child);
        visit(child);
      }
    };
    visit(this);
    return out;
  }
}

function matchComplex(node, sel) {
  if (sel === ".chat-row") return node.classList.contains("chat-row");
  if (sel === ".chat-row.orbit") return node.classList.contains("chat-row") && node.classList.contains("orbit");
  if (sel === ".chat-row.orbit:last-child .body") return node.classList.contains("body");
  if (sel === ".body") return node.classList.contains("body");
  if (sel === ".reply") return node.classList.contains("reply");
  if (sel === ".notice") return node.classList.contains("notice");
  if (sel === "[data-message-action='regenerate']") return node.getAttribute("data-message-action") === "regenerate";
  return node.matches(sel);
}

const byId = {};
function reg(el) { if (el.id) byId[el.id] = el; return el; }

reg(new Element("main", "homeMain", "home-main"));
reg(new Element("div", "welcome", "welcome"));
const thread = reg(new Element("div", "homeThread", "home-thread"));
const turnStatusBar = reg(new Element("div", "turnStatusBar", "turn-status-bar"));
turnStatusBar.hidden = true;
const turnStatusText = reg(new Element("span", "turnStatusText", "turn-status-text"));
turnStatusBar.appendChild(turnStatusText);
thread.appendChild(turnStatusBar);
const input = reg(new Element("input", "homeInput"));
const send = reg(new Element("button", "homeSend", "send-btn-round"));
reg(new Element("div", "homeFollowups", "followup-chips"));

let pendingFetchResolve = null;
const document = {
  documentElement: new Element("html"),
  activeElement: null,
  getElementById(id) { return byId[id] || null; },
  createElement(tag) { return new Element(tag); },
  querySelector() { return null; },
  querySelectorAll() { return []; },
  addEventListener() {},
};
const fetchFn = () => new Promise((resolve) => { pendingFetchResolve = resolve; });
const window = {
  document,
  TextDecoder,
  fetch: fetchFn,
  setTimeout: () => 1,
  clearTimeout: () => {},
  localStorage: { getItem: () => null, setItem: () => {} },
};

vm.runInNewContext(appJs, { window, document, fetch: fetchFn, TextDecoder, console, Math, JSON, Date, Promise, Array, Object, String, RegExp, encodeURIComponent });
window.Orbit.bootstrapHome();

function snap() {
  return thread.children.map((c) => {
    if (c.id === "turnStatusBar") {
      return { kind: "thinking", hidden: c.hidden, text: turnStatusText.textContent };
    }
    if (c.classList.contains("user")) {
      return { kind: "user", text: c.querySelector(".body").textContent };
    }
    const reply = c.querySelector(".reply");
    return {
      kind: "orbit",
      generating: c.classList.contains("is-generating"),
      streaming: c.classList.contains("is-streaming"),
      replyStreaming: reply.classList.contains("streaming"),
      text: reply.textContent,
    };
  });
}

(async () => {
  const report = [];
  for (let turn = 0; turn < 3; turn++) {
    input.value = `Message ${turn + 1}`;
    send.dispatchEvent({ type: "click" });
    const afterSend = snap();

    const blocks = sseTurns[turn].split("\n\n").filter(Boolean);
    const encoder = new TextEncoder();
    const pendingReads = [];
    pendingFetchResolve({
      ok: true,
      status: 200,
      headers: { get: () => "text/event-stream; charset=utf-8" },
      body: { getReader: () => ({ read: () => new Promise(r => pendingReads.push(r)) }) },
    });
    await new Promise(r => setImmediate(r));

    let duringThinking = null;
    let duringAnswer = null;
    let afterDone = null;
    for (const block of blocks) {
      pendingReads.shift()({ value: encoder.encode(block + "\n\n"), done: false });
      await new Promise(r => setImmediate(r));
      const head = block.trim().split("\n")[0];
      if (head === "event: thinking") duringThinking = snap();
      if (head === "event: answer" && !duringAnswer) duringAnswer = snap();
      if (head === "event: done") afterDone = snap();
    }
    pendingReads.shift()({ value: undefined, done: true });
    await new Promise(r => setImmediate(r));
    report.push({ afterSend, duringThinking, duringAnswer, afterDone, finished: snap() });
  }
  console.log(JSON.stringify(report));
})();
"""
        proc = subprocess.run(
            ["node", "-e", node_driver, json.dumps(sse_turns)],
            capture_output=True,
            text=True,
            check=True,
        )
        report = json.loads(proc.stdout.strip())
        self.assertEqual(len(report), 3)

        for idx, turn_report in enumerate(report, start=1):
            expected_prefix = []
            for prev in range(1, idx):
                expected_prefix.append({"kind": "user", "text": f"Message {prev}"})
                expected_prefix.append(
                    {
                        "kind": "orbit",
                        "generating": False,
                        "streaming": False,
                        "replyStreaming": False,
                        "text": f"Reply {prev} part A, part B.",
                    }
                )

            # (1) & (2) Right after send: user message is last visible message,
            # followed immediately by the thinking indicator, never above it.
            self.assertEqual(
                turn_report["afterSend"],
                expected_prefix
                + [
                    {"kind": "user", "text": f"Message {idx}"},
                    {"kind": "thinking", "hidden": False, "text": "Thinking…"},
                    {
                        "kind": "orbit",
                        "generating": True,
                        "streaming": False,
                        "replyStreaming": False,
                        "text": "",
                    },
                ],
            )

            # While thinking event streams: thinking indicator stays right after user message.
            self.assertEqual(
                turn_report["duringThinking"],
                expected_prefix
                + [
                    {"kind": "user", "text": f"Message {idx}"},
                    {"kind": "thinking", "hidden": False, "text": "Reading the file…"},
                    {
                        "kind": "orbit",
                        "generating": True,
                        "streaming": False,
                        "replyStreaming": False,
                        "text": "",
                    },
                ],
            )

            # (3) Once assistant reply starts streaming: thinking indicator hides in place,
            # reply row streams with inline .reply.streaming cursor attached to the text node.
            self.assertEqual(
                turn_report["duringAnswer"],
                expected_prefix
                + [
                    {"kind": "user", "text": f"Message {idx}"},
                    {"kind": "thinking", "hidden": True, "text": ""},
                    {
                        "kind": "orbit",
                        "generating": False,
                        "streaming": True,
                        "replyStreaming": True,
                        "text": f"Reply {idx} part A, ",
                    },
                ],
            )

            # The instant streaming ends (done): .streaming cursor is removed immediately.
            self.assertEqual(
                turn_report["afterDone"],
                expected_prefix
                + [
                    {"kind": "user", "text": f"Message {idx}"},
                    {"kind": "thinking", "hidden": True, "text": ""},
                    {
                        "kind": "orbit",
                        "generating": False,
                        "streaming": False,
                        "replyStreaming": False,
                        "text": f"Reply {idx} part A, part B.",
                    },
                ],
            )

    def test_mic_uses_tap_to_start_and_tap_to_stop(self) -> None:
        home = create_app(provider=FakeStreamingProvider(), store=Store()).test_client().get("/").get_data(as_text=True)
        task = create_app(provider=FakeStreamingProvider(), store=Store()).test_client().get("/task/T1").get_data(as_text=True)
        script = _script()
        self.assertIn('aria-label="Tap to talk"', home)
        self.assertIn('aria-label="Tap to talk"', task)
        self.assertNotIn("Hold to talk", home)
        self.assertNotIn("Hold to talk", task)
        self.assertIn('button.addEventListener("click", toggleRecording)', script)
        self.assertIn('"Stop recording and transcribe"', script)
        self.assertNotIn('button.addEventListener("pointerdown"', script)
        self.assertNotIn('window.addEventListener("pointerup"', script)

    def test_sidebar_has_simple_send_a_suggestion_entry_without_form_or_categories(self) -> None:
        home = create_app(provider=FakeStreamingProvider(), store=Store()).test_client().get("/").get_data(as_text=True)
        self.assertIn("Send a suggestion", home)
        self.assertIn('id="suggestionInput"', home)
        self.assertIn('id="suggestionSubmit"', home)
        self.assertIn('id="suggestionCardHost"', home)
        suggestion_box = home.split('id="suggestionBox"', 1)[1].split('class="connected-apps-row"', 1)[0]
        self.assertNotIn("<form", suggestion_box)
        self.assertNotIn("<select", suggestion_box)

    def test_the_label_never_claims_work_that_is_not_happening(self) -> None:
        # Attachments are not uploaded to the model yet (handoff §5.3), so a
        # "reading your files" label would be a lie.
        self.assertNotIn("Analyzing the provided files", _script())
        # The old standing badge claimed Orbit was working even while idle.
        task = (
            create_app(provider=FakeStreamingProvider(), store=Store())
            .test_client()
            .get("/task/T1")
            .get_data(as_text=True)
        )
        self.assertNotIn("Orbit is working", task)
        self.assertNotIn("Orbit is thinking", task)

    def test_the_status_line_is_wired_on_both_screens(self) -> None:
        home = create_app(provider=FakeStreamingProvider(), store=Store()).test_client().get("/").get_data(as_text=True)
        task = create_app(provider=FakeStreamingProvider(), store=Store()).test_client().get("/task/T1").get_data(as_text=True)
        self.assertIn('id="turnStatusBar"', home)
        self.assertIn('id="turnStatusText"', home)
        self.assertIn('id="taskTurnStatus"', task)
        self.assertIn('id="taskTurnStatusText"', task)
        self.assertIn('turnStatusFor("taskTurnStatus", "taskTurnStatusText")', _script())


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
        # "task_state" is panel bookkeeping, not content: the first thing the
        # model was asked for is still the status line.
        content = [event for event, _ in seen if event != "task_state"]
        self.assertEqual(content[0], "status")
        self.assertEqual(seen[0][0], "task_state")

    def test_chat_mode_task_view_is_unlabeled(self) -> None:
        provider = RecordingProvider(self.PIECES)
        seen = events_as_they_arrive(
            self._client(provider, mode="chat"), "/api/task/T1/stream", {"message": "steer"}, provider
        )
        self.assertEqual(provider.calls, [{"think": True, "report_attempts": False, "thinking_level": "quick"}])
        self.assertNotIn("status", [event for event, _ in seen])


class ChatUIFixesTests(unittest.TestCase):
    """Tests for auto-scroll, inline retry/regenerate on failure, and code blocks."""

    def test_auto_scroll_only_scrolls_when_pinned_to_bottom(self) -> None:
        import subprocess

        node_script = r"""
const fs = require("fs");
const vm = require("vm");
const { TextEncoder, TextDecoder } = require("util");

const appJs = fs.readFileSync("app/static/app.js", "utf8");

class ClassList {
  constructor(el) { this.el = el; this.set = new Set(); }
  _sync() { this.el._className = Array.from(this.set).join(" "); }
  add(...names) { names.forEach(n => n && this.set.add(n)); this._sync(); }
  remove(...names) { names.forEach(n => this.set.delete(n)); this._sync(); }
  toggle(name, force) {
    const shouldAdd = force !== undefined ? !!force : !this.set.has(name);
    if (shouldAdd) this.set.add(name); else this.set.delete(name);
    this._sync();
    return shouldAdd;
  }
  contains(name) { return this.set.has(name); }
}

class Element {
  constructor(tag, id = "", classes = "") {
    this.tagName = tag.toUpperCase();
    this.id = id;
    this.children = [];
    this.parentNode = null;
    this.attributes = {};
    this.dataset = {};
    this.listeners = {};
    this.style = {};
    this.hidden = false;
    this.disabled = false;
    this.value = "";
    this._textContent = "";
    this.scrollTop = 0;
    this.scrollHeight = 500;
    this.clientHeight = 200;
    this.classList = new ClassList(this);
    if (classes) classes.split(/\s+/).forEach(c => c && this.classList.add(c));
  }
  set className(v) {
    this._className = v || "";
    this.classList.set = new Set((v || "").split(/\s+/).filter(Boolean));
  }
  get className() { return Array.from(this.classList.set).join(" "); }
  set textContent(v) { this._textContent = String(v ?? ""); this.children = []; }
  get textContent() {
    if (this.children.length === 0) return this._textContent;
    return this._textContent + this.children.map(c => c.textContent).join("");
  }
  set innerHTML(v) { this._textContent = String(v ?? ""); this.children = []; }
  get innerHTML() { return this.textContent; }
  get nextSibling() {
    if (!this.parentNode) return null;
    const idx = this.parentNode.children.indexOf(this);
    return idx >= 0 && idx + 1 < this.parentNode.children.length ? this.parentNode.children[idx + 1] : null;
  }
  appendChild(child) {
    if (child.parentNode) child.parentNode.removeChild(child);
    child.parentNode = this;
    this.children.push(child);
    return child;
  }
  removeChild(child) {
    const idx = this.children.indexOf(child);
    if (idx >= 0) { this.children.splice(idx, 1); child.parentNode = null; }
    return child;
  }
  insertBefore(newNode, refNode) {
    if (!refNode) return this.appendChild(newNode);
    if (newNode.parentNode) newNode.parentNode.removeChild(newNode);
    const idx = this.children.indexOf(refNode);
    if (idx < 0) return this.appendChild(newNode);
    newNode.parentNode = this;
    this.children.splice(idx, 0, newNode);
    return newNode;
  }
  remove() { if (this.parentNode) this.parentNode.removeChild(this); }
  setAttribute(k, v) { this.attributes[k] = String(v); }
  removeAttribute(k) { delete this.attributes[k]; }
  getAttribute(k) { return this.attributes[k] ?? null; }
  addEventListener(type, fn) { (this.listeners[type] = this.listeners[type] || []).push(fn); }
  dispatchEvent(evt) { (this.listeners[evt.type] || []).forEach(fn => fn.call(this, evt)); }
  focus() {}
  contains(node) {
    let cur = node;
    while (cur) { if (cur === this) return true; cur = cur.parentNode; }
    return false;
  }
  matches(sel) {
    if (sel.startsWith("#")) return this.id === sel.slice(1);
    if (sel.startsWith(".")) return sel.slice(1).split(".").every(c => this.classList.contains(c));
    return this.tagName.toLowerCase() === sel.toLowerCase();
  }
  closest(sel) {
    let cur = this;
    while (cur) { if (cur.matches && cur.matches(sel)) return cur; cur = cur.parentNode; }
    return null;
  }
  querySelector(sel) { return this.querySelectorAll(sel)[0] || null; }
  querySelectorAll(sel) {
    const out = [];
    const visit = (node) => {
      for (const child of node.children) {
        if (matchComplex(child, sel)) out.push(child);
        visit(child);
      }
    };
    visit(this);
    return out;
  }
}

function matchComplex(node, sel) {
  if (sel === ".chat-row") return node.classList.contains("chat-row");
  if (sel === ".chat-row.orbit") return node.classList.contains("chat-row") && node.classList.contains("orbit");
  if (sel === ".chat-row.is-generating, .chat-row.is-streaming") {
    return node.classList.contains("chat-row") && (node.classList.contains("is-generating") || node.classList.contains("is-streaming"));
  }
  if (sel === ".body") return node.classList.contains("body");
  if (sel === ".reply") return node.classList.contains("reply");
  if (sel === ".notice") return node.classList.contains("notice");
  if (sel === ".inline-retry-btn") return node.classList.contains("inline-retry-btn");
  if (sel === ".jump-to-latest") return node.classList.contains("jump-to-latest");
  if (sel === "[data-message-action='regenerate']") return node.getAttribute("data-message-action") === "regenerate";
  if (sel === "[data-message-action='retry']") return node.getAttribute("data-message-action") === "retry";
  return node.matches(sel);
}

const byId = {};
function reg(el) { if (el.id) byId[el.id] = el; return el; }

const homeCenter = reg(new Element("div", "homeCenter", "home-center"));
reg(new Element("main", "homeMain", "home-main"));
reg(new Element("div", "welcome", "welcome"));
const thread = reg(new Element("div", "homeThread", "home-thread"));
homeCenter.appendChild(thread);
const turnStatusBar = reg(new Element("div", "turnStatusBar", "turn-status-bar"));
turnStatusBar.hidden = true;
const turnStatusText = reg(new Element("span", "turnStatusText", "turn-status-text"));
turnStatusBar.appendChild(turnStatusText);
thread.appendChild(turnStatusBar);
const input = reg(new Element("input", "homeInput"));
const send = reg(new Element("button", "homeSend", "send-btn-round"));
reg(new Element("div", "homeFollowups", "followup-chips"));

let pendingFetchResolve = null;
const document = {
  documentElement: new Element("html"),
  activeElement: null,
  getElementById(id) { return byId[id] || null; },
  createElement(tag) { return new Element(tag); },
  querySelector() { return null; },
  querySelectorAll() { return []; },
  addEventListener() {},
};
const fetchFn = () => new Promise((resolve) => { pendingFetchResolve = resolve; });
const window = {
  document,
  TextDecoder,
  fetch: fetchFn,
  setTimeout: () => 1,
  clearTimeout: () => {},
  localStorage: { getItem: () => null, setItem: () => {} },
};

vm.runInNewContext(appJs, { window, document, fetch: fetchFn, TextDecoder, Number, console, Math, JSON, Date, Promise, Array, Object, String, RegExp, encodeURIComponent });
window.Orbit.bootstrapHome();

(async () => {
  // Start at bottom (scrollHeight=500, clientHeight=200 => bottom is scrollTop>=260)
  thread.scrollTop = 300;
  input.value = "First message";
  send.dispatchEvent({ type: "click" });

  // Since user was at bottom, sending scrolled to bottom (scrollHeight=500)
  const scrolledOnSend = thread.scrollTop === 500;

  const encoder = new TextEncoder();
  const pendingReads = [];
  pendingFetchResolve({
    ok: true,
    status: 200,
    headers: { get: () => "text/event-stream; charset=utf-8" },
    body: { getReader: () => ({ read: () => new Promise(r => pendingReads.push(r)) }) },
  });
  await new Promise(r => setImmediate(r));

  // Chunk 1 arrives while user is still at bottom -> auto-scrolls to new scrollHeight (600)
  thread.scrollHeight = 600;
  pendingReads.shift()({ value: encoder.encode("event: answer\ndata: Part 1 \n\n"), done: false });
  await new Promise(r => setImmediate(r));
  const scrolledOnChunk1 = thread.scrollTop === 600;

  // User scrolls up to reread earlier content (scrollTop=80, far from bottom)
  thread.scrollTop = 80;
  thread.dispatchEvent({ type: "scroll" });
  const jumpBtn = homeCenter.querySelector(".jump-to-latest");
  const jumpVisibleWhenScrolledUp = !!(jumpBtn && !jumpBtn.hidden);

  // Chunk 2 arrives while user is scrolled up -> must NOT yank user down!
  thread.scrollHeight = 750;
  pendingReads.shift()({ value: encoder.encode("event: answer\ndata: Part 2\n\n"), done: false });
  await new Promise(r => setImmediate(r));
  const stayedAt80OnChunk2 = thread.scrollTop === 80;

  // User returns to the bottom themselves (or clicks Jump to latest)
  jumpBtn.dispatchEvent({ type: "click" });
  const jumpedToBottom = thread.scrollTop === 750 && jumpBtn.hidden === true;

  // Chunk 3 arrives after user returned to bottom -> auto-scrolls again!
  thread.scrollHeight = 820;
  pendingReads.shift()({ value: encoder.encode("event: answer\ndata:  Part 3\n\n"), done: false });
  await new Promise(r => setImmediate(r));
  const scrolledOnChunk3 = thread.scrollTop === 820;

  console.log(JSON.stringify({
    scrolledOnSend,
    scrolledOnChunk1,
    jumpVisibleWhenScrolledUp,
    stayedAt80OnChunk2,
    jumpedToBottom,
    scrolledOnChunk3,
  }));
})();
"""
        proc = subprocess.run(["node", "-e", node_script], capture_output=True, text=True, check=True)
        result = json.loads(proc.stdout.strip())
        self.assertTrue(result["scrolledOnSend"])
        self.assertTrue(result["scrolledOnChunk1"])
        self.assertTrue(result["jumpVisibleWhenScrolledUp"])
        self.assertTrue(result["stayedAt80OnChunk2"])
        self.assertTrue(result["jumpedToBottom"])
        self.assertTrue(result["scrolledOnChunk3"])

    def test_failed_or_cutoff_message_shows_inline_retry_and_regenerates_in_place(self) -> None:
        import subprocess

        class FlakyThenOkProvider(FakeStreamingProvider):
            def __init__(self) -> None:
                super().__init__()
                self.attempt = 0

            def reply_stream(self, history, system_prompt: str, **kwargs) -> Iterator[StreamChunk]:
                self.attempt += 1
                self.calls.append({"history": [m.content for m in history], "attempt": self.attempt})
                if self.attempt == 1:
                    raise RuntimeError("Provider connection dropped")
                yield StreamChunk(kind="answer", text="Recovered answer in place.")

        store = Store()
        provider = FlakyThenOkProvider()
        client = create_app(provider=provider, store=store).test_client()

        # Attempt 1 fails mid-stream after storing user message
        first_sse = client.post("/api/chat", json={"message": "Explain retry"}).get_data(as_text=True)
        self.assertIn("event: error", first_sse)
        self.assertEqual(len(store.home_thread), 1)
        self.assertEqual(store.home_thread[0].body, "Explain retry")

        # Retrying in place via {"retry": True, "message": "Explain retry"} reuses the user message
        # rather than appending a duplicate, and stores the recovered Orbit answer.
        second_sse = client.post("/api/chat", json={"retry": True, "message": "Explain retry"}).get_data(as_text=True)
        self.assertIn("Recovered answer in place.", second_sse)
        self.assertEqual(len(store.home_thread), 2)
        self.assertEqual([m.who for m in store.home_thread], ["You", "Orbit"])
        self.assertEqual(store.home_thread[1].body, "Recovered answer in place.")

        # Verify client DOM shows inline retry button on failure/cut-off and retries in place
        node_script = r"""
const fs = require("fs");
const vm = require("vm");
const { TextEncoder, TextDecoder } = require("util");

const [firstSse, secondSse] = JSON.parse(process.argv[1]);
const appJs = fs.readFileSync("app/static/app.js", "utf8");

class ClassList {
  constructor(el) { this.el = el; this.set = new Set(); }
  _sync() { this.el._className = Array.from(this.set).join(" "); }
  add(...names) { names.forEach(n => n && this.set.add(n)); this._sync(); }
  remove(...names) { names.forEach(n => this.set.delete(n)); this._sync(); }
  toggle(name, force) {
    const shouldAdd = force !== undefined ? !!force : !this.set.has(name);
    if (shouldAdd) this.set.add(name); else this.set.delete(name);
    this._sync();
    return shouldAdd;
  }
  contains(name) { return this.set.has(name); }
}

class Element {
  constructor(tag, id = "", classes = "") {
    this.tagName = tag.toUpperCase();
    this.id = id;
    this.children = [];
    this.parentNode = null;
    this.attributes = {};
    this.dataset = {};
    this.listeners = {};
    this.style = {};
    this.hidden = false;
    this.disabled = false;
    this.value = "";
    this._textContent = "";
    this.scrollTop = 0;
    this.scrollHeight = 100;
    this.clientHeight = 100;
    this.classList = new ClassList(this);
    if (classes) classes.split(/\s+/).forEach(c => c && this.classList.add(c));
  }
  set className(v) {
    this._className = v || "";
    this.classList.set = new Set((v || "").split(/\s+/).filter(Boolean));
  }
  get className() { return Array.from(this.classList.set).join(" "); }
  set textContent(v) { this._textContent = String(v ?? ""); this.children = []; }
  get textContent() {
    if (this.children.length === 0) return this._textContent;
    return this._textContent + this.children.map(c => c.textContent).join("");
  }
  set innerHTML(v) { this._textContent = String(v ?? ""); this.children = []; }
  get innerHTML() { return this.textContent; }
  get nextSibling() {
    if (!this.parentNode) return null;
    const idx = this.parentNode.children.indexOf(this);
    return idx >= 0 && idx + 1 < this.parentNode.children.length ? this.parentNode.children[idx + 1] : null;
  }
  appendChild(child) {
    if (child.parentNode) child.parentNode.removeChild(child);
    child.parentNode = this;
    this.children.push(child);
    return child;
  }
  removeChild(child) {
    const idx = this.children.indexOf(child);
    if (idx >= 0) { this.children.splice(idx, 1); child.parentNode = null; }
    return child;
  }
  insertBefore(newNode, refNode) {
    if (!refNode) return this.appendChild(newNode);
    if (newNode.parentNode) newNode.parentNode.removeChild(newNode);
    const idx = this.children.indexOf(refNode);
    if (idx < 0) return this.appendChild(newNode);
    newNode.parentNode = this;
    this.children.splice(idx, 0, newNode);
    return newNode;
  }
  remove() { if (this.parentNode) this.parentNode.removeChild(this); }
  setAttribute(k, v) { this.attributes[k] = String(v); }
  removeAttribute(k) { delete this.attributes[k]; }
  getAttribute(k) { return this.attributes[k] ?? null; }
  addEventListener(type, fn) { (this.listeners[type] = this.listeners[type] || []).push(fn); }
  dispatchEvent(evt) {
    let cur = this;
    const eventObj = Object.assign({ target: this }, evt);
    while (cur) {
      (cur.listeners[evt.type] || []).forEach(fn => fn.call(cur, eventObj));
      cur = cur.parentNode;
    }
  }
  focus() {}
  contains(node) {
    let cur = node;
    while (cur) { if (cur === this) return true; cur = cur.parentNode; }
    return false;
  }
  matches(sel) {
    if (sel === "[data-message-action]") return this.getAttribute("data-message-action") !== null;
    if (sel === "[data-code-copy]") return this.getAttribute("data-code-copy") !== null;
    if (sel.startsWith("#")) return this.id === sel.slice(1);
    if (sel.startsWith(".")) return sel.slice(1).split(".").every(c => this.classList.contains(c));
    return this.tagName.toLowerCase() === sel.toLowerCase();
  }
  closest(sel) {
    let cur = this;
    while (cur) { if (cur.matches && cur.matches(sel)) return cur; cur = cur.parentNode; }
    return null;
  }
  querySelector(sel) { return this.querySelectorAll(sel)[0] || null; }
  querySelectorAll(sel) {
    const out = [];
    const visit = (node) => {
      for (const child of node.children) {
        if (matchComplex(child, sel)) out.push(child);
        visit(child);
      }
    };
    visit(this);
    return out;
  }
}

function matchComplex(node, sel) {
  if (sel === ".chat-row") return node.classList.contains("chat-row");
  if (sel === ".chat-row.orbit") return node.classList.contains("chat-row") && node.classList.contains("orbit");
  if (sel === ".chat-row.is-generating, .chat-row.is-streaming") {
    return node.classList.contains("chat-row") && (node.classList.contains("is-generating") || node.classList.contains("is-streaming"));
  }
  if (sel === ".body") return node.classList.contains("body");
  if (sel === ".reply") return node.classList.contains("reply");
  if (sel === ".notice") return node.classList.contains("notice");
  if (sel === ".inline-retry-btn") return node.classList.contains("inline-retry-btn");
  if (sel === "[data-message-action='regenerate']") return node.getAttribute("data-message-action") === "regenerate";
  if (sel === "[data-message-action='retry']") return node.getAttribute("data-message-action") === "retry";
  return node.matches(sel);
}

const byId = {};
function reg(el) { if (el.id) byId[el.id] = el; return el; }
reg(new Element("main", "homeMain", "home-main"));
reg(new Element("div", "welcome", "welcome"));
const thread = reg(new Element("div", "homeThread", "home-thread"));
const turnStatusBar = reg(new Element("div", "turnStatusBar", "turn-status-bar"));
turnStatusBar.hidden = true;
const turnStatusText = reg(new Element("span", "turnStatusText", "turn-status-text"));
turnStatusBar.appendChild(turnStatusText);
thread.appendChild(turnStatusBar);
const input = reg(new Element("input", "homeInput"));
const send = reg(new Element("button", "homeSend", "send-btn-round"));
reg(new Element("div", "homeFollowups", "followup-chips"));

const payloadsSent = [];
const sseQueue = [firstSse, secondSse];
const document = {
  documentElement: new Element("html"),
  activeElement: null,
  getElementById(id) { return byId[id] || null; },
  createElement(tag) { return new Element(tag); },
  querySelector() { return null; },
  querySelectorAll() { return []; },
  addEventListener() {},
};
const fetchFn = (url, opts) => {
  payloadsSent.push(JSON.parse(opts.body));
  const raw = sseQueue.shift();
  const encoder = new TextEncoder();
  let readDone = false;
  return Promise.resolve({
    ok: true,
    status: 200,
    headers: { get: () => "text/event-stream; charset=utf-8" },
    body: {
      getReader: () => ({
        read: () => {
          if (!readDone) {
            readDone = true;
            return Promise.resolve({ value: encoder.encode(raw), done: false });
          }
          return Promise.resolve({ value: undefined, done: true });
        }
      })
    }
  });
};
const window = {
  document,
  TextDecoder,
  fetch: fetchFn,
  setTimeout: () => 1,
  clearTimeout: () => {},
  localStorage: { getItem: () => null, setItem: () => {} },
};

vm.runInNewContext(appJs, { window, document, fetch: fetchFn, TextDecoder, Number, console, Math, JSON, Date, Promise, Array, Object, String, RegExp, encodeURIComponent });
window.Orbit.bootstrapHome();

(async () => {
  input.value = "Explain retry";
  send.dispatchEvent({ type: "click" });
  await new Promise(r => setImmediate(r));
  await new Promise(r => setImmediate(r));

  const retryBtnsAfterFail = thread.querySelectorAll(".inline-retry-btn");
  const hasInlineRetryAfterFail = retryBtnsAfterFail.length > 0;
  const rowsBeforeRetry = thread.querySelectorAll(".chat-row").length;

  // Click the inline retry button on the failed message itself
  retryBtnsAfterFail[0].dispatchEvent({ type: "click" });
  await new Promise(r => setImmediate(r));
  await new Promise(r => setImmediate(r));

  const rowsAfterRetry = thread.querySelectorAll(".chat-row");
  const retryBtnsAfterSuccess = thread.querySelectorAll(".inline-retry-btn");
  const orbitReply = rowsAfterRetry[1].querySelector(".reply").textContent;

  console.log(JSON.stringify({
    hasInlineRetryAfterFail,
    rowsBeforeRetry,
    rowsAfterRetryCount: rowsAfterRetry.length,
    retryBtnsAfterSuccessCount: retryBtnsAfterSuccess.length,
    orbitReply,
    payloadsSent,
  }));
})();
"""
        proc = subprocess.run(
            ["node", "-e", node_script, json.dumps([first_sse, second_sse])],
            capture_output=True,
            text=True,
            check=True,
        )
        res = json.loads(proc.stdout.strip())
        self.assertTrue(res["hasInlineRetryAfterFail"])
        self.assertEqual(res["rowsBeforeRetry"], 2)
        self.assertEqual(res["rowsAfterRetryCount"], 2)  # regenerated in place, no duplicate rows
        self.assertEqual(res["retryBtnsAfterSuccessCount"], 0)
        self.assertEqual(res["orbitReply"], "Recovered answer in place.")
        self.assertEqual(res["payloadsSent"][1], {"retry": True, "message": "Explain retry"})

    def test_code_blocks_render_monospace_syntax_highlighted_with_hover_copy_button(self) -> None:
        import subprocess

        root = Path(__file__).resolve().parent.parent
        layout = (root / "app" / "templates" / "layout.html").read_text(encoding="utf-8")
        self.assertIn(".code-block:hover .code-copy-btn", layout)
        self.assertIn(".code-pre", layout)
        self.assertIn("ui-monospace", layout)
        self.assertIn(".tok-keyword", layout)
        self.assertIn(".tok-diff-add", layout)

        node_script = r"""
const fs = require("fs");
const vm = require("vm");
const { TextEncoder, TextDecoder } = require("util");

const appJs = fs.readFileSync("app/static/app.js", "utf8");

class ClassList {
  constructor(el) { this.el = el; this.set = new Set(); }
  _sync() { this.el._className = Array.from(this.set).join(" "); }
  add(...names) { names.forEach(n => n && this.set.add(n)); this._sync(); }
  remove(...names) { names.forEach(n => this.set.delete(n)); this._sync(); }
  toggle(name, force) {
    const shouldAdd = force !== undefined ? !!force : !this.set.has(name);
    if (shouldAdd) this.set.add(name); else this.set.delete(name);
    this._sync();
    return shouldAdd;
  }
  contains(name) { return this.set.has(name); }
}

class Element {
  constructor(tag, id = "", classes = "") {
    this.tagName = tag.toUpperCase();
    this.id = id;
    this.children = [];
    this.parentNode = null;
    this.attributes = {};
    this.dataset = {};
    this.listeners = {};
    this.style = {};
    this.hidden = false;
    this.disabled = false;
    this.value = "";
    this._textContent = "";
    this.scrollTop = 0;
    this.scrollHeight = 100;
    this.clientHeight = 100;
    this.classList = new ClassList(this);
    if (classes) classes.split(/\s+/).forEach(c => c && this.classList.add(c));
  }
  set className(v) {
    this._className = v || "";
    this.classList.set = new Set((v || "").split(/\s+/).filter(Boolean));
  }
  get className() { return Array.from(this.classList.set).join(" "); }
  set textContent(v) { this._textContent = String(v ?? ""); this.children = []; }
  get textContent() {
    if (this.children.length === 0) return this._textContent;
    return this._textContent + this.children.map(c => c.textContent).join("");
  }
  set innerHTML(v) { this._rawHtml = String(v ?? ""); this._textContent = String(v ?? ""); this.children = []; }
  get innerHTML() { return this._rawHtml ?? this.textContent; }
  get nextSibling() {
    if (!this.parentNode) return null;
    const idx = this.parentNode.children.indexOf(this);
    return idx >= 0 && idx + 1 < this.parentNode.children.length ? this.parentNode.children[idx + 1] : null;
  }
  appendChild(child) {
    if (child.parentNode) child.parentNode.removeChild(child);
    child.parentNode = this;
    this.children.push(child);
    return child;
  }
  removeChild(child) {
    const idx = this.children.indexOf(child);
    if (idx >= 0) { this.children.splice(idx, 1); child.parentNode = null; }
    return child;
  }
  insertBefore(newNode, refNode) {
    if (!refNode) return this.appendChild(newNode);
    if (newNode.parentNode) newNode.parentNode.removeChild(newNode);
    const idx = this.children.indexOf(refNode);
    if (idx < 0) return this.appendChild(newNode);
    newNode.parentNode = this;
    this.children.splice(idx, 0, newNode);
    return newNode;
  }
  remove() { if (this.parentNode) this.parentNode.removeChild(this); }
  setAttribute(k, v) { this.attributes[k] = String(v); }
  removeAttribute(k) { delete this.attributes[k]; }
  getAttribute(k) { return this.attributes[k] ?? null; }
  addEventListener(type, fn) { (this.listeners[type] = this.listeners[type] || []).push(fn); }
  dispatchEvent(evt) {
    let cur = this;
    const eventObj = Object.assign({ target: this }, evt);
    while (cur) {
      (cur.listeners[evt.type] || []).forEach(fn => fn.call(cur, eventObj));
      cur = cur.parentNode;
    }
  }
  focus() {}
  contains(node) {
    let cur = node;
    while (cur) { if (cur === this) return true; cur = cur.parentNode; }
    return false;
  }
  matches(sel) {
    if (sel === "[data-message-action]") return this.getAttribute("data-message-action") !== null;
    if (sel === "[data-code-copy]") return this.getAttribute("data-code-copy") !== null;
    if (sel.startsWith("#")) return this.id === sel.slice(1);
    if (sel.startsWith(".")) return sel.slice(1).split(".").every(c => this.classList.contains(c));
    return this.tagName.toLowerCase() === sel.toLowerCase();
  }
  closest(sel) {
    let cur = this;
    while (cur) { if (cur.matches && cur.matches(sel)) return cur; cur = cur.parentNode; }
    return null;
  }
  querySelector(sel) { return this.querySelectorAll(sel)[0] || null; }
  querySelectorAll(sel) {
    const out = [];
    const visit = (node) => {
      for (const child of node.children) {
        if (matchComplex(child, sel)) out.push(child);
        visit(child);
      }
    };
    visit(this);
    return out;
  }
}

function matchComplex(node, sel) {
  if (sel === ".chat-row") return node.classList.contains("chat-row");
  if (sel === ".chat-row.orbit") return node.classList.contains("chat-row") && node.classList.contains("orbit");
  if (sel === ".chat-row.is-generating, .chat-row.is-streaming") {
    return node.classList.contains("chat-row") && (node.classList.contains("is-generating") || node.classList.contains("is-streaming"));
  }
  if (sel === ".body") return node.classList.contains("body");
  if (sel === ".reply") return node.classList.contains("reply");
  if (sel === ".code-block") return node.classList.contains("code-block");
  if (sel === ".code-copy-btn") return node.classList.contains("code-copy-btn");
  if (sel === ".code-content") return node.classList.contains("code-content");
  if (sel === "[data-message-action='regenerate']") return node.getAttribute("data-message-action") === "regenerate";
  if (sel === "[data-message-action='retry']") return node.getAttribute("data-message-action") === "retry";
  return node.matches(sel);
}

const byId = {};
function reg(el) { if (el.id) byId[el.id] = el; return el; }
reg(new Element("main", "homeMain", "home-main"));
reg(new Element("div", "welcome", "welcome"));
const thread = reg(new Element("div", "homeThread", "home-thread"));
const turnStatusBar = reg(new Element("div", "turnStatusBar", "turn-status-bar"));
turnStatusBar.hidden = true;
const turnStatusText = reg(new Element("span", "turnStatusText", "turn-status-text"));
turnStatusBar.appendChild(turnStatusText);
thread.appendChild(turnStatusBar);
const input = reg(new Element("input", "homeInput"));
const send = reg(new Element("button", "homeSend", "send-btn-round"));
reg(new Element("div", "homeFollowups", "followup-chips"));

let copiedText = null;
const navigator = {
  clipboard: {
    writeText(text) {
      copiedText = text;
      return Promise.resolve();
    }
  }
};
const document = {
  documentElement: new Element("html"),
  activeElement: null,
  getElementById(id) { return byId[id] || null; },
  createElement(tag) { return new Element(tag); },
  querySelector() { return null; },
  querySelectorAll() { return []; },
  addEventListener() {},
};
const answerText = "Here is the fix:\n```python\n# greet\ndef greet(name):\n    return \"Hello\"\n```\nAnd the diff:\n```diff\n-old_line\n+new_line\n```";
const rawSse = `event: answer\ndata: ${answerText.split("\n").join("\ndata: ")}\n\nevent: done\ndata: \n\n`;
const fetchFn = () => {
  const encoder = new TextEncoder();
  let done = false;
  return Promise.resolve({
    ok: true,
    status: 200,
    headers: { get: () => "text/event-stream; charset=utf-8" },
    body: {
      getReader: () => ({
        read: () => {
          if (!done) { done = true; return Promise.resolve({ value: encoder.encode(rawSse), done: false }); }
          return Promise.resolve({ value: undefined, done: true });
        }
      })
    }
  });
};
const window = {
  document,
  TextDecoder,
  fetch: fetchFn,
  setTimeout: () => 1,
  clearTimeout: () => {},
  localStorage: { getItem: () => null, setItem: () => {} },
};

vm.runInNewContext(appJs, { window, document, navigator, fetch: fetchFn, TextDecoder, Number, console, Math, JSON, Date, Promise, Array, Object, String, RegExp, encodeURIComponent });
window.Orbit.bootstrapHome();

(async () => {
  input.value = "Show code";
  send.dispatchEvent({ type: "click" });
  await new Promise(r => setImmediate(r));
  await new Promise(r => setImmediate(r));

  const blocks = thread.querySelectorAll(".code-block");
  const pyHtml = blocks[0].querySelector(".code-content").innerHTML;
  const diffHtml = blocks[1].querySelector(".code-content").innerHTML;
  const copyBtn = blocks[0].querySelector(".code-copy-btn");
  copyBtn.dispatchEvent({ type: "click" });
  await new Promise(r => setImmediate(r));

  console.log(JSON.stringify({
    blockCount: blocks.length,
    pyLang: blocks[0].getAttribute("data-lang"),
    pyHtml,
    diffHtml,
    copiedText,
    copyBtnLabel: copyBtn.textContent,
  }));
})();
"""
        proc = subprocess.run(["node", "-e", node_script], capture_output=True, text=True, check=True)
        res = json.loads(proc.stdout.strip())
        self.assertEqual(res["blockCount"], 2)
        self.assertEqual(res["pyLang"], "python")
        self.assertIn("tok-comment", res["pyHtml"])
        self.assertIn("tok-keyword", res["pyHtml"])
        self.assertIn("tok-func", res["pyHtml"])
        self.assertIn("tok-string", res["pyHtml"])
        self.assertIn("tok-diff-del", res["diffHtml"])
        self.assertIn("tok-diff-add", res["diffHtml"])
        self.assertEqual(res["copiedText"], '# greet\ndef greet(name):\n    return "Hello"')
        self.assertEqual(res["copyBtnLabel"], "Copied")


if __name__ == "__main__":
    unittest.main()
