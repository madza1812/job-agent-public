"""Research analyst: write a brief for each shortlisted job.

    python -m agents.research.brief [JOB_ID] [--db PATH] [--profile PATH] [--log-dir DIR]
                                    [--refresh-company] [--dry-run]

Without JOB_ID: every `shortlisted` job that has no brief yet and isn't on hold. With JOB_ID: that job
(scored, shortlisted, researched, applied or interviewing), even if it has a brief already.
A scored job keeps its status; only a shortlisted one moves to researched.

  1. Company step (prompts/research_company.md): reuse the newest company_research row
     for the company (keyed by companies.normalize_company) if it's under
     COMPANY_CACHE_DAYS old; otherwise one `claude -p --model sonnet` call with only
     WebSearch and WebFetch (LinkedIn denied, as in the details lookup), --max-turns
     COMPANY_MAX_TURNS, and a budget of 8 searches set in the prompt. The reply (markdown
     plus a sources list) is cached.
  2. Role step (prompts/research_role.md): one call with no tools. It gets the company
     research, the job's full description, its score_detail (why, gaps, red flags), and
     the profile's identity, skills, experience and targets, and writes the whole brief.
  3. validate_brief(): every heading of the template in research_role.md (between the
     <!-- template --> markers), a markdown table under the skills heading, at least
     MIN_SOURCES source URLs, and the job's apply link. A failed check retries the call
     once, telling Claude what failed.
  4. The brief goes to briefs/YYYY-MM-DD-<company>-<role>.md, its path to jobs.brief_path,
     and a shortlisted job moves to `researched` (actor research).

A job that fails stays `shortlisted` and is retried on the next run. Usage per call and
per brief goes to logs/research-YYYY-MM-DD.log.
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import sys
import tempfile
import time
import unicodedata
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

import yaml

from agents import db
from agents.common import runlog
from agents.common.claude import (
    ClaudeCallFailed,
    MaxTurnsReached,
    Runner,
    ClaudeUnavailable,
    UsageTotals,
    call_with_retry,
    extract_json,
    log_usage,
    run_claude,
)
from agents.scout import source_trust
from agents.scout.companies import normalize_company
from agents.scout.filters import DEFAULT_PROFILE_PATH, load_profile
from agents.scout.score import plain_text

log = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parents[2]
COMPANY_PROMPT_PATH = ROOT / "prompts" / "research_company.md"
ROLE_PROMPT_PATH = ROOT / "prompts" / "research_role.md"
DEFAULT_BRIEFS_DIR = ROOT / "briefs"

COMPANY_CACHE_DAYS = 30
COMPANY_MAX_TURNS = 20
MAX_SEARCHES = 8  # asked for in the prompt; the CLI has no hard cap, so it's checked in the log
TOOLS = ("WebSearch", "WebFetch")
MIN_SOURCES = 3
DESCRIPTION_CHARS = 12000
RESEARCHABLE = ("scored", "shortlisted", "researched", "applied", "interviewing")
_PROFILE_KEYS = ("identity", "skills", "experience", "targets")
_URL = re.compile(r"https?://[^\s<>()\]]+")


def company_runner(system_prompt_file: Path, user_prompt: str) -> dict[str, Any]:
    deny = [f"WebFetch(domain:{d})" for d in source_trust.load_config()["blocked_fetch_domains"]]
    return run_claude(system_prompt_file, user_prompt, tools=TOOLS, max_turns=COMPANY_MAX_TURNS,
                      deny=deny, timeout=600)


def role_runner(system_prompt_file: Path, user_prompt: str) -> dict[str, Any]:
    return run_claude(system_prompt_file, user_prompt, timeout=600)


class BriefFailed(Exception):
    pass


@dataclass
class BriefUsage:
    job_id: int
    path: str | None = None
    company_cached: bool = False
    usage: UsageTotals = field(default_factory=UsageTotals)


@dataclass
class Result:
    dry_run: bool
    candidates: int = 0
    written: list[BriefUsage] = field(default_factory=list)
    usage: UsageTotals = field(default_factory=UsageTotals)
    errors: list[str] = field(default_factory=list)
    stopped: str | None = None

    def lines(self) -> list[str]:
        if self.dry_run:
            return [f"Research (dry run, no calls): {self.candidates} briefs would be written"]
        out = [f"Research: {len(self.written)} of {self.candidates} briefs written"]
        for b in self.written:
            t = b.usage.total()
            out.append(f"  #{b.job_id:<5} {b.path}  ({b.usage.calls} calls, company research "
                       f"{'cached' if b.company_cached else 'new'}; {t.fields()})")
        if self.written:
            out.append(f"  usage total: {self.usage.total().fields()}")
        if self.stopped:
            out.append(f"  stopped early: {self.stopped}")
        if self.errors:
            out.append("Errors:")
            out += [f"  {e}" for e in self.errors]
        return out


# --- template and validation -------------------------------------------------------

def template_headings(prompt_path: Path = ROLE_PROMPT_PATH) -> list[str]:
    """The `## ` headings between <!-- template --> and <!-- /template --> in the role prompt."""
    text = prompt_path.read_text(encoding="utf-8")
    m = re.search(r"<!-- template -->(.*?)<!-- /template -->", text, re.S)
    if not m:
        raise ValueError(f"{prompt_path.name}: no <!-- template --> block")
    headings = re.findall(r"^## +(.+?)\s*$", m.group(1), re.M)
    if not headings:
        raise ValueError(f"{prompt_path.name}: the template has no ## headings")
    return headings


def sections(text: str) -> dict[str, str]:
    """{heading: body} for each `## ` heading (case-insensitive keys)."""
    out: dict[str, str] = {}
    parts = re.split(r"^## +(.+?)\s*$", text, flags=re.M)
    for i in range(1, len(parts) - 1, 2):
        out[parts[i].strip().lower()] = parts[i + 1]
    return out


def validate_brief(text: str, headings: list[str], apply_link: str | None) -> list[str]:
    """Problems with a brief; empty when it passes."""
    problems = []
    found = sections(text)
    missing = [h for h in headings if h.lower() not in found]
    if missing:
        problems.append("missing sections: " + ", ".join(f"'## {h}'" for h in missing))
    skills = next((h for h in headings if "skill" in h.lower()), None)
    body = found.get(skills.lower(), "") if skills else text
    if not (re.search(r"^\s*\|.*\|\s*$", body, re.M) and re.search(r"^\s*\|\s*:?-{3,}", body, re.M)):
        problems.append(f"no markdown table under '## {skills}'" if skills else "no skills table")
    src = next((h for h in reversed(headings) if "source" in h.lower()), None)
    urls = {u.rstrip(".,;") for u in _URL.findall(found.get(src.lower(), "") if src else "")}
    if len(urls) < MIN_SOURCES:
        problems.append(f"only {len(urls)} source URLs under '## {src}', need at least {MIN_SOURCES}")
    if apply_link and apply_link not in text:
        problems.append(f"the apply link {apply_link} is missing")
    return problems


def parse_company(text: str) -> dict[str, Any]:
    data = extract_json(text, dict)
    markdown = data.get("markdown")
    if not isinstance(markdown, str) or len(markdown.strip()) < 100:
        raise ValueError("'markdown' missing or too short")
    sources = []
    for s in data.get("sources") or []:
        if isinstance(s, dict) and isinstance(s.get("url"), str) and s["url"].startswith(("http://", "https://")):
            sources.append({"n": s.get("n"), "title": str(s.get("title") or "")[:300], "url": s["url"][:2000],
                            "date": s.get("date") if isinstance(s.get("date"), str) else None,
                            "kind": s.get("kind") if s.get("kind") in ("official", "news", "other") else "other"})
    if not sources:
        raise ValueError("no usable sources")
    return {"markdown": markdown.strip(), "sources": sources}


# --- prompts -------------------------------------------------------------------------

def apply_link(job: dict[str, Any]) -> str | None:
    return job.get("apply_url") or job.get("source_url") or job.get("url")


def company_user_prompt(job: dict[str, Any], today: date) -> str:
    return "\n".join([
        f"Today: {today.isoformat()} (news must be from {(today - timedelta(days=365)).isoformat()} or later)",
        f"Company: {job['company']}",
        f"Job title: {job['title']}",
        f"Job posting: {job.get('source_url') or job.get('apply_url') or 'unknown'}",
    ])


def role_system_prompt(profile: dict[str, Any], prompt_path: Path = ROLE_PROMPT_PATH) -> str:
    text = prompt_path.read_text(encoding="utf-8")
    text = text.replace("<!-- template -->\n", "").replace("<!-- /template -->\n", "")
    data = {k: profile[k] for k in _PROFILE_KEYS if k in profile}
    return text + "\nThe candidate's profile:\n" + yaml.safe_dump(data, sort_keys=False, allow_unicode=True, width=1000)


def role_user_prompt(job: dict[str, Any], research: dict[str, Any], today: date) -> str:
    detail = json.loads(job["score_detail"]) if job.get("score_detail") else {}
    trust = job.get("source_trust")
    link = apply_link(job)
    scoring = {
        "final_score": job.get("score"),
        "sub_scores": {k: v.get("value") for k, v in (detail.get("components") or {}).items()},
        "adjustments": detail.get("adjustments") or None,
        "why": detail.get("why") or detail.get("reason"),
        "gaps": detail.get("gaps"),
        "red_flags": detail.get("red_flags"),
        "stack_match": detail.get("stack_match"),
    }
    scoring = {k: v for k, v in scoring.items() if v not in (None, [], {})}
    desc = plain_text(job.get("description") or "")
    if len(desc) > DESCRIPTION_CHARS:
        desc = desc[:DESCRIPTION_CHARS] + " …"
    src = "\n".join(f"{s.get('n') or i}. {s['title']}, {s['url']}" + (f" ({s['date']})" if s.get("date") else "")
                    for i, s in enumerate(research["sources"], 1))
    lines = [
        f"Today: {today.isoformat()}",
        f"Job #{job['id']}: {job['title']} at {job['company']}",
        f"Location: {job.get('location') or 'unknown'}" + (" (remote)" if job.get("remote") else ""),
        f"Salary: {_salary(job)}",
        f"Years of experience asked for: {job.get('years_required') if job.get('years_required') is not None else 'unknown'}",
        f"Apply link (copy exactly): {link or 'none'}",
        f"Posting source: {job.get('source_url') or 'unknown'} ("
        + ("official" if trust == "official" else "aggregator copy, verify before applying" if trust == "aggregator"
           else "unverified") + ")",
        "",
        "## How the job was scored",
        json.dumps(scoring, ensure_ascii=False, indent=1),
        "",
        f"## Company research ({research['created_at'][:10]})",
        research["markdown"],
        "",
        "Sources of the company research:",
        src,
        "",
        "## The job posting",
        desc or "(no description stored)",
    ]
    return "\n".join(lines)


def _salary(job: dict[str, Any]) -> str:
    lo, hi = job.get("salary_min"), job.get("salary_max")
    if not lo and not hi:
        return "not stated"
    cur = job.get("salary_currency") or "USD"
    return (f"{lo:,}-{hi:,} {cur}" if lo and hi else f"{lo or hi:,} {cur}") + (
        " (job site estimate)" if job.get("salary_is_predicted") else "")


# --- the run --------------------------------------------------------------------------

def candidates(job_id: int | None = None, db_path: Path | str | None = None,
               any_status: bool = False) -> list[dict[str, Any]]:
    """any_status: rewrite the given job's existing brief whatever its status (set-posting)."""
    if job_id is not None:
        job = db.get_job(job_id, db_path=db_path)
        if job is None:
            raise LookupError(f"no job with id {job_id}")
        if any_status and job.get("brief_path") and job["status"] != "closed":
            return [job]
        if job["status"] not in RESEARCHABLE:
            raise ValueError(f"job {job_id} is {job['status']}; only {', '.join(RESEARCHABLE)} jobs get a brief")
        return [job]
    return [j for j in db.get_jobs(status="shortlisted", db_path=db_path) if not db.is_held(j)]


