"""AI project summaries, written once from a real first exchange.

Mirrors ``app/titles.py``. After the first completed Main-Agent exchange in a
project's workspace, one lightweight model call summarizes what the project
is, and ``ProjectStore.set_summary`` stores it — so a Projects-page card can
show a genuinely AI-generated description. Until that call lands, the card
shows the owner's own description or nothing: an "AI summary" is never
assembled at render time from the description's own words.

A failed call writes nothing and is not retried for that exchange (the
``titles.py`` rule), so a flaky model can never spend a call per message.
"""

from __future__ import annotations

from typing import Any

MAX_SUMMARY_CHARS = 280

_QUOTE_CHARS = "\"'`*“”‘’"


def clean_summary(raw: str) -> str:
    """Turn a model's raw answer into one clean summary, or raise ValueError.

    Collapses whitespace, drops a leading ``Summary:`` label and wrapping
    quotes, and caps the length at a word boundary. Trailing punctuation is
    kept — a summary is a sentence, unlike a title.
    """
    text = " ".join((raw or "").split())
    text = text.strip(_QUOTE_CHARS + " ")
    if text.lower().startswith("summary:"):
        text = text[len("summary:") :].strip(_QUOTE_CHARS + " ")
    if len(text) > MAX_SUMMARY_CHARS:
        cut = text[:MAX_SUMMARY_CHARS]
        space = cut.rfind(" ")
        text = (cut[:space] if space > 0 else cut).rstrip(".!?:;,") + "…"
    if not text:
        raise ValueError("the model returned an empty summary")
    return text


def request_summary(
    provider: Any,
    project_name: str,
    description: str,
    user_text: str,
    answer: str,
) -> str | None:
    """Ask the provider for a project summary. Any failure yields ``None`` —
    nothing is written, and the card keeps its honest fallback. Never raises.
    """
    summarizer = getattr(provider, "summarize", None)
    if summarizer is None:
        return None
    try:
        return clean_summary(summarizer(project_name, description, user_text, answer))
    except Exception:  # a summary failure must not break the chat turn
        return None
