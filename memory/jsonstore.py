"""A small JSON file for runtime state: read, change, and write back as one step.

Every change happens under a lock, so the web scheduler thread, the web
requests, and the terminal chat cannot interleave a read and a write. The file
is written to a temporary name and then renamed, so a crash mid-write leaves
the old file intact. A corrupt file raises instead of resetting to defaults:
resetting would quietly refire notices and restart timers.
"""

from __future__ import annotations

import json
import os
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

try:  # POSIX only. Elsewhere the in-process lock still applies.
    import fcntl
except ImportError:  # pragma: no cover - Windows
    fcntl = None  # type: ignore[assignment]


class JsonStore:
    def __init__(self, path: str | Path, default: Callable[[], Any]) -> None:
        self.path = Path(path)
        self._default = default
        self._mutex = threading.RLock()

    def read(self) -> Any:
        with self._mutex:
            return self._read_unlocked()

    def update(self, change: Callable[[Any], Any]) -> Any:
        """Run ``change`` on the current data, save the result, and return what it returned.

        ``change`` may mutate the data in place. If it raises, nothing is written.
        """
        with self._mutex, self._file_lock():
            data = self._read_unlocked()
            result = change(data)
            self._write(data)
            return result

    def _read_unlocked(self) -> Any:
        try:
            text = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return self._default()
        if not text.strip():
            return self._default()
        try:
            return json.loads(text)
        except json.JSONDecodeError as exc:
            raise RuntimeError(
                f"{self.path} is not valid JSON ({exc}). Fix or move it; ORBIT will not reset it silently."
            ) from None

    def _write(self, data: Any) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(self.path.name + ".tmp")
        tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        os.replace(tmp, self.path)

    @contextmanager
    def _file_lock(self) -> Iterator[None]:
        if fcntl is None:
            yield
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.with_name(self.path.name + ".lock").open("a") as lock_file:
            fcntl.flock(lock_file, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock_file, fcntl.LOCK_UN)