def _brief_exists(path: str) -> bool:
    p = Path(path)
    return (p if p.is_absolute() else ROOT / p).exists()


def brief_filename(job: dict[str, Any], today: date) -> str:
    return f"{today.isoformat()}-{slug(job['company'], 50)}-{slug(job['title'], 50)}.md"


def slug(text: str, max_len: int) -> str:
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    s = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return (s[:max_len].rstrip("-") or "x")


def research(
    job_id: int | None = None,
    *,
    profile: dict[str, Any] | None = None,
    db_path: Path | str | None = None,
    briefs_dir: Path = DEFAULT_BRIEFS_DIR,
    refresh_company: bool = False,
    dry_run: bool = False,
    any_status: bool = False,
    company_call: Runner = company_runner,
    role_call: Runner = role_runner,
    sleep: Callable[[float], None] = time.sleep,
    today: date | None = None,
    now: datetime | None = None,
) -> Result:
    today = today or date.today()
    result = Result(dry_run=dry_run)
    if not dry_run:
        db.init_db(db_path)
    jobs = candidates(job_id, db_path, any_status)
    if job_id is None and not dry_run:
        # Shortlisted again with a brief already written (e.g. back from "possibly closed",
        # brief rewritten by set-posting): no calls, just researched.
        for job in [j for j in jobs if j.get("brief_path") and _brief_exists(j["brief_path"])]:
            db.set_status(job["id"], "researched", "research", f"brief: {job['brief_path']} (already written)",
                          db_path=db_path)
            log.info("Research: job %d already has a brief; researched", job["id"])
        jobs = [j for j in jobs if not (j.get("brief_path") and _brief_exists(j["brief_path"]))]
    result.candidates = len(jobs)
    if dry_run:
        for j in jobs:
            log.info("Research: would write a brief for job %d %s | %s", j["id"], j["company"], j["title"])
        return result
    if not jobs:
        log.info("Research: nothing to do")
        return result

    profile = profile if profile is not None else load_profile()
    headings = template_headings()
    with tempfile.TemporaryDirectory(prefix="job-agent-research-") as tmp:
        company_prompt = Path(tmp) / "company_prompt.txt"
        company_prompt.write_text(COMPANY_PROMPT_PATH.read_text(encoding="utf-8"), encoding="utf-8")
        role_prompt = Path(tmp) / "role_prompt.txt"
        role_prompt.write_text(role_system_prompt(profile), encoding="utf-8")
        for job in jobs:
            bu = BriefUsage(job_id=job["id"])
            try:
                rs = _company_research(job, bu, refresh_company, company_prompt, company_call, sleep,
                                       today, now, db_path)
                text = _write_brief(job, rs, bu, headings, role_prompt, role_call, sleep, today)
                bu.path = _save(job, rs, text, briefs_dir, today, db_path)
            except ClaudeUnavailable as exc:
                result.stopped = f"stopped at job {job['id']}: {exc}"
                log.error("Research: %s", result.stopped)
                result.usage.merge(bu.usage)
                break
            except (BriefFailed, ClaudeCallFailed, db.TransitionError, db.ActorNotAllowed, LookupError,
                    OSError) as exc:
                log.error("Research: job %d failed, stays %s: %s", job["id"], job["status"], exc)
                result.errors.append(f"job {job['id']}: {exc}")
                result.usage.merge(bu.usage)
                continue
            finally:
                if bu.usage.calls:
                    log.info("usage brief job=%d calls=%d %s", job["id"], bu.usage.calls, bu.usage.total().fields())
            result.written.append(bu)
            result.usage.merge(bu.usage)

    for line in result.lines():
        log.info(line)
    return result


