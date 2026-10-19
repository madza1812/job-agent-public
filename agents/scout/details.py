"""Details: find the official posting for the top scored jobs that lack one, via `claude -p`.

    python -m agents.scout.details [--db PATH] [--log-dir DIR] [--pool N] [--dry-run]

  * The pool is the top POOL_SIZE (15) `scored` jobs by final score. Of those, a job is
    fetched when it has needs_details = 1 (alert emails carry no description) or low
    confidence (a stored description under LOW_CONFIDENCE_CHARS), and no details_status yet.
    A job not found is retried once, on a later day's run, then never again.
  * Free check first, no Claude call, no tokens (free_check): a cached board on another
    ATS (JazzHR, Workable, ...; from set-posting or an earlier find) is searched for the
    exact title (posting.find_on_board: HTML, or the JSON listing of a JavaScript-rendered
    board such as Workable, BambooHR or Breezy); otherwise the public Greenhouse, Lever and
    Ashby APIs, on the cached board or on board-name guesses (ats_api.probe_job, which
    caches hits and misses per company). A match is stored as an official posting with its
    verbatim description. Pool jobs that are aggregator-only or not found (they can't be
    shortlisted), or possibly closed with a cached board, get this free check too.
  * Otherwise one call per job: the same stripped-down `claude -p --model sonnet`, but with
    only WebSearch and WebFetch, pre-approved, and --max-turns MAX_TURNS. WebFetch is
    denied the blocked_fetch_domains in config/source_trust.yaml (LinkedIn is never
    fetched). Search order (prompts/details.md): the known board, ATS domains (listed from
    config/source_trust.yaml), the careers site, public-sector boards, and only then other
    job boards. Pages are only read; nothing is ever submitted. An official posting found
    on an ATS caches the company's board for next time. A find on Workable, BambooHR or
    Breezy is replaced by that ATS's JSON (with_api_posting), since WebFetch can't see it.
  * Found: the posting is official (source_trust.classify_posting) when its source_url or
    its apply_url is on the company's own domain or an approved job site, else aggregator.
    It's stored as source_trust, with the description (plain text), apply_url (the official
    link, when there is one), years_required, needs_details = 0 and details_status = 'found'.
    Salary, still_open and the posted date describe the page that was read, so they count
    only when source_url itself is official: only then is a salary or date stored (the
    date as posting_page, or ats from an ATS's data; never over a better one), and a
    posting that is no longer open closes the job (scout, detail "posting closed"). An
    aggregator's description is used for scoring only. Given the profile, each stored job's
    age is checked again with the new date (age.recheck: a posting too old closes it).
  * Not found (or out of turns, or "found" with no description or link):
    details_status = source_trust = 'not_found'; the job stays as it is and is retried
    once on a later day.
  * Usage per call, including web searches (run by Haiku, so logged as its own model),
    goes to logs/score-YYYY-MM-DD.log.
"""

from __future__ import annotations

import argparse
import logging
import sys
import tempfile
import time
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any, Callable

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
import requests

from agents.scout import age, ats_api, posting, source_trust
from agents.scout.companies import normalize_company
from agents.scout.filters import DEFAULT_PROFILE_PATH, load_profile
from agents.scout.score import plain_text
from agents.scout.shortlist import possibly_closed

log = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parents[2]
PROMPT_PATH = ROOT / "prompts" / "details.md"

POOL_SIZE = 15
LOW_CONFIDENCE_CHARS = 800
MAX_ATTEMPTS = 2  # the first lookup and one retry on a later day
MAX_TURNS = 10
TOOLS = ("WebSearch", "WebFetch")
DESCRIPTION_MAX_CHARS = 20000
CLOSED_DETAIL = "posting closed"
_ATS_FIELDS = ("location", "posted_at", "posted_at_source", "office_days")  # the page's structured data


def default_runner(system_prompt_file: Path, user_prompt: str) -> dict[str, Any]:
    deny = [f"WebFetch(domain:{d})" for d in source_trust.load_config()["blocked_fetch_domains"]]
    return run_claude(system_prompt_file, user_prompt, tools=TOOLS, max_turns=MAX_TURNS, deny=deny)


@dataclass
class Result:
    dry_run: bool
    candidates: int = 0
    found: int = 0
    free_found: int = 0  # by the free board check, no Claude call
    rechecked: int = 0  # possibly closed jobs checked on their board
    official: int = 0
    aggregator: int = 0
    not_found: int = 0
    closed: int = 0
    too_old: int = 0  # closed by age.recheck once the posting's date was known
    usage: UsageTotals = field(default_factory=UsageTotals)
    errors: list[str] = field(default_factory=list)
    stopped: str | None = None

    def lines(self) -> list[str]:
        if self.dry_run:
            return [f"Details (dry run, no calls): {self.candidates} jobs would be looked up"]
        t = self.usage.total()
        out = [
            f"Details: {self.candidates} looked up, {self.found} found ({self.official} official,"
            f" {self.aggregator} aggregator; {self.free_found} by the free board check), {self.not_found} not found,"
            f" {self.closed} closed (posting closed), {self.too_old} closed (posting too old);"
            f" {self.rechecked} aggregator/not-found jobs checked free",
            f"  usage total: {t.fields()}",
        ]
        if self.stopped:
            out.append(f"  stopped early: {self.stopped}")
        if self.errors:
            out.append("Errors:")
            out += [f"  {e}" for e in self.errors]
        return out


