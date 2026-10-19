"""Score (pass 1): rate jobs against profile.yaml with Claude, in batches, via `claude -p`.

    python -m agents.scout.score [--db PATH] [--profile PATH] [--preferred PATH]
                                 [--log-dir DIR] [--batch-size N] [--limit N] [--rescore] [--dry-run]

  * Claude judges only what needs judgment: skills_match, seniority_fit, role_interest,
    company_similarity and employer_quality (each 0-100), whether the employer is a
    staffing agency, and any dealbreaker. Its instructions, with the scales, are in
    prompts/score_pass1.md. The profile it gets includes feedback.liked_examples and
    disliked_examples (.\\run.ps1 rate). Code computes everything else and the final
    score (compute_final), and stores the full breakdown as JSON in jobs.score_detail.
  * Final score: weighted average of the components with profile.yaml scoring_weights,
    where company_pref = tier score (dream 100 / strong 80 / interested 65) when the job
    matched a preferred company, otherwise company_similarity; location = 100 for a
    matching location or remote, else 50, unless profile.yaml has `commute` settings
    (location_score: remote, per-place scores, office days from pass 2); compensation =
    50 when unknown. Then -10 for a
    staffing agency, -5 if sponsored, clamped to 0-100; a dealbreaker caps it at 20.
    Pass 2 (pass2.py) uses the same compute_final().
  * Pass 1 never shortlists. Scored `new` jobs move to `scored`; `scored` jobs that have
    no score_detail yet (scored before it existed) are rescored in place. --rescore first
    resets every `scored` job to `new` (clearing its score), for after a prompt or
    weight change; shortlisted and later jobs are never touched.
  * Each batch is one `claude -p --model sonnet` call with no tools (agents/common/claude.py).
    Usage of every batch goes to logs/score-YYYY-MM-DD.log.
  * A job missing from Claude's reply, or with an invalid entry, is left as it was for
    the next run. Hitting the usage limit stops the run; other failures retry with backoff.

--dry-run builds the batches and prints their sizes without calling Claude.
"""

from __future__ import annotations

import argparse
import html
import json
import logging
import re
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

import yaml

from agents import db
from agents.common import runlog
from agents.common.claude import (  # noqa: F401  (re-exported for callers and tests)
    CLAUDE_FLAGS,
    ClaudeCallFailed,
    Runner,
    ClaudeUnavailable,
    UsageTotals,
    call_with_retry,
    extract_json,
    log_usage,
    run_claude,
)
from agents.scout.companies import DEFAULT_PREFERRED_PATH, load_preferred
from agents.scout.filters import DEFAULT_PROFILE_PATH, REMOTE, load_profile, location_matches, matched_places

log = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_LOG_DIR = runlog.DEFAULT_LOG_DIR

DEFAULT_BATCH_SIZE = 15
DESCRIPTION_CHARS = 2000  # per job; feed postings can be very long

# The final-score rules. compute_final() is the only place they're applied.
FORMULA_VERSION = 3  # 2: employer_quality; 3: commute location scores
TIER_SCORES = {"dream": 100, "strong": 80, "interested": 65}
LOCATION_MATCH = 100
LOCATION_OTHER = 50
COMPENSATION_UNKNOWN = 50
COMPENSATION_AT_OR_ABOVE_MIN = 100
COMPENSATION_BELOW_MIN = 20
STAFFING_AGENCY_PENALTY = 10
SPONSORED_PENALTY = 5
DEALBREAKER_CAP = 20

# Components Claude rates. The rest of scoring_weights is computed in code.
CLAUDE_COMPONENTS = ("skills_match", "seniority_fit", "role_interest", "company_similarity", "employer_quality")
COMPONENTS = ("skills_match", "seniority_fit", "role_interest", "employer_quality", "location", "compensation",
              "company_pref")

# Profile keys Claude needs. sanitize, location_aliases, scoring_weights etc. stay out.
_PROFILE_KEYS = ("identity", "skills", "experience", "targets", "feedback")

# The instructions Claude gets, including the scale of each rated component.
PROMPT_PATH = ROOT / "prompts" / "score_pass1.md"


