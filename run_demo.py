"""Demo launcher: run ORBIT's web UI with a STUBBED streaming provider.

This lets you exercise the full UI and SSE streaming flow without a Gemini API
key. For the real model, run `python -m app.server` with GEMINI_API_KEY set in
.env — that path uses the real GeminiProvider.reply_stream against live Gemini.

The stub here only stands in for the model; everything else (routes, templates,
the store, the approval gate, SSE wiring) is the real code path.
"""

from __future__ import annotations

from collections.abc import Iterator

from providers.base import StreamChunk

from app.server import create_app
from app.store import Store


class StubStreamingProvider:
    """Canned stand-in for GeminiProvider — no network, deterministic chunks."""

    def reply(self, history, system_prompt: str) -> str:
        return "A canned one-shot reply."

    def reply_stream(
        self, history, system_prompt: str, *, think: bool = False
    ) -> Iterator[StreamChunk]:
        if think:
            yield StreamChunk(kind="thinking", text="I am checking the repository context before changing anything.")
            yield StreamChunk(kind="answer", text="Here is my plan: add a status field, then wire it into the lookup reply.")
        else:
            yield StreamChunk(kind="answer", text="Hello from Orbit — how can I help?")


def main() -> None:
    app = create_app(provider=StubStreamingProvider(), store=Store())
    app.run(host="0.0.0.0", port=5000, debug=True)


if __name__ == "__main__":
    main()
