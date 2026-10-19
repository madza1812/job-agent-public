"""SQLite storage for jobs, applications and the event log.

This module is the single place where job status transitions are enforced.
Agents must change status through set_status(), never with their own UPDATEs.

Concurrency: the daily run and the review app may use the database at the same time.
connect() puts it in WAL mode (readers never wait for a writer) with a BUSY_TIMEOUT_S busy
timeout (a writer waits for another writer instead of failing). Keep transactions short:
never hold one open across a Claude call, a web request or a Gmail read.

CLI:
    python -m agents.db init
    python -m agents.db show [--status scored] [--limit 20]
    python -m agents.db mark-applied <job_id> --method ats [--notes "..."]
"""

from __future__ import annotations

import argparse
import logging
import json
import re
import sqlite3
import sys
from contextlib import contextmanager
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Iterator

log = logging.getLogger(__name__)

DEFAULT_DB_PATH = Path(__file__).resolve().parent.parent / "data" / "jobs.db"
BUSY_TIMEOUT_S = 30
BACKUP_DIR_NAME = "backups"  # next to the database: data/backups/
KEEP_BACKUPS = 10
_BACKUP_NAME = re.compile(r"^jobs-\d{8}-\d{4}(-\d+)?\.db$")

STATUSES = (
    "new",
    "scored",
    "shortlisted",
    "researched",
    "applied",
    "interviewing",
    "closed",
)

# Forward moves along the lifecycle. Any status other than closed may also move to closed.
# The one way back: closed -> new, only for scout, and only when the job's latest close
# was a filter rejection (detail starting with FILTERED_DETAIL_PREFIX). See set_status().
TRANSITIONS: dict[str, set[str]] = {
    "new": {"scored"},
    "scored": {"shortlisted"},
    "shortlisted": {"researched"},
    "researched": {"applied"},
    "applied": {"interviewing"},
    "interviewing": set(),
    "closed": set(),
}

ACTORS = ("user", "scout", "research", "tracker")

# The user may mark a job applied from any of these (and from closed, after a tracker "Did
# you apply?" item). Agents never may.
USER_APPLY_FROM = ("new", "scored", "shortlisted", "researched")

# Detail on a close by the hard filters: "filtered: <reason>".
FILTERED_DETAIL_PREFIX = "filtered:"

# The only details with which scout may move a shortlisted or researched job back to
# scored: its posting may be closed, or it was found only on an aggregator.
POSSIBLY_CLOSED_DETAIL = "possibly closed"
AGGREGATOR_ONLY_DETAIL = "aggregator only"
MOVE_BACK_DETAILS = (POSSIBLY_CLOSED_DETAIL, AGGREGATOR_ONLY_DETAIL)

# Everything the tracker may do, and nothing else: an interview, assessment or scheduling
# email moves applied -> interviewing; a rejection closes with detail REJECTED_DETAIL.
REJECTED_DETAIL = "rejected"
TRACKER_MOVES: dict[tuple[str, str], str | None] = {  # (from, to) -> required detail, None = any
    ("applied", "interviewing"): None,
    ("applied", "closed"): REJECTED_DETAIL,
    ("interviewing", "closed"): REJECTED_DETAIL,
}

# ATS feeds. Job alert emails use "email:<sender type>", e.g. "email:linkedin".
SOURCES = ("greenhouse", "lever", "ashby")
EMAIL_SOURCE_PREFIX = "email:"

# Where jobs.posted_at came from, worst to best: the alert email's own date (the posting is
# at least that old), an alert's age badge ("3 days ago", an estimate), the posting page or
# an ATS's data. A date is only ever replaced by one from an equal or better source, and
# alert dates never replace each other (the first sighting is the best bound).
POSTED_AT_SOURCES = ("email_date", "alert_badge", "posting_page", "ats")
_DATE_RANK = {"email_date": 1, "alert_badge": 2, "posting_page": 3, "ats": 3}

TIERS = ("dream", "strong", "interested")

_REQUIRED_JOB_FIELDS = ("source", "external_id", "company", "title")
_OPTIONAL_JOB_FIELDS = (
    "location",
    "url",
    "description",
    "posted_at",
    "posted_at_source",
    "remote",
    "salary_min",
    "salary_max",
    "salary_currency",
    "salary_is_predicted",
    "needs_details",
    "dedupe_key",
    "preferred_match",
    "preferred_tier",
    "sponsored",
)

