"""The "Last run" line at the top of the digest and the review page."""

from __future__ import annotations

from datetime import date, datetime
from pathlib import Path

from agents import db

NO_RUN_TODAY = "No run today yet"


def summary(db_path: Path | str | None = None, today: date | None = None) -> tuple[str, bool]:
    """(text, ok): start and finish time (local) and each step's status of today's latest
    daily run, or NO_RUN_TODAY. ok is False when a step didn't pass or the run didn't finish."""
    run = db.last_daily_run(db_path=db_path)
    if run is None or _local(run["started_at"]).date() != (today or date.today()):
        return NO_RUN_TODAY, True
    started = _local(run["started_at"])
    if run["finished_at"]:
        finished = _local(run["finished_at"])
        when = f"{started:%Y-%m-%d %H:%M} – {finished:%H:%M}"
        if finished.date() != started.date():
            when = f"{started:%Y-%m-%d %H:%M} – {finished:%Y-%m-%d %H:%M}"
    else:
        when = f"{started:%Y-%m-%d %H:%M} – not finished"
    steps = ", ".join(f"{name} {state}" for name, state in run["steps"]) or "no steps yet"
    ok = bool(run["finished_at"]) and all(state == "ok" for _, state in run["steps"])
    return f"{when} · {steps}", ok


def _local(iso: str) -> datetime:
    return datetime.fromisoformat(iso).astimezone()
