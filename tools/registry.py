"""Tool registration, mandatory confirmation, dispatch, and automatic auditing."""

from __future__ import annotations

import copy
import contextvars
import json
import threading
import time
import traceback
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
_AUDIT_ACCOUNT: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "orbit_audit_account", default=None
)


def current_account_id() -> str | None:
    """Current request/tool account, or None outside an account context."""
    account_id = _AUDIT_ACCOUNT.get()
    if account_id:
        return account_id
    try:
        from flask import has_request_context, session
        if has_request_context():
            value = session.get("account_id")
            return value if isinstance(value, str) and value else None
    except ImportError:
        pass
    return None


def audit_account_context(account_id: str):
    """Bind audit and account-aware tools emitted during this operation."""
    class _Binding:
        def __enter__(self):
            self.token = _AUDIT_ACCOUNT.set(account_id)
            return self

        def __exit__(self, *_exc):
            _AUDIT_ACCOUNT.reset(self.token)
    return _Binding()
# Caps for failure diagnostics: enough for a real traceback, small enough that one
# failure cannot bloat audit.log.
MAX_AUDIT_ERROR_CHARS = 2_000
MAX_AUDIT_TRACEBACK_CHARS = 8_000
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
RejectedExecutionStatus = Literal[
    "tool_call_limit",
    "invalid_model_response",
    "unknown_tool",
    "invalid_arguments",
]


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
    handler: Callable[..., Any]
    invoke: Callable[..., ToolOutcome]


def _describe_exception(error: BaseException) -> dict[str, Any]:
    """The real failure, flattened into JSON-safe fields: type, message, chain, traceback."""
    chain: list[str] = []
    seen: set[int] = set()
    current: BaseException | None = error
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        chain.append(f"{type(current).__module__}.{type(current).__qualname__}: {current}")
        current = current.__cause__ or current.__context__
    formatted = "".join(
        traceback.format_exception(type(error), error, error.__traceback__)
    ).strip()
    return {
        "error_type": f"{type(error).__module__}.{type(error).__qualname__}",
        "error": str(error)[:MAX_AUDIT_ERROR_CHARS],
        "error_chain": chain,
        "traceback": formatted[:MAX_AUDIT_TRACEBACK_CHARS],
    }


class AuditLog:
    """Append one JSON-lines audit event for each dispatched tool call."""

    def __init__(
        self,
        path: str | Path = DEFAULT_AUDIT_LOG_PATH,
        *,
        database: Any | None = None,
        account_id: str | None = None,
    ) -> None:
        self.path = Path(path)
        self.database = database
        self.account_id = account_id
        if account_id is not None and database is None:
            raise ValueError("account_id requires a SQLite database")
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
        self._write(event)

    def record_model_event(self, **fields: Any) -> None:
        """Append one model-level event: which model answered, or why an attempt failed.

        These carry model ids, roles, reasons and timings only. They never carry
        prompts, replies, or credentials.
        """
        event = {
            "timestamp": datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace(
                "+00:00", "Z"
            ),
            **fields,
        }
        self._write(event)

    def record_event(self, event: str, **fields: Any) -> None:
        """Append one non-tool event, such as a heartbeat notice fired or dismissed.

        Fields must be identifiers, reasons and timings, never message text. Use
        :meth:`record_error` instead when the point of the event is a failure.
        """
        record = {
            "timestamp": datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace(
                "+00:00", "Z"
            ),
            "event": event,
            **fields,
        }
        self._write(record)

    def record_error(self, event: str, error: BaseException, **fields: Any) -> None:
        """Append one failure event carrying the actual exception, not just a summary.

        The browser only has room for one short line, and a log line that says
        "service unavailable" cannot be acted on. This writes what really went
        wrong -- exception type, message, cause chain and formatted traceback --
        so the cause is diagnosable from ``audit.log`` alone. Callers may add
        identifiers and timings; prompts, replies, audio, and credentials never
        belong here.
        """
        self.record_event(event, **{**_describe_exception(error), **fields})

    def _write(self, event: Mapping[str, Any]) -> None:
        line = json.dumps(event, ensure_ascii=False, separators=(",", ":"), default=str)
        if self.database is not None:
            import uuid

            account_id = self.account_id or current_account_id()
            if not account_id:
                raise ValueError("account audit event needs an account context")
            with self.database.connect() as db:
                db.execute("INSERT OR IGNORE INTO accounts(id) VALUES (?)", (account_id,))
                db.execute(
                    "INSERT INTO analytics_data(id, account_id, payload) VALUES (?, ?, ?)",
                    (f"event-{uuid.uuid4().hex}", account_id, line),
                )
            return
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
        audit_database: Any | None = None,
    ) -> None:
        if confirmation_gate is None:
            raise ValueError("A confirmation gate is required; tools may not run silently.")
        self._confirmation_gate = confirmation_gate
        self._audit_log = AuditLog(audit_log_path, database=audit_database)
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
                handler=handler,
                invoke=confirmed_and_audited,
            )
            return confirmed_and_audited

        return decorator

    def validate(
        self, tool_name: str, arguments: Mapping[str, Any] | None
    ) -> tuple[RejectedExecutionStatus, str] | None:
        """Check a call without running it or asking for consent.

        Returns ``None`` when the call could be dispatched, otherwise the
        rejection status and a message. Nothing is audited here; the caller
        decides what to record.
        """
        if not isinstance(tool_name, str) or tool_name not in self._tools:
            return "unknown_tool", "This tool is not registered; nothing was executed."
        if not isinstance(arguments, Mapping):
            return "invalid_arguments", "Invalid arguments: expected an object of named values."
        try:
            signature(self._tools[tool_name].handler).bind(**arguments)
        except TypeError as exc:
            return "invalid_arguments", f"Invalid arguments: {exc}"
        return None

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
