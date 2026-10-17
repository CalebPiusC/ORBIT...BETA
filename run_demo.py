"""Demo launcher: run ORBIT's web UI with a STUBBED streaming provider.

This lets you exercise the full UI and SSE streaming flow without a Gemini API
key. For the real model, run `python -m app.server` with GEMINI_API_KEY set in
.env — that path uses the real GeminiProvider.reply_stream against live Gemini.

The stub here only stands in for the model; everything else (routes, templates,
the store, the approval gate, SSE wiring) is the real code path.
"""

from __future__ import annotations

import os
import secrets
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
        self,
        history,
        system_prompt: str,
        *,
        think: bool = False,
        report_attempts: bool = False,
        allow_tools: bool = False,
        thinking_level: str = "quick",
    ) -> Iterator[StreamChunk]:
        if report_attempts:
            yield StreamChunk(
                kind="status",
                text=f"STUB: no model called for {thinking_level.title()} mode",
            )
        if think:
            # Narrated in character so the Agent panel has real evidence to work
            # with: the checklist moves because a narration line mentioned it.
            yield StreamChunk(
                kind="thinking",
                text=(
                    "STUB: no model called — narrating as if I had read the "
                    "repository and found the order code in orders.py."
                ),
            )
            yield StreamChunk(kind="answer", text="STUB: this text is canned. Run `python -m app.server` with GEMINI_API_KEY in .env for your real model.")
        else:
            yield StreamChunk(kind="answer", text="STUB: this text is canned, not Gemini. Run `python -m app.server` with GEMINI_API_KEY in .env.")


def main() -> None:
    print("ORBIT demo UI · provider = StubStreamingProvider (no network, canned replies)")
    print("For the real model: set GEMINI_API_KEY in .env and run  python -m app.server")
    # run_app refuses to serve on the committed default session secret, and it
    # is right to. The demo mints a random one per run instead of shipping a
    # known key: sessions then do not survive a restart, which is the correct
    # trade for a launcher whose replies are canned anyway. Set
    # ORBIT_SECRET_KEY in .env if you want them to.
    app = create_app(provider=StubStreamingProvider(), store=Store())
    if not os.environ.get("ORBIT_SECRET_KEY", "").strip():
        app.secret_key = secrets.token_urlsafe(48)
        print("ORBIT_SECRET_KEY not set — signing this run's sessions with a random key.")
    run_app(app)


if __name__ == "__main__":
    main()
