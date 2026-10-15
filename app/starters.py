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

# The workspace's Main Agent thread, when it has no messages yet. Same rule:
# real prompts the person could have typed, about THIS project — the Main
# Agent's context block is what makes "this project" mean something.
PROJECT_STARTERS: tuple[Starter, ...] = (
    Starter(
        "Summarize this project",
        "Tell me what this project is and what state it's in, based on everything "
        "you know about it.",
    ),
    Starter(
        "What first?",
        "Look at the tasks on this project's map and tell me which one I should "
        "start with and why. If the map is empty, help me break the project's goal "
        "into its first few tasks.",
    ),
    Starter(
        "Plan the next task",
        "Help me turn the next piece of work for this project into a concrete task "
        "I can add to the map: what it covers, what done looks like, and what to "
        "watch out for.",
    ),
)
