"""Plan and heartbeat tools, registered through the shared gate and audit wrapper.

Like every other tool here, nothing runs until the person types ``yes``. A
declined call changes nothing and is audited.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from heartbeat.schedule import local_time, utc_now
from memory.plans import PlanStore
from tools.registry import ToolRegistry

if TYPE_CHECKING:
    from heartbeat.service import Heartbeat


def register_plan_tools(
    registry: ToolRegistry,
    *,
    plans: PlanStore,
    heartbeat: Heartbeat,
) -> None:
    @registry.register(
        name="save_plan",
        description=(
            "Save today's plan for Caleb as one short line in the local plans file. Use only when "
            "the person explicitly asks you to save or write down a plan for today. Pass one line."
        ),
        parameters={
            "type": "object",
            "properties": {
                "plan": {
                    "type": "string",
                    "description": "Today's plan in one short line.",
                }
            },
            "required": ["plan"],
        },
    )
    def save_plan(plan: str) -> str:
        today = local_time(utc_now()).date()
        plans.add(plan, today, saved_at=utc_now().isoformat(timespec="seconds"))
        return f"Saved the plan for {today.isoformat()}."

    @registry.register(
        name="pause_heartbeat",
        description=(
            "Pause ORBIT's background reminders (missing-plan and check-in notices, and phone push). "
            "Use only when the person explicitly asks you to pause reminders or the heartbeat."
        ),
        parameters={"type": "object", "properties": {}},
    )
    def pause_heartbeat() -> str:
        heartbeat.set_paused(True, source="tool")
        return "Reminders are paused. They stay paused until someone resumes them."

    @registry.register(
        name="resume_heartbeat",
        description=(
            "Resume ORBIT's background reminders after they were paused. Use only when the person "
            "explicitly asks you to resume reminders or the heartbeat."
        ),
        parameters={"type": "object", "properties": {}},
    )
    def resume_heartbeat() -> str:
        heartbeat.set_paused(False, source="tool")
        return "Reminders are running again."
