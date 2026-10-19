"""Free ATS check through the public Greenhouse, Lever and Ashby job-board APIs.

    python -m agents.scout.ats_api JOB_ID [JOB_ID ...] [--refresh] [--dry-run]
    (.\\run.ps1 ats-check <id> [<id> ...])

For a company with no cached board, board names are guessed from its normalized name
(slug_guesses: "sitewirebyhexacorp", "sitewire", "sitewire-by-hexacorp", ...) and tried on

    Greenhouse  https://boards-api.greenhouse.io/v1/boards/{slug}/jobs
    Lever       https://api.lever.co/v0/postings/{slug}?mode=json
    Ashby       https://api.ashbyhq.com/posting-api/job-board/{slug}

A board only counts if it returns jobs and its company matches: Greenhouse names the
board (and each job's company_name); Lever and Ashby name no company, so the company's
name must appear in at least half of the board's job descriptions. The first board that
counts is cached in company_boards and ats_probes (hit); none is a miss, cached for
MISS_RETRY_DAYS. Then the job's title is matched against the board's (similarity >=
TITLE_MATCH, difflib) and its location checked. A match gives the verbatim posting with an
ATS source_url, so it's official.

No tokens, only plain HTTPS GETs to public read-only APIs. The details step (details.py)
runs this before any Claude lookup.
"""

from __future__ import annotations

import argparse
import html
import logging
import re
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import requests

from agents import db
from agents.common import runlog
from agents.scout import posting
from agents.scout.companies import normalize_company
from agents.scout.fetchers import _iso_date, _iso_date_from_ms

log = logging.getLogger(__name__)

APIS = ("greenhouse", "lever", "ashby")
TITLE_MATCH = 0.85
MISS_RETRY_DAYS = 30
MAX_SLUGS = 5
TIMEOUT_S = 20
_NATIONWIDE = {"", "united states", "us", "usa", "remote", "anywhere", "remote us", "united states remote"}
_BOARD_HOSTS = {"job-boards.greenhouse.io": "greenhouse", "boards.greenhouse.io": "greenhouse",
                "jobs.lever.co": "lever", "jobs.ashbyhq.com": "ashby"}


@dataclass
class Board:
    ats: str
    slug: str
    name: str | None  # the company name the API gives (Greenhouse only)
    jobs: list[dict[str, Any]]  # {id, title, location, remote, url, apply_url, text}

    @property
    def url(self) -> str:
        return {"greenhouse": f"https://job-boards.greenhouse.io/{self.slug}",
                "lever": f"https://jobs.lever.co/{self.slug}",
                "ashby": f"https://jobs.ashbyhq.com/{self.slug}"}[self.ats]


@dataclass
class ProbeResult:
    job_id: int
    company_key: str
    status: str  # hit, cached-hit, miss, cached-miss
    board: Board | None = None
    tried: list[str] = field(default_factory=list)
    match: dict[str, Any] | None = None  # the matched posting, with "ratio" and "description"
    best: dict[str, Any] | None = None  # the closest title when nothing matched
    note: str = ""

    def lines(self) -> list[str]:
        out = []
        if self.board:
            out.append(f"board: {self.board.ats}/{self.board.slug} ({self.status}), "
                       f"{len(self.board.jobs)} jobs, {self.board.url}")
        else:
            out.append(f"no board ({self.status})")
        if self.tried:
            out.append("tried: " + "; ".join(self.tried))
        if self.match:
            out.append(f"MATCH {self.match['ratio']:.2f}: {self.match['title']!r} ({self.match['location'] or 'no location'})"
                       f" {self.match['url']}")
        elif self.best:
            out.append(f"no title match; closest {self.best['ratio']:.2f}: {self.best['title']!r} "
                       f"({self.best['location'] or 'no location'})")
        if self.note:
            out.append(self.note)
        return out


def slug_guesses(company: str) -> list[str]:
    words = normalize_company(company).split()
    if not words:
        return []
    raw = re.sub(r"[^a-z0-9]+", "", (company or "").lower())
    first_two = "".join(words[:2]) if len(words) > 2 and words[1] not in {"by", "and", "of", "the", "for", "at"} else ""
    out = ["".join(words), words[0], "-".join(words), first_two, raw]
    seen: list[str] = []
    for s in out:
        if s and len(s) >= 2 and s not in seen:
            seen.append(s)
    return seen[:MAX_SLUGS]


