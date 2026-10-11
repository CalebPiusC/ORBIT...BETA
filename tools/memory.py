"""The fact-memory tools, registered through the shared gate and audit wrapper.

Like ``write_note``, nothing here runs until the person types ``yes``. A declined
call writes nothing, and every call (including declines) is audited.
"""

from __future__ import annotations

from pathlib import Path

from memory.facts import DEFAULT_FACTS_PATH, FactStore
from tools.registry import ToolRegistry


def register_memory_tools(
    registry: ToolRegistry,
    *,
    facts_path: str | Path = DEFAULT_FACTS_PATH,
) -> None:
    store = FactStore(facts_path)

    @registry.register(
        name="remember_fact",
        description=(
            "Save one background fact about Caleb to the local facts file, so later replies "
            "can use it as context. Use only when the person explicitly asks you to remember "
            "something. Pass one short sentence on one line; do not rewrite it."
        ),
        parameters={
            "type": "object",
            "properties": {
                "fact": {
                    "type": "string",
                    "description": "One short sentence about Caleb, on a single line.",
                }
            },
            "required": ["fact"],
        },
    )
    def remember_fact(fact: str) -> str:
        if store.add(fact):
            return "Saved. It will be used as background context from the next reply."
        return "That fact is already saved; nothing changed."

    @registry.register(
        name="forget_fact",
        description=(
            "Remove saved background facts about Caleb that exactly match the given text "
            "(case-insensitive). Use only when the person explicitly asks you to forget something."
        ),
        parameters={
            "type": "object",
            "properties": {
                "fact": {
                    "type": "string",
                    "description": "The exact text of the saved fact to remove.",
                }
            },
            "required": ["fact"],
        },
    )
    def forget_fact(fact: str) -> str:
        removed = store.remove(fact)
        if removed == 0:
            return "No saved fact matched that text; nothing changed."
        return f"Removed {removed} matching fact(s)."