@dataclass
class Result:
    dry_run: bool
    batches: int = 0
    reset: int = 0  # scored jobs moved back to new by --rescore
    scored: int = 0
    rescored: int = 0  # already `scored`, given a score_detail
    unanswered: int = 0  # jobs Claude's reply left out or got wrong; unchanged
    usage: UsageTotals = field(default_factory=UsageTotals)
    errors: list[str] = field(default_factory=list)
    stopped: str | None = None

    def lines(self) -> list[str]:
        reset = f"{self.reset} scored jobs {'would be ' if self.dry_run else ''}reset to new; " if self.reset else ""
        if self.dry_run:
            return [f"Score (dry run, no calls): {reset}{self.batches} batches"]
        out = [] if not reset else [f"Rescore: {reset.rstrip('; ')}"]
        out += [
            f"Score: {self.batches} batches, {self.scored} scored, {self.rescored} rescored,"
            f" {self.unanswered} unchanged",
            f"  usage total: {self.usage.total().fields()}",
        ]
        if self.stopped:
            out.append(f"  stopped early: {self.stopped}")
        if self.errors:
            out.append("Errors:")
            out += [f"  {e}" for e in self.errors]
        return out


def build_system_prompt(profile: dict[str, Any], preferred: list[dict[str, Any]],
                        prompt_path: Path = PROMPT_PATH) -> str:
    """Instructions plus the parts of profile.yaml and the preferred list Claude needs."""
    data: dict[str, Any] = {k: profile[k] for k in _PROFILE_KEYS if k in profile}
    data["preferred_companies"] = [
        {"name": c["name"], "tier": c.get("tier"), **({"why": c["why"]} if c.get("why") else {})}
        for c in preferred
    ]
    instructions = prompt_path.read_text(encoding="utf-8")
    return instructions + "\n" + yaml.safe_dump(data, sort_keys=False, allow_unicode=True, width=1000)


def job_payload(job: dict[str, Any], description_chars: int = DESCRIPTION_CHARS) -> dict[str, Any]:
    """The fields of one job sent to Claude. Empty values are dropped to save tokens."""
    desc = plain_text(job.get("description") or "")
    if len(desc) > description_chars:
        desc = desc[:description_chars] + " …"
    payload = {
        "id": job["id"],
        "title": job["title"],
        "company": job["company"],
        "location": job.get("location"),
        "needs_details": bool(job.get("needs_details")) or None,
        "description": desc,
    }
    return {k: v for k, v in payload.items() if v not in (None, "")}


def build_user_prompt(jobs: list[dict[str, Any]], description_chars: int = DESCRIPTION_CHARS) -> str:
    lines = [json.dumps(job_payload(j, description_chars), ensure_ascii=False, separators=(",", ":")) for j in jobs]
    return f"Rate these {len(jobs)} postings:\n" + "\n".join(lines)


def compensation_score(job: dict[str, Any], profile: dict[str, Any]) -> tuple[int, str]:
    """100 at or above min_salary, 20 below it, 50 when unknown (no salary, a non-USD
    salary, or no min_salary in the profile). A job site's estimate counts, marked as such."""
    min_salary = (profile.get("hard_filters") or {}).get("min_salary")
    top = job.get("salary_max") or job.get("salary_min")
    if top is None or min_salary is None or job.get("salary_currency") not in (None, "USD"):
        return COMPENSATION_UNKNOWN, "unknown"
    kind = "estimated" if job.get("salary_is_predicted") else "posted"
    if top >= min_salary:
        return COMPENSATION_AT_OR_ABOVE_MIN, f"{kind} top {top:,} >= min {min_salary:,}"
    return COMPENSATION_BELOW_MIN, f"{kind} top {top:,} < min {min_salary:,}"