def _company_research(job, bu: BriefUsage, refresh: bool, prompt_file: Path, call: Runner, sleep,
                      today: date, now: datetime | None, db_path) -> dict[str, Any]:
    key = normalize_company(job["company"])
    cached = None if refresh else db.get_company_research(key, db_path=db_path)
    now = now or datetime.now(timezone.utc)
    if cached and now - datetime.fromisoformat(cached["created_at"]) < timedelta(days=COMPANY_CACHE_DAYS):
        bu.company_cached = True
        log.info("Research: job %d uses cached company research for %r from %s", job["id"], key,
                 cached["created_at"])
        return cached

    label = f"research job={job['id']} step=company"
    problem = ""
    for attempt in (1, 2):
        try:
            reply, seconds = call_with_retry(call, prompt_file, company_user_prompt(job, today), label, sleep)
        except MaxTurnsReached as exc:
            log_usage(label, exc.reply, 0.0, bu.usage, "max_turns")
            raise BriefFailed(f"company research ran out of turns ({COMPANY_MAX_TURNS})") from exc
        usages = log_usage(label, reply, seconds, bu.usage, f"attempt={attempt}")
        searches = sum(u.web_searches for u in usages)
        if searches > MAX_SEARCHES:
            log.warning("Research: job %d company step used %d web searches (budget %d)",
                        job["id"], searches, MAX_SEARCHES)
        try:
            data = parse_company(str(reply.get("result") or ""))
        except ValueError as exc:
            problem = str(exc)
            log.warning("Research: job %d company reply unusable (attempt %d): %s", job["id"], attempt, exc)
            continue
        db.add_company_research(key, job["company"], data["markdown"], data["sources"], db_path=db_path)
        return db.get_company_research(key, db_path=db_path)
    raise BriefFailed(f"company research unusable after 2 attempts: {problem}")


