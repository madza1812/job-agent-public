"""Shortlist: move the day's best scored jobs to `shortlisted`.

    python -m agents.scout.shortlist [--db PATH] [--profile PATH] [--dry-run]

Picks up to SHORTLIST_COUNT (3) `scored` jobs, highest final score first, whose score is
at least profile.yaml shortlist_min_score (default 60), that have never been shortlisted
before, and that are verified: a pass 2 score (from a full posting) and source_trust
official. Fewer than 3 is fine on weak days. Runs after pass 2. Recruiter pools (talent
pools and general applications, is_recruiter_pool), jobs the user put on hold
(db.is_held) and jobs whose posting is too old (age.over_limit) are never shortlisted.

Moved back first: a shortlisted or researched job whose pass 2 says the posting may be
closed (possibly_closed, or a red flag saying so) goes back to `scored` with detail
"possibly closed", and one whose posting is only an aggregator's with "aggregator only"
(actor scout). The next-best verified job takes its place in today's 3. A moved-back job
may be shortlisted again once it passes (re-check rule).

Unverified, aggregator-only and possibly closed jobs that would have made the cut show in
the digest under "Worth checking yourself" (see would_have_made_it()).
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import sys
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any

from agents import db
from agents.common import runlog
from agents.scout import age
from agents.scout.filters import DEFAULT_PROFILE_PATH, load_profile

log = logging.getLogger(__name__)

SHORTLIST_COUNT = 3
DEFAULT_MIN_SCORE = 60
VERIFIED_TRUST = ("official",)  # aggregator-only jobs go to "Worth checking yourself"
IN_PLAY = ("shortlisted", "researched")
# A red flag that says the posting may be gone, e.g. "posting shown as closed on aggregators".
_CLOSED_FLAG = re.compile(
    r"\b(closed|expired|filled|no longer (open|accepting|available)|not accepting)\b", re.I)
_NOT_CLOSED = re.compile(r"\bclosed[- ](source|loop|captioning)\b", re.I)
# A title that says it's a recruiter pool, not an opening (pass 2 also reads the text).
_POOL_TITLE = re.compile(r"\b(talent (network|pool)|general application|future opportunities)\b", re.I)


def _detail(job: dict[str, Any]) -> dict[str, Any]:
    return json.loads(job["score_detail"]) if job.get("score_detail") else {}


def possibly_closed(job: dict[str, Any]) -> bool:
    """Pass 2 says the posting may be closed: its possibly_closed field, or a red flag."""
    detail = _detail(job)
    if detail.get("possibly_closed") is True:
        return True
    return any(_CLOSED_FLAG.search(f) and not _NOT_CLOSED.search(f) for f in detail.get("red_flags") or [])


def is_recruiter_pool(job: dict[str, Any]) -> bool:
    """A talent pool or general application, not a job: pass 2's posting_type, or the
    title says so. Never shortlisted; the digest lists these on their own."""
    kind = _detail(job).get("posting_type")
    return (kind is not None and kind != "job") or bool(_POOL_TITLE.search(job.get("title") or ""))


def is_verified(job: dict[str, Any]) -> bool:
    """Scored from a full posting (pass 2) whose source was found."""
    detail = _detail(job)
    return detail.get("pass") == 2 and job.get("source_trust") in VERIFIED_TRUST


def eligible(job: dict[str, Any], min_score: float, ever_shortlisted: dict[int, str] | set[int]) -> bool:
    """Everything the shortlist asks except verification."""
    return job["score"] is not None and job["score"] >= min_score and job["id"] not in ever_shortlisted


@dataclass
class Result:
    dry_run: bool
    min_score: float
    already_today: int = 0  # shortlisted earlier today and still in play; they count toward the 3
    demoted: list[dict[str, Any]] = field(default_factory=list)  # moved back: possibly closed
    picked: list[dict[str, Any]] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def lines(self) -> list[str]:
        would = "would shortlist" if self.dry_run else "shortlisted"
        out = []
        if self.demoted:
            moved = "would move" if self.dry_run else "moved"
            out.append(f"Shortlist: {moved} {len(self.demoted)} back to scored (possibly closed or aggregator only)")
            out += [f"  #{j['id']:<5} {j['score']:>5.1f}  {j['company'][:24]:<24}  {j['title']}" for j in self.demoted]
        out.append(f"Shortlist: {would} {len(self.picked)} (min score {self.min_score})"
                   + (f", {self.already_today} already shortlisted today" if self.already_today else ""))
        out += [f"  #{j['id']:<5} {j['score']:>5.1f}  {j['company'][:24]:<24}  {j['title']}" for j in self.picked]
        if self.errors:
            out.append("Errors:")
            out += [f"  {e}" for e in self.errors]
        return out


def shortlist(
    profile: dict[str, Any],
    *,
    db_path: Path | str | None = None,
    count: int = SHORTLIST_COUNT,
    dry_run: bool = False,
) -> Result:
    min_score = profile.get("shortlist_min_score", DEFAULT_MIN_SCORE)
    result = Result(dry_run=dry_run, min_score=min_score)
    if not dry_run:
        db.init_db(db_path)
    conn = db.connect(db_path)
    try:
        demoted_ids = set()
        for job in [j for status in IN_PLAY for j in db.get_jobs(status=status, conn=conn)]:
            if possibly_closed(job):
                why = db.POSSIBLY_CLOSED_DETAIL
            elif job.get("source_trust") == "aggregator":
                why = db.AGGREGATOR_ONLY_DETAIL
            else:
                continue
            if not dry_run:
                try:
                    with conn:
                        db.set_status(job["id"], "scored", "scout", why, conn=conn)
                except (db.TransitionError, db.ActorNotAllowed, LookupError) as exc:
                    log.error("Shortlist: moving job %d back failed: %s", job["id"], exc)
                    result.errors.append(f"job {job['id']}: {exc}")
                    continue
            log.info("Shortlist: job %d %s | %s %s; back to scored", job["id"], job["company"], job["title"], why)
            demoted_ids.add(job["id"])
            result.demoted.append(job)

        before = db.shortlisted_at(conn=conn)
        # A second run on the same day tops up to `count`; it never adds another 3. A job
        # moved back as possibly closed doesn't count, so the next-best one replaces it.
        result.already_today = shortlisted_today(conn=conn, exclude=demoted_ids)
        scored = db.get_top_jobs(10_000, "scored", conn=conn)
        # Re-check rule: a job moved back (possibly closed, aggregator only) may be
        # shortlisted again once it passes: an official posting, not possibly closed.
        moved_back = db.moved_back_at(conn=conn)
        barred = {jid for jid in before if jid not in moved_back}
        pool = [j for j in scored if eligible(j, min_score, barred) and is_verified(j) and not possibly_closed(j)
                and not is_recruiter_pool(j) and not db.is_held(j) and not age.over_limit(j, profile)]
        for job in pool[:max(0, count - result.already_today)]:
            if not dry_run:
                try:
                    with conn:
                        db.set_status(job["id"], "shortlisted", "scout",
                                      f"top {count}: score {job['score']} >= {min_score}", conn=conn)
                except (db.TransitionError, db.ActorNotAllowed, LookupError) as exc:
                    log.error("Shortlist: job %d failed: %s", job["id"], exc)
                    result.errors.append(f"job {job['id']}: {exc}")
                    continue
            result.picked.append(job)
    finally:
        conn.close()
    for line in result.lines():
        log.info(line)
    return result


def shortlisted_today(*, db_path: Path | str | None = None, conn: Any = None, today: date | None = None,
                      exclude: set[int] | frozenset[int] = frozenset()) -> int:
    """Jobs shortlisted today (by scout or the user) that still count toward today's
    SHORTLIST_COUNT: not moved back to scored since."""
    today = today or date.today()
    statuses = {j["id"]: j["status"] for j in db.get_jobs(db_path=db_path, conn=conn)}
    return sum(1 for jid, at in db.shortlisted_at(db_path=db_path, conn=conn).items()
               if _local_date(at) == today and jid not in exclude and statuses.get(jid) != "scored")


class Refused(Exception):
    """Shortlist now: the job doesn't qualify; the message says why."""


