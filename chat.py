"""One-shot text chat with Gemini and the initial confirmed local tool."""

from __future__ import annotations

import sys

from providers.base import ChatMessage
from providers.gemini import GeminiProvider
from tools import build_tool_registry

SYSTEM_PROMPT = (
    "You are ORBIT, a helpful assistant. Reply clearly and concisely. "
    "Use write_note only when the user explicitly asks you to save a note, "
    "and pass the exact one-line note text they requested. Never claim it was "
    "saved unless the tool reports success. If the person declines, do not retry."
)


def main() -> int:
    try:
        provider = GeminiProvider(tool_registry=build_tool_registry())
    except RuntimeError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 2

    prompt = input("You: ").strip()
    if not prompt:
        print("Enter a message to send to Gemini.", file=sys.stderr)
        return 2

    answer = provider.reply(
        history=[ChatMessage(role="user", content=prompt)],
        system_prompt=SYSTEM_PROMPT,
    )
    print(f"ORBIT: {answer}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