def location_score(job: dict[str, Any], profile: dict[str, Any], office_days: int | None = None) -> tuple[int, str]:
    """50 when the location doesn't clearly match. A match scores 100 without `commute` in
    the profile; with it, the best of the matched places: `remote`, or the place's entry
    under `commute.places`, either a flat score or scores by office days per week
    ("1-2", "3", "4-5", ..., plus "unknown"). office_days comes from pass 2; 0 is remote."""
    matched, why = location_matches(job, profile)
    commute = profile.get("commute")
    if not matched:
        return LOCATION_OTHER, why
    if not commute:
        return LOCATION_MATCH, why
    places = matched_places(job, profile)
    if office_days == 0:
        places.append(REMOTE)
    scored = [_place_score(commute, place, office_days) for place in places] or [(LOCATION_MATCH, why)]
    value, note = max(scored, key=lambda s: s[0])
    return value, note


def _place_score(commute: dict[str, Any], place: str, office_days: int | None) -> tuple[int, str]:
    if place == REMOTE:
        return int(commute.get("remote", LOCATION_MATCH)), "remote"
    rule = (commute.get("places") or {}).get(place)
    if rule is None:
        return LOCATION_MATCH, f"{place}"
    if isinstance(rule, (int, float)):
        return int(rule), f"{place}, any office days"
    if office_days is None:
        return int(rule["unknown"]), f"{place}, office days unknown"
    for key, value in rule.items():
        if key == "unknown":
            continue
        low, _, high = str(key).partition("-")
        if int(low) <= office_days <= int(high or low):
            return int(value), f"{place}, {office_days} office days/week"
    return int(rule["unknown"]), f"{place}, {office_days} office days/week (no rule)"


def check_commute(profile: dict[str, Any]) -> None:
    """Raise ValueError if profile.yaml `commute` is malformed."""
    commute = profile.get("commute")
    if not commute:
        return
    places = commute.get("places") or {}
    known = set((profile.get("hard_filters") or {}).get("locations") or [])
    for place, rule in places.items():
        if place not in known:
            raise ValueError(f"commute.places.{place} is not in hard_filters.locations")
        if isinstance(rule, (int, float)):
            continue
        if not isinstance(rule, dict) or "unknown" not in rule:
            raise ValueError(f"commute.places.{place} needs a score or office-day scores with 'unknown'")
        for key in rule:
            if key != "unknown" and not re.fullmatch(r"\d(-\d)?", str(key)):
                raise ValueError(f"commute.places.{place}: bad office-days key {key!r} (use 3 or 1-2)")


def compute_final(job: dict[str, Any], rating: dict[str, Any], profile: dict[str, Any]) -> tuple[float, dict[str, Any]]:
    """The final score and its full breakdown, from Claude's rating and the job's data."""
    weights = profile.get("scoring_weights") or {}
    unknown = set(weights) - set(COMPONENTS)
    if unknown or not weights:
        raise ValueError(f"scoring_weights must use only {COMPONENTS}; unknown: {sorted(unknown)}")
    check_commute(profile)
    total_weight = sum(weights.values())

    components: dict[str, dict[str, Any]] = {
        k: {"value": rating[k], "source": "claude"}
        for k in ("skills_match", "seniority_fit", "role_interest", "employer_quality")
    }
    tier = job.get("preferred_tier")
    if tier:
        components["company_pref"] = {"value": TIER_SCORES[tier], "source": "code",
                                      "note": f"preferred {tier}: {job.get('preferred_match')}"}
    else:
        components["company_pref"] = {"value": rating["company_similarity"], "source": "claude",
                                      "note": "not preferred; company_similarity"}
    # The ATS's own remote flag (jobs.office_days) beats pass 2's reading of the text.
    office_days = job.get("office_days") if job.get("office_days") is not None else rating.get("office_days")
    value, why = location_score(job, profile, office_days)
    components["location"] = {"value": value, "source": "code", "note": why}
    value, why = compensation_score(job, profile)
    components["compensation"] = {"value": value, "source": "code", "note": why}

    weighted = sum(weights.get(k, 0) * c["value"] for k, c in components.items()) / total_weight
    score = weighted
    adjustments = []
    if rating["staffing_agency"]:
        score -= STAFFING_AGENCY_PENALTY
        adjustments.append({"staffing_agency": -STAFFING_AGENCY_PENALTY})
    if job.get("sponsored"):
        score -= SPONSORED_PENALTY
        adjustments.append({"sponsored": -SPONSORED_PENALTY})
    score = min(100.0, max(0.0, score))
    capped = bool(rating["dealbreaker"]) and score > DEALBREAKER_CAP
    if rating["dealbreaker"]:
        score = min(score, DEALBREAKER_CAP)
    final = round(score, 1)

    detail = {
        "formula_version": FORMULA_VERSION,
        "weights": weights,
        "components": components,
        "company_similarity": rating["company_similarity"],
        "weighted": round(weighted, 2),
        "adjustments": adjustments,
        "dealbreaker": rating["dealbreaker"],
        "capped_at": DEALBREAKER_CAP if capped else None,
        "final": final,
        "reason": rating["reason"],
    }
    return final, detail


