"""Application tracker: read application emails (Gmail, read-only) and update statuses.

    python -m agents.tracker.track [--since-days N] [--dry-run] [--no-toast] [--db PATH] [--log-dir DIR]

  1. Queries (match.py): one per applied or interviewing job, one for all ATS senders,
     one for recruiter-sounding mail. The window is the last FIRST_RUN_DAYS (30) days on
     the first run, and again whenever the set of applied/interviewing jobs changed since
     the last run (its signature, jobs_signature()); otherwise since the last run, with
     a day of overlap. --since-days overrides it. label:JobAlerts is always excluded.
  2. Headers only (From, Subject, Date), scored against every job, for each message not
     yet checked against the current jobs signature (tracker_header_checks). A likely
     match, any other ATS email, or a recruiter-looking email has its body fetched,
     unless that (message, job) pair was already processed. An unmatched message is
     only a header check: when the applied/interviewing jobs change, it's checked again
     (headers only, no Claude call) and may match then. Security codes are ignored.
  3. One `claude -p` call per batch classifies them (classify.py).
  4. Outcomes, through db.set_status() with actor tracker only (db.TRACKER_MOVES):
     interview_invite / assessment / scheduling: applied -> interviewing;
     rejection: -> closed, detail "rejected"; offer: no change, flagged.
     Low confidence, or not about the matched job: needs_review, no change.
     An application email for a job not marked applied: "Did you apply?".
     Recruiter mail about a job not applied to: recruiter outreach.
  5. A reply toast (agents/common/toast.py), when notify is given: a new
     interview_invite, assessment, scheduling, rejection, offer or needs_review shows
     e.g. "Contoso: interview invite — check digest". Nothing new, no toast.

Each classified message is recorded in tracker_messages per (message, job) (ID, date,
sender domain, one-line summary; never the body or subject), so no pair is classified
twice. A header check is stored only once its message is resolved, so a message Claude
didn't answer for is retried. Usage goes to logs/tracker-YYYY-MM-DD.log.
"""

from __future__ import annotations

import argparse
import logging
import sqlite3
import sys
import tempfile
import time
from contextlib import closing
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from agents import db
from agents.common import gmail, runlog, toast
from agents.common.claude import (
    ClaudeCallFailed,
    ClaudeUnavailable,
    Runner,
    UsageTotals,
    call_with_retry,
    log_usage,
    run_claude,
)
from agents.scout import source_trust
from agents.scout.companies import normalize_company
from agents.tracker import classify, match

log = logging.getLogger(__name__)

FIRST_RUN_DAYS = 30
OVERLAP = timedelta(days=1)
ACTIVE = ("applied", "interviewing")
TO_INTERVIEWING = ("interview_invite", "assessment", "scheduling")
APPLICATION_MAIL = ("confirmation", "assessment", "interview_invite", "scheduling", "rejection")
# New mail worth a toast: a reply that needs the user, or something to look at.
REPLY_CATEGORIES = ("interview_invite", "assessment", "scheduling", "rejection", "offer")
TOAST_TITLE = "Job agent"
TOAST_SHOWN = 2  # names in the toast; the rest are counted


@dataclass
class Result:
    dry_run: bool
    since: datetime | None = None
    candidates: int = 0
    queries: int = 0
    found: int = 0
    already: int = 0
    ignored: int = 0
    unmatched: int = 0
    likely: list[str] = field(default_factory=list)  # dry run: what would be classified
    lines_out: list[str] = field(default_factory=list)  # one per classified message
    classified: int = 0
    status_changes: int = 0
    needs_review: int = 0
    replies: list[str] = field(default_factory=list)  # "Contoso: interview invite", for the toast
    usage: UsageTotals = field(default_factory=UsageTotals)
    errors: list[str] = field(default_factory=list)
    stopped: str | None = None

    def lines(self) -> list[str]:
        since = f"{self.since.astimezone():%Y-%m-%d %H:%M}" if self.since else "?"
        head = "Tracker (dry run: headers only, no Claude, nothing stored)" if self.dry_run else "Tracker"
        out = [f"{head}: since {since}, {self.candidates} applied/interviewing job(s), {self.queries} Gmail queries",
               f"  messages: {self.found} found, {self.already} already checked, {self.ignored} ignored "
               f"(security codes), {self.unmatched} unmatched, "
               f"{len(self.likely) if self.dry_run else self.classified} "
               f"{'would be classified' if self.dry_run else 'classified'}"]
        out += [f"  {line}" for line in (self.likely if self.dry_run else self.lines_out)]
        if not self.dry_run:
            out.append(f"  status changes: {self.status_changes}, needs review: {self.needs_review}")
            if self.usage.calls:
                out.append(f"  usage total: {self.usage.total().fields()}")
        if self.stopped:
            out.append(f"  stopped early: {self.stopped}")
        if self.errors:
            out.append("Errors:")
            out += [f"  {e}" for e in self.errors]
        return out