def needs_lookup(job: dict[str, Any], today: date | None = None) -> bool:
    status = job.get("details_status")
    if status == "not_found":
        # One retry, on a later day than the last try.
        last = job.get("details_fetched_at")
        return (job.get("details_attempts") or 0) < MAX_ATTEMPTS and (
            not last or datetime.fromisoformat(last).astimezone().date() < (today or date.today()))
    if status:
        return False
    return bool(job.get("needs_details")) or len(plain_text(job.get("description") or "")) < LOW_CONFIDENCE_CHARS


def candidates(db_path: Path | str | None = None, pool_size: int = POOL_SIZE,
               today: date | None = None) -> list[dict[str, Any]]:
    return [j for j in db.get_top_jobs(pool_size, "scored", db_path=db_path) if needs_lookup(j, today)]


def build_system_prompt(prompt_path: Path = PROMPT_PATH) -> str:
    config = source_trust.load_config()
    return (prompt_path.read_text(encoding="utf-8")
            .replace("{{ATS_DOMAINS}}", ", ".join(config["ats_domains"]))
            .replace("{{PUBLIC_SECTOR_DOMAINS}}", ", ".join(config["public_sector_domains"])))


def build_user_prompt(job: dict[str, Any], board: str | None = None, today: date | None = None) -> str:
    lines = [
        f"Today: {(today or date.today()).isoformat()}",
        f"Company: {job['company']}",
        f"Title: {job['title']}",
        f"Location: {job.get('location') or 'unknown'}",
        f"Alert link (to identify the job only): {job.get('url') or 'none'}",
    ]
    if board:
        lines.append(f"Company's known job board: {board} (the exact title isn't listed there by its link"
                     " text; check it for a renamed copy before searching elsewhere)")
    return "\n".join(lines)


def free_check(job: dict[str, Any], board: str | None, session: Any = None,
               db_path: Path | str | None = None) -> dict[str, Any] | None:
    """The job's posting without Claude, shaped like parse_details()'s result, or None.
    A cached board on another ATS (JazzHR, Workable, ...) is read as HTML; otherwise the
    Greenhouse, Lever and Ashby APIs are used: the cached board, or board-name guesses
    (ats_api.probe_job, which caches hits and misses)."""
    try:
        if board and not ats_api.board_from_url(board):
            url = posting.find_on_board(board, job["title"], job.get("location"), session)
            if not url:
                return None
            page = posting.fetch_posting(url, session)
            return {"found": True, "description": page["description"], "source_url": url, "apply_url": url,
                    "years_required": None, "salary": page["salary"], "still_open": True,
                    **{k: page.get(k) for k in _ATS_FIELDS}}
        r = ats_api.probe_job(job, session=session, db_path=db_path)
    except (posting.PostingError, requests.RequestException) as exc:
        log.warning("Details: job %d free check failed: %s", job["id"], exc)
        return None
    for line in r.lines():
        log.info("Details: job %d free ATS check: %s", job["id"], line)
    if not r.match or len(r.match["description"]) < posting.MIN_DESCRIPTION_CHARS:
        return None
    return {"found": True, "description": r.match["description"], "source_url": r.match["url"],
            "apply_url": r.match.get("apply_url") or r.match["url"], "years_required": None, "salary": None,
            "still_open": True, "posted_at": r.match.get("posted_at"), "posted_at_source": "ats"}


def parse_details(text: str) -> dict[str, Any]:
    """Validated details. Raises ValueError when the reply is unusable."""
    data = extract_json(text, dict)
    if not isinstance(data.get("found"), bool):
        raise ValueError("'found' missing or not a bool")
    if not data["found"]:
        return {"found": False}
    description = plain_text(str(data.get("description") or ""))[:DESCRIPTION_MAX_CHARS]
    source_url = _url(data.get("source_url"))
    # A JavaScript-rendered posting may come back with no text; with_api_posting() reads it.
    if not source_url or not (description or posting.api_url_for(source_url)):
        return {"found": False, "why": "found, but no description or link"}
    still_open = data.get("still_open")
    if not isinstance(still_open, bool):
        still_open = True  # unsure means open
    years = data.get("years_required")
    years = years if isinstance(years, int) and not isinstance(years, bool) and 0 <= years <= 40 else None
    salary = None
    s = data.get("salary")
    if isinstance(s, dict):
        lo, hi = _amount(s.get("min")), _amount(s.get("max"))
        if lo or hi:
            salary = (lo, hi, str(s.get("currency") or "USD")[:3].upper())
    try:
        posted = date.fromisoformat(str(data.get("posted_date") or "")[:10])
    except ValueError:
        posted = None
    posted = posted.isoformat() if posted and posted <= date.today() else None
    return {
        "found": True,
        "description": description,
        "source_url": source_url,
        "apply_url": _url(data.get("apply_url")),
        "years_required": years,
        "salary": salary,
        "still_open": still_open,
        "posted_at": posted,
        "posted_at_source": "posting_page",
    }