def ready(job: dict[str, Any], min_score: float, today: date | None = None) -> bool:
    """Shows "Shortlist now": scored, an official posting, a score at the bar, not held.
    shortlist_now() checks the rest."""
    return (job["status"] == "scored" and job.get("source_trust") in VERIFIED_TRUST and job.get("score") is not None
            and job["score"] >= min_score and not db.is_held(job, today))


def shortlist_now(job_id: int, profile: dict[str, Any], *, db_path: Path | str | None = None,
                  count: int = SHORTLIST_COUNT, today: date | None = None) -> str:
    """The user shortlists one job (review app), within today's SHORTLIST_COUNT. Raises
    Refused with the reason when it doesn't qualify. Returns the old status."""
    min_score = profile.get("shortlist_min_score", DEFAULT_MIN_SCORE)
    job = db.get_job(job_id, db_path=db_path)
    if job is None:
        raise LookupError(f"no job with id {job_id}")
    if job["status"] != "scored":
        raise Refused(f"it's {job['status']}; only a scored job can be shortlisted")
    if db.is_held(job, today):
        raise Refused("it's on hold; unhold it first")
    if job.get("source_trust") not in VERIFIED_TRUST:
        raise Refused(f"no official posting (source: {job.get('source_trust') or 'unknown'}); "
                      "paste the official posting URL first")
    if _detail(job).get("pass") != 2:
        raise Refused("it hasn't been scored from the full posting yet (pass 2)")
    if job["score"] is None or job["score"] < min_score:
        raise Refused(f"its score {job['score'] or 0:.1f} is below shortlist_min_score {min_score}")
    if possibly_closed(job):
        raise Refused("the posting may be closed")
    if is_recruiter_pool(job):
        raise Refused("it's a talent pool or general application, not a job")
    too_old = age.refusal(job, profile, today)
    if too_old:
        raise Refused(too_old)
    done = shortlisted_today(db_path=db_path, today=today)
    if done >= count:
        raise Refused(f"{done} jobs were shortlisted today already (the limit is {count} a day)")
    return db.set_status(job_id, "shortlisted", "user", f"shortlisted by you: score {job['score']} >= {min_score}",
                         db_path=db_path)


