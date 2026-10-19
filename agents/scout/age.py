"""Posting age: close jobs whose posting is too old to be worth it.

    python -m agents.scout.age [--db PATH] [--profile PATH] [--dry-run]

A job's age is today minus jobs.posted_at, and posted_at_source says how far to trust it
(db.POSTED_AT_SOURCES): ats or posting_page (the posting's own date), alert_badge (an
alert's "3 days ago", an estimate) or email_date (the alert email's date; the posting is
at least that old, so the date is unverified).

The limit is hard_filters.max_posting_age_days (profile.yaml, default 10). When the
posting is confirmed open (source_trust official, or a feed job, and pass 2 doesn't say it
may be closed) it's hard_filters.max_official_open_age_days (default 30), or
max_official_open_age_days_preferred (default 45) when the company matches
preferred_companies.yaml (jobs.preferred_match).

A `new`, `scored`, `shortlisted` or `researched` job over its limit is closed by scout
with detail "posting too old: <N> days". Held jobs are never closed (the app and digest
flag them instead), applied and interviewing jobs never checked, and a job the user
reopened after such a close is left alone. Runs in the daily run's cleanup step and
whenever a better date arrives (details lookup, set-posting, the review app's Find posting
and paste URL); the shortlist and Shortlist now refuse jobs over the limit.
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import date, datetime
from pathlib import Path
from typing import Any

from agents import db
from agents.common import runlog
from agents.scout.filters import DEFAULT_PROFILE_PATH, load_profile

log = logging.getLogger(__name__)

DEFAULT_MAX_AGE_DAYS = 10
DEFAULT_OFFICIAL_OPEN_MAX_AGE_DAYS = 30
DEFAULT_PREFERRED_OPEN_MAX_AGE_DAYS = 45
TOO_OLD_PREFIX = "posting too old:"
CHECKED = ("new", "scored", "shortlisted", "researched")
POSTING_SOURCES = ("ats", "posting_page")
HELD_NOTE = "older posting, check it's still open"


def limits(profile: dict[str, Any]) -> tuple[int, int, int]:
    """(max age, max age of a confirmed open official posting, the same at a preferred
    company) in days."""
    hf = profile.get("hard_filters") or {}
    return (int(hf.get("max_posting_age_days") or DEFAULT_MAX_AGE_DAYS),
            int(hf.get("max_official_open_age_days") or DEFAULT_OFFICIAL_OPEN_MAX_AGE_DAYS),
            int(hf.get("max_official_open_age_days_preferred") or DEFAULT_PREFERRED_OPEN_MAX_AGE_DAYS))


def limits_text(profile: dict[str, Any]) -> str:
    max_age, official_open, preferred_open = limits(profile)
    return (f"limit {max_age} days; {official_open} for a confirmed open official posting, "
            f"{preferred_open} at a preferred company")


def confirmed_open(job: dict[str, Any]) -> bool:
    """An official posting (a feed job is one from the start) that pass 2 doesn't doubt."""
    from agents.scout.shortlist import possibly_closed  # shortlist imports this module

    official = job.get("source_trust") == "official" or (job.get("source") in db.SOURCES
                                                         and not job.get("source_trust"))
    return official and not possibly_closed(job)


def age_days(job: dict[str, Any], today: date | None = None) -> int | None:
    try:
        return max(0, ((today or date.today()) - date.fromisoformat((job.get("posted_at") or "")[:10])).days)
    except ValueError:
        return None


def limit_for(job: dict[str, Any], profile: dict[str, Any]) -> int:
    max_age, official_open, preferred_open = limits(profile)
    if not confirmed_open(job):
        return max_age
    return preferred_open if job.get("preferred_match") else official_open


def over_limit(job: dict[str, Any], profile: dict[str, Any], today: date | None = None) -> bool:
    age = age_days(job, today)
    return age is not None and age > limit_for(job, profile)


def unverified(job: dict[str, Any]) -> bool:
    """Only the alert email's date is known."""
    return bool(job.get("posted_at")) and job.get("posted_at_source") not in (*POSTING_SOURCES, "alert_badge")


def label(job: dict[str, Any], today: date | None = None) -> str:
    """"Posted 12 days ago (from posting)", "Posted ~3 days ago (alert)", "Posted at least
    2 days ago (email date, date unverified)" or "Date unknown"."""
    n = age_days(job, today)
    if n is None:
        return "Date unknown"
    when = "today" if n == 0 else f"{n} day{'s' if n != 1 else ''} ago"
    source = job.get("posted_at_source")
    if source in POSTING_SOURCES:
        return f"Posted {when} (from posting)"
    if source == "alert_badge":
        return f"Posted {'~' if n else ''}{when} (alert)"
    return f"Posted {'today or earlier' if n == 0 else 'at least ' + when} (email date, date unverified)"


def note(job: dict[str, Any], profile: dict[str, Any], today: date | None = None) -> str:
    """label(), plus HELD_NOTE for a held job over its limit (held jobs are never closed)."""
    text = label(job, today)
    if db.is_held(job, today) and over_limit(job, profile, today):
        text += f" — {HELD_NOTE}"
    return text


