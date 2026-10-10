from __future__ import annotations

import json
import unittest
from collections.abc import Iterator
from typing import Any

from providers.base import ChatMessage, StreamChunk

from app.server import create_app
from app.store import Connection, Store


class FakeStreamingProvider:
    """Stand-in for GeminiProvider: returns canned streamed chunks, no network."""

    def reply(self, history, system_prompt: str) -> str:
        return "A canned one-shot reply."

    def reply_stream(
        self, history, system_prompt: str, *, think: bool = False
    ) -> Iterator[StreamChunk]:
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
        self.assertEqual(events[-1][0], "done")
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

    def test_task_stream_requires_message_when_not_initial(self):
        resp = self.client.post(
            "/api/task/T1/stream", json={}, content_type="application/json"
        )
        self.assertEqual(resp.status_code, 400)


if __name__ == "__main__":
    unittest.main()