# Versioned migrations. Index i holds the SQL that moves the schema from
# version i to version i + 1. PRAGMA user_version records the current version.
# Append new migrations; never edit one that has shipped.
MIGRATIONS: list[str] = [
    """
    CREATE TABLE jobs (
        id            INTEGER PRIMARY KEY,
        source        TEXT NOT NULL CHECK (source IN ('greenhouse', 'lever', 'ashby')),
        external_id   TEXT NOT NULL,
        company       TEXT NOT NULL,
        title         TEXT NOT NULL,
        location      TEXT,
        url           TEXT,
        description   TEXT,
        posted_at     TEXT,
        status        TEXT NOT NULL DEFAULT 'new' CHECK (status IN (
                          'new', 'scored', 'shortlisted', 'researched',
                          'applied', 'interviewing', 'closed')),
        score         REAL,
        first_seen_at TEXT NOT NULL,
        last_seen_at  TEXT NOT NULL,
        updated_at    TEXT NOT NULL,
        UNIQUE (source, external_id)
    );

    CREATE TABLE applications (
        id         INTEGER PRIMARY KEY,
        job_id     INTEGER NOT NULL REFERENCES jobs(id),
        applied_at TEXT NOT NULL,
        method     TEXT NOT NULL,
        notes      TEXT
    );

    CREATE TABLE events (
        id          INTEGER PRIMARY KEY,
        job_id      INTEGER REFERENCES jobs(id),
        at          TEXT NOT NULL,
        actor       TEXT NOT NULL,
        event       TEXT NOT NULL,
        from_status TEXT,
        to_status   TEXT,
        detail      TEXT
    );

    CREATE INDEX idx_jobs_status ON jobs(status);
    CREATE INDEX idx_jobs_score ON jobs(score);
    CREATE INDEX idx_events_job_id ON events(job_id);
    CREATE INDEX idx_applications_job_id ON applications(job_id);
    """,
    # 2: fields from the normalized fetcher output that the scorer needs.
    """
    ALTER TABLE jobs ADD COLUMN remote INTEGER CHECK (remote IN (0, 1));
    ALTER TABLE jobs ADD COLUMN salary_min INTEGER;
    ALTER TABLE jobs ADD COLUMN salary_max INTEGER;
    ALTER TABLE jobs ADD COLUMN salary_currency TEXT;
    ALTER TABLE jobs ADD COLUMN dedupe_key TEXT;
    CREATE INDEX idx_jobs_dedupe_key ON jobs(dedupe_key);
    CREATE INDEX idx_jobs_last_seen_at ON jobs(last_seen_at);
    """,
    # 3: rebuild jobs without the CHECK on source, so new sources (email alerts) need no
    # table rebuild. Valid sources are enforced in Python (SOURCES). SQLite can't
    # drop a CHECK in place; init_db turns foreign keys off around migrations.
    """
    CREATE TABLE jobs_new (
        id              INTEGER PRIMARY KEY,
        source          TEXT NOT NULL,
        external_id     TEXT NOT NULL,
        company         TEXT NOT NULL,
        title           TEXT NOT NULL,
        location        TEXT,
        url             TEXT,
        description     TEXT,
        posted_at       TEXT,
        status          TEXT NOT NULL DEFAULT 'new' CHECK (status IN (
                            'new', 'scored', 'shortlisted', 'researched',
                            'applied', 'interviewing', 'closed')),
        score           REAL,
        first_seen_at   TEXT NOT NULL,
        last_seen_at    TEXT NOT NULL,
        updated_at      TEXT NOT NULL,
        remote          INTEGER CHECK (remote IN (0, 1)),
        salary_min      INTEGER,
        salary_max      INTEGER,
        salary_currency TEXT,
        dedupe_key      TEXT,
        UNIQUE (source, external_id)
    );
    INSERT INTO jobs_new (
        id, source, external_id, company, title, location, url, description, posted_at,
        status, score, first_seen_at, last_seen_at, updated_at, remote, salary_min,
        salary_max, salary_currency, dedupe_key)
    SELECT
        id, source, external_id, company, title, location, url, description, posted_at,
        status, score, first_seen_at, last_seen_at, updated_at, remote, salary_min,
        salary_max, salary_currency, dedupe_key
    FROM jobs;
    DROP TABLE jobs;
    ALTER TABLE jobs_new RENAME TO jobs;
    CREATE INDEX idx_jobs_status ON jobs(status);
    CREATE INDEX idx_jobs_score ON jobs(score);
    CREATE INDEX idx_jobs_dedupe_key ON jobs(dedupe_key);
    CREATE INDEX idx_jobs_last_seen_at ON jobs(last_seen_at);
    """,
    # 4: preferred-company match, estimated-salary and needs-details flags, the
    # log of job alert emails already parsed, and notices for the digest.
    """
    ALTER TABLE jobs ADD COLUMN salary_is_predicted INTEGER NOT NULL DEFAULT 0
        CHECK (salary_is_predicted IN (0, 1));
    ALTER TABLE jobs ADD COLUMN needs_details INTEGER NOT NULL DEFAULT 0
        CHECK (needs_details IN (0, 1));
    ALTER TABLE jobs ADD COLUMN preferred_match TEXT;
    ALTER TABLE jobs ADD COLUMN preferred_tier TEXT
        CHECK (preferred_tier IN ('dream', 'strong', 'interested'));
    CREATE INDEX idx_jobs_preferred_tier ON jobs(preferred_tier);

    CREATE TABLE processed_emails (
        message_id   TEXT PRIMARY KEY,
        processed_at TEXT NOT NULL,
        source       TEXT,
        jobs_found   INTEGER NOT NULL DEFAULT 0
    );

    -- Problems that need the user, e.g. an expired Gmail login. Active while cleared_at is NULL.
    CREATE TABLE notices (
        key        TEXT PRIMARY KEY,
        message    TEXT NOT NULL,
        raised_at  TEXT NOT NULL,
        cleared_at TEXT
    );
    """,
    # 5: sponsored listings (e.g. Indeed digest ads) that carry no job page link.
    """
    ALTER TABLE jobs ADD COLUMN sponsored INTEGER NOT NULL DEFAULT 0
        CHECK (sponsored IN (0, 1));
    """,
    # 6: how the final score was computed (JSON: components, weights, adjustments, cap).
    """
    ALTER TABLE jobs ADD COLUMN score_detail TEXT;
    """,
    # 7: the details fetch (official posting found or not, its links, years asked for)
    # and the user's thumbs up/down.
    """
    ALTER TABLE jobs ADD COLUMN details_status TEXT CHECK (details_status IN ('found', 'not_found'));
    ALTER TABLE jobs ADD COLUMN details_fetched_at TEXT;
    ALTER TABLE jobs ADD COLUMN apply_url TEXT;
    ALTER TABLE jobs ADD COLUMN source_url TEXT;
    ALTER TABLE jobs ADD COLUMN years_required INTEGER;
    ALTER TABLE jobs ADD COLUMN user_rating INTEGER CHECK (user_rating IN (-1, 1));
    """,
    # 8: how far to trust the posting's source, and how often the details lookup ran.
    # Feed jobs come straight from the company's ATS. Lookups so far count as one try.
    """
    ALTER TABLE jobs ADD COLUMN source_trust TEXT
        CHECK (source_trust IN ('official', 'aggregator', 'not_found'));
    ALTER TABLE jobs ADD COLUMN details_attempts INTEGER NOT NULL DEFAULT 0;
    UPDATE jobs SET source_trust = 'official' WHERE source IN ('greenhouse', 'lever', 'ashby');
    UPDATE jobs SET source_trust = 'not_found' WHERE details_status = 'not_found';
    UPDATE jobs SET details_attempts = 1 WHERE details_status IS NOT NULL;
    """,
    # 9: the Research analyst. Company research is cached per normalized company name
    # (newest row wins; older rows are kept as history), and each job keeps its brief's path.
    """
    CREATE TABLE company_research (
        id          INTEGER PRIMARY KEY,
        company_key TEXT NOT NULL,
        company     TEXT NOT NULL,
        markdown    TEXT NOT NULL,
        sources     TEXT NOT NULL,
        created_at  TEXT NOT NULL
    );
    CREATE INDEX idx_company_research_key ON company_research(company_key, created_at);
    ALTER TABLE jobs ADD COLUMN brief_path TEXT;
    """,
    # 10: each company's official job board (an ATS listing page), so its postings can be
    # checked with a plain HTTP request instead of a web search. source: 'user' (set-posting)
    # or 'lookup' (an official ATS posting found by the details lookup).
    """
    CREATE TABLE company_boards (
        company_key TEXT PRIMARY KEY,
        company     TEXT NOT NULL,
        board_url   TEXT NOT NULL,
        source      TEXT NOT NULL CHECK (source IN ('user', 'lookup')),
        updated_at  TEXT NOT NULL
    );
    """,
    # 11: the public ATS API probe per company (ats_api.py): which board-name guesses were
    # tried, and the board found (hit) or not (miss). A miss is retried after a while.
    """
    CREATE TABLE ats_probes (
        company_key TEXT PRIMARY KEY,
        company     TEXT NOT NULL,
        result      TEXT NOT NULL CHECK (result IN ('hit', 'miss')),
        ats         TEXT,
        slug        TEXT,
        tried       TEXT NOT NULL,
        checked_at  TEXT NOT NULL
    );
    """,
    # 12: office days per week stated by an ATS's structured data (0 = remote), read
    # from the JSON APIs of JavaScript-rendered boards (posting.api_posting).
    """
    ALTER TABLE jobs ADD COLUMN office_days INTEGER CHECK (office_days BETWEEN 0 AND 5);
    """,
    # 13: one row per daily run, for the digest's and review page's "Last run" line.
    # steps is a JSON list of [step, status]; finished_at stays NULL while it runs.
    """
    CREATE TABLE daily_runs (
        id          INTEGER PRIMARY KEY,
        started_at  TEXT NOT NULL,
        finished_at TEXT,
        steps       TEXT NOT NULL DEFAULT '[]'
    );
    """,
    # 14: the application tracker (agents/tracker/). One row per Gmail message it looked
    # at, so none is classified twice (per message and job since 15). Never the body or subject: the sender's domain,
    # the date, and Claude's one-line summary. outcome says what came of it.
    """
    CREATE TABLE tracker_messages (
        message_id    TEXT PRIMARY KEY,
        job_id        INTEGER REFERENCES jobs(id),
        msg_date      TEXT,
        sender_domain TEXT NOT NULL,
        match_score   INTEGER NOT NULL,
        category      TEXT CHECK (category IN ('confirmation', 'assessment', 'interview_invite', 'scheduling',
                                               'rejection', 'offer', 'recruiter_outreach', 'other')),
        confidence    INTEGER,
        summary       TEXT,
        deadline      TEXT,
        next_action   TEXT,
        company       TEXT,
        role          TEXT,
        outcome       TEXT NOT NULL CHECK (outcome IN ('unmatched', 'ignored', 'event', 'status_change',
                                                       'needs_review', 'offer', 'did_you_apply', 'outreach')),
        processed_at  TEXT NOT NULL
    );
    CREATE INDEX idx_tracker_messages_job ON tracker_messages(job_id, msg_date);
    CREATE TABLE tracker_runs (
        id       INTEGER PRIMARY KEY,
        ran_at   TEXT NOT NULL,
        since    TEXT NOT NULL,
        messages INTEGER NOT NULL DEFAULT 0
    );
    """,
    # 15: tracker messages are processed per (message, job), not per message, and an
    # unmatched message is only a header check: tracker_header_checks keeps the jobs
    # signature (the applied/interviewing job IDs) it was checked against, so it's checked
    # again, headers only, once that set changes. Old 'unmatched' rows become checks with
    # an empty signature (checked again on the next run). tracker_runs keeps the signature.
    """
    CREATE TABLE tracker_header_checks (
        message_id    TEXT PRIMARY KEY,
        jobs_sig      TEXT NOT NULL,
        sender_domain TEXT NOT NULL,
        msg_date      TEXT,
        best_job_id   INTEGER REFERENCES jobs(id),
        match_score   INTEGER NOT NULL,
        checked_at    TEXT NOT NULL
    );
    INSERT INTO tracker_header_checks (message_id, jobs_sig, sender_domain, msg_date, best_job_id, match_score,
                                       checked_at)
        SELECT message_id, '', sender_domain, msg_date, job_id, match_score, processed_at FROM tracker_messages;
    CREATE TABLE tracker_messages_new (
        id            INTEGER PRIMARY KEY,
        message_id    TEXT NOT NULL,
        job_id        INTEGER REFERENCES jobs(id),
        msg_date      TEXT,
        sender_domain TEXT NOT NULL,
        match_score   INTEGER NOT NULL,
        category      TEXT CHECK (category IN ('confirmation', 'assessment', 'interview_invite', 'scheduling',
                                               'rejection', 'offer', 'recruiter_outreach', 'other')),
        confidence    INTEGER,
        summary       TEXT,
        deadline      TEXT,
        next_action   TEXT,
        company       TEXT,
        role          TEXT,
        outcome       TEXT NOT NULL CHECK (outcome IN ('ignored', 'event', 'status_change', 'needs_review',
                                                       'offer', 'did_you_apply', 'outreach')),
        processed_at  TEXT NOT NULL
    );
    INSERT INTO tracker_messages_new (message_id, job_id, msg_date, sender_domain, match_score, category, confidence,
                                      summary, deadline, next_action, company, role, outcome, processed_at)
        SELECT message_id, job_id, msg_date, sender_domain, match_score, category, confidence, summary, deadline,
               next_action, company, role, outcome, processed_at
        FROM tracker_messages WHERE outcome != 'unmatched';
    DROP TABLE tracker_messages;
    ALTER TABLE tracker_messages_new RENAME TO tracker_messages;
    CREATE UNIQUE INDEX idx_tracker_messages_pair ON tracker_messages(message_id, IFNULL(job_id, 0));
    CREATE INDEX idx_tracker_messages_job ON tracker_messages(job_id, msg_date);
    ALTER TABLE tracker_runs ADD COLUMN jobs_sig TEXT;
    """,
    # 16: inputs for `.\run.ps1 stats`: each alert email's sender address; alert emails no
    # parser could read (kept out of processed_emails so a later run retries them); and
    # new postings the hard filters rejected at fetch (one row per posting, so a feed job
    # seen every day counts once).
    """
    ALTER TABLE processed_emails ADD COLUMN sender TEXT;
    CREATE TABLE unparsed_emails (
        message_id    TEXT PRIMARY KEY,
        sender        TEXT NOT NULL,
        first_seen_at TEXT NOT NULL,
        last_seen_at  TEXT NOT NULL
    );
    CREATE TABLE filtered_postings (
        posting_key   TEXT PRIMARY KEY,
        reason        TEXT NOT NULL,
        company       TEXT,
        title         TEXT,
        first_seen_at TEXT NOT NULL
    );
    """,
    # 17: the user can put a job on hold from the review app. A hold is not a status: a
    # held job keeps its status, is left out of the shortlist and "Still waiting", and
    # returns by itself once held_until (YYYY-MM-DD, local) arrives; NULL means until
    # unheld. hold_reason is set for every hold, so it alone says whether one was placed.
    """
    ALTER TABLE jobs ADD COLUMN held_until TEXT;
    ALTER TABLE jobs ADD COLUMN hold_reason TEXT;
    """,
    # 18: where posted_at came from (POSTED_AT_SOURCES), so a better date can replace a worse
    # one and the age check knows how far to trust it. Until now an email job's date was the
    # alert email's, and a feed job's the ATS's.
    """
    ALTER TABLE jobs ADD COLUMN posted_at_source TEXT;
    UPDATE jobs SET posted_at_source = 'email_date' WHERE posted_at IS NOT NULL AND source LIKE 'email:%';
    UPDATE jobs SET posted_at_source = 'ats' WHERE posted_at IS NOT NULL AND source NOT LIKE 'email:%';
    """,
]


