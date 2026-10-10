"""Interactive chat with Gemini, through the same gated provider as the UI.

One-shot on purpose at first; that made it feel broken next to the web chat, so
it keeps the connection open now. Conversation history carries across turns, a
provider failure ends the turn and not the session, and the loop itself is
testable via ``run_session`` (the real Gemini call is still a manual check).
"""

from __future__ import annotations

import sys
from collections.abc import Callable
from typing import Any

from providers.base import ChatMessage
from providers.gemini import GeminiProvider
from tools import build_tool_registry

SYSTEM_PROMPT = (
    "You are ORBIT, a helpful assistant. Reply clearly and concisely. "
    "Use write_note only when the user explicitly asks you to save a note, "
    "and pass the exact one-line note text they requested. Never claim it was "
    "saved unless the tool reports success. If the person declines, do not retry."
)

# History is capped so a long session cannot grow the request without limit.
MAX_MESSAGES = 20

HELP = "Enter sends. /help this list · /reset clears history · /quit exits."


def run_session(
    provider: Any,
    *,
    read: Callable[[str], str] = input,
    write: Callable[[str], None] = print,
) -> int:
    """Drive the prompt/reply loop. Returns a process exit code.

    ``read``/``write`` are injectable so the loop is testable offline, and so a
    non-interactive stdin (EOF) exits cleanly instead of raising.
    """
    history: list[ChatMessage] = []
    write(HELP)

    while True:
        try:
            prompt = read("\nYou: ").strip()
        except (EOFError, KeyboardInterrupt):
            write("")
            return 0

        if not prompt:
            continue
        if prompt in ("/quit", "/exit"):
            return 0
        if prompt == "/help":
            write(HELP)
            continue
        if prompt == "/reset":
            history.clear()
            write("(history cleared)")
            continue

        history.append(ChatMessage(role="user", content=prompt))
        if len(history) > MAX_MESSAGES:
            del history[: len(history) - MAX_MESSAGES]

        try:
            answer = provider.reply(history=history, system_prompt=SYSTEM_PROMPT)
        except Exception as exc:  # a failed turn must not end the session
            history.pop()  # keep the user's turn so they can simply send it again
            write(f"ORBIT: [error] {type(exc).__name__}: {exc}")
            write("(your message is still queued — send it again to retry)")
            continue

        history.append(ChatMessage(role="assistant", content=answer))
        write(f"\nORBIT: {answer}")


def main() -> int:
    try:
        provider = GeminiProvider(tool_registry=build_tool_registry())
    except RuntimeError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 2

    print(f"ORBIT · {provider.model} · chat mode (tools are gated by the confirmation prompt)")
    return run_session(provider)


if __name__ == "__main__":
    raise SystemExit(main())
