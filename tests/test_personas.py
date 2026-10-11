"""Model display names and status lines, offline.

Rules under test:
- Names follow slot order: slot 0 (primary) is Titan, slot 1 (fallback) is Europa.
  Io, Ganymede, Callisto, Triton and Luna are reserved for future slots, in that
  order. Nothing is named past
  the assigned slots.
- Status lines ("Asking Titan…") appear only when the caller asks for them
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
from providers.personas import RESERVED_NAMES, SLOT_NAMES, slot_name

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
        self.configs: list[Any] = []

    def generate_content_stream(self, *, model: str, contents: Any, config: Any):
        self.calls.append(model)
        self.configs.append(config)
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
        self.assertEqual(slot_name(0), "Titan")  # primary
        self.assertEqual(slot_name(1), "Europa")  # fallback
        self.assertEqual(SLOT_NAMES, ("Titan", "Europa"))

    def test_reserved_names_are_not_handed_out(self) -> None:
        self.assertEqual(RESERVED_NAMES, ("Io", "Ganymede", "Callisto", "Triton", "Luna"))
        for name in ("Io", "Ganymede", "Callisto", "Triton", "Luna"):
            self.assertIn(name, RESERVED_NAMES)
            self.assertNotIn(name, SLOT_NAMES)

    def test_no_name_past_the_assigned_slots(self) -> None:
        with self.assertRaises(ValueError) as caught:
            slot_name(len(SLOT_NAMES))
        self.assertIn("Io", str(caught.exception))  # the error says what is reserved


class AgentModeStatusTests(unittest.TestCase):
    def test_primary_answer_is_announced_before_the_reply(self) -> None:
        provider = provider_for({PRIMARY: ["Hello ", "there."]})
        out = run(provider, report_attempts=True)
        self.assertEqual(out[0], ("status", "Asking Titan…"))
        self.assertEqual(
            [text for kind, text in out if kind == "answer"], ["Hello ", "there."]
        )

    def test_failover_names_both_models_in_order(self) -> None:
        provider = provider_for({PRIMARY: api_error(503), FALLBACK: ["From Europa."]})
        out = run(provider, report_attempts=True)
        statuses = [text for kind, text in out if kind == "status"]
        self.assertEqual(
            statuses,
            ["Asking Titan…", "Titan didn't answer (Google returned 503). Asking Europa…"],
        )
        self.assertEqual([t for k, t in out if k == "answer"], ["From Europa."])
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


class QuickDeepSelectionTests(unittest.TestCase):
    def test_quick_uses_primary_model_and_tight_budget(self) -> None:
        provider = provider_for({PRIMARY: ["quick answer"]})
        out = run(provider, thinking_level="quick", report_attempts=True)
        models = provider._client.models
        self.assertEqual(models.calls, [PRIMARY])
        self.assertEqual(models.configs[0].max_output_tokens, 512)
        self.assertEqual([text for kind, text in out if kind == "status"], ["Asking Titan…"])
        self.assertEqual(provider.last_model_used, PRIMARY)

    def test_deep_prefers_fallback_with_larger_budget_and_actual_persona(self) -> None:
        provider = provider_for({FALLBACK: ["deep answer"]})
        out = run(provider, thinking_level="deep", report_attempts=True)
        models = provider._client.models
        self.assertEqual(models.calls, [FALLBACK])
        self.assertEqual(models.configs[0].max_output_tokens, 4096)
        self.assertEqual([text for kind, text in out if kind == "status"], ["Asking Europa…"])
        self.assertEqual(provider.last_model_used, FALLBACK)

    def test_deep_failover_labels_the_primary_that_actually_answers(self) -> None:
        provider = provider_for({FALLBACK: api_error(504), PRIMARY: ["primary fallback"]})
        out = run(provider, thinking_level="deep", report_attempts=True)
        statuses = [text for kind, text in out if kind == "status"]
        self.assertEqual(
            statuses,
            ["Asking Europa…", "Europa didn't answer (Google returned 504). Asking Titan…"],
        )
        self.assertEqual(provider.last_model_used, PRIMARY)

    def test_deep_without_a_configured_fallback_uses_primary_with_deep_budget(self) -> None:
        provider = GeminiProvider(
            client=SimpleNamespace(models=StreamScript({PRIMARY: ["deep"]})),
            model=PRIMARY,
            fallback_model="",
            audit_log=None,
        )
        out = run(provider, thinking_level="deep", report_attempts=True)
        models = provider._client.models
        self.assertEqual(models.calls, [PRIMARY])
        self.assertEqual(models.configs[0].max_output_tokens, 4096)
        self.assertEqual([text for kind, text in out if kind == "status"], ["Asking Titan…"])

    def test_rejects_unknown_thinking_level(self) -> None:
        provider = provider_for({PRIMARY: ["not used"]})
        with self.assertRaisesRegex(ValueError, "thinking_level"):
            run(provider, thinking_level="extreme")
        self.assertEqual(provider._client.models.calls, [])


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
