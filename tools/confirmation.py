"""Reusable, fail-closed confirmation gate for consequential tool actions."""

from __future__ import annotations

import json
import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Protocol


@dataclass(frozen=True, slots=True)
class ProposedAction:
    """The exact registered action and arguments awaiting user approval."""

    tool_name: str
    description: str
    arguments: Mapping[str, Any]


class ConfirmationGate(Protocol):
    def confirm(self, action: ProposedAction) -> bool:
        """Return True only after the person explicitly approves the action."""


class ConsoleConfirmationGate:
    """Present an action's exact arguments and require the person to type yes."""

    def __init__(
        self,
        *,
        input_fn: Callable[[str], str] | None = None,
        output_fn: Callable[[str], None] | None = None,
    ) -> None:
        self._input = input if input_fn is None else input_fn
        self._output = print if output_fn is None else output_fn

    def confirm(self, action: ProposedAction) -> bool:
        arguments_json = json.dumps(
            dict(action.arguments),
            ensure_ascii=True,
            indent=2,
            sort_keys=True,
            default=str,
        )
        self._output("\nConfirmation required — nothing has been executed yet.")
        self._output(f"Action: {action.tool_name}")
        self._output(action.description)
        self._output("Proposed arguments (JSON; string values are the exact inputs):")
        self._output(arguments_json)
        self._output("Only typing 'yes' approves this action; any other response declines it.")

        try:
            interactive = sys.stdin.isatty()
        except Exception:
            interactive = False
        if not interactive:
            self._output("No interactive terminal is available. The action was declined.")
            return False

        try:
            answer = self._input("Type yes to approve: ")
        except (EOFError, KeyboardInterrupt):
            self._output("No confirmation received. The action was declined.")
            return False

        approved = isinstance(answer, str) and answer.strip().casefold() == "yes"
        if not approved:
            self._output("Declined. Nothing was executed.")
        return approved
