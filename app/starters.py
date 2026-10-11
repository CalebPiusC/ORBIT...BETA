"""The three shortcuts on the welcome screen.

They are real prompts, not labels: clicking one sends it through the ordinary
composer path, so it is a conversation starter the person could have typed.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Starter:
    label: str
    prompt: str

    def as_dict(self) -> dict[str, str]:
        return {"label": self.label, "prompt": self.prompt}


WELCOME_STARTERS: tuple[Starter, ...] = (
    Starter(
        "Build something",
        "Help me build something new. Ask me what it should do, then propose the "
        "smallest version that actually works.",
    ),
    Starter(
        "Explore an idea",
        "I want to think through an idea. Ask me the questions that would sharpen "
        "it, then summarize what we learn.",
    ),
    Starter(
        "Review my work",
        "Review the work I've done: tell me what's weak, what's missing, and what "
        "I should fix first.",
    ),
)