class TransitionError(ValueError):
    """The requested status change is not an allowed transition."""


class ActorNotAllowed(Exception):
    """The actor may not make this status change."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect(db_path: Path | str | None = None) -> sqlite3.Connection:
    """Open a connection with foreign keys on and rows as sqlite3.Row, in WAL mode with a
    busy timeout, so the daily run and the review app can work at the same time."""
    conn = sqlite3.connect(Path(db_path) if db_path else DEFAULT_DB_PATH, timeout=BUSY_TIMEOUT_S)
    conn.row_factory = sqlite3.Row
    conn.execute(f"PRAGMA busy_timeout = {int(BUSY_TIMEOUT_S * 1000)}")
    conn.execute("PRAGMA journal_mode = WAL")  # stored in the file; a no-op once set
    conn.execute("PRAGMA synchronous = NORMAL")  # safe with WAL, and faster
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


@contextmanager
def _transaction(
    db_path: Path | str | None, conn: sqlite3.Connection | None
) -> Iterator[sqlite3.Connection]:
    """Use the caller's connection (caller owns the transaction), or open one and commit."""
    if conn is not None:
        yield conn
        return
    own = connect(db_path)
    try:
        with own:
            yield own
    finally:
        own.close()


def pending_migrations(db_path: Path | str | None = None) -> int:
    """How many migrations init_db() would apply (0 for a database that doesn't exist yet:
    there's nothing to back up)."""
    path = Path(db_path) if db_path else DEFAULT_DB_PATH
    if not path.exists():
        return 0
    conn = connect(path)
    try:
        return max(0, len(MIGRATIONS) - conn.execute("PRAGMA user_version").fetchone()[0])
    finally:
        conn.close()


def backup(db_path: Path | str | None = None, backup_dir: Path | None = None, keep: int = KEEP_BACKUPS,
           now: datetime | None = None) -> Path:
    """Copy the database (SQLite's backup API: consistent even while others write) to
    <backup_dir>/jobs-YYYYMMDD-HHMM.db (default data/backups/) and keep the newest `keep`."""
    path = Path(db_path) if db_path else DEFAULT_DB_PATH
    backup_dir = backup_dir or path.parent / BACKUP_DIR_NAME
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = (now or datetime.now()).strftime("%Y%m%d-%H%M")
    dest = backup_dir / f"jobs-{stamp}.db"
    n = 1
    while dest.exists():  # two backups in one minute
        dest = backup_dir / f"jobs-{stamp}-{n}.db"
        n += 1
    src, out = connect(path), sqlite3.connect(dest)
    try:
        src.backup(out)
    finally:
        out.close()
        src.close()
    old = sorted((p for p in backup_dir.iterdir() if _BACKUP_NAME.match(p.name)),
                 key=lambda p: (p.stat().st_mtime, p.name))
    for stale in old[:max(0, len(old) - keep)]:
        try:
            stale.unlink()
        except OSError as exc:
            log.warning("Could not delete old backup %s: %s", stale.name, exc)
    log.info("Database backed up to %s", dest)
    return dest


def init_db(db_path: Path | str | None = None) -> Path:
    """Create the database file if missing and apply any pending migrations."""
    path = Path(db_path) if db_path else DEFAULT_DB_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = connect(path)
    try:
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        if version < len(MIGRATIONS):
            # Table rebuilds drop and rename tables that others reference. Foreign
            # keys can only be toggled outside a transaction; checked again below.
            conn.execute("PRAGMA foreign_keys = OFF")
            for target, sql in enumerate(MIGRATIONS[version:], start=version + 1):
                # user_version can't take a bound parameter; target is an int we control.
                conn.executescript(f"BEGIN;\n{sql}\nPRAGMA user_version = {int(target)};\nCOMMIT;")
            conn.execute("PRAGMA foreign_keys = ON")
            problems = conn.execute("PRAGMA foreign_key_check").fetchall()
            if problems:
                raise RuntimeError(f"foreign key violations after migration: {[tuple(p) for p in problems]}")
    finally:
        conn.close()
    return path


def log_event(
    job_id: int | None,
    event: str,
    actor: str,
    *,
    from_status: str | None = None,
    to_status: str | None = None,
    detail: str | None = None,
    db_path: Path | str | None = None,
    conn: sqlite3.Connection | None = None,
) -> int:
    """Append a row to the events table and return its id."""
    _check_actor(actor)
    with _transaction(db_path, conn) as c:
        cur = c.execute(
            "INSERT INTO events (job_id, at, actor, event, from_status, to_status, detail)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (job_id, _now(), actor, event, from_status, to_status, detail),
        )
        return cur.lastrowid


