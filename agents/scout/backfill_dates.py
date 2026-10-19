"""One-time backfill of posting dates for open official jobs, without Claude.

    python -m agents.scout.backfill_dates [--db PATH] [--profile PATH] [--dry-run]

Jobs stored before posted_at_source existed carry the alert email's date. For each job
that isn't closed and whose source_trust is official:

  1. The stored description is searched for a stated date ("Posted Aug 21, 2026",
     "Date posted: 2026-08-21", "Published on 08/21/2026"; description_date). Source
     posting_page.
  2. Otherwise the official posting is read again with the no-Claude readers, official
     links only (source_url, then apply_url): the ATS's public JSON (Workable, BambooHR,
     Breezy through posting.api_posting; Greenhouse, Lever and Ashby through ats_json_date),
     source ats; else the page's JSON-LD datePosted or published-date metadata
     (posting.page_date), source posting_page.

A date is stored only where the better-source rule allows it (db.offer_posted_at). Then
each job's age is checked as when any better date arrives (age.close_too_old: 10 days, or
30 for a confirmed open official posting, 45 at a preferred company; held, applied and
interviewing jobs are never closed). --dry-run reads the pages but writes nothing, not even
the pending migration, and prints what would change and close.
"""

from __future__ import annotations

import argparse
import logging
import re
import sys
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import requests

from agents import db
from agents.common import runlog
from agents.scout import age, posting, source_trust
from agents.scout.fetchers import _iso_date, _iso_date_from_ms
from agents.scout.filters import DEFAULT_PROFILE_PATH, load_profile

log = logging.getLogger(__name__)

_MONTH = (r"jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|aug(?:ust)?|"
          r"sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?")
_DATE = (rf"(?P<date>\d{{4}}-\d{{2}}-\d{{2}}|\d{{1,2}}/\d{{1,2}}/\d{{4}}"
         rf"|(?:{_MONTH})\.?\s+\d{{1,2}}(?:st|nd|rd|th)?,?\s+\d{{4}}|\d{{1,2}}\s+(?:{_MONTH})\.?,?\s+\d{{4}})")
_STATED = re.compile(
    rf"\b(?:date\s+posted|posting\s+date|posted(?:\s+on)?|published(?:\s+on)?|publication\s+date"
    rf"|date\s+published|open(?:ing)?\s+date|date\s+opened)\s*[:\-–]?\s*{_DATE}", re.I)
_GREENHOUSE = re.compile(r"^/([^/]+)/jobs/(\d+)")
_LEVER_ASHBY = re.compile(r"^/([^/]+)/([0-9a-f-]{36})")


