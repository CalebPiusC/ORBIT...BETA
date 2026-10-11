"""Reusable, fail-closed confirmation gate for consequential tool actions."""

from __future__ import annotations

import copy
import json
import sys
import threading
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
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


class DeferredConfirmationGate:
    """Confirmation for the browser: the person decides on a card, not at a prompt.

    A model proposal is queued and shown to the person. When they click Approve
    or Decline, the caller opens :meth:`decision` for that one action and then
    dispatches it. ``confirm`` never waits. It returns the decision recorded for
    exactly this action (same tool, same arguments), and declines if there is
    none. The lock is held for the whole dispatch, so two decisions cannot mix.
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._decision: tuple[str, dict[str, Any], bool] | None = None

    @contextmanager
    def decision(
        self, tool_name: str, arguments: Mapping[str, Any], *, approved: bool
    ) -> Iterator[None]:
        with self._lock:
            self._decision = (tool_name, copy.deepcopy(dict(arguments)), approved is True)
            try:
                yield
            finally:
                self._decision = None

    def confirm(self, action: ProposedAction) -> bool:
        recorded = self._decision
        if recorded is None:
            return False
        tool_name, arguments, approved = recorded
        if action.tool_name != tool_name or dict(action.arguments) != arguments:
            return False
        return approved
