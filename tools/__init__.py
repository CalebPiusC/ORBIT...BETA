"""Human-confirmed, automatically audited ORBIT tools."""

from pathlib import Path
from typing import TYPE_CHECKING

from tools.confirmation import ConfirmationGate, ConsoleConfirmationGate, ProposedAction
from memory.facts import DEFAULT_FACTS_PATH
from memory.plans import DEFAULT_PLANS_PATH, PlanStore
from tools.memory import register_memory_tools
from tools.plans import register_plan_tools
from tools.notes import DEFAULT_NOTES_PATH, register_write_note
from tools.registry import DEFAULT_AUDIT_LOG_PATH, AuditLog, ToolDefinition, ToolOutcome, ToolRegistry

if TYPE_CHECKING:
    from heartbeat.service import Heartbeat


def build_tool_registry(
    *,
    confirmation_gate: ConfirmationGate | None = None,
    audit_log_path: str | Path = DEFAULT_AUDIT_LOG_PATH,
    audit_database=None,
    notes_path: str | Path = DEFAULT_NOTES_PATH,
    facts_path: str | Path = DEFAULT_FACTS_PATH,
    plans_path: str | Path = DEFAULT_PLANS_PATH,
    heartbeat_path: str | Path | None = None,
    heartbeat: "Heartbeat | None" = None,
) -> ToolRegistry:
    """Build the initial registry with a mandatory interactive confirmation gate."""
    gate = confirmation_gate if confirmation_gate is not None else ConsoleConfirmationGate()
    registry = ToolRegistry(
        gate, audit_log_path=audit_log_path, audit_database=audit_database
    )
    register_write_note(registry, notes_path=notes_path, database=audit_database)
    register_memory_tools(registry, facts_path=facts_path, database=audit_database)
    if heartbeat is None:
        # Pause and resume are file-backed, so this works without the web scheduler
        # running: the scheduler re-reads the state file on every tick.
        from heartbeat.service import DEFAULT_HEARTBEAT_PATH, Heartbeat
        from config import load_heartbeat_settings

        heartbeat = Heartbeat(
            settings=load_heartbeat_settings(),
            state_path=heartbeat_path if heartbeat_path is not None else DEFAULT_HEARTBEAT_PATH,
            plans=PlanStore(plans_path),
            audit=AuditLog(audit_log_path),
        )
    scoped_plans = (
        heartbeat
        if heartbeat is not None and callable(getattr(heartbeat, "_plans_for_current", None))
        else PlanStore(plans_path)
    )
    register_plan_tools(registry, plans=scoped_plans, heartbeat=heartbeat)
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
