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


# ---------------------------------------------------------------------------
# The workspace map (design/ORBIT UI___2, Workspace frames): stages render as
# stacked cards of task rows, never as a node-and-line graph. A row's suffix
# and a stage's capsule are derived from stored state only:
#
#   * "Approved" / "Applied" come from the approval gate's own flags;
#   * "Needs approval" means a run finished a turn cleanly (status IDLE) and
#     nothing was approved or applied yet — a draft exists, nothing moved;
#   * "Up next" is positional: the first still-planned row on the whole map,
#     exactly once, so one row names the next step and the rest stay planned;
#   * a stage capsule is "Complete" when every row is closed, "N need
#     approval" (the copper dot) when any draft waits, "In progress" while a
#     run streams, "Up next" on the first stage whose rows are all still
#     planned, "n of m complete" when some are closed, else "Planned".
# ---------------------------------------------------------------------------

CLOSED_STATES = ("done", "approved", "applied")


def proposal_ready(task: Any) -> bool:
    """True when a finished run left a draft nobody approved or applied yet."""
    if getattr(task, "done", False):
        return False
    if getattr(task, "approved", False) or getattr(task, "applied", False):
        return False
    if getattr(task, "rejected", False):
        return False
    run = getattr(task, "run", None)
    if run is None:
        return False
    return getattr(run, "status", IDLE) == IDLE


def workspace_state(task: Any) -> tuple[str, str]:
    """(key, label) for one workspace map row, from stored state only."""
    if getattr(task, "applied", False):
        return ("applied", "Applied")
    if getattr(task, "approved", False):
        return ("approved", "Approved")
    if getattr(task, "done", False):
        return ("done", "Done")
    if proposal_ready(task):
        return ("needs_approval", "Needs approval")
    if getattr(task, "rejected", False):
        # A refused draft is not "Planned" and not "Needs approval": the
        # person said no to this one. It needs new work — that is the
        # attention state, named for what actually happened.
        return ("needs_attention", "Declined")
    run = getattr(task, "run", None)
    if run is not None and getattr(run, "status", IDLE) == RUNNING:
        return ("in_progress", "In progress")
    if run is not None and getattr(run, "status", IDLE) == FAILED:
        return ("needs_attention", "Needs attention")
    return ("planned", "Planned")


def workspace_stages(tasks: list[Any]) -> list[dict[str, Any]]:
    """The map as the workspace draws it: ordered stage cards with capsules."""
    stages = map_stages(tasks)
    rows_by_stage: dict[str, list[dict[str, Any]]] = {}
    for stage in stages:
        rows: list[dict[str, Any]] = []
        for cell in stage["tasks"]:
            task = next(t for t in tasks if t.id == cell["id"])
            key, label = workspace_state(task)
            rows.append({**cell, "state": key, "state_label": label})
        rows_by_stage[stage["name"]] = rows

    # "Up next" names the first still-planned row on the whole map, once.
    up_next_id = ""
    for stage in stages:
        for row in rows_by_stage[stage["name"]]:
            if row["state"] == "planned":
                up_next_id = row["id"]
                break
        if up_next_id:
            break
    up_next_stage = ""
    for stage in stages:
        for row in rows_by_stage[stage["name"]]:
            if row["id"] == up_next_id:
                row["state_label"] = "Up next"
                up_next_stage = stage["name"]
                break

    out: list[dict[str, Any]] = []
    for index, stage in enumerate(stages, start=1):
        rows = rows_by_stage[stage["name"]]
        waiting = sum(1 for r in rows if r["state"] == "needs_approval")
        closed = sum(1 for r in rows if r["state"] in CLOSED_STATES)
        running = sum(1 for r in rows if r["state"] == "in_progress")
        if closed == len(rows):
            chip = {"key": "complete", "label": "Complete", "dot": False}
        elif waiting:
            chip = {
                "key": "approval",
                "label": f"{waiting} need approval"
                if waiting != 1
                else "1 needs approval",
                "dot": True,
            }
        elif running:
            chip = {"key": "progress", "label": "In progress", "dot": False}
        elif stage["name"] == up_next_stage:
            chip = {"key": "upnext", "label": "Up next", "dot": False}
        elif closed:
            chip = {
                "key": "count",
                "label": f"{closed} of {len(rows)} complete",
                "dot": False,
            }
        else:
            chip = {"key": "planned", "label": "Planned", "dot": False}
        out.append(
            {
                "index": index,
                "name": stage["name"],
                "chip": chip,
                "rows": rows,
                "done": stage["done"],
                "total": stage["total"],
            }
        )
    return out