def upsert_job(
    job: dict[str, Any],
    actor: str = "scout",
    *,
    db_path: Path | str | None = None,
    conn: sqlite3.Connection | None = None,
) -> tuple[int, bool]:
    """Insert a new job as `new`, or bump last_seen_at if (source, external_id) exists.

    Never touches status or score of an existing job. Returns (job_id, inserted).
    """
    _check_actor(actor)
    missing = [k for k in _REQUIRED_JOB_FIELDS if not job.get(k)]
    if missing:
        raise ValueError(f"job is missing required fields: {', '.join(missing)}")
    unknown = set(job) - set(_REQUIRED_JOB_FIELDS) - set(_OPTIONAL_JOB_FIELDS)
    if unknown:
        raise ValueError(f"job has unknown fields: {', '.join(sorted(unknown))}")
    if not _valid_source(job["source"]):
        raise ValueError(f"unknown source {job['source']!r}; expected one of {SOURCES} or {EMAIL_SOURCE_PREFIX}<type>")
    if job.get("preferred_tier") is not None and job["preferred_tier"] not in TIERS:
        raise ValueError(f"unknown preferred_tier {job['preferred_tier']!r}; expected one of {TIERS}")
    date_source = _posted_at_source(job.get("posted_at"), job.get("posted_at_source"),
                                    "ats" if job["source"] in SOURCES else "email_date")

    now = _now()
    with _transaction(db_path, conn) as c:
        cur = c.execute(
            "INSERT INTO jobs (source, external_id, company, title, location, url,"
            " description, posted_at, posted_at_source, remote, salary_min, salary_max, salary_currency,"
            " salary_is_predicted, needs_details, dedupe_key, preferred_match, preferred_tier,"
            " sponsored, source_trust, status, first_seen_at, last_seen_at, updated_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'new', ?, ?, ?)"
            " ON CONFLICT (source, external_id) DO NOTHING",
            (
                job["source"],
                str(job["external_id"]),
                job["company"],
                job["title"],
                job.get("location"),
                job.get("url"),
                job.get("description"),
                job.get("posted_at"),
                date_source,
                job.get("remote"),
                job.get("salary_min"),
                job.get("salary_max"),
                job.get("salary_currency"),
                int(bool(job.get("salary_is_predicted"))),
                int(bool(job.get("needs_details"))),
                job.get("dedupe_key"),
                job.get("preferred_match"),
                job.get("preferred_tier"),
                int(bool(job.get("sponsored"))),
                "official" if job["source"] in SOURCES else None,  # feeds are the company's own ATS
                now,
                now,
                now,
            ),
        )
        if cur.rowcount == 1:
            job_id = cur.lastrowid
            log_event(job_id, "inserted", actor, to_status="new", conn=c)
            return job_id, True

        c.execute(
            "UPDATE jobs SET last_seen_at = ? WHERE source = ? AND external_id = ?",
            (now, job["source"], str(job["external_id"])),
        )
        row = c.execute(
            "SELECT id FROM jobs WHERE source = ? AND external_id = ?",
            (job["source"], str(job["external_id"])),
        ).fetchone()
        offer_posted_at(row["id"], job.get("posted_at"), date_source, conn=c)
        return row["id"], False


def _posted_at_source(posted_at: str | None, source: str | None, default: str) -> str | None:
    if not posted_at:
        return None
    source = source or default
    if source not in POSTED_AT_SOURCES:
        raise ValueError(f"unknown posted_at_source {source!r}; expected one of {POSTED_AT_SOURCES}")
    return source


def better_date_source(new: str, old: str | None) -> bool:
    """Whether a date from `new` may replace one from `old` (None: no date stored). A
    posting's date replaces an equally good one (it's newer); alert dates never do."""
    rank, old_rank = _DATE_RANK[new], _DATE_RANK.get(old or "", 0)
    return rank > old_rank or rank == old_rank == _DATE_RANK["ats"]


def offer_posted_at(
    job_id: int,
    posted_at: str | None,
    source: str | None,
    *,
    db_path: Path | str | None = None,
    conn: sqlite3.Connection | None = None,
) -> bool:
    """Store posted_at (YYYY-MM-DD) from `source` unless the stored date's source is better
    (better_date_source). Returns whether it was stored."""
    source = _posted_at_source(posted_at, source, "posting_page")
    if source is None:
        return False
    with _transaction(db_path, conn) as c:
        row = c.execute("SELECT posted_at, posted_at_source FROM jobs WHERE id = ?", (job_id,)).fetchone()
        if row is None:
            raise LookupError(f"no job with id {job_id}")
        old = row["posted_at_source"] if row["posted_at"] else None
        if not better_date_source(source, old) or (row["posted_at"], old) == (posted_at[:10], source):
            return False
        c.execute("UPDATE jobs SET posted_at = ?, posted_at_source = ? WHERE id = ?", (posted_at[:10], source, job_id))
        return True


def touch_job(
    source: str,
    external_id: str,
    *,
    posted_at: str | None = None,
    posted_at_source: str | None = None,
    db_path: Path | str | None = None,
    conn: sqlite3.Connection | None = None,
) -> bool:
    """Set last_seen_at to now if the job exists, and offer this sighting's posted_at (a
    later alert may carry an age badge). Returns whether it existed."""
    with _transaction(db_path, conn) as c:
        cur = c.execute(
            "UPDATE jobs SET last_seen_at = ? WHERE source = ? AND external_id = ?",
            (_now(), source, str(external_id)),
        )
        if cur.rowcount != 1:
            return False
        if posted_at:
            row = c.execute("SELECT id FROM jobs WHERE source = ? AND external_id = ?",
                            (source, str(external_id))).fetchone()
            offer_posted_at(row["id"], posted_at,
                            posted_at_source or ("ats" if source in SOURCES else "email_date"), conn=c)
        return True


def update_preferred(
    source: str,
    external_id: str,
    preferred_match: str | None,
    preferred_tier: str | None,
    *,
    db_path: Path | str | None = None,
    conn: sqlite3.Connection | None = None,
) -> None:
    """Refresh the preferred-company match, e.g. after preferred_companies.yaml changes."""
    if preferred_tier is not None and preferred_tier not in TIERS:
        raise ValueError(f"unknown preferred_tier {preferred_tier!r}; expected one of {TIERS}")
    with _transaction(db_path, conn) as c:
        c.execute(
            "UPDATE jobs SET preferred_match = ?, preferred_tier = ? WHERE source = ? AND external_id = ?",
            (preferred_match, preferred_tier, source, str(external_id)),
        )


def find_open_job_by_dedupe_key(
    key: str,
    *,
    db_path: Path | str | None = None,
    conn: sqlite3.Connection | None = None,
) -> dict[str, Any] | None:
    """A job that isn't closed with this dedupe_key, if any."""
    with _transaction(db_path, conn) as c:
        row = c.execute(
            "SELECT * FROM jobs WHERE dedupe_key = ? AND status != 'closed' ORDER BY id LIMIT 1",
            (key,),
        ).fetchone()
    return dict(row) if row else None


def is_email_processed(
    message_id: str,
    *,
    db_path: Path | str | None = None,
    conn: sqlite3.Connection | None = None,
) -> bool:
    with _transaction(db_path, conn) as c:
        row = c.execute("SELECT 1 FROM processed_emails WHERE message_id = ?", (message_id,)).fetchone()
    return row is not None


def record_processed_email(
    message_id: str,
    source: str | None,
    jobs_found: int,
    *,
    sender: str | None = None,
    db_path: Path | str | None = None,
    conn: sqlite3.Connection | None = None,
) -> None:
    """Mark an email as handled so later runs skip it. `source` is None for a skipped
    non-job email from a sender with no parser."""
    with _transaction(db_path, conn) as c:
        c.execute(
            "INSERT INTO processed_emails (message_id, processed_at, source, jobs_found, sender) VALUES (?, ?, ?, ?, ?)",
            (message_id, _now(), source, int(jobs_found), sender),
        )


def record_unparsed_email(
    message_id: str,
    sender: str,
    *,
    db_path: Path | str | None = None,
    conn: sqlite3.Connection | None = None,
) -> None:
    """An alert email no parser could read (it stays unprocessed, so it's retried)."""
    now = _now()
    with _transaction(db_path, conn) as c:
        c.execute(
            "INSERT INTO unparsed_emails (message_id, sender, first_seen_at, last_seen_at) VALUES (?, ?, ?, ?)"
            " ON CONFLICT (message_id) DO UPDATE SET last_seen_at = excluded.last_seen_at",
            (message_id, sender, now, now),
        )


def record_filtered_posting(
    posting_key: str,
    reason: str,
    company: str | None,
    title: str | None,
    *,
    db_path: Path | str | None = None,
    conn: sqlite3.Connection | None = None,
) -> None:
    """A new posting the hard filters rejected; the first rejection is kept."""
    with _transaction(db_path, conn) as c:
        c.execute(
            "INSERT OR IGNORE INTO filtered_postings (posting_key, reason, company, title, first_seen_at)"
            " VALUES (?, ?, ?, ?, ?)",
            (posting_key, reason, company, title, _now()),
        )


def raise_notice(
    key: str,
    message: str,
    *,
    db_path: Path | str | None = None,
    conn: sqlite3.Connection | None = None,
) -> None:
    """Make a notice active. raised_at keeps the time it first became active."""
    now = _now()
    with _transaction(db_path, conn) as c:
        c.execute(
            "INSERT INTO notices (key, message, raised_at) VALUES (?, ?, ?)"
            " ON CONFLICT (key) DO UPDATE SET message = excluded.message,"
            " raised_at = CASE WHEN notices.cleared_at IS NULL THEN notices.raised_at ELSE excluded.raised_at END,"
            " cleared_at = NULL",
            (key, message, now),
        )