@dataclass
class Pending:
    """A message whose body is fetched, waiting for classification."""
    message_id: str
    header: dict[str, Any]
    cand: match.Candidate | None
    score: int
    message: dict[str, Any] | None = None
    pairs: set[tuple[str, int | None]] = field(default_factory=set)  # processed (message, job) pairs


def jobs_signature(active: list[match.Candidate]) -> str:
    """The set of applied/interviewing jobs, e.g. "14,36"; a header check is redone when it changes."""
    return ",".join(str(i) for i in sorted(c.job["id"] for c in active))


def window_start(now: datetime, since_days: int | None, db_path: Path | str | None, jobs_sig: str = "") -> datetime:
    """--since-days; else 30 days on the first run or when the jobs changed (so older
    unmatched mail is checked again); else since the last run, with a day of overlap."""
    if since_days is not None:
        return now - timedelta(days=since_days)
    try:
        last = db.last_tracker_run(db_path=db_path)
    except sqlite3.OperationalError:  # a dry run on a database without the tracker tables
        last = None
    if last and last.get("jobs_sig") == jobs_sig:
        return datetime.fromisoformat(last["ran_at"]) - OVERLAP
    return now - timedelta(days=FIRST_RUN_DAYS)


def candidates(db_path: Path | str | None, config: dict[str, Any]) -> tuple[list[match.Candidate], list[match.Candidate]]:
    """(applied or interviewing jobs, with their company domains; every other job)."""
    applied_at = {j["id"]: j["applied_at"] for j in db.get_applications(db_path=db_path)}
    active, others = [], []
    for job in db.get_jobs(db_path=db_path):
        job = {**job, "applied_at": applied_at.get(job["id"])}
        if job["status"] in ACTIVE:
            board = db.get_company_board(normalize_company(job["company"]), db_path=db_path)
            active.append(match.Candidate(job, match.company_domains(job, board and board["board_url"], config)))
        else:
            others.append(match.Candidate(job))
    return active, others


def track(
    mailbox: Any,
    *,
    db_path: Path | str | None = None,
    since_days: int | None = None,
    dry_run: bool = False,
    runner: Runner = run_claude,
    sleep: Callable[[float], None] = time.sleep,
    now: datetime | None = None,
    config: dict[str, Any] | None = None,
    notify: Callable[[str, str], Any] | None = None,
) -> Result:
    """notify(title, message) shows the reply toast (toast.notify); None shows nothing."""
    now = now or datetime.now(timezone.utc)
    config = config or source_trust.load_config()
    ats = config["ats_sender_domains"]
    result = Result(dry_run=dry_run)
    if not dry_run:
        db.init_db(db_path)
    active, others = candidates(db_path, config)
    result.candidates = len(active)
    sig = jobs_signature(active)
    since = result.since = window_start(now, since_days, db_path, sig)

    try:
        pending = _scan(mailbox, active, others, ats, since, sig, result, db_path, dry_run)
        if dry_run:
            return _done(result)
        for p in pending:
            p.message = mailbox.get_message(p.message_id)
    except gmail.GmailAuthExpired as exc:
        result.stopped = str(exc)
        log.error("Tracker: %s", exc)
        return _done(result)

    _classify_all(pending, sig, result, db_path, runner, sleep)
    message = reply_toast(result.replies)
    if notify and message:
        notify(TOAST_TITLE, message)
    if not result.stopped:
        db.record_tracker_run(now.isoformat(timespec="seconds"), since.isoformat(timespec="seconds"),
                              result.found - result.already, sig, db_path=db_path)
    return _done(result)


def _done(result: Result) -> Result:
    for line in result.lines():
        log.info(line)
    return result


