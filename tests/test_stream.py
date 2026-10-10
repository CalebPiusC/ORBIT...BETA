from __future__ import annotations

import unittest
from types import SimpleNamespace
from typing import Any

from providers.base import ChatMessage, StreamChunk
from providers.gemini import GeminiProvider, _split_thinking


def fake_streaming_client(chunks: list[str]) -> Any:
    """Build a client whose generate_content_stream yields text-only responses."""

    class FakeModels:
        def generate_content_stream(self, **kwargs: Any):
            for text in chunks:
                yield SimpleNamespace(text=text, parts=[])

    class FakeClient:
        models = FakeModels()

    return FakeClient()


class SplitThinkingTests(unittest.TestCase):
    def _run(self, chunks):
        return [(c.kind, c.text) for c in _split_thinking(iter(chunks))]

    def test_answer_only_when_no_tags(self):
        self.assertEqual(
            self._run(["Just a plain answer with no markers."]),
            [("answer", "Just a plain answer with no markers.")],
        )

    def test_thinking_then_answer_single_chunk(self):
        self.assertEqual(
            self._run(["<thinking>Let me reason.</thinking>Here is the answer."]),
            [("thinking", "Let me reason."), ("answer", "Here is the answer.")],
        )

    def test_thinking_streamed_across_chunks(self):
        chunks = ["<thi", "nking>rea", "soning", "</thinking>", "the answer"]
        self.assertEqual(
            self._run(chunks),
            [("thinking", "reasoning"), ("answer", "the answer")],
        )

    def test_multiline_thinking_preserved(self):
        self.assertEqual(
            self._run(["<thinking>Line one.\nLine two.</thinking>Final words"]),
            [("thinking", "Line one.\nLine two."), ("answer", "Final words")],
        )

    def test_answer_streams_chunk_by_chunk_not_at_the_end(self):
        # The answer must reach the caller while the model is still generating.
        # Record how many source pieces had been read when each answer piece came out.
        pulled = []

        def source():
            for piece in ["<thinking>r</thinking>", "Hel", "lo ", "there"]:
                pulled.append(piece)
                yield piece

        answers = []
        for chunk in _split_thinking(source()):
            if chunk.kind == "answer":
                answers.append((chunk.text, len(pulled)))
        self.assertEqual([text for text, _ in answers], ["Hel", "lo ", "there"])
        # "Hel" left before the stream was finished (4 pieces in total).
        self.assertLess(answers[0][1], 4)
        self.assertLess(answers[1][1], 4)

    def test_text_held_back_for_a_tag_is_not_lost_at_the_end(self):
        # "<th" could have been an opening tag; when the stream ends it was plain text.
        self.assertEqual(self._run(["<th"]), [("answer", "<th")])

    def test_no_closing_tag_keeps_thinking(self):
        # If the model never closes the tag, what streamed stays as thinking.
        out = self._run(["<thinking>some narration that never closes"])
        self.assertEqual(out[0][0], "thinking")


class GeminiStreamTests(unittest.TestCase):
    def test_reply_stream_answer_only(self):
        client = fake_streaming_client(["Hello ", "there, ", "Caleb."])
        provider = GeminiProvider(client=client)
        chunks = list(
            provider.reply_stream(
                [ChatMessage(role="user", content="Hi")],
                system_prompt="Be helpful.",
                think=False,
            )
        )
        self.assertEqual(
            [c.text for c in chunks], ["Hello ", "there, ", "Caleb."]
        )
        for c in chunks:
            self.assertEqual(c.kind, "answer")

    def test_reply_stream_with_thinking(self):
        client = fake_streaming_client(
            ["<thinking>Checking the repo.</thinking>Plan: add a status field."]
        )
        provider = GeminiProvider(client=client)
        chunks = list(
            provider.reply_stream(
                [ChatMessage(role="user", content="Start the task.")],
                system_prompt="Agent prompt.",
                think=True,
            )
        )
        kinds = [c.kind for c in chunks]
        self.assertIn("thinking", kinds)
        self.assertIn("answer", kinds)
        self.assertEqual(kinds[0], "thinking")
        self.assertEqual(kinds[-1], "answer")
        thinking_text = "".join(c.text for c in chunks if c.kind == "thinking")
        self.assertEqual(thinking_text, "Checking the repo.")
        answer_text = "".join(c.text for c in chunks if c.kind == "answer")
        self.assertEqual(answer_text, "Plan: add a status field.")

    def test_reply_stream_rejects_empty_history(self):
        provider = GeminiProvider(client=fake_streaming_client([]))
        with self.assertRaises(ValueError):
            list(provider.reply_stream([], "Be helpful.", think=False))


if __name__ == "__main__":
    unittest.main()