def _local_date(iso: str) -> date:
    return datetime.fromisoformat(iso).astimezone().date()


def would_have_made_it(
    picked_today: list[dict[str, Any]],
    min_score: float,
    *,
    db_path: Path | str | None = None,
    count: int = SHORTLIST_COUNT,
    profile: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Scored jobs that would be in today's top `count` if verification weren't required
    and their postings were known to be open: unverified jobs never shortlisted, and
    possibly closed jobs (including ones moved back from the shortlist). Ranked together
    with today's picks; the ones that make the cut are returned. With the profile, jobs
    whose posting is too old are left out."""
    moved_back = db.moved_back_at(db_path=db_path)
    ever = {jid for jid in db.shortlisted_at(db_path=db_path) if jid not in moved_back}
    doubtful = [j for j in db.get_top_jobs(10_000, "scored", db_path=db_path)
                if not is_recruiter_pool(j) and not db.is_held(j)
                and not (profile is not None and age.over_limit(j, profile))
                and ((possibly_closed(j) and eligible(j, min_score, set()))
                     or (eligible(j, min_score, ever) and not is_verified(j)))]
    picked_ids = {j["id"] for j in picked_today}
    ranked = sorted([*picked_today, *doubtful], key=lambda j: (-(j["score"] or 0), j["id"]))[:count]
    out = [j for j in ranked if j["id"] not in picked_ids]
    # A job moved off the shortlist today is always listed, whatever its rank.
    today = date.today()
    moved_today = {jid for jid, at in db.moved_back_at(db_path=db_path).items() if _local_date(at) == today}
    out += [j for j in doubtful if j["id"] in moved_today and j not in out]
    return sorted(out, key=lambda j: (-(j["score"] or 0), j["id"]))


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="replace")
    parser = argparse.ArgumentParser(prog="python -m agents.scout.shortlist", description=__doc__.split("\n")[0])
    parser.add_argument("--db", type=Path, default=None)
    parser.add_argument("--profile", type=Path, default=DEFAULT_PROFILE_PATH)
    parser.add_argument("--log-dir", type=Path, default=runlog.DEFAULT_LOG_DIR)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    with runlog.log_to(runlog.log_path("score", args.log_dir)) as log_file:
        result = shortlist(load_profile(args.profile), db_path=args.db, dry_run=args.dry_run)
    print("\n".join(result.lines()))
    print(f"Log: {log_file}")
    return 1 if result.errors else 0


if __name__ == "__main__":
    sys.exit(main())