def refusal(job: dict[str, Any], profile: dict[str, Any], today: date | None = None) -> str | None:
    """Why the shortlist won't take this job, or None."""
    if not over_limit(job, profile, today):
        return None
    return f"the posting is {age_days(job, today)} days old (the limit is {limit_for(job, profile)} days)"


def too_old(profile: dict[str, Any], *, db_path: Path | str | None = None, today: date | None = None,
            job_ids: list[int] | None = None) -> list[dict[str, Any]]:
    """Open, not held jobs over their limit (all, or those in job_ids), each with age_days
    and age_limit; jobs the user reopened after an age close are left out."""
    today = today or date.today()
    jobs = ([j for j in (db.get_job(i, db_path=db_path) for i in job_ids) if j] if job_ids is not None
            else [j for s in CHECKED for j in db.get_jobs(status=s, db_path=db_path)])
    out = [{**job, "age_days": age_days(job, today), "age_limit": limit_for(job, profile)}
           for job in jobs if would_close(job, profile, db_path=db_path, today=today)]
    return sorted(out, key=lambda j: j["id"])


def would_close(job: dict[str, Any], profile: dict[str, Any], *, db_path: Path | str | None = None,
                today: date | None = None) -> bool:
    """Open and checked (not applied, interviewing or held), over its limit, and not a job
    the user reopened after an age close."""
    if job["status"] not in CHECKED or db.is_held(job, today) or not over_limit(job, profile, today):
        return False
    # Closed as too old before and reopened by the user: their call.
    return not any(e["to_status"] == "closed" and (e["detail"] or "").startswith(TOO_OLD_PREFIX)
                   for e in db.get_job_events(job["id"], db_path=db_path))


def close_too_old(profile: dict[str, Any], *, db_path: Path | str | None = None, today: date | None = None,
                  dry_run: bool = False, job_ids: list[int] | None = None) -> list[dict[str, Any]]:
    """Close the jobs too_old() finds (scout, "posting too old: <N> days"); returns them."""
    closed = []
    for job in too_old(profile, db_path=db_path, today=today, job_ids=job_ids):
        detail = f"{TOO_OLD_PREFIX} {job['age_days']} days"
        if not dry_run:
            try:
                db.set_status(job["id"], "closed", "scout", detail, db_path=db_path)
            except (db.TransitionError, db.ActorNotAllowed, LookupError) as exc:  # changed meanwhile
                log.warning("Age: job %d not closed: %s", job["id"], exc)
                continue
        log.info("Age: job %d %s | %s %s (%s; limit %d)", job["id"], job["company"], job["title"],
                 "would close" if dry_run else "closed", label(job, today), job["age_limit"])
        closed.append(job)
    return closed


def recheck(job_id: int, profile: dict[str, Any], *, db_path: Path | str | None = None,
            today: date | None = None) -> str | None:
    """Check one job after a better date arrived; returns the close detail if it closed."""
    closed = close_too_old(profile, db_path=db_path, today=today, job_ids=[job_id])
    return f"{TOO_OLD_PREFIX} {closed[0]['age_days']} days" if closed else None


def closed_on(today: date | None = None, *, db_path: Path | str | None = None) -> list[dict[str, Any]]:
    """Jobs closed as too old today (local date), each with `closed_detail`."""
    today = today or date.today()
    jobs = {j["id"]: j for j in db.get_jobs(status="closed", db_path=db_path)}
    return [{**jobs[jid], "closed_detail": detail}
            for jid, (at, detail) in db.latest_closes(TOO_OLD_PREFIX, db_path=db_path).items()
            if jid in jobs and datetime.fromisoformat(at).astimezone().date() == today]


def held_too_old(profile: dict[str, Any], *, db_path: Path | str | None = None,
                 today: date | None = None) -> list[dict[str, Any]]:
    """Held jobs over their limit: flagged, never closed."""
    return [j for s in CHECKED for j in db.get_jobs(status=s, db_path=db_path)
            if db.is_held(j, today) and over_limit(j, profile, today)]


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="replace")
    parser = argparse.ArgumentParser(prog="python -m agents.scout.age", description=__doc__.split("\n")[0])
    parser.add_argument("--db", type=Path, default=None)
    parser.add_argument("--profile", type=Path, default=DEFAULT_PROFILE_PATH)
    parser.add_argument("--dry-run", action="store_true", help="list what would close; change nothing")
    args = parser.parse_args(argv)
    profile = load_profile(args.profile)
    if not args.dry_run:
        db.init_db(args.db)
    with runlog.log_to(runlog.log_path("score")):
        jobs = close_too_old(profile, db_path=args.db, dry_run=args.dry_run)
    verb = "Would close" if args.dry_run else "Closed"
    print(f"{verb} {len(jobs)} job{'s' if len(jobs) != 1 else ''} with a posting too old ({limits_text(profile)})")
    for j in jobs:
        print(f"  #{j['id']:<5} {j['company'][:24]:<24}  {j['title'][:50]:<50}  {label(j)}")
    for j in held_too_old(profile, db_path=args.db):
        print(f"  held, not closed: #{j['id']} {j['company']}: {j['title']} ({label(j)}; {HELD_NOTE})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
