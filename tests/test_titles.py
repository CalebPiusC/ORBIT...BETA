"""Conversation titles: cleaning, failure handling, and the no-tools title call.

Offline. The SDK is replaced by a script, as in test_fallback.py.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from app.titles import MAX_TITLE_CHARS, UNTITLED, clean_title, request_title
from providers.base import ChatMessage
from providers.gemini import GeminiProvider
from tests.test_fallback import FALLBACK, PRIMARY, ScriptedModels, api_error, reply
from tools import build_tool_registry


class CleanTitleTests(unittest.TestCase):
    def test_strips_quotes_label_and_trailing_punctuation(self) -> None:
        self.assertEqual(clean_title('  "Order status tracking."  '), "Order status tracking")
        self.assertEqual(clean_title("Title: Pricing page copy!"), "Pricing page copy")
        self.assertEqual(clean_title("**Bot order lookup**"), "Bot order lookup")

    def test_collapses_newlines_and_spacing(self) -> None:
        self.assertEqual(clean_title("Order\n\n   status\ttracking"), "Order status tracking")

    def test_long_titles_are_cut_at_a_word_boundary(self) -> None:
        long = "word " * 40
        cleaned = clean_title(long)
        self.assertLessEqual(len(cleaned), MAX_TITLE_CHARS)
        self.assertFalse(cleaned.endswith(" "))
        self.assertTrue(set(cleaned.split()) == {"word"})

    def test_empty_titles_are_refused(self) -> None:
        for raw in ("", "   ", '""', "Title:"):
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                clean_title(raw)


class _TitleStub:
    def __init__(self, result=None, error=None) -> None:
        self.result = result
        self.error = error
        self.calls = 0

    def title(self, user_text: str, answer: str) -> str:
        self.calls += 1
        if self.error is not None:
            raise self.error
        return self.result


class RequestTitleTests(unittest.TestCase):
    def test_returns_the_cleaned_model_title(self) -> None:
        stub = _TitleStub(result='"Track my order."')
        self.assertEqual(request_title(stub, "where is my order", "It is on the way."), "Track my order")
        self.assertEqual(stub.calls, 1)

    def test_a_failed_call_is_untitled_not_a_truncation(self) -> None:
        stub = _TitleStub(error=RuntimeError("network down"))
        self.assertEqual(request_title(stub, "a very long first message " * 5, "reply"), UNTITLED)

    def test_an_empty_answer_is_untitled(self) -> None:
        self.assertEqual(request_title(_TitleStub(result="   "), "hi", "hello"), UNTITLED)

    def test_a_provider_without_title_is_untitled(self) -> None:
        self.assertEqual(request_title(object(), "hi", "hello"), UNTITLED)


class GeminiTitleCallTests(unittest.TestCase):
    """The title call is one lightweight request. It carries no tool declarations."""

    class CapturingModels(ScriptedModels):
        def __init__(self, script) -> None:
            super().__init__(script)
            self.configs: list[object] = []

        def generate_content(self, *, model, contents, config):
            self.configs.append(config)
            return super().generate_content(model=model, contents=contents, config=config)

    def _provider(self, script, temp: Path, registry):
        models = self.CapturingModels(script)
        provider = GeminiProvider(
            model=PRIMARY,
            fallback_model=FALLBACK,
            client=SimpleNamespace(models=models),
            tool_registry=registry,
            audit_log=None,
        )
        return provider, models

    def test_title_call_has_no_tools_even_when_tools_are_registered(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            registry = build_tool_registry(
                audit_log_path=temp / "audit.log",
                notes_path=temp / "notes.txt",
                facts_path=temp / "facts.txt",
            )
            self.assertTrue(registry.definitions, "the registry must have tools for this to mean anything")
            provider, models = self._provider({PRIMARY: [reply("Order tracking")]}, temp, registry)
            title = provider.title("where is my order", "It is on the way.")

        self.assertEqual(title, "Order tracking")
        self.assertEqual(len(models.configs), 1)
        self.assertFalse(getattr(models.configs[0], "tools", None))

    def test_title_call_falls_back_like_any_other_call(self) -> None:
        provider, models = self._provider(
            {PRIMARY: [api_error(503, "UNAVAILABLE")], FALLBACK: [reply("Pricing copy")]},
            Path(tempfile.gettempdir()),
            None,
        )
        self.assertEqual(provider.title("pricing", "here"), "Pricing copy")
        self.assertEqual(models.calls, [("generate", PRIMARY), ("generate", FALLBACK)])

    def test_base_title_goes_through_reply_with_the_title_prompt(self) -> None:
        from providers.base import ChatProvider, TITLE_SYSTEM_PROMPT

        seen: dict[str, object] = {}

        class Plain(ChatProvider):
            def reply(self, history, system_prompt):
                seen["history"] = list(history)
                seen["system"] = system_prompt
                return "Diff review"

        self.assertEqual(Plain().title("explain this diff", "It adds a field."), "Diff review")
        self.assertEqual(seen["system"], TITLE_SYSTEM_PROMPT)
        history = seen["history"]
        self.assertIsInstance(history[0], ChatMessage)
        self.assertIn("explain this diff", history[0].content)


if __name__ == "__main__":
    unittest.main()