def _scan(mailbox, active, others, ats, since, sig, result, db_path, dry_run) -> list[Pending]:
    """List messages; read the headers of those not checked against this jobs signature;
    record the header check of unmatched and ignored ones; return the rest to classify."""
    named: dict[str, set[int]] = {}  # message -> jobs whose query (naming the company) found it
    from_ats: set[str] = set()
    recruiter: set[str] = set()
    for cand in active:
        for mid in mailbox.list_messages(match.job_query(cand, ats, since)):
            named.setdefault(mid, set()).add(cand.job["id"])
    for mid in mailbox.list_messages(match.ats_query(ats, since)):
        named.setdefault(mid, set())
        from_ats.add(mid)
    for mid in mailbox.list_messages(match.recruiter_query(since)):
        named.setdefault(mid, set())
        recruiter.add(mid)
    result.queries = len(active) + 2
    result.found = len(named)
    checks, pairs = _seen(named, db_path)
    fresh = sorted(mid for mid in named if checks.get(mid) != sig)
    result.already = len(named) - len(fresh)

    pending: list[Pending] = []
    for mid in fresh:
        header = mailbox.get_headers(mid)
        domain = match.sender_domain(header.get("sender") or "")
        cand, score, why = match.best_job(header, active + others, ats, named[mid])
        is_ats = match.domain_matches(domain, ats)
        job_id = cand.job["id"] if cand else None
        if match.IGNORED_SUBJECT.search(header.get("subject") or ""):
            result.ignored += 1
            outcome = "ignored"
        elif not (match.is_likely(score, why) or is_ats or (mid in recruiter and match.looks_like_recruiter(header))):
            result.unmatched += 1
            outcome = "unmatched"
        elif (mid, job_id) in pairs:
            result.already += 1  # this job's classification of it stands
            outcome = "seen"
        else:
            outcome = None  # a weak job match is kept only if the body names the company
        if outcome:
            if not dry_run:
                with closing(db.connect(db_path)) as conn, conn:
                    db.record_header_check(mid, sig, domain, _utc(header.get("date")), job_id, score, conn=conn)
                    if outcome == "ignored" and (mid, job_id) not in pairs:
                        db.record_tracker_message({"message_id": mid, "msg_date": _utc(header.get("date")),
                                                   "sender_domain": domain, "match_score": score,
                                                   "outcome": outcome, "job_id": job_id}, conn=conn)
                conn.close()
            continue
        pending.append(Pending(mid, header, cand, score, pairs=pairs))
        target = f"#{cand.job['id']} {cand.job['company']}" if cand else "no job"
        result.likely.append(f"{(header.get('date') or '')[:10]} {domain}: {target} "
                             f"(score {score}{': ' + ', '.join(why) if why else ''})")
    return pending


def _seen(named: dict[str, set[int]], db_path) -> tuple[dict[str, str], set[tuple[str, int | None]]]:
    """(header checks, processed pairs). A dry run never migrates: before the tracker
    tables (or this version of them) exist, nothing has been seen."""
    try:
        return (db.tracker_header_checks(list(named), db_path=db_path),
                db.tracker_pairs(list(named), db_path=db_path))
    except sqlite3.OperationalError:  # no such table
        return {}, set()


def _classify_all(pending: list[Pending], sig: str, result: Result, db_path, runner, sleep) -> None:
    # Verify the company in the body; a message that doesn't name it isn't that job's.
    for p in pending:
        text = classify.message_text(p.message)
        if p.cand and not match.body_names_company(p.message, p.cand.job["company"], text):
            p.cand = None
    # Without its job, a message may be one already processed about no job.
    done = [p for p in pending if not p.cand and (p.message_id, None) in p.pairs]
    for p in done:
        db.record_header_check(p.message_id, sig, match.sender_domain(p.header.get("sender") or ""),
                               _utc(p.header.get("date")), None, p.score, db_path=db_path)
        result.already += 1
    pending = [p for p in pending if p not in done]
    batches = [pending[i:i + classify.BATCH_SIZE] for i in range(0, len(pending), classify.BATCH_SIZE)]
    if not batches:
        return
    with tempfile.TemporaryDirectory(prefix="job-agent-track-") as tmp:
        prompt_file = Path(tmp) / "system_prompt.txt"
        prompt_file.write_text(classify.PROMPT_PATH.read_text(encoding="utf-8"), encoding="utf-8")
        for n, batch in enumerate(batches, 1):
            label = f"track batch={n}/{len(batches)}"
            items = [classify.payload(i, p.message, p.cand and p.cand.job) for i, p in enumerate(batch, 1)]
            try:
                reply, seconds = call_with_retry(runner, prompt_file, classify.build_user_prompt(items), label, sleep)
            except ClaudeUnavailable as exc:
                result.stopped = f"stopped at batch {n}/{len(batches)}: {exc}"
                log.error("Tracker: %s", result.stopped)
                return
            except ClaudeCallFailed as exc:
                result.errors.append(f"batch {n}: {exc}")
                continue
            log_usage(label, reply, seconds, result.usage, f"emails={len(batch)}")
            try:
                ratings = classify.parse(str(reply.get("result") or ""), set(range(1, len(batch) + 1)))
            except ValueError as exc:
                result.errors.append(f"batch {n}: reply unusable: {exc}")
                continue
            for i, p in enumerate(batch, 1):
                if i in ratings:
                    _apply(p, ratings[i], sig, result, db_path)
                # A message missing from the reply gets no header check, so the next run retries it.


