"""Conversations: the composer position, history titles, and the naming call.

Offline. The provider is a stub, so no network and no real model calls.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from collections.abc import Iterator
from pathlib import Path

from providers.base import StreamChunk

from app.server import create_app
from app.store import PLACEHOLDER_TITLE, Store
from app.titles import UNTITLED
from tests.test_app import parse_sse
from tools.registry import AuditLog

ROOT = Path(__file__).resolve().parents[1]


class TitlingProvider:
    """Streams one canned answer per call and records every title request."""

    def __init__(self, answers: list[str], title: str = "Order status tracking", fail_title: bool = False) -> None:
        self.answers = list(answers)
        self.title_text = title
        self.fail_title = fail_title
        self.title_calls: list[tuple[str, str]] = []

    def reply(self, history, system_prompt: str) -> str:  # pragma: no cover - not used by the chat route
        raise AssertionError("the chat route must stream, not call reply()")

    def title(self, user_text: str, answer: str) -> str:
        self.title_calls.append((user_text, answer))
        if self.fail_title:
            raise RuntimeError("naming call failed")
        return self.title_text

    def reply_stream(
        self, history, system_prompt: str, *, think: bool = False, report_attempts: bool = False, thinking_level: str = "quick"
    ) -> Iterator[StreamChunk]:
        text = self.answers.pop(0) if self.answers else "Okay."
        if text:
            yield StreamChunk(kind="answer", text=text)


def _client(provider, store: Store | None = None):
    store = store or Store()
    return create_app(provider=provider, store=store).test_client(), store


def _send(client, text: str) -> list[tuple[str, str]]:
    resp = client.post("/api/chat", json={"message": text}, content_type="application/json")
    return parse_sse(resp.get_data(as_text=True))


class StoreConversationTests(unittest.TestCase):
    def test_a_fresh_store_has_no_active_conversation_and_an_empty_thread(self) -> None:
        store = Store()
        self.assertIsNone(store.active_conversation_id)
        self.assertEqual(store.home_thread, [])

    def test_start_conversation_puts_a_placeholder_at_the_top_of_today(self) -> None:
        store = Store()
        conv = store.start_conversation()
        self.assertEqual(store.active_conversation_id, conv.id)
        self.assertEqual(conv.title, PLACEHOLDER_TITLE)
        self.assertEqual(store.conversations_grouped()["Today"][0].id, conv.id)
        self.assertFalse(conv.titled)

    def test_new_conversation_resets_to_fresh_and_keeps_the_old_thread(self) -> None:
        store = Store()
        conv = store.start_conversation()
        store.new_conversation()
        self.assertIsNone(store.active_conversation_id)
        self.assertEqual(store.home_thread, [])
        self.assertIsNotNone(store.get_conversation(conv.id))

    def test_each_conversation_has_its_own_thread(self) -> None:
        store = Store()
        first = store.start_conversation()
        store.append_message(first.id, _msg("hello"))
        store.new_conversation()
        second = store.start_conversation()
        self.assertEqual(store.home_thread, [])
        store.append_message(second.id, _msg("other"))
        self.assertEqual([m.body for m in store.threads[first.id]], ["hello"])
        self.assertEqual([m.body for m in store.threads[second.id]], ["other"])


def _msg(body: str):
    from app.store import ChatMessage

    return ChatMessage(who="You", time="09:00", body=body, is_orbit=False)


class ComposerPositionTests(unittest.TestCase):
    def test_a_fresh_conversation_centers_the_composer(self) -> None:
        client, _ = _client(TitlingProvider([]))
        body = client.get("/").get_data(as_text=True)
        self.assertIn('<div class="home-main" id="homeMain">', body)
        self.assertIn("Good ", body)  # the greeting shows only when centered

    def test_after_a_send_the_composer_is_docked_on_reload(self) -> None:
        client, _ = _client(TitlingProvider(["Hi there."]))
        _send(client, "hello")
        body = client.get("/").get_data(as_text=True)
        self.assertIn('<div class="home-main is-docked" id="homeMain">', body)

    def test_new_conversation_resets_to_a_centered_composer(self) -> None:
        client, _ = _client(TitlingProvider(["Hi there."]))
        _send(client, "hello")
        resp = client.post("/api/conversations/new", json={}, content_type="application/json")
        self.assertEqual(resp.get_json(), {"active_conversation_id": None})
        body = client.get("/").get_data(as_text=True)
        self.assertIn('<div class="home-main" id="homeMain">', body)
        self.assertNotIn('class="history-item active"', body)

    def test_the_new_conversation_control_is_wired(self) -> None:
        client, _ = _client(TitlingProvider([]))
        self.assertIn('id="newConvo"', client.get("/").get_data(as_text=True))


class HistoryTitleTests(unittest.TestCase):
    def test_the_first_send_announces_the_conversation_before_the_reply(self) -> None:
        provider = TitlingProvider(["Your order ships Friday."])
        client, _ = _client(provider)
        events = _send(client, "where is my order")
        kinds = [k for k, _ in events]
        self.assertEqual(kinds[0], "conversation")
        created = json.loads(events[0][1])
        self.assertEqual(created["title"], PLACEHOLDER_TITLE)
        self.assertLess(kinds.index("conversation"), kinds.index("answer"))

    def test_the_title_arrives_after_the_first_exchange_completes(self) -> None:
        provider = TitlingProvider(["Your order ships Friday."], title="Order shipping date")
        client, store = _client(provider)
        events = _send(client, "where is my order")
        kinds = [k for k, _ in events]
        self.assertLess(kinds.index("done"), kinds.index("title"))
        named = json.loads(events[kinds.index("title")][1])
        self.assertEqual(named["title"], "Order shipping date")
        conv = store.get_conversation(named["id"])
        self.assertEqual(conv.title, "Order shipping date")
        self.assertEqual(provider.title_calls, [("where is my order", "Your order ships Friday.")])

    def test_the_title_is_the_model_title_not_the_first_message(self) -> None:
        provider = TitlingProvider(["Sure."], title="Pricing page copy")
        client, _ = _client(provider)
        first = _send(client, "Could you please draft some pricing page copy for the new plan tiers today?")
        titles = [json.loads(d)["title"] for k, d in first if k == "title"]
        self.assertEqual(titles, ["Pricing page copy"])
        self.assertNotIn("Could you", titles[0], "the title is not a slice of the first message")

    def test_only_one_naming_call_per_conversation(self) -> None:
        provider = TitlingProvider(["One.", "Two.", "Three."])
        client, _ = _client(provider)
        _send(client, "first")
        _send(client, "second")
        _send(client, "third")
        self.assertEqual(len(provider.title_calls), 1)

    def test_a_failed_naming_call_is_untitled_and_never_retried(self) -> None:
        provider = TitlingProvider(["One.", "Two."], fail_title=True)
        client, store = _client(provider)
        events = _send(client, "first")
        named = json.loads(next(d for k, d in events if k == "title"))
        self.assertEqual(named["title"], UNTITLED)
        self.assertEqual(store.get_conversation(named["id"]).title, UNTITLED)
        _send(client, "second")
        self.assertEqual(len(provider.title_calls), 1, "a failed naming call is not retried")

    def test_a_turn_with_no_answer_does_not_use_up_the_naming_call(self) -> None:
        provider = TitlingProvider(["", "Real answer."], title="Real title")
        with tempfile.TemporaryDirectory() as temp_dir:
            client = create_app(
                provider=provider,
                store=Store(),
                model_audit=AuditLog(Path(temp_dir) / "audit.log"),
            ).test_client()
            first = _send(client, "first")
            self.assertIn("error", [k for k, _ in first])  # the empty turn is reported
            self.assertEqual(provider.title_calls, [])
            second = _send(client, "second")
            named = [json.loads(d)["title"] for k, d in second if k == "title"]
            self.assertEqual(named, ["Real title"])

    def test_a_steered_send_reuses_the_same_conversation(self) -> None:
        provider = TitlingProvider(["One.", "Two."])
        client, store = _client(provider)
        first = _send(client, "first")
        conv_id = json.loads(next(d for k, d in first if k == "conversation"))["id"]
        second = _send(client, "second")
        self.assertNotIn("conversation", [k for k, _ in second])
        self.assertEqual(len(store.threads[conv_id]), 4)
        self.assertEqual(len(store.conversations_grouped()["Today"]), 3)  # 2 seeded + 1 new


class TemplateSyncTests(unittest.TestCase):
    """The mockup is the spec: it must carry the same composer structure and rules."""

    def test_mockup_has_the_composer_dock_and_the_docked_rules(self) -> None:
        mock = (ROOT / "design" / "orbit_mockup.html").read_text(encoding="utf-8")
        app_css = (ROOT / "app" / "templates" / "layout.html").read_text(encoding="utf-8")
        self.assertIn('class="composer-dock"', mock)
        for selector in (".composer-dock", ".home-main.is-docked .composer-dock", ".home-main.is-docked .home-center"):
            self.assertIn(selector, mock)
            self.assertIn(selector, app_css)

    def test_new_conversation_and_history_markup_match_the_app(self) -> None:
        mock = (ROOT / "design" / "orbit_mockup.html").read_text(encoding="utf-8")
        self.assertIn("+ New conversation", mock)
        self.assertRegex(mock, r'class="history-item active"')


if __name__ == "__main__":
    unittest.main()
