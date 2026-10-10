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

from app.server import create_app, run_app
from app.store import Store


class StubStreamingProvider:
    """Canned stand-in for GeminiProvider — no network, deterministic chunks.

    Every reply is labelled STUB on purpose: the last thing this project needs is
    someone reading a canned sentence and believing their model answered.
    """

    def reply(self, history, system_prompt: str) -> str:
        return "STUB: canned one-shot reply. No model was called."

    def reply_stream(
        self, history, system_prompt: str, *, think: bool = False
    ) -> Iterator[StreamChunk]:
        if think:
            yield StreamChunk(kind="thinking", text="STUB: no model called — showing the thinking block.")
            yield StreamChunk(kind="answer", text="STUB: this text is canned. Run `python -m app.server` with GEMINI_API_KEY in .env for your real model.")
        else:
            yield StreamChunk(kind="answer", text="STUB: this text is canned, not Gemini. Run `python -m app.server` with GEMINI_API_KEY in .env.")


def main() -> None:
    print("ORBIT demo UI · provider = StubStreamingProvider (no network, canned replies)")
    print("For the real model: set GEMINI_API_KEY in .env and run  python -m app.server")
    app = create_app(provider=StubStreamingProvider(), store=Store())
    run_app(app)


if __name__ == "__main__":
    main()
