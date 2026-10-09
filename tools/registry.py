"""Tool registration, mandatory confirmation, dispatch, and automatic auditing."""

from __future__ import annotations

import copy
import json
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from functools import wraps
from inspect import signature
from pathlib import Path
from types import MappingProxyType
from typing import Any, Literal

from tools.confirmation import ConfirmationGate, ProposedAction

DEFAULT_AUDIT_LOG_PATH = Path(__file__).resolve().parents[1] / "audit.log"
ConfirmationStatus = Literal["confirmed", "declined"]
ExecutionStatus = Literal[
    "succeeded",
    "not_run",
    "invalid_arguments",
    "confirmation_failed",
    "failed",
    "unknown_tool",
    "tool_call_limit",
    "invalid_model_response",
]
RejectedExecutionStatus = Literal["tool_call_limit", "invalid_model_response"]


@dataclass(frozen=True, slots=True)
class ToolDefinition:
    """Provider-neutral function metadata exposed to a model."""

    name: str
    description: str
    parameters: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class ToolOutcome:
    """Result of one dispatched tool call, including its consent decision."""

    tool_name: str
    arguments: Mapping[str, Any]
    confirmation: ConfirmationStatus
    execution: ExecutionStatus
    result: Any

    def as_function_response(self) -> dict[str, Any]:
        return {
            "confirmation": self.confirmation,
            "execution": self.execution,
            "result": self.result,
        }


@dataclass(frozen=True, slots=True)
class _RegisteredTool:
    definition: ToolDefinition
    invoke: Callable[..., ToolOutcome]


class AuditLog:
    """Append one JSON-lines audit event for each dispatched tool call."""

    def __init__(self, path: str | Path = DEFAULT_AUDIT_LOG_PATH) -> None:
        self.path = Path(path)
        self._lock = threading.Lock()

    def record(
        self,
        *,
        tool_name: str,
        arguments: Mapping[str, Any],
        confirmation: ConfirmationStatus,
        execution: ExecutionStatus,
        duration_seconds: float,
    ) -> None:
        event = {
            "timestamp": datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace(
                "+00:00", "Z"
            ),
            "tool_name": tool_name,
            "arguments": arguments,
            "confirmation": confirmation,
            "confirmed": confirmation == "confirmed",
            "execution": execution,
            "duration_ms": round(duration_seconds * 1000, 3),
        }
        line = json.dumps(event, ensure_ascii=False, separators=(",", ":"), default=str)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._lock, self.path.open("a", encoding="utf-8") as audit_file:
            audit_file.write(line + "\n")


