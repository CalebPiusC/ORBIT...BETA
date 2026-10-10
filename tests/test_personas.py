"""Model display names and status lines, offline.

Rules under test:
- Names follow slot order: slot 0 (primary) is Chidi, slot 1 (fallback) is Ada,
  then Obi, Chika, Zara, Jiden, Oma. Nothing is named past that.
- Status lines ("Asking Chidi…") appear only when the caller asks for them
  (Agent mode). Chat mode gets no status line, even when the primary fails.
- Status lines never mix into the reply or thinking text.
- Answer text reaches the caller as it is generated, not at the end.
"""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from typing import Any

import httpx
from google.genai.errors import APIError

from providers.base import ChatMessage
from providers.gemini import GeminiProvider
from providers.personas import SLOT_NAMES, slot_name

PRIMARY = "primary-test-model"
FALLBACK = "fallback-test-model"
HISTORY = [ChatMessage(role="user", content="Hi")]


def api_error(code: int) -> APIError:
    return APIError(code, {"error": {"code": code, "message": "x", "status": "X"}}, None)


class StreamScript:
    """Each model name gets one outcome: a list of text pieces, or an exception."""

    def __init__(self, script: dict[str, Any]) -> None:
        self._script = dict(script)
        self.calls: list[str] = []

    def generate_content_stream(self, *, model: str, contents: Any, config: Any):
        self.calls.append(model)
        outcome = self._script[model]
        if isinstance(outcome, BaseException):
            raise outcome

        def pieces():
            for text in outcome:
                yield SimpleNamespace(text=text, parts=[])

        return pieces()


def provider_for(script: dict[str, Any]) -> GeminiProvider:
    client = SimpleNamespace(models=StreamScript(script))
    return GeminiProvider(client=client, model=PRIMARY, fallback_model=FALLBACK, audit_log=None)


def run(provider: GeminiProvider, **kwargs: Any) -> list[tuple[str, str]]:
    return [(c.kind, c.text) for c in provider.reply_stream(HISTORY, "sys", **kwargs)]


class SlotNameTests(unittest.TestCase):
    def test_names_follow_slot_order(self) -> None:
        self.assertEqual(slot_name(0), "Chidi")  # primary
        self.assertEqual(slot_name(1), "Ada")  # fallback
        self.assertEqual(
            SLOT_NAMES[2:], ("Obi", "Chika", "Zara", "Jiden", "Oma")
        )
        for slot, name in enumerate(SLOT_NAMES):
            self.assertEqual(slot_name(slot), name)

    def test_no_name_past_the_reserved_list(self) -> None:
        with self.assertRaises(ValueError):
            slot_name(len(SLOT_NAMES))


class AgentModeStatusTests(unittest.TestCase):
    def test_primary_answer_is_announced_before_the_reply(self) -> None:
        provider = provider_for({PRIMARY: ["Hello ", "there."]})
        out = run(provider, report_attempts=True)
        self.assertEqual(out[0], ("status", "Asking Chidi…"))
        self.assertEqual(
            [text for kind, text in out if kind == "answer"], ["Hello ", "there."]
        )

    def test_failover_names_both_models_in_order(self) -> None:
        provider = provider_for({PRIMARY: api_error(503), FALLBACK: ["From Ada."]})
        out = run(provider, report_attempts=True)
        statuses = [text for kind, text in out if kind == "status"]
        self.assertEqual(
            statuses,
            ["Asking Chidi…", "Chidi didn't answer (Google returned 503). Asking Ada…"],
        )
        self.assertEqual([t for k, t in out if k == "answer"], ["From Ada."])
        # Status lines come before any reply text.
        kinds = [k for k, _ in out]
        self.assertLess(kinds.index("status"), kinds.index("answer"))

    def test_status_lines_never_carry_model_ids(self) -> None:
        provider = provider_for({PRIMARY: api_error(504), FALLBACK: ["ok"]})
        for kind, text in run(provider, report_attempts=True):
            if kind == "status":
                self.assertNotIn(PRIMARY, text)
                self.assertNotIn(FALLBACK, text)

    def test_timeouts_are_described_in_plain_words(self) -> None:
        provider = provider_for(
            {PRIMARY: httpx.ReadTimeout("slow"), FALLBACK: ["ok"]}
        )
        statuses = [t for k, t in run(provider, report_attempts=True) if k == "status"]
        self.assertIn("the request timed out", statuses[1])

    def test_status_text_does_not_leak_into_thinking_or_reply(self) -> None:
        provider = provider_for(
            {PRIMARY: api_error(503), FALLBACK: ["<thinking>Plan.</thinking>", "Done."]}
        )
        out = run(provider, think=True, report_attempts=True)
        thinking = "".join(t for k, t in out if k == "thinking")
        answer = "".join(t for k, t in out if k == "answer")
        self.assertEqual(thinking, "Plan.")
        self.assertEqual(answer, "Done.")


class ChatModeHasNoStatusTests(unittest.TestCase):
    def test_no_status_when_not_requested_even_on_failover(self) -> None:
        provider = provider_for({PRIMARY: api_error(503), FALLBACK: ["Plain reply."]})
        out = run(provider)  # report_attempts defaults to False
        self.assertNotIn("status", [k for k, _ in out])
        self.assertEqual(out, [("answer", "Plain reply.")])

    def test_no_status_on_a_normal_chat_turn(self) -> None:
        out = run(provider_for({PRIMARY: ["Hi."]}))
        self.assertEqual(out, [("answer", "Hi.")])


class ReplyStreamsAsItArrivesTests(unittest.TestCase):
    def test_answer_pieces_arrive_before_the_stream_finishes(self) -> None:
        pulled: list[str] = []

        class Models:
            def generate_content_stream(self, *, model, contents, config):
                def pieces():
                    for text in ["Hel", "lo ", "wor", "ld"]:
                        pulled.append(text)
                        yield SimpleNamespace(text=text, parts=[])

                return pieces()

        provider = GeminiProvider(
            client=SimpleNamespace(models=Models()), model=PRIMARY, fallback_model=None, audit_log=None
        )
        seen_before_end: list[int] = []
        for chunk in provider.reply_stream(HISTORY, "sys", think=True):
            if chunk.kind == "answer":
                seen_before_end.append(len(pulled))
        self.assertEqual(len(seen_before_end), 4)
        self.assertLess(seen_before_end[0], 4)  # the first answer piece left early


if __name__ == "__main__":
    unittest.main()