def clear_notice(
    key: str,
    *,
    db_path: Path | str | None = None,
    conn: sqlite3.Connection | None = None,
) -> None:
    with _transaction(db_path, conn) as c:
        c.execute("UPDATE notices SET cleared_at = ? WHERE key = ? AND cleared_at IS NULL", (_now(), key))


def get_active_notices(
    *,
    db_path: Path | str | None = None,
    conn: sqlite3.Connection | None = None,
) -> list[dict[str, Any]]:
    """Notices the digest must show at the top, oldest first."""
    with _transaction(db_path, conn) as c:
        rows = c.execute("SELECT * FROM notices WHERE cleared_at IS NULL ORDER BY raised_at, key").fetchall()
    return [dict(r) for r in rows]


def start_daily_run(
    *,
    db_path: Path | str | None = None,
    conn: sqlite3.Connection | None = None,
) -> int:
    """Record that a daily run started; returns its id for finish_daily_run()."""
    with _transaction(db_path, conn) as c:
        return c.execute("INSERT INTO daily_runs (started_at) VALUES (?)", (_now(),)).lastrowid


def finish_daily_run(
    run_id: int,
    steps: list[tuple[str, str]],
    *,
    finished: bool = True,
    db_path: Path | str | None = None,
    conn: sqlite3.Connection | None = None,
) -> None:
    """Store the steps' statuses so far, and the finish time when finished."""
    with _transaction(db_path, conn) as c:
        c.execute("UPDATE daily_runs SET steps = ?, finished_at = COALESCE(?, finished_at) WHERE id = ?",
                  (json.dumps([list(s) for s in steps]), _now() if finished else None, run_id))


def last_daily_run(
    *,
    db_path: Path | str | None = None,
    conn: sqlite3.Connection | None = None,
) -> dict[str, Any] | None:
    """The newest daily run, with steps as a list of (step, status), or None."""
    with _transaction(db_path, conn) as c:
        row = c.execute("SELECT * FROM daily_runs ORDER BY started_at DESC, id DESC LIMIT 1").fetchone()
    if row is None:
        return None
    run = dict(row)
    run["steps"] = [tuple(s) for s in json.loads(run["steps"])]
    return run


TRACKER_MESSAGE_FIELDS = ("message_id", "job_id", "msg_date", "sender_domain", "match_score", "category",
                          "confidence", "summary", "deadline", "next_action", "company", "role", "outcome")


def tracker_header_checks(
    message_ids: list[str],
    *,
    db_path: Path | str | None = None,
    conn: sqlite3.Connection | None = None,
) -> dict[str, str]:
    """{message_id: jobs signature it was last checked against} for these messages."""
    with _transaction(db_path, conn) as c:
        return {r["message_id"]: r["jobs_sig"] for r in c.execute(
            "SELECT message_id, jobs_sig FROM tracker_header_checks WHERE message_id IN (SELECT value FROM json_each(?))",
            (json.dumps(list(message_ids)),))}


def record_header_check(
    message_id: str,
    jobs_sig: str,
    sender_domain: str,
    msg_date: str | None,
    best_job_id: int | None,
    match_score: int,
    *,
    db_path: Path | str | None = None,
    conn: sqlite3.Connection | None = None,
) -> None:
    """A message's headers were checked against this set of applied/interviewing jobs."""
    with _transaction(db_path, conn) as c:
        c.execute(
            "INSERT INTO tracker_header_checks (message_id, jobs_sig, sender_domain, msg_date, best_job_id,"
            " match_score, checked_at) VALUES (?, ?, ?, ?, ?, ?, ?)"
            " ON CONFLICT (message_id) DO UPDATE SET jobs_sig = excluded.jobs_sig,"
            " best_job_id = excluded.best_job_id, match_score = excluded.match_score, checked_at = excluded.checked_at",
            (message_id, jobs_sig, sender_domain, msg_date, best_job_id, match_score, _now()),
        )


def tracker_pairs(
    message_ids: list[str],
    *,
    db_path: Path | str | None = None,
    conn: sqlite3.Connection | None = None,
) -> set[tuple[str, int | None]]:
    """(message_id, job_id) pairs already processed; job_id None for a message about no job."""
    with _transaction(db_path, conn) as c:
        return {(r["message_id"], r["job_id"]) for r in c.execute(
            "SELECT message_id, job_id FROM tracker_messages WHERE message_id IN (SELECT value FROM json_each(?))",
            (json.dumps(list(message_ids)),))}


def record_tracker_message(
    message: dict[str, Any],
    *,
    db_path: Path | str | None = None,
    conn: sqlite3.Connection | None = None,
) -> None:
    """Store what the tracker made of one message (TRACKER_MESSAGE_FIELDS; never its body)."""
    values = [message.get(k) for k in TRACKER_MESSAGE_FIELDS]
    with _transaction(db_path, conn) as c:
        c.execute(
            "INSERT INTO tracker_messages (message_id, job_id, msg_date, sender_domain, match_score, category,"
            " confidence, summary, deadline, next_action, company, role, outcome, processed_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (*values, _now()),
        )


def get_tracker_messages(
    *,
    job_id: int | None = None,
    db_path: Path | str | None = None,
    conn: sqlite3.Connection | None = None,
) -> list[dict[str, Any]]:
    """Messages the tracker matched or classified, oldest first; only one job's when
    job_id is given. Unmatched messages are only header checks, never here."""
    with _transaction(db_path, conn) as c:
        rows = c.execute(
            "SELECT * FROM tracker_messages WHERE (? IS NULL OR job_id = ?)"
            " ORDER BY msg_date, message_id",
            (job_id, job_id),
        ).fetchall()
    return [dict(r) for r in rows]


def last_tracker_run(
    *,
    db_path: Path | str | None = None,
    conn: sqlite3.Connection | None = None,
) -> dict[str, Any] | None:
    with _transaction(db_path, conn) as c:
        row = c.execute("SELECT * FROM tracker_runs ORDER BY ran_at DESC, id DESC LIMIT 1").fetchone()
    return dict(row) if row else None


def record_tracker_run(
    ran_at: str,
    since: str,
    messages: int,
    jobs_sig: str | None = None,
    *,
    db_path: Path | str | None = None,
    conn: sqlite3.Connection | None = None,
) -> None:
    with _transaction(db_path, conn) as c:
        c.execute("INSERT INTO tracker_runs (ran_at, since, messages, jobs_sig) VALUES (?, ?, ?, ?)",
                  (ran_at, since, messages, jobs_sig))


def get_job_events(
    job_id: int,
    *,
    db_path: Path | str | None = None,
    conn: sqlite3.Connection | None = None,
) -> list[dict[str, Any]]:
    """A job's events, oldest first."""
    with _transaction(db_path, conn) as c:
        rows = c.execute("SELECT * FROM events WHERE job_id = ? ORDER BY at, id", (job_id,)).fetchall()
    return [dict(r) for r in rows]


def get_applications(
    *,
    db_path: Path | str | None = None,
    conn: sqlite3.Connection | None = None,
) -> list[dict[str, Any]]:
    """Every job the user applied to (an applications row, latest one per job) or that is
    applied/interviewing, with applied_at, newest application first."""
    with _transaction(db_path, conn) as c:
        rows = c.execute(
            "SELECT j.*, (SELECT MAX(a.applied_at) FROM applications a WHERE a.job_id = j.id) AS applied_at"
            " FROM jobs j WHERE j.status IN ('applied', 'interviewing')"
            " OR EXISTS (SELECT 1 FROM applications a WHERE a.job_id = j.id)"
            " ORDER BY applied_at DESC, j.id DESC"
        ).fetchall()
    return [dict(r) for r in rows]


def get_unseen_jobs(
    before: str,
    statuses: tuple[str, ...],
    *,
    db_path: Path | str | None = None,
    conn: sqlite3.Connection | None = None,
) -> list[dict[str, Any]]:
    """Jobs in one of `statuses` whose last_seen_at is earlier than `before` (ISO timestamp)."""
    unknown = set(statuses) - set(STATUSES)
    if unknown:
        raise ValueError(f"unknown statuses {sorted(unknown)}")
    with _transaction(db_path, conn) as c:
        rows = c.execute(
            "SELECT * FROM jobs WHERE last_seen_at < ?"
            " AND status IN (SELECT value FROM json_each(?)) ORDER BY id",
            (before, json.dumps(list(statuses))),
        ).fetchall()
    return [dict(r) for r in rows]