def with_api_posting(details: dict[str, Any], session: Any = None) -> dict[str, Any] | None:
    """The lookup's find with the verbatim posting from the ATS's JSON when its URL is on a
    JavaScript-rendered board (Workable, BambooHR, Breezy; WebFetch sees an empty page
    there), plus that board's location, published date and office days. Unchanged for
    other URLs. None when the JSON can't be read and the lookup brought no text either."""
    url = next((u for u in (details["source_url"], details.get("apply_url")) if posting.api_url_for(u or "")), None)
    if not url:
        return details
    try:
        page = posting.api_posting(url, session)
    except (posting.PostingError, requests.RequestException) as exc:
        log.warning("Details: %s JSON not readable: %s", url, exc)
        return details if details["description"] else None
    # The ATS's date wins; without one, the date the lookup read on the page stays.
    dated = ("posted_at", "posted_at_source") if page.get("posted_at") else ()
    return {**details, "description": page["description"], "source_url": url,
            **{k: page.get(k) for k in _ATS_FIELDS if k not in ("posted_at", "posted_at_source")},
            **{k: page[k] for k in dated}}


def fetch_details(
    *,
    db_path: Path | str | None = None,
    pool_size: int = POOL_SIZE,
    dry_run: bool = False,
    runner: Runner = default_runner,
    sleep: Callable[[float], None] = time.sleep,
    today: date | None = None,
    session: Any = None,
    job_ids: list[int] | None = None,
    profile: dict[str, Any] | None = None,
) -> Result:
    """job_ids: look up exactly these jobs (not closed), whatever their place in the pool or
    their earlier attempts: the user asked (Find posting in the review app). profile: check
    each found job's age again with its new date (age.recheck)."""
    result = Result(dry_run=dry_run)
    if not dry_run:
        db.init_db(db_path)
    if job_ids is not None:
        jobs = [j for j in (db.get_job(i, db_path=db_path) for i in job_ids) if j and j["status"] != "closed"]
        pool = jobs
    else:
        jobs = candidates(db_path, pool_size, today)
        pool = db.get_top_jobs(pool_size, "scored", db_path=db_path)
    boards: dict[int, str] = {}
    for j in pool:
        b = db.get_company_board(normalize_company(j["company"]), db_path=db_path)
        if b:
            boards[j["id"]] = b["board_url"]
    ids = {j["id"] for j in jobs}
    # Aggregator-only and not-found jobs can't be shortlisted; the free check may upgrade them.
    # So may a possibly closed job whose company board is cached.
    rechecks = [j for j in pool if j["id"] not in ids and job_ids is None
                and (j.get("source_trust") in (source_trust.AGGREGATOR, source_trust.NOT_FOUND)
                     or (possibly_closed(j) and j["id"] in boards))]
    result.candidates = len(jobs)
    if dry_run:
        for j in jobs:
            log.info("Details: would look up job %d %s | %s%s", j["id"], j["company"], j["title"],
                     " (board first)" if j["id"] in boards else "")
        for j in rechecks:
            log.info("Details: would check %s job %d free (%s)", j.get("source_trust"), j["id"],
                     boards.get(j["id"]) or "ATS API guesses")
        return result

    with tempfile.TemporaryDirectory(prefix="job-agent-details-") as tmp:
        prompt_file = Path(tmp) / "system_prompt.txt"
        prompt_file.write_text(build_system_prompt(), encoding="utf-8")
        conn = db.connect(db_path)
        session = session or requests.Session()
        try:
            for job in rechecks:
                result.rechecked += 1
                found = free_check(job, boards.get(job["id"]), session, db_path)
                if found:
                    result.free_found += 1
                    _store(conn, result, job, found, profile, db_path)
            for job in jobs:
                board = boards.get(job["id"])
                found = free_check(job, board, session, db_path)
                if found:
                    result.free_found += 1
                    _store(conn, result, job, found, profile, db_path)
                    continue
                label = f"details job={job['id']}"
                try:
                    reply, seconds = call_with_retry(runner, prompt_file, build_user_prompt(job, board, today), label,
                                                     sleep)
                except ClaudeUnavailable as exc:
                    result.stopped = f"stopped at job {job['id']}: {exc}"
                    log.error("Details: %s", result.stopped)
                    break
                except MaxTurnsReached as exc:
                    log_usage(label, exc.reply, 0.0, result.usage, "max_turns")
                    _not_found(conn, result, job, f"out of turns ({MAX_TURNS})")
                    continue
                except ClaudeCallFailed as exc:
                    result.errors.append(f"job {job['id']}: {exc}")
                    continue

                log_usage(label, reply, seconds, result.usage)
                try:
                    details = parse_details(str(reply.get("result") or ""))
                except ValueError as exc:
                    log.error("Details: job %d reply unusable: %s", job["id"], exc)
                    result.errors.append(f"job {job['id']}: reply unusable: {exc}")
                    continue
                if not details["found"]:
                    _not_found(conn, result, job, details.get("why") or "not found")
                    continue
                details = with_api_posting(details, session)
                if details is None:
                    _not_found(conn, result, job, "posting found, but its text couldn't be read")
                    continue
                _store(conn, result, job, details, profile, db_path)
        finally:
            conn.close()

    for line in result.lines():
        log.info(line)
    return result