class ToolRegistry:
    """Register tools with a mandatory confirmation and audit wrapper.

    Applying ``@registry.register(...)`` wraps every registered handler so its
    call is confirmed first and recorded automatically, without tool-specific
    audit code. A confirmation gate is required; there is no fail-open default.
    """

    def __init__(
        self,
        confirmation_gate: ConfirmationGate,
        *,
        audit_log_path: str | Path = DEFAULT_AUDIT_LOG_PATH,
    ) -> None:
        if confirmation_gate is None:
            raise ValueError("A confirmation gate is required; tools may not run silently.")
        self._confirmation_gate = confirmation_gate
        self._audit_log = AuditLog(audit_log_path)
        self._tools: dict[str, _RegisteredTool] = {}

    @property
    def definitions(self) -> tuple[ToolDefinition, ...]:
        return tuple(
            ToolDefinition(
                name=tool.definition.name,
                description=tool.definition.description,
                parameters=copy.deepcopy(dict(tool.definition.parameters)),
            )
            for tool in self._tools.values()
        )

    def reject_call(
        self,
        tool_name: str,
        arguments: Mapping[str, Any] | None,
        *,
        execution: RejectedExecutionStatus,
        message: str,
    ) -> ToolOutcome:
        """Audit a model call that must be blocked before it reaches a tool."""
        started = time.perf_counter()
        name = tool_name if isinstance(tool_name, str) and tool_name else "unknown_tool"
        safe_arguments = (
            copy.deepcopy(dict(arguments))
            if isinstance(arguments, Mapping)
            else {"_invalid_arguments": arguments}
        )
        duration = time.perf_counter() - started
        self._audit_log.record(
            tool_name=name,
            arguments=safe_arguments,
            confirmation="declined",
            execution=execution,
            duration_seconds=duration,
        )
        return ToolOutcome(
            tool_name=name,
            arguments=safe_arguments,
            confirmation="declined",
            execution=execution,
            result={"error": message},
        )

    def register(
        self,
        *,
        name: str,
        description: str,
        parameters: Mapping[str, Any],
    ) -> Callable[[Callable[..., Any]], Callable[..., ToolOutcome]]:
        """Register and wrap a handler with consent and audit behavior."""
        if not name or not name.isidentifier():
            raise ValueError("Tool names must be non-empty Python identifiers.")
        if name in self._tools:
            raise ValueError(f"Tool {name!r} is already registered.")

        definition = ToolDefinition(
            name=name,
            description=description,
            parameters=copy.deepcopy(dict(parameters)),
        )

        def decorator(handler: Callable[..., Any]) -> Callable[..., ToolOutcome]:
            @wraps(handler)
            def confirmed_and_audited(**arguments: Any) -> ToolOutcome:
                return self._invoke_registered(definition, handler, arguments)

            self._tools[name] = _RegisteredTool(
                definition=definition,
                invoke=confirmed_and_audited,
            )
            return confirmed_and_audited

        return decorator

    def dispatch(self, tool_name: str, arguments: Mapping[str, Any] | None) -> ToolOutcome:
        """Dispatch a model function call through the registered wrapper."""
        call_started = time.perf_counter()
        name = tool_name if isinstance(tool_name, str) and tool_name else "unknown_tool"
        if isinstance(arguments, Mapping):
            safe_arguments = copy.deepcopy(dict(arguments))
        else:
            safe_arguments = {"_invalid_arguments": arguments}

        registered = self._tools.get(name)
        if registered is None:
            duration = time.perf_counter() - call_started
            self._audit_log.record(
                tool_name=name,
                arguments=safe_arguments,
                confirmation="declined",
                execution="unknown_tool",
                duration_seconds=duration,
            )
            return ToolOutcome(
                tool_name=name,
                arguments=safe_arguments,
                confirmation="declined",
                execution="unknown_tool",
                result={"error": "This tool is not registered; nothing was executed."},
            )

        return registered.invoke(**safe_arguments)

    def _invoke_registered(
        self,
        definition: ToolDefinition,
        handler: Callable[..., Any],
        arguments: Mapping[str, Any],
    ) -> ToolOutcome:
        started = time.perf_counter()
        safe_arguments = copy.deepcopy(dict(arguments))
        confirmation: ConfirmationStatus = "declined"
        execution: ExecutionStatus = "not_run"
        result: Any = {"message": "The action was not executed."}

        try:
            try:
                signature(handler).bind(**safe_arguments)
            except TypeError as exc:
                execution = "invalid_arguments"
                result = {"error": f"Invalid arguments: {exc}"}
                return ToolOutcome(
                    definition.name, safe_arguments, confirmation, execution, result
                )

            action = ProposedAction(
                tool_name=definition.name,
                description=definition.description,
                arguments=MappingProxyType(copy.deepcopy(safe_arguments)),
            )
            try:
                approved = self._confirmation_gate.confirm(action) is True
            except Exception:
                execution = "confirmation_failed"
                result = {"error": "Confirmation could not be obtained; nothing was executed."}
                return ToolOutcome(
                    definition.name, safe_arguments, confirmation, execution, result
                )

            if not approved:
                result = {"message": "The user declined this action. It was not executed."}
                return ToolOutcome(
                    definition.name, safe_arguments, confirmation, execution, result
                )

            confirmation = "confirmed"
            try:
                result = handler(**safe_arguments)
                execution = "succeeded"
            except Exception as exc:
                execution = "failed"
                result = {"error": f"{type(exc).__name__}: {exc}"}
            except BaseException:
                execution = "failed"
                raise

            return ToolOutcome(
                definition.name, safe_arguments, confirmation, execution, result
            )
        finally:
            self._audit_log.record(
                tool_name=definition.name,
                arguments=safe_arguments,
                confirmation=confirmation,
                execution=execution,
                duration_seconds=time.perf_counter() - started,
            )