def _write_brief(job, research_row, bu: BriefUsage, headings, prompt_file: Path, call: Runner, sleep,
                 today: date) -> str:
    label = f"research job={job['id']} step=role"
    base = role_user_prompt(job, research_row, today)
    prompt = base
    problems: list[str] = []
    for attempt in (1, 2):
        reply, seconds = call_with_retry(call, prompt_file, prompt, label, sleep)
        log_usage(label, reply, seconds, bu.usage, f"attempt={attempt}")
        text = _strip_fences(str(reply.get("result") or ""))
        problems = validate_brief(text, headings, apply_link(job))
        if not problems:
            return text
        log.warning("Research: job %d brief failed checks (attempt %d): %s", job["id"], attempt, "; ".join(problems))
        prompt = (base + "\n\nYour previous brief failed these checks:\n" + "\n".join(f"- {p}" for p in problems)
                  + "\nWrite the whole brief again, following the template exactly.")
    raise BriefFailed("brief failed checks after 2 attempts: " + "; ".join(problems))


def _strip_fences(text: str) -> str:
    text = text.strip()
    m = re.fullmatch(r"```(?:markdown|md)?\s*\n(.*)\n```", text, re.S)
    text = m.group(1).strip() if m else text
    start = text.find("## ")
    return text[start:] if start > 0 else text