def set_status(
    job_id: int,
    new_status: str,
    actor: str,
    detail: str | None = None,
    *,
    db_path: Path | str | None = None,
    conn: sqlite3.Connection | None = None,
) -> str:
    """Move a job to new_status if the transition and actor are allowed. Returns the old status.

    Allowed: one step forward along the lifecycle, or any non-closed status to closed.
    Only actor "user" may set `applied`, from any status in USER_APPLY_FROM. The only way out of closed is closed -> new,
    by actor "scout", for a job whose latest close was a filter rejection (a detail
    starting with FILTERED_DETAIL_PREFIX); it's logged. scored -> new, by actor "scout",
    resets a job for rescoring and clears its score. shortlisted or researched -> scored is
    allowed only for actor "scout" with a detail in MOVE_BACK_DETAILS (its posting looks
    closed, or no official posting is known); the score stays. Actor "tracker" may only
    make the moves in TRACKER_MOVES. Only actor "user" may reopen: closed -> scored (the
    score stays), or closed -> applied for a job with a tracker "Did you apply?" item
    (mark_applied). Every change is logged to events.
    """
    _check_actor(actor)
    if new_status not in STATUSES:
        raise ValueError(f"unknown status {new_status!r}")
    if new_status == "applied" and actor != "user":
        raise ActorNotAllowed(f"only the user may set a job to applied (actor was {actor!r})")

    with _transaction(db_path, conn) as c:
        row = c.execute("SELECT status FROM jobs WHERE id = ?", (job_id,)).fetchone()
        if row is None:
            raise LookupError(f"no job with id {job_id}")
        old_status = row["status"]
        if actor == "tracker":
            if (old_status, new_status) not in TRACKER_MOVES:
                raise ActorNotAllowed(f"tracker may not move job {job_id} {old_status} -> {new_status}")
            required = TRACKER_MOVES[(old_status, new_status)]
            if required is not None and detail != required:
                raise ActorNotAllowed(f"tracker may close job {job_id} only with detail {required!r}")
        if old_status == "closed" and new_status == "scored":
            # The user takes back a close (feedback.reopen); the score stays.
            if actor != "user":
                raise ActorNotAllowed(f"only the user may reopen a closed job (actor was {actor!r})")
            log.info("Reopening job %d: closed -> scored by the user (%s)", job_id, detail)
        elif old_status == "closed" and new_status == "applied":
            # applied is user-only (checked above); from closed only when the tracker saw an
            # application email for this job and asked "Did you apply?".
            if not _has_did_you_apply(c, job_id):
                raise TransitionError(f"job {job_id}: closed -> applied is allowed only for a job with a "
                                      "tracker \"Did you apply?\" item")
            log.info("Job %d: closed -> applied by the user, after a \"Did you apply?\" item (%s)", job_id, detail)
        elif new_status == "applied" and old_status in USER_APPLY_FROM:
            pass  # the user applied (actor checked above), whether or not a brief was written
        elif old_status == "closed" and new_status == "new":
            if actor != "scout":
                raise ActorNotAllowed(f"only scout may reopen a filtered job (actor was {actor!r})")
            if not _closed_by_filter(c, job_id):
                raise TransitionError(f"job {job_id}: closed -> new is allowed only for jobs closed by the filters")
            log.info("Reopening job %d: closed by the filters, now passes (%s)", job_id, detail)
        elif old_status == "scored" and new_status == "new":
            # Rescore after a prompt or weight change. The old score goes with it.
            if actor != "scout":
                raise ActorNotAllowed(f"only scout may reset a scored job for rescoring (actor was {actor!r})")
            c.execute("UPDATE jobs SET score = NULL, score_detail = NULL WHERE id = ?", (job_id,))
        elif old_status in ("shortlisted", "researched") and new_status == "scored":
            if actor != "scout":
                raise ActorNotAllowed(f"only scout may move a {old_status} job back to scored (actor was {actor!r})")
            if detail not in MOVE_BACK_DETAILS:
                raise TransitionError(f"job {job_id}: {old_status} -> scored is allowed only with detail "
                                      f"{' or '.join(map(repr, MOVE_BACK_DETAILS))}")
        elif not _is_allowed(old_status, new_status):
            raise TransitionError(f"job {job_id}: {old_status} -> {new_status} is not allowed")

        # Guard on the old status so a concurrent change can't be silently overwritten.
        cur = c.execute(
            "UPDATE jobs SET status = ?, updated_at = ? WHERE id = ? AND status = ?",
            (new_status, _now(), job_id, old_status),
        )
        if cur.rowcount != 1:
            raise TransitionError(f"job {job_id}: status changed concurrently, try again")
        log_event(
            job_id,
            "status_change",
            actor,
            from_status=old_status,
            to_status=new_status,
            detail=detail,
            conn=c,
        )
        return old_status


def set_score(
    job_id: int,
    score: float,
    detail: dict[str, Any] | None = None,
    *,
    db_path: Path | str | None = None,
    conn: sqlite3.Connection | None = None,
) -> None:
    """Store a job's final score (0-100) and how it was computed. Status changes go
    through set_status()."""
    if not 0 <= score <= 100:
        raise ValueError(f"score {score!r} is outside 0-100")
    detail_json = None if detail is None else json.dumps(detail, ensure_ascii=False)
    with _transaction(db_path, conn) as c:
        cur = c.execute(
            "UPDATE jobs SET score = ?, score_detail = ?, updated_at = ? WHERE id = ?",
            (score, detail_json, _now(), job_id),
        )
        if cur.rowcount != 1:
            raise LookupError(f"no job with id {job_id}")


def set_dedupe_key(
    job_id: int,
    key: str,
    *,
    db_path: Path | str | None = None,
    conn: sqlite3.Connection | None = None,
) -> None:
    """Store a recomputed dedupe_key, e.g. after the company or title normalization changed."""
    with _transaction(db_path, conn) as c:
        c.execute("UPDATE jobs SET dedupe_key = ? WHERE id = ?", (key, job_id))


def get_filtered_closed_jobs(
    *,
    db_path: Path | str | None = None,
    conn: sqlite3.Connection | None = None,
) -> list[dict[str, Any]]:
    """Closed jobs whose latest close was a filter rejection, each with `closed_detail`."""
    with _transaction(db_path, conn) as c:
        rows = c.execute(
            "SELECT * FROM (SELECT j.*, (SELECT e.detail FROM events e"
            "   WHERE e.job_id = j.id AND e.event = 'status_change' AND e.to_status = 'closed'"
            "   ORDER BY e.id DESC LIMIT 1) AS closed_detail"
            " FROM jobs j WHERE j.status = 'closed')"
            " WHERE substr(closed_detail, 1, ?) = ? ORDER BY id",
            (len(FILTERED_DETAIL_PREFIX), FILTERED_DETAIL_PREFIX),
        ).fetchall()
    return [dict(r) for r in rows]


def latest_closes(
    prefix: str,
    *,
    db_path: Path | str | None = None,
    conn: sqlite3.Connection | None = None,
) -> dict[int, tuple[str, str]]:
    """{job_id: (at, detail)} of each job's latest close whose detail starts with prefix."""
    with _transaction(db_path, conn) as c:
        rows = c.execute(
            "SELECT e.job_id, e.at, e.detail FROM events e WHERE e.event = 'status_change' AND e.to_status = 'closed'"
            " AND e.id = (SELECT MAX(id) FROM events x WHERE x.job_id = e.job_id AND x.event = 'status_change'"
            "             AND x.to_status = 'closed')"
            " AND substr(e.detail, 1, ?) = ?",
            (len(prefix), prefix),
        ).fetchall()
    return {r["job_id"]: (r["at"], r["detail"]) for r in rows}


def get_jobs(
    status: str | None = None,
    limit: int | None = None,
    *,
    db_path: Path | str | None = None,
    conn: sqlite3.Connection | None = None,
) -> list[dict[str, Any]]:
    """Return jobs as dicts, highest score first, optionally filtered by status."""
    if status is not None and status not in STATUSES:
        raise ValueError(f"unknown status {status!r}")
    # LIMIT -1 means no limit in SQLite.
    params = (status, status, -1 if limit is None else int(limit))
    with _transaction(db_path, conn) as c:
        rows = c.execute(
            "SELECT * FROM jobs WHERE (? IS NULL OR status = ?)"
            " ORDER BY score DESC NULLS LAST, id LIMIT ?",
            params,
        ).fetchall()
    return [dict(r) for r in rows]


def get_job(
    job_id: int,
    *,
    db_path: Path | str | None = None,
    conn: sqlite3.Connection | None = None,
) -> dict[str, Any] | None:
    with _transaction(db_path, conn) as c:
        row = c.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
    return dict(row) if row else None


