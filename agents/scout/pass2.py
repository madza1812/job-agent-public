"""Score pass 2: rescore the top scored jobs from their full posting text.

    python -m agents.scout.pass2 [--db PATH] [--profile PATH] [--preferred PATH]
                                 [--log-dir DIR] [--pool N] [--batch-size N] [--dry-run]

  * The pool is the top POOL_SIZE (15) `scored` jobs by final score, the same pool the
    details fetch (details.py) works on. A job in it is rescored when it has a full
    description (needs_details = 0 and at least LOW_CONFIDENCE_CHARS of text) and no
    pass 2 score yet. A job whose posting wasn't found keeps its pass 1 score.
  * Instructions are in prompts/score_pass2.md: the pass 1 schema plus posting_type
    (job / talent_pool / general_application), years_required, office_days, stack_match,
    red_flags, why and gaps. The final score is recomputed in
    code by score.compute_final(), exactly as in pass 1 except that office_days feeds the
    location score (score.location_score); the extra fields go into score_detail
    (with "pass": 2). Status stays `scored`; shortlist.py picks from here.
  * Each batch is one `claude -p --model sonnet` call with no tools. Usage goes to
    logs/score-YYYY-MM-DD.log.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from agents import db
from agents.common import runlog
from agents.common.claude import (
    ClaudeCallFailed,
    Runner,
    ClaudeUnavailable,
    UsageTotals,
    call_with_retry,
    log_usage,
    run_claude,
)
from agents.scout import score
from agents.scout.companies import DEFAULT_PREFERRED_PATH, load_preferred
from agents.scout.details import LOW_CONFIDENCE_CHARS, POOL_SIZE
from agents.scout.filters import DEFAULT_PROFILE_PATH, load_profile

log = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parents[2]
PROMPT_PATH = ROOT / "prompts" / "score_pass2.md"
DEFAULT_BATCH_SIZE = 5  # full descriptions are long
DESCRIPTION_CHARS = 8000
# What a posting really is. Anything but "job" is a recruiter pool: never shortlisted
# (shortlist.is_recruiter_pool), listed in its own digest section.
POSTING_TYPES = ("job", "talent_pool", "general_application")


@dataclass
class Result:
    dry_run: bool
    candidates: int = 0
    rescored: int = 0
    unanswered: int = 0
    usage: UsageTotals = field(default_factory=UsageTotals)
    errors: list[str] = field(default_factory=list)
    stopped: str | None = None

    def lines(self) -> list[str]:
        if self.dry_run:
            return [f"Pass 2 (dry run, no calls): {self.candidates} jobs would be rescored"]
        out = [
            f"Pass 2: {self.rescored} of {self.candidates} jobs rescored, {self.unanswered} unchanged",
            f"  usage total: {self.usage.total().fields()}",
        ]
        if self.stopped:
            out.append(f"  stopped early: {self.stopped}")
        if self.errors:
            out.append("Errors:")
            out += [f"  {e}" for e in self.errors]
        return out


def score_pass(job: dict[str, Any]) -> int | None:
    if not job.get("score_detail"):
        return None
    return json.loads(job["score_detail"]).get("pass", 1)


def needs_pass2(job: dict[str, Any]) -> bool:
    """No pass 2 score yet, or a newer posting made it stale (posting.mark_needs_pass2)."""
    if not job.get("score_detail"):
        return True
    detail = json.loads(job["score_detail"])
    return detail.get("pass", 1) != 2 or bool(detail.get("needs_pass2"))


def candidates(db_path: Path | str | None = None, pool_size: int = POOL_SIZE,
               job_ids: list[int] | None = None) -> list[dict[str, Any]]:
    """The pool's jobs that need pass 2; or exactly job_ids (any status, any description
    length: set-posting gives a verified official posting), when given."""
    if job_ids is not None:
        jobs = (db.get_job(i, db_path=db_path) for i in job_ids)
        return [j for j in jobs if j and j["status"] != "closed" and (j.get("description") or "").strip()]
    else:
        pool = [j for j in db.get_top_jobs(pool_size, "scored", db_path=db_path) if needs_pass2(j)]
    return [
        j for j in pool
        if not j.get("needs_details")
        and len(score.plain_text(j.get("description") or "")) >= LOW_CONFIDENCE_CHARS
    ]


def valid_pass2_rating(item: dict[str, Any]) -> dict[str, Any] | None:
    rating = score.valid_rating(item)
    if rating is None:
        return None
    years = item.get("years_required")
    rating["years_required"] = years if isinstance(years, int) and not isinstance(years, bool) and 0 <= years <= 40 else None
    kind = item.get("posting_type")
    rating["posting_type"] = kind if kind in POSTING_TYPES else "job"
    days = item.get("office_days")
    rating["office_days"] = days if isinstance(days, int) and not isinstance(days, bool) and 0 <= days <= 5 else None
    rating["stack_match"] = str(item.get("stack_match") or "").strip()[:400]
    rating["why"] = str(item.get("why") or "").strip()[:500]
    rating["possibly_closed"] = item.get("possibly_closed") is True
    for key in ("red_flags", "gaps"):
        value = item.get(key) or []
        if not isinstance(value, list):
            return None
        rating[key] = [str(v).strip()[:150] for v in value if str(v).strip()][:10]
    return rating


def rescore(
    profile: dict[str, Any],
    preferred: list[dict[str, Any]],
    *,
    db_path: Path | str | None = None,
    pool_size: int = POOL_SIZE,
    batch_size: int = DEFAULT_BATCH_SIZE,
    dry_run: bool = False,
    runner: Runner = run_claude,
    sleep: Callable[[float], None] = time.sleep,
    job_ids: list[int] | None = None,
) -> Result:
    result = Result(dry_run=dry_run)
    if not dry_run:
        db.init_db(db_path)
    jobs = candidates(db_path, pool_size, job_ids)
    result.candidates = len(jobs)
    if dry_run or not jobs:
        return result
    batches = [jobs[i:i + batch_size] for i in range(0, len(jobs), batch_size)]

    with tempfile.TemporaryDirectory(prefix="job-agent-pass2-") as tmp:
        prompt_file = Path(tmp) / "system_prompt.txt"
        prompt_file.write_text(score.build_system_prompt(profile, preferred, PROMPT_PATH), encoding="utf-8")
        conn = db.connect(db_path)
        try:
            for n, batch in enumerate(batches, 1):
                label = f"pass2 batch={n}/{len(batches)}"
                user_prompt = score.build_user_prompt(batch, DESCRIPTION_CHARS)
                try:
                    reply, seconds = call_with_retry(runner, prompt_file, user_prompt, label, sleep)
                except ClaudeUnavailable as exc:
                    result.stopped = f"stopped at batch {n}/{len(batches)}: {exc}"
                    log.error("Pass 2: %s", result.stopped)
                    break
                except ClaudeCallFailed as exc:
                    result.errors.append(f"batch {n}: {exc}")
                    continue
                usages = log_usage(label, reply, seconds, result.usage, f"jobs={len(batch)}")
                try:
                    ratings = score.parse_ratings(str(reply.get("result") or ""), {j["id"] for j in batch},
                                                  valid_pass2_rating)
                except ValueError as exc:
                    log.error("Pass 2: batch %d reply unusable: %s", n, exc)
                    result.errors.append(f"batch {n}: reply unusable: {exc}")
                    continue
                result.unanswered += len(batch) - len(ratings)
                model = ",".join(u.model for u in usages)
                for job in batch:
                    if job["id"] in ratings:
                        _store(conn, result, job, ratings[job["id"]], profile, model)
        finally:
            conn.close()

    for line in result.lines():
        log.info(line)
    return result


def _store(conn, result: Result, job: dict[str, Any], rating: dict[str, Any], profile: dict[str, Any], model: str) -> None:
    final, detail = score.compute_final(job, rating, profile)
    previous = json.loads(job["score_detail"]) if job.get("score_detail") else {}
    detail.update({
        "pass": 2,
        "pass1_final": previous.get("pass1_final", previous.get("final", job.get("score"))),
        "posting_type": rating["posting_type"],
        "years_required": rating["years_required"],
        "office_days": rating["office_days"],
        "stack_match": rating["stack_match"],
        "red_flags": rating["red_flags"],
        "possibly_closed": rating["possibly_closed"],
        "why": rating["why"],
        "gaps": rating["gaps"],
        "model": model,
    })
    try:
        with conn:
            db.set_score(job["id"], final, detail, conn=conn)
    except LookupError as exc:
        result.errors.append(f"job {job['id']}: {exc}")
        return
    result.rescored += 1
    log.info("Pass 2: job %d %s -> %s (%s)", job["id"], detail["pass1_final"], final, rating["reason"])


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="replace")
    parser = argparse.ArgumentParser(prog="python -m agents.scout.pass2", description=__doc__.split("\n")[0])
    parser.add_argument("--db", type=Path, default=None)
    parser.add_argument("--profile", type=Path, default=DEFAULT_PROFILE_PATH)
    parser.add_argument("--preferred", type=Path, default=DEFAULT_PREFERRED_PATH)
    parser.add_argument("--log-dir", type=Path, default=runlog.DEFAULT_LOG_DIR)
    parser.add_argument("--pool", type=int, default=POOL_SIZE)
    parser.add_argument("--batch-size", type=int, default=DEFAULT_BATCH_SIZE)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    with runlog.log_to(runlog.log_path("score", args.log_dir)) as log_file:
        result = rescore(load_profile(args.profile), load_preferred(args.preferred), db_path=args.db,
                         pool_size=args.pool, batch_size=args.batch_size, dry_run=args.dry_run)
    print("\n".join(result.lines()))
    print(f"Log: {log_file}")
    return 1 if result.errors or result.stopped else 0


if __name__ == "__main__":
    sys.exit(main())