def _save(job, research_row, text: str, briefs_dir: Path, today: date, db_path) -> str:
    briefs_dir.mkdir(parents=True, exist_ok=True)
    path = briefs_dir / brief_filename(job, today)
    header = [
        f"# {job['company']}: {job['title']}",
        "",
        f"Job #{job['id']} · score {job['score']:.1f} · brief written {today.isoformat()} · "
        f"company research from {research_row['created_at'][:10]}",
        "",
        "*(stated)* = from the posting · *(inferred)* = read between the lines",
        "",
    ]
    path.write_text("\n".join(header) + "\n" + text.strip() + "\n", encoding="utf-8")
    try:
        stored = path.resolve().relative_to(ROOT).as_posix()
    except ValueError:  # briefs_dir outside the project (tests)
        stored = str(path)
    conn = db.connect(db_path)
    try:
        with conn:
            db.set_brief_path(job["id"], stored, conn=conn)
            current = db.get_job(job["id"], conn=conn)["status"]
            if current == "shortlisted":
                db.set_status(job["id"], "researched", "research", f"brief: {stored}", conn=conn)
    finally:
        conn.close()
    log.info("Research: job %d brief written to %s", job["id"], stored)
    return stored


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="replace")
    parser = argparse.ArgumentParser(prog="python -m agents.research.brief", description=__doc__.split("\n")[0])
    parser.add_argument("job_id", type=int, nargs="?", help="one job (default: every shortlisted job without a brief)")
    parser.add_argument("--db", type=Path, default=None)
    parser.add_argument("--profile", type=Path, default=DEFAULT_PROFILE_PATH)
    parser.add_argument("--log-dir", type=Path, default=runlog.DEFAULT_LOG_DIR)
    parser.add_argument("--refresh-company", action="store_true", help="ignore cached company research")
    parser.add_argument("--dry-run", action="store_true", help="list the jobs that would get a brief; call nothing")
    args = parser.parse_args(argv)

    with runlog.log_to(runlog.log_path("research", args.log_dir)) as log_file:
        try:
            result = research(args.job_id, profile=load_profile(args.profile), db_path=args.db,
                              refresh_company=args.refresh_company, dry_run=args.dry_run)
        except (LookupError, ValueError) as exc:
            print(f"Error: {exc}")
            return 2
    print("\n".join(result.lines()))
    print(f"Log: {log_file}")
    return 1 if result.errors or result.stopped else 0


if __name__ == "__main__":
    sys.exit(main())
