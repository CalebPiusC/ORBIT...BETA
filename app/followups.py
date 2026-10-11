"""Contextual follow-up chips for the home chat.

The rule this module exists to protect: a chip is only offered when there is
something real in the reply to act on. Today that means exactly one signal —
Orbit's last answer contains a code block — which is an observable property of
the text on screen, not a guess about what the conversation was "about".

Anything that cannot be tied to the reply is not offered. No chips at all is
better than chips that suggest Orbit can do something it was never asked about.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# A fenced block with something in it: ```lang\n … ```
CODE_FENCE_RE = re.compile(r"```[ \t]*[A-Za-z0-9_+#.-]*\r?\n[^`]*?```", re.DOTALL)


@dataclass(frozen=True)
class Followup:
    label: str
    prompt: str

    def as_dict(self) -> dict[str, str]:
        return {"label": self.label, "prompt": self.prompt}


# Three actions that are genuinely available on code Orbit just produced.
CODE_FOLLOWUPS: tuple[Followup, ...] = (
    Followup(
        "Explain this code",
        "Walk me through the code you just wrote: what each part does, and why you "
        "structured it that way.",
    ),
    Followup(
        "Find potential bugs",
        "Read back the code you just wrote and list the bugs, edge cases and failure "
        "modes you can find in it. Say how you would fix each one.",
    ),
    Followup(
        "Improve implementation",
        "Improve the code you just wrote: clearer, safer and more complete. Show the "
        "revised version and explain what changed and why.",
    ),
)


def has_code_block(text: str) -> bool:
    return bool(text) and bool(CODE_FENCE_RE.search(text))


def suggest_followups(answer: str) -> tuple[Followup, ...]:
    """Chips for this reply, or an empty tuple when nothing is genuinely relevant."""
    if has_code_block(answer):
        return CODE_FOLLOWUPS
    return ()


def last_orbit_answer(messages: list[object]) -> str:
    """The text of the newest Orbit reply in a thread ("" when there is none)."""
    for message in reversed(list(messages)):
        if getattr(message, "is_orbit", False) and getattr(message, "action", None) is None:
            return getattr(message, "body", "") or ""
    return ""


def followups_for(messages: list[object]) -> tuple[Followup, ...]:
    return suggest_followups(last_orbit_answer(messages))