def store_details(
    job_id: int,
    *,
    description: str,
    apply_url: str | None,
    source_url: str | None,
    years_required: int | None,
    source_trust: str,
    salary: tuple[int | None, int | None, str | None] | None = None,
    location: str | None = None,
    posted_at: str | None = None,
    posted_at_source: str | None = None,
    office_days: int | None = None,
    db_path: Path | str | None = None,
    conn: sqlite3.Connection | None = None,
) -> None:
    """Store the posting found by the details fetch and clear needs_details. source_trust
    is 'official' or 'aggregator'. A stated salary replaces any stored one (and is not an
    estimate); the caller passes no salary from an aggregator. location and office_days
    come from an ATS's structured data and replace the stored values when given; posted_at
    (from posted_at_source, default posting_page) replaces the stored date only when that
    came from no better source (offer_posted_at)."""
    if source_trust not in ("official", "aggregator"):
        raise ValueError(f"source_trust must be official or aggregator, not {source_trust!r}")
    now = _now()
    with _transaction(db_path, conn) as c:
        cur = c.execute(
            "UPDATE jobs SET description = ?, apply_url = ?, source_url = ?, years_required = ?,"
            " source_trust = ?, needs_details = 0, details_status = 'found', details_fetched_at = ?,"
            " details_attempts = details_attempts + 1, updated_at = ? WHERE id = ?",
            (description, apply_url, source_url, years_required, source_trust, now, now, job_id),
        )
        if cur.rowcount != 1:
            raise LookupError(f"no job with id {job_id}")
        if salary and (salary[0] or salary[1]):
            c.execute(
                "UPDATE jobs SET salary_min = ?, salary_max = ?, salary_currency = ?, salary_is_predicted = 0"
                " WHERE id = ?",
                (salary[0], salary[1], salary[2], job_id),
            )
        c.execute(
            "UPDATE jobs SET location = COALESCE(?, location), office_days = COALESCE(?, office_days) WHERE id = ?",
            (location, office_days, job_id),
        )
        offer_posted_at(job_id, posted_at, posted_at_source, conn=c)


def set_source_trust(
    job_id: int,
    source_trust: str,
    apply_url: str | None,
    detail: str,
    *,
    db_path: Path | str | None = None,
    conn: sqlite3.Connection | None = None,
) -> None:
    """A found posting's trust re-derived from its links (retrust), with the apply link to
    show; logs a trust_changed event (actor scout)."""
    if source_trust not in ("official", "aggregator"):
        raise ValueError(f"source_trust must be official or aggregator, not {source_trust!r}")
    with _transaction(db_path, conn) as c:
        cur = c.execute(
            "UPDATE jobs SET source_trust = ?, apply_url = ?, updated_at = ? WHERE id = ?",
            (source_trust, apply_url, _now(), job_id),
        )
        if cur.rowcount != 1:
            raise LookupError(f"no job with id {job_id}")
        log_event(job_id, "trust_changed", "scout", detail=detail, conn=c)


def set_details_not_found(
    job_id: int,
    *,
    db_path: Path | str | None = None,
    conn: sqlite3.Connection | None = None,
) -> None:
    """The lookup found no posting. Counts the attempt; details.py retries once, a day later."""
    now = _now()
    with _transaction(db_path, conn) as c:
        c.execute(
            "UPDATE jobs SET details_status = 'not_found', source_trust = 'not_found', details_fetched_at = ?,"
            " details_attempts = details_attempts + 1, updated_at = ? WHERE id = ?",
            (now, now, job_id),
        )


def set_user_rating(
    job_id: int,
    rating: int,
    *,
    db_path: Path | str | None = None,
    conn: sqlite3.Connection | None = None,
) -> None:
    """The user's thumbs up (1) or down (-1) on a job."""
    if rating not in (-1, 1):
        raise ValueError(f"rating must be 1 or -1, not {rating!r}")
    with _transaction(db_path, conn) as c:
        cur = c.execute("UPDATE jobs SET user_rating = ?, updated_at = ? WHERE id = ?", (rating, _now(), job_id))
        if cur.rowcount != 1:
            raise LookupError(f"no job with id {job_id}")
        log_event(job_id, "rated", "user", detail="up" if rating == 1 else "down", conn=c)


HOLDABLE = ("new", "scored", "shortlisted", "researched")


def set_hold(
    job_id: int,
    reason: str,
    held_until: str | None = None,
    *,
    db_path: Path | str | None = None,
    conn: sqlite3.Connection | None = None,
) -> None:
    """The user puts a job on hold until held_until (YYYY-MM-DD; None: until unheld).
    Only jobs not yet applied to; the status stays. Logged as a `held` event."""
    reason = (reason or "").strip()
    if not reason:
        raise ValueError("a reason is required")
    if held_until is not None:
        held_until = date.fromisoformat(held_until).isoformat()  # ValueError on a bad date
    with _transaction(db_path, conn) as c:
        row = c.execute("SELECT status FROM jobs WHERE id = ?", (job_id,)).fetchone()
        if row is None:
            raise LookupError(f"no job with id {job_id}")
        if row["status"] not in HOLDABLE:
            raise TransitionError(f"job {job_id} is {row['status']}; only {', '.join(HOLDABLE)} jobs can be held")
        c.execute("UPDATE jobs SET held_until = ?, hold_reason = ?, updated_at = ? WHERE id = ?",
                  (held_until, reason, _now(), job_id))
        log_event(job_id, "held", "user", detail=f"until {held_until or 'unheld'}: {reason}", conn=c)


def clear_hold(
    job_id: int,
    *,
    db_path: Path | str | None = None,
    conn: sqlite3.Connection | None = None,
) -> None:
    """The user takes a job off hold. Logged as an `unheld` event."""
    with _transaction(db_path, conn) as c:
        cur = c.execute("UPDATE jobs SET held_until = NULL, hold_reason = NULL, updated_at = ? WHERE id = ?",
                        (_now(), job_id))
        if cur.rowcount != 1:
            raise LookupError(f"no job with id {job_id}")
        log_event(job_id, "unheld", "user", conn=c)


def is_held(job: dict[str, Any], today: date | None = None) -> bool:
    """On hold today: a hold was placed and its held_until (if any) hasn't arrived yet."""
    if not job.get("hold_reason"):
        return False
    until = job.get("held_until")
    return until is None or date.fromisoformat(until) > (today or date.today())


def get_company_research(
    company_key: str,
    *,
    db_path: Path | str | None = None,
    conn: sqlite3.Connection | None = None,
) -> dict[str, Any] | None:
    """The newest cached research for a company (sources decoded from JSON), or None."""
    with _transaction(db_path, conn) as c:
        row = c.execute(
            "SELECT * FROM company_research WHERE company_key = ? ORDER BY created_at DESC, id DESC LIMIT 1",
            (company_key,),
        ).fetchone()
    if row is None:
        return None
    out = dict(row)
    out["sources"] = json.loads(out["sources"])
    return out


def add_company_research(
    company_key: str,
    company: str,
    markdown: str,
    sources: list[dict[str, Any]],
    *,
    db_path: Path | str | None = None,
    conn: sqlite3.Connection | None = None,
) -> int:
    with _transaction(db_path, conn) as c:
        cur = c.execute(
            "INSERT INTO company_research (company_key, company, markdown, sources, created_at)"
            " VALUES (?, ?, ?, ?, ?)",
            (company_key, company, markdown, json.dumps(sources, ensure_ascii=False), _now()),
        )
    return int(cur.lastrowid)


def get_company_board(
    company_key: str,
    *,
    db_path: Path | str | None = None,
    conn: sqlite3.Connection | None = None,
) -> dict[str, Any] | None:
    with _transaction(db_path, conn) as c:
        row = c.execute("SELECT * FROM company_boards WHERE company_key = ?", (company_key,)).fetchone()
    return dict(row) if row else None


def set_company_board(
    company_key: str,
    company: str,
    board_url: str,
    source: str,
    *,
    db_path: Path | str | None = None,
    conn: sqlite3.Connection | None = None,
) -> None:
    """Cache a company's board. A board the user gave is never replaced by a lookup's."""
    with _transaction(db_path, conn) as c:
        if source == "lookup":
            row = c.execute("SELECT source FROM company_boards WHERE company_key = ?", (company_key,)).fetchone()
            if row and row["source"] == "user":
                return
        c.execute(
            "INSERT INTO company_boards (company_key, company, board_url, source, updated_at) VALUES (?, ?, ?, ?, ?)"
            " ON CONFLICT(company_key) DO UPDATE SET company = excluded.company, board_url = excluded.board_url,"
            " source = excluded.source, updated_at = excluded.updated_at",
            (company_key, company, board_url, source, _now()),
        )


