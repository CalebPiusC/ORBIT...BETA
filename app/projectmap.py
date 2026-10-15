"""The Project Map's data — derived at render time, never stored or guessed.

The map answers "what is happening inside this project" from the only truthful
source that exists today: each task's run state (``app.taskplan.TaskRun``).
Labels mean exactly what the run state can prove:

  * ``not_started``     — the task has never run (``run is None``).
  * ``in_progress``     — its run is mid-turn right now.
  * ``needs_attention`` — the last turn ended without an answer.
  * ``waiting``         — a turn finished; the next move is the person's
                          steer, review, or approval. Nothing advances on its
                          own — that is the approval gate's philosophy applied
                          to the map.

A turn ending is not "the task is done", so no bucket here is called Done.
Steps and file counts come from the run object itself, the same source the
Agent panel renders — the map can never out-report what the panel shows.
"""

from __future__ import annotations

from typing import Any

from app.taskplan import FAILED, IDLE, RUNNING

BUCKETS: tuple[tuple[str, str], ...] = (
    ("not_started", "Not started"),
    ("in_progress", "In progress"),
    ("needs_attention", "Needs attention"),
    ("waiting", "Waiting on you"),
)
BUCKET_LABELS: dict[str, str] = dict(BUCKETS)


def bucket_for(task: Any) -> str:
    run = getattr(task, "run", None)
    if run is None:
        return "not_started"
    status = getattr(run, "status", IDLE)
    if status == RUNNING:
        return "in_progress"
    if status == FAILED:
        return "needs_attention"
    return "waiting"


def map_for(tasks: list[Any]) -> list[dict[str, Any]]:
    """Group ``tasks`` into the four buckets, in display order.

    Each entry carries only what the map cells may show: the task's identity
    and its real step counts. A task with no run reports no steps.
    """
    grouped: dict[str, list[dict[str, Any]]] = {key: [] for key, _ in BUCKETS}
    for task in tasks:
        run = getattr(task, "run", None)
        steps = list(getattr(run, "steps", []) or [])
        grouped[bucket_for(task)].append(
            {
                "id": task.id,
                "title": task.title,
                "has_run": run is not None,
                "steps_done": sum(1 for s in steps if getattr(s, "status", "") == "done"),
                "steps_total": len(steps),
                "steps": [
                    {"title": getattr(s, "title", ""), "status": getattr(s, "status", "")}
                    for s in steps
                ],
            }
        )
    return [
        {"key": key, "label": label, "tasks": grouped[key]}
        for key, label in BUCKETS
    ]
