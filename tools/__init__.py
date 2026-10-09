"""Human-confirmed, automatically audited ORBIT tools."""

from pathlib import Path

from tools.confirmation import ConfirmationGate, ConsoleConfirmationGate, ProposedAction
from tools.notes import DEFAULT_NOTES_PATH, register_write_note
from tools.registry import DEFAULT_AUDIT_LOG_PATH, ToolDefinition, ToolOutcome, ToolRegistry


def build_tool_registry(
    *,
    confirmation_gate: ConfirmationGate | None = None,
    audit_log_path: str | Path = DEFAULT_AUDIT_LOG_PATH,
    notes_path: str | Path = DEFAULT_NOTES_PATH,
) -> ToolRegistry:
    """Build the initial registry with a mandatory interactive confirmation gate."""
    gate = confirmation_gate if confirmation_gate is not None else ConsoleConfirmationGate()
    registry = ToolRegistry(gate, audit_log_path=audit_log_path)
    register_write_note(registry, notes_path=notes_path)
    return registry


__all__ = [
    "ConfirmationGate",
    "ConsoleConfirmationGate",
    "ProposedAction",
    "ToolDefinition",
    "ToolOutcome",
    "ToolRegistry",
    "build_tool_registry",
]
