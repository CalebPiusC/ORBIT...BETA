"""Short titles for conversations, named once after the first exchange.

After a conversation's first completed exchange, ORBIT makes one lightweight
model call (``provider.title``) and stores the result in the sidebar history.
This is not a truncation of the first message. If the call fails for any
reason, the conversation is stored as ``UNTITLED`` and the call is not
retried, so a flaky model can never spend a call per message.
"""

from __future__ import annotations

from typing import Any

UNTITLED = "Untitled conversation"
PLACEHOLDER = "New conversation"  # shown in the sidebar until the title lands
MAX_TITLE_CHARS = 60

_QUOTE_CHARS = "\"'`*“”‘’"


def clean_title(raw: str) -> str:
    """Turn a model's raw answer into one short line, or raise ValueError.

    Collapses whitespace, drops a leading ``Title:`` label and wrapping quotes,
    removes trailing punctuation, and caps the length at a word boundary.
    """
    text = " ".join((raw or "").split())
    text = text.strip(_QUOTE_CHARS + " ")
    if text.lower().startswith("title:"):
        text = text[len("title:") :].strip(_QUOTE_CHARS + " ")
    text = text.rstrip(".!?:;,")
    if len(text) > MAX_TITLE_CHARS:
        cut = text[:MAX_TITLE_CHARS]
        space = cut.rfind(" ")
        text = cut[:space] if space > 0 else cut
        text = text.rstrip(".!?:;,")
    if not text:
        raise ValueError("the model returned an empty title")
    return text


def request_title(provider: Any, user_text: str, answer: str) -> str:
    """Ask the provider for a title. Any failure yields ``UNTITLED``; never raises."""
    titler = getattr(provider, "title", None)
    if titler is None:
        return UNTITLED
    try:
        return clean_title(titler(user_text, answer))
    except Exception:  # a naming failure must not break the chat turn
        return UNTITLED
