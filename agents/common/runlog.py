"""Send log records to a file in logs/ for the duration of a run or a step."""

from __future__ import annotations

import logging
import re
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Iterator

log = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_LOG_DIR = ROOT / "logs"
_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"
KEEP_DAYS = 30
_DATED = re.compile(r"-(\d{4}-\d{2}-\d{2})\.log$")


def log_path(name: str, log_dir: Path | None = None) -> Path:
    """logs/<name>-YYYY-MM-DD.log (local date)."""
    return (log_dir or DEFAULT_LOG_DIR) / f"{name}-{datetime.now().date().isoformat()}.log"


@contextmanager
def log_to(path: Path) -> Iterator[Path]:
    """Add a UTF-8 file handler on the root logger at INFO; nest freely."""
    path.parent.mkdir(parents=True, exist_ok=True)
    handler = logging.FileHandler(path, encoding="utf-8")
    handler.setFormatter(logging.Formatter(_FORMAT))
    root = logging.getLogger()
    old_level = root.level
    root.addHandler(handler)
    if root.level == logging.NOTSET or root.level > logging.INFO:
        root.setLevel(logging.INFO)
    try:
        yield path
    finally:
        root.removeHandler(handler)
        root.setLevel(old_level)
        handler.close()


def prune_logs(log_dir: Path | None = None, keep_days: int = KEEP_DAYS, today: date | None = None) -> list[Path]:
    """Delete logs/*.log older than keep_days: by the date in the name (<name>-YYYY-MM-DD.log),
    else by modification time. Returns what was deleted; a file that can't go is skipped."""
    cutoff = (today or date.today()) - timedelta(days=keep_days)
    deleted = []
    for path in sorted((log_dir or DEFAULT_LOG_DIR).glob("*.log")):
        m = _DATED.search(path.name)
        try:
            day = date.fromisoformat(m.group(1)) if m else datetime.fromtimestamp(path.stat().st_mtime).date()
        except (ValueError, OSError):
            continue
        if day < cutoff:
            try:
                path.unlink()
            except OSError as exc:
                log.warning("Could not delete old log %s: %s", path.name, exc)
                continue
            deleted.append(path)
    return deleted
