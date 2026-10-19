"""Scout run: discover jobs, filter, store, tag preferred companies, close removed feed postings.

    python -m agents.scout.fetch [--db PATH] [--profile PATH] [--preferred PATH]
                                 [--gmail-token PATH] [--since-days N] [--log-dir DIR]

Discovery sources:
  * Job alert emails (primary): Gmail messages matching "label:JobAlerts
    newer_than:<N>d" (--since-days, default 3), read with the gmail.readonly
    scope. Each message is parsed once; processed_emails remembers it. Known
    non-job emails (Indeed's "is now active" confirmations) are recorded as
    skipped. Emails from a sender with no parser, or that yield no jobs, are
    logged and left unrecorded, so a later run (or a new parser) retries them.
    An expired or revoked Gmail login raises the "gmail_auth_expired" notice,
    which the digest shows until a run reads Gmail successfully again.
  * ATS feeds, only for preferred companies with poll_feed: true. Fetched
    first, so when an alert email has the same posting the fuller feed copy wins.

The run fails only when neither source is configured (no Gmail token and no
poll_feed company). An empty preferred list is fine.

A job already in the database is marked seen whenever it is fetched again, even
if it no longer passes the filters. Only feed jobs are closed as "posting
removed" after 3 days unseen, and only for feeds that fetched successfully this
run. Email jobs are never closed that way: an alert stops mentioning a job
long before the posting is gone.
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from agents import db
from agents.common import gmail
from agents.scout import email_alerts, fetchers
from agents.scout.companies import (
    DEFAULT_PREFERRED_PATH,
    PreferredCompaniesError,
    feed_companies,
    load_preferred,
    match_preferred,
)
from agents.scout.filters import DEFAULT_PROFILE_PATH, apply_hard_filters, dedupe_key, load_profile

log = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_LOG_DIR = ROOT / "logs"

FETCHERS: dict[str, Callable[..., list[dict[str, Any]]]] = {
    "greenhouse": fetchers.fetch_greenhouse,
    "lever": fetchers.fetch_lever,
    "ashby": fetchers.fetch_ashby,
}
REQUEST_DELAY_SECONDS = 1.0
CLOSE_AFTER = timedelta(days=3)
CLOSABLE_STATUSES = ("new", "scored", "shortlisted", "researched")
REMOVED_DETAIL = "posting removed"
GMAIL_EXPIRED_NOTICE = "gmail_auth_expired"
GMAIL_EXPIRED_MESSAGE = (
    "Gmail login expired or was revoked, so job alert emails were not read. "
    "Run: python -m agents.common.gmail auth"
)


class NoDiscoverySource(RuntimeError):
    """Neither Gmail nor any poll_feed company is configured."""


@dataclass
class SenderStats:
    addresses: set[str] = field(default_factory=set)
    emails_read: int = 0
    jobs_found: int = 0
    unparsed: int = 0
    skipped: int = 0
    ignored: int = 0


@dataclass
class Summary:
    started_at: str
    email_alerts: bool = False
    feed_companies: int = 0
    emails_already_processed: int = 0
    gmail_auth_expired: bool = False
    senders: dict[str, SenderStats] = field(default_factory=dict)
    fetched: int = 0
    passed: int = 0
    new: int = 0
    new_preferred: int = 0
    updated: int = 0
    closed: int = 0
    duplicates: int = 0
    errors: list[str] = field(default_factory=list)
    rejections: Counter = field(default_factory=Counter)

    def lines(self) -> list[str]:
        sources = []
        if self.email_alerts:
            sources.append("job alert emails")
        if self.feed_companies:
            sources.append(f"ATS feeds ({self.feed_companies} companies)")
        out = [f"Scout run {self.started_at}"]
        if self.gmail_auth_expired:
            out.append(f"  WARNING: {GMAIL_EXPIRED_MESSAGE}")
        out += [
            f"  sources:        {', '.join(sources) or 'none'}",
            f"  fetched:        {self.fetched}",
            f"  passed filters: {self.passed}",
            f"  new:            {self.new} ({self.new_preferred} at preferred companies)",
            f"  updated:        {self.updated}",
            f"  closed:         {self.closed}",
            f"  errors:         {len(self.errors)}",
        ]
        if self.duplicates:
            out.append(f"  duplicates skipped: {self.duplicates}")
        if self.email_alerts:
            out.append(f"  emails already processed: {self.emails_already_processed}")
            for name, st in sorted(self.senders.items()):
                label = name if name in st.addresses else f"{name} ({', '.join(sorted(st.addresses))})"
                line = f"  email {label}: {st.emails_read} read / {st.jobs_found} jobs found / {st.unparsed} unparsed"
                if st.skipped:
                    line += f" / {st.skipped} skipped (not job lists)"
                if st.ignored:
                    line += f" / {st.ignored} ignored"
                out.append(line)
        if self.rejections:
            out.append("Rejections by filter:")
            out += [f"  {reason}: {n}" for reason, n in sorted(self.rejections.items(), key=lambda x: (-x[1], x[0]))]
        if self.errors:
            out.append("Errors:")
            out += [f"  {e}" for e in self.errors]
        return out


def run(
    profile: dict[str, Any],
    preferred: list[dict[str, Any]],
    *,
    mailbox: email_alerts.Mailbox | None = None,
    since_days: int = email_alerts.DEFAULT_SINCE_DAYS,
    db_path: Path | str | None = None,
    fetcher_map: dict[str, Callable[..., list[dict[str, Any]]]] | None = None,
    sleep: Callable[[float], None] | None = None,
    now: datetime | None = None,
) -> Summary:
    fetcher_map = fetcher_map or FETCHERS
    sleep = sleep or time.sleep
    now = now or datetime.now(timezone.utc)

    feeds = feed_companies(preferred)
    if mailbox is None and not feeds:
        raise NoDiscoverySource(
            "No discovery source is configured. Run `python -m agents.common.gmail auth` to connect "
            "Gmail (job alert emails), or give a company in preferred_companies.yaml poll_feed: true "
            "with ats and slug."
        )

    summary = Summary(
        started_at=now.isoformat(timespec="seconds"),
        email_alerts=mailbox is not None,
        feed_companies=len(feeds),
    )
    requests_made = 0

    def pace() -> None:
        nonlocal requests_made
        if requests_made:
            sleep(REQUEST_DELAY_SECONDS)
        requests_made += 1

    db.init_db(db_path)
    conn = db.connect(db_path)
    try:
        ctx = _RunContext(conn, profile, preferred, now, summary)

        fetched_feeds: list[str] = []
        for entry in feeds:
            label = f"{entry['name']} ({entry['ats']}/{entry['slug']})"
            pace()
            try:
                jobs = fetcher_map[entry["ats"]](entry["slug"], company=entry["name"])
            except Exception as exc:  # one bad board must not stop the run
                log.exception("Fetching %s failed", label)
                _error(summary, f"{label}: {exc}")
                continue
            with conn:
                ctx.store(jobs)
            fetched_feeds.append(f"{entry['ats']}:{entry['slug']}:")
            log.info("%s: %d postings", label, len(jobs))

        if mailbox is not None:
            _run_email_alerts(ctx, mailbox, since_days)

        with conn:
            _close_removed(conn, fetched_feeds, now, summary)
    finally:
        conn.close()

    for line in summary.lines():
        log.info(line)
    return summary


class _RunContext:
    def __init__(self, conn, profile, preferred, now: datetime, summary: Summary) -> None:
        self.conn = conn
        self.profile = profile
        self.preferred = preferred
        self.now = now
        self.summary = summary
        self.seen_ids: set[str] = set()

    def store(self, jobs: list[dict[str, Any]]) -> None:
        s = self.summary
        for job in jobs:
            # The same posting often appears in several alert emails.
            if job["id"] in self.seen_ids:
                continue
            self.seen_ids.add(job["id"])
            s.fetched += 1

            match = match_preferred(job["company"], self.preferred)
            preferred_match, preferred_tier = match if match else (None, None)

            # The preferred match sets the age limit of an open official (feed) posting.
            passed, reason = apply_hard_filters({**job, "preferred_match": preferred_match}, self.profile,
                                                today=self.now.date())
            if passed:
                s.passed += 1
            else:
                s.rejections[reason.split(":", 1)[0]] += 1

            # Known jobs are marked seen regardless of filters, so they don't go stale.
            if db.touch_job(job["source"], job["id"], posted_at=job.get("posted_at"),
                            posted_at_source=job.get("posted_at_source"), conn=self.conn):
                db.update_preferred(job["source"], job["id"], preferred_match, preferred_tier, conn=self.conn)
                s.updated += 1
                continue
            if not passed:
                # New postings only (known jobs returned above), once each: stats' "filtered out".
                db.record_filtered_posting(f"{job['source']}:{job['id']}", reason, job.get("company"),
                                           job.get("title"), conn=self.conn)
                continue

            key = dedupe_key(job)
            twin = db.find_open_job_by_dedupe_key(key, conn=self.conn)
            if twin:
                s.duplicates += 1
                log.info("Skipping %s: same posting as job %d (%s)", job["id"], twin["id"], key)
                continue
            _, inserted = db.upsert_job(
                _to_db_job(job, key, preferred_match, preferred_tier), actor="scout", conn=self.conn
            )
            if inserted:
                s.new += 1
                if preferred_match:
                    s.new_preferred += 1


def _run_email_alerts(ctx: _RunContext, mailbox: email_alerts.Mailbox, since_days: int) -> None:
    s, conn = ctx.summary, ctx.conn

    def already_processed(message_id: str) -> bool:
        if db.is_email_processed(message_id, conn=conn):
            s.emails_already_processed += 1
            return True
        return False

    try:
        for alert in email_alerts.read_alerts(mailbox, skip=already_processed, since_days=since_days):
            sender = alert.message["sender"]
            stats = s.senders.setdefault(alert.sender_type or sender, SenderStats())
            stats.addresses.add(sender)
            stats.emails_read += 1
            stats.jobs_found += len(alert.jobs)
            if alert.skipped or alert.ignored:
                if alert.ignored:
                    stats.ignored += 1
                else:
                    stats.skipped += 1
                with conn:
                    db.record_processed_email(alert.message["id"], alert.source, 0, sender=sender, conn=conn)
                continue
            if alert.error:
                _error(s, f"{alert.sender_type} parser, message {alert.message['id']}: {alert.error}")
            if not alert.parsed:
                stats.unparsed += 1
                with conn:
                    db.record_unparsed_email(alert.message["id"], sender, conn=conn)
                continue
            with conn:
                ctx.store(alert.jobs)
                db.record_processed_email(alert.message["id"], alert.source, len(alert.jobs), sender=sender,
                                          conn=conn)
    except gmail.GmailAuthExpired as exc:
        # Needs the user: the digest keeps showing this until a run reads Gmail again.
        log.error("Gmail login expired: %s", exc)
        s.gmail_auth_expired = True
        _error(s, GMAIL_EXPIRED_MESSAGE)
        with conn:
            db.raise_notice(GMAIL_EXPIRED_NOTICE, GMAIL_EXPIRED_MESSAGE, conn=conn)
        return
    except Exception as exc:  # Gmail unreachable or token unusable: keep the feed results
        log.exception("Reading job alert emails failed")
        _error(s, f"job alert emails: {exc}")
        return
    with conn:
        db.clear_notice(GMAIL_EXPIRED_NOTICE, conn=conn)


def _close_removed(conn, fetched_feeds: list[str], now: datetime, summary: Summary) -> None:
    if not fetched_feeds:
        return
    cutoff = (now - CLOSE_AFTER).isoformat(timespec="seconds")
    for job in db.get_unseen_jobs(cutoff, CLOSABLE_STATUSES, conn=conn):
        if not job["external_id"].startswith(tuple(fetched_feeds)):
            continue
        try:
            db.set_status(job["id"], "closed", "scout", detail=REMOVED_DETAIL, conn=conn)
        except (db.TransitionError, db.ActorNotAllowed) as exc:
            _error(summary, f"closing job {job['id']}: {exc}")
            continue
        summary.closed += 1


def _to_db_job(job: dict[str, Any], key: str, preferred_match: str | None, preferred_tier: str | None) -> dict[str, Any]:
    return {
        "source": job["source"],
        "external_id": job["id"],
        "company": job["company"],
        "title": job["title"],
        "location": job["location"],
        "url": job["url"],
        "description": job["description"],
        "posted_at": job["posted_at"],
        "posted_at_source": job.get("posted_at_source"),
        "remote": job["remote"],
        "salary_min": job["salary_min"],
        "salary_max": job["salary_max"],
        "salary_currency": job["salary_currency"],
        "salary_is_predicted": job.get("salary_is_predicted", 0),
        "needs_details": job.get("needs_details", 0),
        "sponsored": job.get("sponsored", 0),
        "dedupe_key": key,
        "preferred_match": preferred_match,
        "preferred_tier": preferred_tier,
    }


def _error(summary: Summary, message: str) -> None:
    log.error(message)
    summary.errors.append(message)


def _positive_int(value: str) -> int:
    n = int(value)
    if n < 1:
        raise argparse.ArgumentTypeError("must be at least 1")
    return n


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="replace")

    parser = argparse.ArgumentParser(prog="python -m agents.scout.fetch", description=__doc__.split("\n")[0])
    parser.add_argument("--db", type=Path, default=None)
    parser.add_argument("--profile", type=Path, default=DEFAULT_PROFILE_PATH)
    parser.add_argument("--preferred", type=Path, default=DEFAULT_PREFERRED_PATH)
    parser.add_argument("--gmail-token", type=Path, default=gmail.TOKEN_PATH)
    parser.add_argument("--since-days", type=_positive_int, default=email_alerts.DEFAULT_SINCE_DAYS,
                        help="read alert emails from the last N days (default %(default)s)")
    parser.add_argument("--log-dir", type=Path, default=DEFAULT_LOG_DIR)
    args = parser.parse_args(argv)

    args.log_dir.mkdir(parents=True, exist_ok=True)
    log_path = args.log_dir / f"scout-{datetime.now().date().isoformat()}.log"
    handler = logging.FileHandler(log_path, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    root = logging.getLogger()
    old_level = root.level
    root.addHandler(handler)
    root.setLevel(logging.INFO)
    try:
        try:
            preferred = load_preferred(args.preferred)
            if not preferred:
                log.info("No preferred companies in %s; matching skipped", args.preferred)
            summary = run(
                load_profile(args.profile),
                preferred,
                mailbox=gmail.Mailbox(token_path=args.gmail_token) if args.gmail_token.exists() else None,
                since_days=args.since_days,
                db_path=args.db,
            )
        except (NoDiscoverySource, PreferredCompaniesError) as exc:
            log.error("Scout not run: %s", exc)
            print(f"Error: {exc}", file=sys.stderr)
            print(f"Log: {log_path}", file=sys.stderr)
            return 2
    finally:
        root.removeHandler(handler)
        root.setLevel(old_level)
        handler.close()

    print("\n".join(summary.lines()))
    print(f"Log: {log_path}")
    attempted = int(summary.email_alerts) + summary.feed_companies
    return 1 if attempted and len(summary.errors) >= attempted else 0


if __name__ == "__main__":
    sys.exit(main())