def parse_date(text: str) -> date | None:
    """A date written as 2026-08-21, 08/21/2026 (US order), Aug 21, 2026 or 21 August 2026."""
    text = re.sub(r"(\d)(st|nd|rd|th)\b", r"\1", text.strip().replace(",", " ").replace(".", " "), flags=re.I)
    text = " ".join(text.split())
    for fmt in ("%Y-%m-%d", "%m/%d/%Y"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            pass
    parts = text.split()
    if len(parts) == 3:
        month, day = (parts[0], parts[1]) if parts[0].isalpha() else (parts[1], parts[0])
        try:
            return datetime.strptime(f"{month[:3]} {day} {parts[2]}", "%b %d %Y").date()
        except ValueError:
            return None
    return None


def description_date(text: str | None, today: date | None = None) -> str | None:
    """The first posted or published date the description states, if plausible."""
    today = today or date.today()
    for m in _STATED.finditer(text or ""):
        found = parse_date(m["date"])
        if found and date(2000, 1, 1) <= found <= today:
            return found.isoformat()
    return None


def ats_json_date(url: str, session: Any) -> str | None:
    """The posting's date from the Greenhouse, Lever or Ashby public API, or None for
    another host or when the API doesn't list it."""
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    if host.endswith("greenhouse.io") and (m := _GREENHOUSE.match(parts.path)):
        data = posting.get_json(f"https://boards-api.greenhouse.io/v1/boards/{m[1]}/jobs/{m[2]}", session)
        return _iso_date(data.get("first_published") or data.get("updated_at"))
    if host == "jobs.lever.co" and (m := _LEVER_ASHBY.match(parts.path)):
        data = posting.get_json(f"https://api.lever.co/v0/postings/{m[1]}/{m[2]}", session)
        return _iso_date_from_ms(data.get("createdAt"))
    if host == "jobs.ashbyhq.com" and (m := _LEVER_ASHBY.match(parts.path)):
        data = posting.get_json(f"https://api.ashbyhq.com/posting-api/job-board/{m[1]}", session)
        job = next((j for j in data.get("jobs") or [] if j.get("id") == m[2]), None)
        return _iso_date(job.get("publishedAt")) if job else None
    return None


def official_urls(job: dict[str, Any]) -> list[str]:
    out = []
    for url in (job.get("source_url"), job.get("apply_url")):
        if url and url not in out and source_trust.classify(url, job["company"]) == source_trust.OFFICIAL:
            out.append(url)
    return out


def reread_date(job: dict[str, Any], session: Any) -> tuple[str | None, str | None, str]:
    """(date, source, how) from the official posting, read again without Claude."""
    problems = []
    for url in official_urls(job):
        try:
            if posting.api_url_for(url):
                found = (posting.api_posting(url, session) or {}).get("posted_at")
                if found:
                    return found, "ats", f"ATS JSON for {url}"
            found = ats_json_date(url, session)
            if found:
                return found, "ats", f"ATS API for {url}"
            found = posting.page_date(posting.get(url, session))
            if found:
                return found, "posting_page", f"page metadata of {url}"
            problems.append(f"{url}: no date on the page")
        except (posting.PostingError, requests.RequestException, ValueError, AttributeError) as exc:
            problems.append(f"{url}: {exc}")
    return None, None, "; ".join(problems) or "no official link to read"


@dataclass
class Row:
    job: dict[str, Any]
    old_date: str | None
    old_source: str | None
    new_date: str | None
    new_source: str | None
    how: str
    stored: bool  # the better-source rule lets the new date replace the old one
    after: dict[str, Any]  # the job as it would be with the stored date

    def verdict(self, profile: dict[str, Any], db_path: Path | str | None, today: date) -> str:
        j = self.after
        if j["status"] in ("applied", "interviewing"):
            return "never closed (applied)" if j["status"] == "applied" else "never closed (interviewing)"
        if db.is_held(j, today):
            return f"held, never closed: {age.HELD_NOTE}" if age.over_limit(j, profile, today) else "held, ok"
        if age.would_close(j, profile, db_path=db_path, today=today):
            return "WOULD CLOSE"
        return "ok"


def plan(*, db_path: Path | str | None = None, session: Any = None, today: date | None = None) -> list[Row]:
    """What the backfill would store, job by job (reads pages; writes nothing)."""
    today = today or date.today()
    session = session or requests.Session()
    rows = []
    for job in db.get_jobs(db_path=db_path):
        if job["status"] == "closed" or job.get("source_trust") != source_trust.OFFICIAL:
            continue
        old_source = job.get("posted_at_source") or (
            None if not job.get("posted_at") else "ats" if job["source"] in db.SOURCES else "email_date")
        found = description_date(job.get("description"), today)
        if found:
            new_date, new_source, how = found, "posting_page", "stated in the stored description"
        else:
            new_date, new_source, how = reread_date(job, session)
        stored = bool(new_date) and db.better_date_source(new_source, old_source) and (
            (job.get("posted_at") or "")[:10], old_source) != (new_date, new_source)
        after = {**job, "posted_at_source": old_source}
        if stored:
            after.update(posted_at=new_date, posted_at_source=new_source)
        rows.append(Row(job, job.get("posted_at"), old_source, new_date, new_source, how, stored, after))
        log.info("Backfill dates: job %d %s -> %s (%s; %s)", job["id"], job.get("posted_at"), new_date, new_source, how)
    return rows


def apply(rows: list[Row], profile: dict[str, Any], *, db_path: Path | str | None = None,
          today: date | None = None) -> list[dict[str, Any]]:
    """Store the planned dates (better source wins) and close the jobs now too old."""
    stored = []
    for row in rows:
        if row.stored and db.offer_posted_at(row.job["id"], row.new_date, row.new_source, db_path=db_path):
            db.log_event(row.job["id"], "posted_at_backfilled", "scout",
                         detail=f"{row.old_date} ({row.old_source}) -> {row.new_date} ({row.new_source}): {row.how}",
                         db_path=db_path)
            stored.append(row.job["id"])
    return age.close_too_old(profile, db_path=db_path, today=today, job_ids=stored) if stored else []


def lines(rows: list[Row], profile: dict[str, Any], *, db_path: Path | str | None = None,
          today: date | None = None, dry_run: bool = True) -> list[str]:
    today = today or date.today()
    out = [f"{'Would backfill' if dry_run else 'Backfilled'} posting dates for {len(rows)} open official job"
           f"{'s' if len(rows) != 1 else ''} ({age.limits_text(profile)})"]
    for r in rows:
        new = f"{r.new_date} ({r.new_source})" if r.new_date else "none found"
        keep = "" if r.stored or not r.new_date else " — not stored: the old date's source is as good or better"
        out += [f"  #{r.job['id']:<4} {r.job['status']:<11} {r.job['company'][:28]}: {r.job['title'][:50]}",
                f"        old {r.old_date or 'none'} ({r.old_source or '-'}) -> new {new}{keep}",
                f"        how: {r.how}",
                f"        age: {age.label(r.after, today)}; limit {age.limit_for(r.after, profile)} days; "
                f"{r.verdict(profile, db_path, today)}"]
    return out


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="replace")
    parser = argparse.ArgumentParser(prog="python -m agents.scout.backfill_dates", description=__doc__.split("\n")[0])
    parser.add_argument("--db", type=Path, default=None)
    parser.add_argument("--profile", type=Path, default=DEFAULT_PROFILE_PATH)
    parser.add_argument("--dry-run", action="store_true", help="read the postings, print the plan, change nothing")
    args = parser.parse_args(argv)
    profile = load_profile(args.profile)
    with runlog.log_to(runlog.log_path("score")):
        if not args.dry_run and db.pending_migrations(args.db):
            print(f"Backed up the database to {db.backup(args.db)} before migrating")
            db.init_db(args.db)
        rows = plan(db_path=args.db)
        print("\n".join(lines(rows, profile, db_path=args.db, dry_run=args.dry_run)))
        if args.dry_run:
            return 0
        closed = apply(rows, profile, db_path=args.db)
    print(f"Stored {sum(r.stored for r in rows)} date(s); closed {len(closed)} job(s) as too old"
          + "".join(f"\n  #{j['id']} {j['company']}: posting too old: {j['age_days']} days" for j in closed))
    return 0


if __name__ == "__main__":
    sys.exit(main())
