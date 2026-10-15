"""The Project Map's data — grouped into the owner's stages, derived from real
task state, never guessed.

From the owner's design frames (2026-10-15): the map is a checklist of stages
("Foundation", "Find & organize", "Ready to share"), each with its tasks and a
"x / y complete" count. Rules this module enforces:

  * Stages are filed by the person — ``Task.stage`` is user data. A task
    without one lands in the map's plain "Tasks" group; no stage is invented
    from keywords.
  * A row's state is derived (``row_state``): "Complete" only when the person
    marked it (``Task.done``); a turn that merely ended is "Turn finished",
    never complete. Order matches how urgent each state is to see.
  * "Next up" is the next not-complete task after the selected one *in its
    stage* (creation order). "Builds on" is owner-asserted data
    (``Task.builds_on``), never inferred.
"""

from __future__ import annotations

from typing import Any

from app.taskplan import FAILED, IDLE, RUNNING

UNSTAGED = "Tasks"

# key, label — the row states, in the order the map badge logic checks them.
def row_state(task: Any) -> tuple[str, str]:
    """(key, label) for one task row, from data only."""
    if getattr(task, "done", False):
        return ("done", "Complete")
    run = getattr(task, "run", None)
    if run is None:
        return ("planned", "Planned")
    status = getattr(run, "status", IDLE)
    if status == RUNNING:
        return ("in_progress", "In progress")
    if status == FAILED:
        return ("needs_attention", "Needs attention")
    return ("finished", "Turn finished")


def task_cell(task: Any) -> dict[str, Any]:
    """What one map row may show."""
    key, label = row_state(task)
    run = getattr(task, "run", None)
    steps = list(getattr(run, "steps", []) or [])
    return {
        "id": task.id,
        "title": task.title,
        "state": key,
        "state_label": label,
        "done": bool(getattr(task, "done", False)),
        "steps_done": sum(1 for s in steps if getattr(s, "status", "") == "done"),
        "steps_total": len(steps),
    }


def _stage_name(task: Any) -> str:
    stage = getattr(task, "stage", None)
    return stage if stage else UNSTAGED


def map_stages(tasks: list[Any]) -> list[dict[str, Any]]:
    """Group ``tasks`` (creation order) into stages, first-appearance order.

    The unstaged group, when it is not the only one, renders last: stages the
    person named lead the map.
    """
    order: list[str] = []
    grouped: dict[str, list[dict[str, Any]]] = {}
    done_count: dict[str, int] = {}
    for task in tasks:
        name = _stage_name(task)
        if name not in grouped:
            order.append(name)
            grouped[name] = []
            done_count[name] = 0
        cell = task_cell(task)
        grouped[name].append(cell)
        if cell["done"]:
            done_count[name] += 1
    named = [n for n in order if n != UNSTAGED]
    names = named + ([UNSTAGED] if UNSTAGED in order else [])
    return [
        {
            "name": name,
            "tasks": grouped[name],
            "done": done_count[name],
            "total": len(grouped[name]),
        }
        for name in names
        if name in grouped
    ]


def progress(tasks: list[Any]) -> tuple[int, int]:
    """(complete, total) across the whole map — person-marked completes only."""
    return (sum(1 for t in tasks if getattr(t, "done", False)), len(tasks))


def latest_task(tasks: list[Any]) -> Any | None:
    """The most recently added task (creation order), for card footers."""
    return tasks[-1] if tasks else None


def next_up(tasks: list[Any], task: Any) -> Any | None:
    """The next not-complete task after ``task`` in its stage, or None."""
    stage = _stage_name(task)
    seen = False
    for candidate in tasks:
        if candidate is task:
            seen = True
            continue
        if not seen:
            continue
        if _stage_name(candidate) == stage and not getattr(candidate, "done", False):
            return candidate
    return None