def _get_json(session: Any, url: str) -> Any:
    resp = session.get(url, timeout=TIMEOUT_S, headers={"User-Agent": posting.USER_AGENT})
    if resp.status_code != 200:
        return None
    try:
        return resp.json()
    except ValueError:
        return None


def fetch_board(ats: str, slug: str, session: Any) -> Board | None:
    """The board's open jobs, or None when there's no such board (or it has no jobs)."""
    if ats == "greenhouse":
        data = _get_json(session, f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs")
        jobs = (data or {}).get("jobs") if isinstance(data, dict) else None
        if not jobs:
            return None
        meta = _get_json(session, f"https://boards-api.greenhouse.io/v1/boards/{slug}") or {}
        names = {j.get("company_name") for j in jobs if j.get("company_name")}
        name = meta.get("name") or (names.pop() if len(names) == 1 else None)
        return Board(ats, slug, name, [{
            "id": j.get("id"), "title": j.get("title") or "", "location": (j.get("location") or {}).get("name"),
            "remote": False, "url": f"https://job-boards.greenhouse.io/{slug}/jobs/{j.get('id')}",
            "apply_url": j.get("absolute_url"), "text": None,
            "posted_at": _iso_date(j.get("first_published") or j.get("updated_at"))} for j in jobs])
    if ats == "lever":
        data = _get_json(session, f"https://api.lever.co/v0/postings/{slug}?mode=json")
        if not isinstance(data, list) or not data:
            return None
        return Board(ats, slug, None, [{
            "id": p.get("id"), "title": p.get("text") or "",
            "location": "; ".join(p.get("categories", {}).get("allLocations") or [])
            or p.get("categories", {}).get("location"),
            "remote": (p.get("workplaceType") == "remote"), "url": p.get("hostedUrl"), "apply_url": p.get("applyUrl"),
            "text": _lever_html(p), "posted_at": _iso_date_from_ms(p.get("createdAt"))} for p in data])
    if ats == "ashby":
        data = _get_json(session, f"https://api.ashbyhq.com/posting-api/job-board/{slug}")
        jobs = [j for j in ((data or {}).get("jobs") or []) if j.get("isListed", True)] if isinstance(data, dict) else []
        if not jobs:
            return None
        return Board(ats, slug, None, [{
            "id": j.get("id"), "title": j.get("title") or "",
            "location": "; ".join([j.get("location") or ""] + [s.get("location") or "" for s in j.get("secondaryLocations") or []]).strip("; "),
            "remote": bool(j.get("isRemote")), "url": j.get("jobUrl"), "apply_url": j.get("applyUrl"),
            "text": j.get("descriptionHtml") or j.get("descriptionPlain"),
            "posted_at": _iso_date(j.get("publishedAt"))} for j in jobs])
    raise ValueError(f"unknown ATS {ats!r}")


def _lever_html(p: dict[str, Any]) -> str:
    parts = [p.get("description") or ""]
    for lst in p.get("lists") or []:
        parts.append(f"<h3>{html.escape(lst.get('text') or '')}</h3><ul>{lst.get('content') or ''}</ul>")
    parts.append(p.get("additional") or "")
    return "".join(parts)


def company_matches(company: str, board: Board) -> bool:
    """The board belongs to this company: Greenhouse's board name, or (Lever, Ashby) the
    company's name in at least half of the board's job descriptions."""
    key = normalize_company(company)
    if board.name:
        other = normalize_company(board.name)
        a, b = key.split(), other.split()
        return bool(a and b) and (key == other or "".join(a) == "".join(b)
                                  or (a[0] == b[0] and len(a[0]) >= 4))
    display = re.sub(r"\s+-\s+.*$", "", company or "").strip()  # "Northgate Industrial Systems - NIS"
    words = display.split()
    if not words:
        return False
    # Case-sensitive, so the company "Ascend" isn't confused with the verb "ascend".
    pattern = re.compile(r"\b" + r"\s+".join(map(re.escape, words if len(words) <= 2 else words[:2])) + r"\b")
    texts = [posting.html_to_text(j["text"]) for j in board.jobs if j.get("text")]
    hits = sum(1 for t in texts if pattern.search(t))
    return bool(texts) and hits * 2 >= len(texts)


_OTHER_COUNTRIES = (
    "canada", "uk", "united kingdom", "england", "ireland", "spain", "poland", "germany", "france", "netherlands",
    "portugal", "italy", "india", "mexico", "brazil", "argentina", "colombia", "australia", "new zealand",
    "singapore", "japan", "israel", "philippines", "romania", "ukraine", "sweden", "switzerland", "emea", "apac",
    "latam", "europe",
)


def _foreign(location: str) -> bool:
    """The location names a country (or region) other than the US, and not the US too."""
    loc = f" {posting.normalize_title(location)} "
    us = any(f" {w} " in loc for w in ("us", "usa", "united states", "u s"))
    return not us and any(f" {c} " in loc for c in _OTHER_COUNTRIES)


def location_score(job_location: str | None, posting_location: str | None, posting_remote: bool) -> int:
    """2: same city; 1: compatible (nationwide job, or a US/unspecified remote posting);
    0: not this job (another city, or another country). The jobs we see are US jobs."""
    have = posting.normalize_title(posting_location or "")
    if _foreign(posting_location or "") and not _foreign(job_location or ""):
        return 0
    city = posting.normalize_title((job_location or "").split(",")[0])
    if city and city not in _NATIONWIDE and f" {city} " in f" {have} ":
        return 2
    want = posting.normalize_title(job_location or "")
    if want in _NATIONWIDE or "remote" in want.split():
        return 1
    return 1 if (posting_remote or "remote" in have.split() or not have) else 0


def location_ok(job_location: str | None, posting_location: str | None, posting_remote: bool) -> bool:
    return location_score(job_location, posting_location, posting_remote) > 0


def best_match(job: dict[str, Any], board: Board) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """(match, closest): among postings with title similarity >= TITLE_MATCH and a
    compatible location, the best by similarity, then location (same city before a US
    remote copy; companies often post one title per country); and the closest title
    overall for the report."""
    want = posting.normalize_title(job["title"])
    scored = []
    for p in board.jobs:
        ratio = SequenceMatcher(None, want, posting.normalize_title(p["title"])).ratio()
        loc = location_score(job.get("location"), p.get("location"), p.get("remote"))
        scored.append({**p, "ratio": ratio, "location_score": loc})
    scored.sort(key=lambda p: (-round(p["ratio"], 2), -p["location_score"]))
    closest = scored[0] if scored else None
    for p in scored:
        if p["ratio"] >= TITLE_MATCH and p["location_score"] > 0:
            return p, closest
    return None, closest


def description_for(board: Board, p: dict[str, Any], session: Any) -> str:
    if board.ats == "greenhouse":
        data = _get_json(session, f"https://boards-api.greenhouse.io/v1/boards/{board.slug}/jobs/{p['id']}") or {}
        content = html.unescape(data.get("content") or "")
        return posting.html_to_text(content)
    return posting.html_to_text(p.get("text") or "")


def board_from_url(board_url: str) -> tuple[str, str] | None:
    """(ats, slug) for a cached board on Greenhouse, Lever or Ashby."""
    parts = urlsplit(board_url)
    ats = _BOARD_HOSTS.get((parts.hostname or "").lower())
    path = [p for p in parts.path.split("/") if p]
    return (ats, path[0]) if ats and path else None


def probe_job(job: dict[str, Any], *, session: Any = None, db_path: Path | str | None = None,
              refresh: bool = False, now: datetime | None = None) -> ProbeResult:
    session = session or requests.Session()
    key = normalize_company(job["company"])
    result = ProbeResult(job_id=job["id"], company_key=key, status="miss")
    now = now or datetime.now(timezone.utc)

    board = None
    cached_board = db.get_company_board(key, db_path=db_path)
    api_board = board_from_url(cached_board["board_url"]) if cached_board else None
    cached = db.get_ats_probe(key, db_path=db_path)
    if api_board:
        board = fetch_board(*api_board, session)
        result.status = "cached-hit"
        if board is None:
            result.note = f"cached board {cached_board['board_url']} returned no jobs"
            return result
    elif cached and not refresh and cached["result"] == "hit" and cached["ats"] and cached["slug"]:
        board = fetch_board(cached["ats"], cached["slug"], session)
        result.status = "cached-hit"
    elif (cached and not refresh and cached["result"] == "miss"
          and now - datetime.fromisoformat(cached["checked_at"]) < timedelta(days=MISS_RETRY_DAYS)):
        result.status = "cached-miss"
        result.tried = cached["tried"]
        return result
    else:
        for slug in slug_guesses(job["company"]):
            for ats in APIS:
                try:
                    found = fetch_board(ats, slug, session)
                except requests.RequestException as exc:
                    result.tried.append(f"{ats}/{slug}: error {type(exc).__name__}")
                    continue
                if found is None:
                    result.tried.append(f"{ats}/{slug}: none")
                    continue
                if not company_matches(job["company"], found):
                    who = f"board name {found.name!r}" if found.name else "company not named in its postings"
                    result.tried.append(f"{ats}/{slug}: {len(found.jobs)} jobs, {who}: not this company")
                    continue
                result.tried.append(f"{ats}/{slug}: {len(found.jobs)} jobs, company matches")
                board = found
                break
            if board:
                break
        if board:
            result.status = "hit"
            db.set_ats_probe(key, job["company"], "hit", result.tried, board.ats, board.slug, db_path=db_path)
            db.set_company_board(key, job["company"], board.url, "lookup", db_path=db_path)
        else:
            db.set_ats_probe(key, job["company"], "miss", result.tried, db_path=db_path)
            return result

    if board is None:
        return result
    result.board = board
    match, closest = best_match(job, board)
    result.best = closest
    if match:
        match["description"] = description_for(board, match, session)
        result.match = match
    return result


def store_match(job: dict[str, Any], result: ProbeResult, *, conn: Any) -> None:
    """Store a matched posting as official (it's on the ATS) and flag pass 2."""
    m = result.match
    db.store_details(job["id"], description=m["description"], apply_url=m.get("apply_url") or m["url"],
                     source_url=m["url"], years_required=job.get("years_required"), source_trust="official",
                     posted_at=m.get("posted_at"), posted_at_source="ats", conn=conn)
    db.log_event(job["id"], "ats_api_match", "scout",
                 detail=f"{result.board.ats}/{result.board.slug}: {m['title']} ({m['ratio']:.2f})", conn=conn)
    posting.mark_needs_pass2(job["id"], conn=conn)


def main(argv: list[str] | None = None) -> int:
    from agents.scout import pass2
    from agents.scout.companies import DEFAULT_PREFERRED_PATH, load_preferred
    from agents.scout.filters import DEFAULT_PROFILE_PATH, load_profile

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="replace")
    parser = argparse.ArgumentParser(prog="python -m agents.scout.ats_api",
                                     description="Free ATS check through the Greenhouse, Lever and Ashby APIs")
    parser.add_argument("job_ids", type=int, nargs="+")
    parser.add_argument("--refresh", action="store_true", help="ignore a cached miss")
    parser.add_argument("--dry-run", action="store_true", help="report only; store nothing (probe results are still cached)")
    parser.add_argument("--db", type=Path, default=None)
    parser.add_argument("--profile", type=Path, default=DEFAULT_PROFILE_PATH)
    parser.add_argument("--preferred", type=Path, default=DEFAULT_PREFERRED_PATH)
    parser.add_argument("--log-dir", type=Path, default=runlog.DEFAULT_LOG_DIR)
    args = parser.parse_args(argv)

    db.init_db(args.db)
    session = requests.Session()
    matched = []
    with runlog.log_to(runlog.log_path("score", args.log_dir)) as log_file:
        for job_id in args.job_ids:
            job = db.get_job(job_id, db_path=args.db)
            if job is None:
                print(f"#{job_id}: no such job")
                continue
            r = probe_job(job, session=session, db_path=args.db, refresh=args.refresh)
            print(f"#{job_id} {job['company']}: {job['title']} ({job.get('location') or 'no location'})")
            for line in r.lines():
                print(f"  {line}")
                log.info("ATS check job %d: %s", job_id, line)
            if r.match and not args.dry_run:
                conn = db.connect(args.db)
                try:
                    with conn:
                        store_match(job, r, conn=conn)
                finally:
                    conn.close()
                matched.append(job_id)
                print(f"  stored as official ({len(r.match['description']):,} characters, verbatim)")
        if matched:
            result = pass2.rescore(load_profile(args.profile), load_preferred(args.preferred), db_path=args.db,
                                   job_ids=matched)
            for job_id in matched:
                job = db.get_job(job_id, db_path=args.db)
                print(f"#{job_id} pass 2: score now {job['score']}")
            print("\n".join(result.lines()))
    print(f"Log: {log_file}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
