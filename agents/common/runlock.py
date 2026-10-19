"""A lock file so two daily runs never overlap (Task Scheduler plus a manual run, say).

The lock is created atomically and holds the owner's pid and start time. A lock not
touched for STALE_AFTER (2 hours) is stale, e.g. left by a crashed or killed run, and is
taken over. The holder refreshes it with touch() between steps, so a long run stays live.
"""

from __future__ import annotations

import json
import logging
import os
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

log = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_LOCK = ROOT / "data" / "daily.lock"
STALE_AFTER_S = 2 * 60 * 60


class LockBusy(RuntimeError):
    """Another run holds a fresh lock."""


class Lock:
    def __init__(self, path: Path) -> None:
        self.path = path

    def touch(self) -> None:
        """Mark the lock live; a lock untouched for STALE_AFTER_S counts as stale."""
        try:
            os.utime(self.path)
        except OSError as exc:
            log.warning("Could not refresh lock %s: %s", self.path, exc)


@contextmanager
def hold(path: Path = DEFAULT_LOCK, stale_after_s: float = STALE_AFTER_S) -> Iterator[Lock]:
    """Take the lock or raise LockBusy; released on exit."""
    path.parent.mkdir(parents=True, exist_ok=True)
    owner = json.dumps({"pid": os.getpid(), "started_at": datetime.now(timezone.utc).isoformat(timespec="seconds")})
    for _ in range(2):  # a second try after clearing a stale lock
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            try:
                age = time.time() - path.stat().st_mtime
            except FileNotFoundError:  # released meanwhile
                continue
            if age < stale_after_s:
                raise LockBusy(f"another daily run holds {path.name} ({_holder(path)}), "
                               f"last active {int(age // 60)} min ago") from None
            log.warning("Lock %s is stale (%d min old, %s); taking it over", path, age // 60, _holder(path))
            path.unlink(missing_ok=True)
            continue
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(owner)
        break
    else:
        raise LockBusy(f"could not take {path.name}")
    try:
        yield Lock(path)
    finally:
        try:
            if path.read_text(encoding="utf-8") == owner:  # never remove a lock taken over from us
                path.unlink()
        except OSError:
            pass


def _holder(path: Path) -> str:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return f"pid {data.get('pid')}, started {data.get('started_at')}"
    except (OSError, ValueError, AttributeError):
        return "owner unknown"