def parse_ratings(text: str, job_ids: set[int],
                  validate: Callable[[dict[str, Any]], dict[str, Any] | None] | None = None) -> dict[int, dict[str, Any]]:
    """{job_id: rating} for valid entries about jobs in this batch."""
    validate = validate or valid_rating
    ratings: dict[int, dict[str, Any]] = {}
    for item in extract_json(text, list):
        if not isinstance(item, dict):
            continue
        job_id = item.get("id")
        if not isinstance(job_id, int) or job_id not in job_ids or job_id in ratings:
            continue
        rating = validate(item)
        if rating is None:
            log.warning("Score: job %d has an invalid rating: %s", job_id, item)
            continue
        ratings[job_id] = rating
    return ratings


def valid_rating(item: dict[str, Any]) -> dict[str, Any] | None:
    """The pass 1 fields of a rating, normalized, or None if any is missing or invalid."""
    rating: dict[str, Any] = {}
    for key in CLAUDE_COMPONENTS:
        value = item.get(key)
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 <= value <= 100:
            return None
        rating[key] = round(value)
    if not isinstance(item.get("staffing_agency"), bool):
        return None
    rating["staffing_agency"] = item["staffing_agency"]
    dealbreaker = item.get("dealbreaker")
    if dealbreaker is not None and not isinstance(dealbreaker, str):
        return None
    rating["dealbreaker"] = (dealbreaker or "").strip()[:200] or None
    rating["reason"] = str(item.get("reason") or "").strip()[:200]
    return rating


def score_jobs(
    profile: dict[str, Any],
    preferred: list[dict[str, Any]],
    *,
    db_path: Path | str | None = None,
    batch_size: int = DEFAULT_BATCH_SIZE,
    limit: int | None = None,
    dry_run: bool = False,
    rescore: bool = False,
    runner: Runner = run_claude,
    sleep: Callable[[float], None] = time.sleep,
) -> Result:
    result = Result(dry_run=dry_run)
    if not dry_run:
        db.init_db(db_path)
    if rescore:
        result.reset = _reset_scored(db_path, dry_run)
    jobs = db.get_jobs("new", db_path=db_path)
    if not dry_run:  # a dry run against an unmigrated database may lack score_detail
        jobs += [j for j in db.get_jobs("scored", db_path=db_path) if j.get("score_detail") is None]
    elif rescore:  # nothing was reset, but these are what a real run would score
        jobs += db.get_jobs("scored", db_path=db_path)
    jobs = sorted(jobs, key=lambda j: j["id"])[:limit]
    batches = [jobs[i:i + batch_size] for i in range(0, len(jobs), batch_size)]
    result.batches = len(batches)
    system_prompt = build_system_prompt(profile, preferred)

    if dry_run:
        for n, batch in enumerate(batches, 1):
            log.info("Batch %d/%d: %d jobs, prompt %d chars + system %d chars",
                     n, len(batches), len(batch), len(build_user_prompt(batch)), len(system_prompt))
        return result

    with tempfile.TemporaryDirectory(prefix="job-agent-score-") as tmp:
        prompt_file = Path(tmp) / "system_prompt.txt"
        prompt_file.write_text(system_prompt, encoding="utf-8")
        conn = db.connect(db_path)
        try:
            for n, batch in enumerate(batches, 1):
                label = f"batch={n}/{len(batches)}"
                try:
                    reply, seconds = call_with_retry(runner, prompt_file, build_user_prompt(batch),
                                                     f"Score: {label}", sleep)
                except ClaudeUnavailable as exc:
                    result.stopped = f"stopped at batch {n}/{len(batches)}: {exc}"
                    log.error("Score: %s", result.stopped)
                    break
                except ClaudeCallFailed as exc:
                    result.errors.append(f"batch {n}: {exc}")
                    continue

                usages = log_usage(label, reply, seconds, result.usage, f"jobs={len(batch)}")
                try:
                    ratings = parse_ratings(str(reply.get("result") or ""), {j["id"] for j in batch})
                except ValueError as exc:  # includes json.JSONDecodeError
                    log.error("Score: batch %d reply unusable: %s", n, exc)
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


