"""Waiting cleanup: close shortlisted jobs nobody acted on.

    python -m agents.scout.waiting [--db PATH] [--profile PATH] [--dry-run]

A `shortlisted` or `researched` job shortlisted at least waiting_expiry_days ago
(profile.yaml, default DEFAULT_EXPIRY_DAYS) with no action by the user since (no event
with actor user after the latest shortlisting: a rating, a hold or unhold, a posting set,
a skip or an apply) is closed by scout with detail "stale: no action <N> days". Held jobs
are never touched, and applied or interviewing jobs aren't shortlisted/researched, so
they never are either. `.\\run.ps1 reopen <id> "reason"` takes a stale close back
(closed -> scored, actor user). Runs in the daily run before the shortlist; the digest
lists the day's closes under "Cleaned up today".
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from agents import db
from agents.common import runlog
from agents.scout.filters import DEFAULT_PROFILE_PATH, load_profile

log = logging.getLogger(__name__)

DEFAULT_EXPIRY_DAYS = 10
STALE_PREFIX = "stale:"
WAITING = ("shortlisted", "researched")


def expiry_days(profile: dict[str, Any]) -> int:
    return int(profile.get("waiting_expiry_days") or DEFAULT_EXPIRY_DAYS)


def stale_jobs(days: int, *, db_path: Path | str | None = None, now: datetime | None = None) -> list[dict[str, Any]]:
    """Waiting jobs shortlisted `days` or more ago with no user action since, not held."""
    now = now or datetime.now(timezone.utc)
    shortlisted = db.shortlisted_at(db_path=db_path)
    out = []
    for job in [j for s in WAITING for j in db.get_jobs(status=s, db_path=db_path)]:
        at = shortlisted.get(job["id"])
        if at is None or db.is_held(job, now.astimezone().date()):
            continue
        since = datetime.fromisoformat(at)
        if now - since < timedelta(days=days):
            continue
        if any(e["actor"] == "user" and e["at"] > at for e in db.get_job_events(job["id"], db_path=db_path)):
            continue
        out.append(job)
    return out


def expire(profile: dict[str, Any], *, db_path: Path | str | None = None, now: datetime | None = None,
           dry_run: bool = False) -> list[dict[str, Any]]:
    """Close the stale waiting jobs (scout, "stale: no action <N> days"); returns them."""
    days = expiry_days(profile)
    closed = []
    for job in stale_jobs(days, db_path=db_path, now=now):
        if not dry_run:
            try:
                db.set_status(job["id"], "closed", "scout", f"{STALE_PREFIX} no action {days} days", db_path=db_path)
            except (db.TransitionError, db.ActorNotAllowed, LookupError) as exc:  # changed meanwhile
                log.warning("Waiting: job %d not closed: %s", job["id"], exc)
                continue
        log.info("Waiting: job %d %s | %s %s (no action %d days)", job["id"], job["company"], job["title"],
                 "would close" if dry_run else "closed", days)
        closed.append(job)
    return closed


def cleaned_up(today: date | None = None, *, db_path: Path | str | None = None) -> list[dict[str, Any]]:
    """Jobs closed as stale today (local date), each with `closed_detail`."""
    today = today or date.today()
    jobs = {j["id"]: j for j in db.get_jobs(status="closed", db_path=db_path)}
    out = []
    for job_id, (at, detail) in db.latest_closes(STALE_PREFIX, db_path=db_path).items():
        if job_id in jobs and datetime.fromisoformat(at).astimezone().date() == today:
            out.append({**jobs[job_id], "closed_detail": detail})
    return sorted(out, key=lambda j: (-(j["score"] or 0), j["id"]))


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="replace")
    parser = argparse.ArgumentParser(prog="python -m agents.scout.waiting", description=__doc__.split("\n")[0])
    parser.add_argument("--db", type=Path, default=None)
    parser.add_argument("--profile", type=Path, default=DEFAULT_PROFILE_PATH)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    with runlog.log_to(runlog.log_path("score")):
        jobs = expire(load_profile(args.profile), db_path=args.db, dry_run=args.dry_run)
    verb = "Would close" if args.dry_run else "Closed"
    print(f"{verb} {len(jobs)} stale waiting job{'s' if len(jobs) != 1 else ''}")
    for j in jobs:
        print(f"  #{j['id']:<5} {j['company'][:24]:<24}  {j['title']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