def _store(conn, result: Result, job: dict[str, Any], details: dict[str, Any],
           profile: dict[str, Any] | None = None, db_path: Path | str | None = None) -> None:
    trust, official_url = source_trust.classify_posting(details["source_url"], details["apply_url"], job["company"])
    official = trust == source_trust.OFFICIAL
    # Salary, still_open and the ATS fields describe the page read, so they need source_url official.
    page_official = official and official_url == details["source_url"]
    close = page_official and not details["still_open"]
    apply_url = details["apply_url"]
    if official and source_trust.classify(apply_url, job["company"]) != source_trust.OFFICIAL:
        apply_url = official_url  # never send the user to an aggregator when an official link exists
    try:
        with conn:
            db.store_details(
                job["id"],
                description=details["description"],
                apply_url=apply_url,
                source_url=details["source_url"],
                years_required=details["years_required"],
                source_trust=trust,
                salary=details["salary"] if page_official else None,
                **({k: details.get(k) for k in _ATS_FIELDS} if page_official else {}),
                conn=conn,
            )
            if close:
                db.set_status(job["id"], "closed", "scout", CLOSED_DETAIL, conn=conn)
            elif official:
                posting.cache_board(job, official_url, "lookup", conn=conn)
                posting.mark_needs_pass2(job["id"], conn=conn)
    except (db.TransitionError, db.ActorNotAllowed, LookupError) as exc:
        log.error("Details: job %d not stored: %s", job["id"], exc)
        result.errors.append(f"job {job['id']}: {exc}")
        return
    result.found += 1
    if official:
        result.official += 1
    else:
        result.aggregator += 1
    if close:
        result.closed += 1
    log.info("Details: job %d found (%s) at %s%s", job["id"], trust, details["source_url"],
             "; posting closed, job closed" if close else "")
    if profile is not None and not close and age.recheck(job["id"], profile, db_path=db_path):
        result.too_old += 1


def _not_found(conn, result: Result, job: dict[str, Any], why: str) -> None:
    with conn:
        db.set_details_not_found(job["id"], conn=conn)
    result.not_found += 1
    log.info("Details: job %d %s | %s: %s; kept", job["id"], job["company"], job["title"], why)


def _url(value: Any) -> str | None:
    if isinstance(value, str) and value.strip().lower().startswith(("http://", "https://")):
        return value.strip()[:2000]
    return None


def _amount(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not 1000 <= value <= 2_000_000:
        return None
    return int(value)


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="replace")
    parser = argparse.ArgumentParser(prog="python -m agents.scout.details", description=__doc__.split("\n")[0])
    parser.add_argument("--db", type=Path, default=None)
    parser.add_argument("--profile", type=Path, default=DEFAULT_PROFILE_PATH)
    parser.add_argument("--log-dir", type=Path, default=runlog.DEFAULT_LOG_DIR)
    parser.add_argument("--pool", type=int, default=POOL_SIZE, help=f"top N scored jobs to consider (default {POOL_SIZE})")
    parser.add_argument("--dry-run", action="store_true", help="list the jobs that would be looked up; call nothing")
    args = parser.parse_args(argv)

    with runlog.log_to(runlog.log_path("score", args.log_dir)) as log_file:
        result = fetch_details(db_path=args.db, pool_size=args.pool, dry_run=args.dry_run,
                               profile=load_profile(args.profile))
    print("\n".join(result.lines()))
    print(f"Log: {log_file}")
    return 1 if result.errors or result.stopped else 0


if __name__ == "__main__":
    sys.exit(main())