def _reset_scored(db_path: Path | str | None, dry_run: bool) -> int:
    """Move every `scored` job back to `new` (clearing its score) for a rescore. Jobs at
    shortlisted or later are never touched. All or nothing."""
    jobs = db.get_jobs("scored", db_path=db_path)
    if dry_run:
        return len(jobs)
    conn = db.connect(db_path)
    try:
        with conn:
            for job in jobs:
                db.set_status(job["id"], "new", "scout", "rescore: reset for rescoring", conn=conn)
    finally:
        conn.close()
    log.info("Rescore: %d scored jobs reset to new", len(jobs))
    return len(jobs)


def _store(conn, result: Result, job: dict[str, Any], rating: dict[str, Any], profile: dict[str, Any], model: str) -> None:
    final, detail = compute_final(job, rating, profile)
    detail["pass"] = 1
    detail["model"] = model
    try:
        with conn:
            db.set_score(job["id"], final, detail, conn=conn)
            if job["status"] == "new":
                db.set_status(job["id"], "scored", "scout", f"score {final}: {rating['reason']}", conn=conn)
    except (db.TransitionError, db.ActorNotAllowed, LookupError) as exc:
        # E.g. the user moved the job while the batch ran. The transaction rolled back.
        log.error("Score: job %d not stored: %s", job["id"], exc)
        result.errors.append(f"job {job['id']}: {exc}")
        return
    if job["status"] == "new":
        result.scored += 1
    else:
        result.rescored += 1
    log.info("Score: job %d -> %s (%s)", job["id"], final, rating["reason"])


def plain_text(text: str) -> str:
    # Feed descriptions are often escaped HTML: unescape, drop tags, unescape entities.
    text = re.sub(r"<[^>]+>", " ", html.unescape(text))
    return re.sub(r"\s+", " ", html.unescape(text)).strip()


def _positive(value: str) -> int:
    n = int(value)
    if n < 1:
        raise argparse.ArgumentTypeError("must be at least 1")
    return n


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="replace")

    parser = argparse.ArgumentParser(prog="python -m agents.scout.score", description=__doc__.split("\n")[0])
    parser.add_argument("--db", type=Path, default=None)
    parser.add_argument("--profile", type=Path, default=DEFAULT_PROFILE_PATH)
    parser.add_argument("--preferred", type=Path, default=DEFAULT_PREFERRED_PATH)
    parser.add_argument("--log-dir", type=Path, default=DEFAULT_LOG_DIR)
    parser.add_argument("--batch-size", type=_positive, default=DEFAULT_BATCH_SIZE)
    parser.add_argument("--limit", type=_positive, default=None, help="score at most N jobs this run")
    parser.add_argument("--rescore", action="store_true",
                        help="first reset every scored job (never shortlisted or later) to new, then score")
    parser.add_argument("--dry-run", action="store_true", help="build batches, call nothing, change nothing")
    args = parser.parse_args(argv)

    with runlog.log_to(runlog.log_path("score", args.log_dir)) as log_file:
        result = score_jobs(
            load_profile(args.profile),
            load_preferred(args.preferred),
            db_path=args.db,
            batch_size=args.batch_size,
            limit=args.limit,
            dry_run=args.dry_run,
            rescore=args.rescore,
        )

    print("\n".join(result.lines()))
    print(f"Log: {log_file}")
    return 1 if result.errors or result.stopped else 0


if __name__ == "__main__":
    sys.exit(main())
