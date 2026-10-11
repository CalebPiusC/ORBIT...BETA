"""The background scheduler: one daemon thread that ticks the heartbeat.

It is a thread in the web process, not a separate process. A daemon thread dies
with the process, so it never keeps the server alive. A failed tick is logged and
the loop carries on, so one bad read cannot silence the heartbeat for good.
"""

from __future__ import annotations

import logging
import threading
from typing import Any

log = logging.getLogger("orbit.heartbeat")


def run_scheduler(heartbeat: Any, *, interval_seconds: float, stop: threading.Event) -> None:
    while not stop.is_set():
        try:
            heartbeat.tick()
        except Exception:  # keep the heartbeat alive through one bad tick
            log.exception("heartbeat tick failed; the next tick will try again")
        stop.wait(interval_seconds)


def start_scheduler(heartbeat: Any, *, interval_seconds: float) -> tuple[threading.Thread, threading.Event]:
    """Start the scheduler thread. Returns the thread and the event that stops it."""
    stop = threading.Event()
    thread = threading.Thread(
        target=run_scheduler,
        kwargs={"heartbeat": heartbeat, "interval_seconds": interval_seconds, "stop": stop},
        name="orbit-heartbeat",
        daemon=True,
    )
    thread.start()
    return thread, stop