def get_ats_probe(
    company_key: str,
    *,
    db_path: Path | str | None = None,
    conn: sqlite3.Connection | None = None,
) -> dict[str, Any] | None:
    with _transaction(db_path, conn) as c:
        row = c.execute("SELECT * FROM ats_probes WHERE company_key = ?", (company_key,)).fetchone()
    if row is None:
        return None
    out = dict(row)
    out["tried"] = json.loads(out["tried"])
    return out


def set_ats_probe(
    company_key: str,
    company: str,
    result: str,
    tried: list[str],
    ats: str | None = None,
    slug: str | None = None,
    *,
    db_path: Path | str | None = None,
    conn: sqlite3.Connection | None = None,
) -> None:
    with _transaction(db_path, conn) as c:
        c.execute(
            "INSERT INTO ats_probes (company_key, company, result, ats, slug, tried, checked_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?) ON CONFLICT(company_key) DO UPDATE SET company = excluded.company,"
            " result = excluded.result, ats = excluded.ats, slug = excluded.slug, tried = excluded.tried,"
            " checked_at = excluded.checked_at",
            (company_key, company, result, ats, slug, json.dumps(tried), _now()),
        )


def set_brief_path(
    job_id: int,
    path: str,
    *,
    db_path: Path | str | None = None,
    conn: sqlite3.Connection | None = None,
) -> None:
    with _transaction(db_path, conn) as c:
        cur = c.execute("UPDATE jobs SET brief_path = ?, updated_at = ? WHERE id = ?", (path, _now(), job_id))
        if cur.rowcount != 1:
            raise LookupError(f"no job with id {job_id}")


def shortlisted_at(
    *,
    db_path: Path | str | None = None,
    conn: sqlite3.Connection | None = None,
) -> dict[int, str]:
    """{job_id: when it was last shortlisted} for every job ever shortlisted. Usually the
    only time; a job moved back as possibly closed can be shortlisted again later."""
    with _transaction(db_path, conn) as c:
        rows = c.execute(
            "SELECT job_id, MAX(at) AS at FROM events"
            " WHERE event = 'status_change' AND to_status = 'shortlisted' GROUP BY job_id"
        ).fetchall()
    return {r["job_id"]: r["at"] for r in rows}


def moved_back_at(
    *,
    db_path: Path | str | None = None,
    conn: sqlite3.Connection | None = None,
) -> dict[int, str]:
    """{job_id: when it was last moved off the shortlist (possibly closed, aggregator only)}."""
    with _transaction(db_path, conn) as c:
        rows = c.execute(
            "SELECT job_id, MAX(at) AS at FROM events WHERE event = 'status_change' AND to_status = 'scored'"
            " AND from_status IN ('shortlisted', 'researched') AND detail IN (?, ?) GROUP BY job_id",
            MOVE_BACK_DETAILS,
        ).fetchall()
    return {r["job_id"]: r["at"] for r in rows}


def get_top_jobs(
    limit: int = 15,
    status: str | None = None,
    *,
    db_path: Path | str | None = None,
    conn: sqlite3.Connection | None = None,
) -> list[dict[str, Any]]:
    """Scored jobs that aren't closed (or only those in `status`), highest score first."""
    if status is not None and status not in STATUSES:
        raise ValueError(f"unknown status {status!r}")
    with _transaction(db_path, conn) as c:
        rows = c.execute(
            "SELECT * FROM jobs WHERE score IS NOT NULL AND status != 'closed' AND (? IS NULL OR status = ?)"
            " ORDER BY score DESC, id LIMIT ?",
            (status, status, int(limit)),
        ).fetchall()
    return [dict(r) for r in rows]


def record_application(
    job_id: int,
    method: str,
    notes: str | None = None,
    applied_at: str | None = None,
    *,
    db_path: Path | str | None = None,
    conn: sqlite3.Connection | None = None,
) -> int:
    """Record how and when the user applied. Does not change status; see mark_applied()."""
    if not method:
        raise ValueError("method is required")
    with _transaction(db_path, conn) as c:
        cur = c.execute(
            "INSERT INTO applications (job_id, applied_at, method, notes) VALUES (?, ?, ?, ?)",
            (job_id, applied_at or _now(), method, notes),
        )
        return cur.lastrowid


def mark_applied(
    job_id: int,
    method: str,
    notes: str | None = None,
    *,
    db_path: Path | str | None = None,
) -> int:
    """The user marks a job applied: status change and application record in one transaction.
    A hold on the job ends: it's applied to now."""
    conn = connect(db_path)
    try:
        with conn:
            set_status(job_id, "applied", "user", detail=f"method={method}", conn=conn)
            conn.execute("UPDATE jobs SET held_until = NULL, hold_reason = NULL WHERE id = ?", (job_id,))
            return record_application(job_id, method, notes, conn=conn)
    finally:
        conn.close()


def _closed_by_filter(c: sqlite3.Connection, job_id: int) -> bool:
    row = c.execute(
        "SELECT detail FROM events WHERE job_id = ? AND event = 'status_change' AND to_status = 'closed'"
        " ORDER BY id DESC LIMIT 1",
        (job_id,),
    ).fetchone()
    return row is not None and (row["detail"] or "").startswith(FILTERED_DETAIL_PREFIX)


def _has_did_you_apply(c: sqlite3.Connection, job_id: int) -> bool:
    return c.execute("SELECT 1 FROM tracker_messages WHERE job_id = ? AND outcome = 'did_you_apply' LIMIT 1",
                     (job_id,)).fetchone() is not None


def _is_allowed(old_status: str, new_status: str) -> bool:
    if new_status == "closed":
        return old_status != "closed"
    return new_status in TRANSITIONS.get(old_status, set())


def _valid_source(source: str) -> bool:
    if source in SOURCES:
        return True
    return source.startswith(EMAIL_SOURCE_PREFIX) and len(source) > len(EMAIL_SOURCE_PREFIX)


def _check_actor(actor: str) -> None:
    if actor not in ACTORS:
        raise ValueError(f"unknown actor {actor!r}; expected one of {ACTORS}")


def _cmd_init(args: argparse.Namespace) -> int:
    path = init_db(args.db)
    print(f"Database ready at {path}")
    return 0


def _cmd_show(args: argparse.Namespace) -> int:
    jobs = get_jobs(args.status, args.limit, db_path=args.db)
    if not jobs:
        print("No jobs found.")
        return 0
    print(f"{'ID':>5}  {'STATUS':<12}  {'SCORE':>5}  {'COMPANY':<24}  TITLE")
    for j in jobs:
        score = "" if j["score"] is None else f"{j['score']:.1f}"
        print(f"{j['id']:>5}  {j['status']:<12}  {score:>5}  {j['company'][:24]:<24}  {j['title']}")
    return 0


def _cmd_top(args: argparse.Namespace) -> int:
    jobs = get_top_jobs(args.n, db_path=args.db)
    if not jobs:
        print("No scored jobs.")
        return 0
    print(f"{'ID':>5}  {'SCORE':>5}  {'STATUS':<12}  {'TIER':<10}  {'COMPANY':<24}  TITLE")
    for j in jobs:
        print(f"{j['id']:>5}  {j['score']:>5.1f}  {j['status']:<12}  {j['preferred_tier'] or '':<10}"
              f"  {j['company'][:24]:<24}  {j['title']}")
    return 0


def _cmd_mark_applied(args: argparse.Namespace) -> int:
    try:
        mark_applied(args.job_id, args.method, args.notes, db_path=args.db)
    except (TransitionError, LookupError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    print(f"Job {args.job_id} marked applied (method: {args.method}).")
    return 0


def main(argv: list[str] | None = None) -> int:
    # Job titles can contain characters the Windows console codepage can't encode.
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="replace")

    parser = argparse.ArgumentParser(prog="python -m agents.db", description=__doc__.split("\n")[0])
    parser.add_argument("--db", type=Path, default=None, help=f"database path (default: {DEFAULT_DB_PATH})")
    sub = parser.add_subparsers(dest="command", required=True)

    p_init = sub.add_parser("init", help="create the database and apply migrations")
    p_init.set_defaults(func=_cmd_init)

    p_show = sub.add_parser("show", help="list jobs")
    p_show.add_argument("--status", choices=STATUSES)
    p_show.add_argument("--limit", type=int)
    p_show.set_defaults(func=_cmd_show)

    p_top = sub.add_parser("top", help="highest-scoring jobs that aren't closed")
    p_top.add_argument("n", type=int, nargs="?", default=15)
    p_top.set_defaults(func=_cmd_top)

    p_apply = sub.add_parser("mark-applied", help="mark a job applied (you, by hand)")
    p_apply.add_argument("job_id", type=int)
    p_apply.add_argument("--method", required=True, help="e.g. ats, email, referral")
    p_apply.add_argument("--notes")
    p_apply.set_defaults(func=_cmd_mark_applied)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