def decide(c: dict[str, Any], job: dict[str, Any] | None) -> tuple[str, str | None]:
    """(outcome, new status or None) for a classified message."""
    if classify.needs_review(c, job is not None):
        return "needs_review", None
    cat = c["category"]
    if cat == "offer":
        return "offer", None
    if cat == "recruiter_outreach":
        return ("event" if job and job["status"] in ACTIVE else "outreach"), None
    if cat == "other" or job is None:
        return "event", None
    if job["status"] not in ACTIVE:
        return ("did_you_apply" if cat in APPLICATION_MAIL else "event"), None
    if cat in TO_INTERVIEWING and job["status"] == "applied":
        return "status_change", "interviewing"
    if cat == "rejection":
        return "status_change", "closed"
    return "event", None


def _apply(p: Pending, c: dict[str, Any], sig: str, result: Result, db_path) -> None:
    job = p.cand.job if p.cand else None
    outcome, new_status = decide(c, job)
    conn = db.connect(db_path)
    try:
        with conn:
            if new_status:
                detail = db.REJECTED_DETAIL if new_status == "closed" else f"{c['category']}: {c['summary']}"
                try:
                    db.set_status(job["id"], new_status, "tracker", detail, conn=conn)
                except (db.TransitionError, db.ActorNotAllowed, LookupError) as exc:
                    result.errors.append(f"job {job['id']}: {exc}")
                    outcome, new_status = "event", None
            db.record_tracker_message({
                "message_id": p.message_id, "job_id": job["id"] if job else None,
                "msg_date": _utc(p.header.get("date")),
                "sender_domain": match.sender_domain(p.header.get("sender") or ""), "match_score": p.score,
                "outcome": outcome, **{k: c[k] for k in ("category", "confidence", "summary", "deadline",
                                                         "next_action", "company", "role")},
            }, conn=conn)
            db.record_header_check(p.message_id, sig, match.sender_domain(p.header.get("sender") or ""),
                                   _utc(p.header.get("date")), job["id"] if job else None, p.score, conn=conn)
    finally:
        conn.close()
    result.classified += 1
    result.status_changes += bool(new_status)
    result.needs_review += outcome == "needs_review"
    if outcome == "needs_review" or c["category"] in REPLY_CATEGORIES:
        who = job["company"] if job else (c["company"] or match.sender_domain(p.header.get("sender") or ""))
        what = "email to review" if outcome == "needs_review" else c["category"].replace("_", " ")
        result.replies.append(f"{who}: {what}")
    target = f"#{job['id']} {job['company']}" if job else (c["company"] or "no job")
    effect = {"status_change": f"-> {new_status}", "needs_review": "NEEDS REVIEW, no change",
              "offer": "OFFER, no automatic change", "did_you_apply": f"Did you apply? (job is {job and job['status']})",
              "outreach": "recruiter outreach"}.get(outcome, "no change")
    result.lines_out.append(f"{(p.header.get('date') or '')[:10]} {target}: {c['category']} "
                            f"({c['confidence']}%) {c['summary']} [{effect}]")


def reply_toast(replies: list[str]) -> str | None:
    """ "Contoso: interview invite — check digest"; None when there's nothing new."""
    if not replies:
        return None
    shown = "; ".join(replies[:TOAST_SHOWN])
    more = f" and {len(replies) - TOAST_SHOWN} more" if len(replies) > TOAST_SHOWN else ""
    return f"{shown}{more} — check digest"


def _utc(iso: str | None) -> str | None:
    if not iso:
        return None
    return datetime.fromisoformat(iso).astimezone(timezone.utc).isoformat(timespec="seconds")


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="replace")
    parser = argparse.ArgumentParser(prog="python -m agents.tracker.track", description=__doc__.split("\n")[0])
    parser.add_argument("--db", type=Path, default=None)
    parser.add_argument("--since-days", type=int, default=None,
                        help=f"look back N days (default: since the last run, {FIRST_RUN_DAYS} on the first)")
    parser.add_argument("--log-dir", type=Path, default=runlog.DEFAULT_LOG_DIR)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--no-toast", action="store_true", help="don't show a Windows notification")
    args = parser.parse_args(argv)
    if not gmail.TOKEN_PATH.exists():
        print(f"No Gmail token at {gmail.TOKEN_PATH}. {gmail.EXPIRED_HINT}", file=sys.stderr)
        return 2
    with runlog.log_to(runlog.log_path("tracker", args.log_dir)) as log_file:
        result = track(gmail.Mailbox(), db_path=args.db, since_days=args.since_days, dry_run=args.dry_run,
                       notify=None if args.no_toast else toast.notify)
    print("\n".join(result.lines()))
    print(f"Log: {log_file}")
    return 1 if result.errors or result.stopped else 0


if __name__ == "__main__":
    sys.exit(main())
