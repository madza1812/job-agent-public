"""Official postings fetched with plain HTTP (no Claude, no tokens).

    python -m agents.scout.posting <job_id> <url> [--db PATH] [--profile PATH] [--preferred PATH]
    (.\\run.ps1 set-posting <job_id> <url>)

set-posting: the user gives a job's official posting URL. It's fetched once, and its
verbatim description (schema.org JobPosting JSON-LD when the page has it, else the page's
description element) is stored with apply_url = source_url = the URL, source_trust =
official and needs_details = 0. A salary stated in the JSON-LD is stored too. Workable,
BambooHR and Breezy postings render with JavaScript (or hide their text from the page
parser), so they're read from the ATS's public JSON instead (api_posting), which also
gives the location, the published date and remote (office_days 0). Then pass 2
reruns for that job, and its brief is rewritten if it has one. The company's job board
(derived from the URL, see board_url_for) is cached in company_boards, so the details step
finds the company's later postings with the free board check (find_on_board) before it
spends a web search.

Pages are only read: never a form submitted, never a tracking link, never a domain in
blocked_fetch_domains (config/source_trust.yaml).
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import sys
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlsplit

import requests
from bs4 import BeautifulSoup

from agents import db
from agents.common import runlog
from agents.scout import source_trust
from agents.scout.companies import normalize_company

log = logging.getLogger(__name__)

TIMEOUT_S = 30
USER_AGENT = "Mozilla/5.0 (personal job-search assistant; reads public postings only)"
MIN_DESCRIPTION_CHARS = 200
MAX_DESCRIPTION_CHARS = 20000
MAX_SAME_TITLE = 3  # postings fetched to pick by location when a board lists the title more than once
_BLOCK_TAGS = ("p", "div", "br", "li", "ul", "ol", "h1", "h2", "h3", "h4", "h5", "h6", "tr", "section")


class PostingError(Exception):
    pass


def get(url: str, session: Any = None) -> str:
    check_url(url)
    s = session or requests
    resp = s.get(url, timeout=TIMEOUT_S, headers={"User-Agent": USER_AGENT})
    if resp.status_code != 200:
        raise PostingError(f"{url}: HTTP {resp.status_code}")
    return resp.text


def check_url(url: str) -> None:
    parts = urlsplit(url or "")
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise PostingError(f"not an http(s) URL: {url!r}")
    host = parts.hostname.lower()
    if any(host == d or host.endswith("." + d) for d in source_trust.load_config()["blocked_fetch_domains"]):
        raise PostingError(f"{host} is never fetched (blocked_fetch_domains)")


def html_to_text(html: str) -> str:
    """Plain text that keeps the posting's paragraphs and bullets."""
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()
    for li in soup.find_all("li"):
        li.insert(0, "- ")
    for tag in soup.find_all(_BLOCK_TAGS):
        tag.insert_after("\n")
    text = soup.get_text()
    lines = [re.sub(r"[ \t\xa0]+", " ", line).strip() for line in text.splitlines()]
    out = re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()
    return out[:MAX_DESCRIPTION_CHARS]


def parse_posting(html: str, url: str) -> dict[str, Any]:
    """{title, description, location, salary, posted_at, posted_at_source, office_days} from
    a posting page (posted_at from page_date). A page gives no office days (pass 2 reads them
    from the text)."""
    soup = BeautifulSoup(html, "html.parser")
    ld = _job_posting_ld(soup)
    title = (ld.get("title") if ld else None) or (soup.title.get_text(strip=True) if soup.title else "")
    if ld and ld.get("description"):
        description = html_to_text(str(ld["description"]))
    else:
        el = soup.select_one("#job-description, .job-description, #job_description, #content .job__description, "
                             ".posting-page .section-wrapper, [data-automation-id=jobPostingDescription], main")
        description = html_to_text(str(el)) if el else ""
    if len(description) < MIN_DESCRIPTION_CHARS:
        raise PostingError(f"{url}: no job description found on the page ({len(description)} characters)")
    return {"title": str(title).strip(), "description": description,
            "location": _ld_location(ld) if ld else None, "salary": _ld_salary(ld) if ld else None,
            "posted_at": page_date(soup), "posted_at_source": "posting_page", "office_days": None}


# Page metadata naming when a page was published, after JSON-LD's datePosted.
_META_DATE_ATTRS = (("property", "article:published_time"), ("property", "og:published_time"),
                    ("name", "datePosted"), ("name", "date"), ("name", "publish_date"),
                    ("name", "publication_date"), ("name", "dcterms.date"), ("name", "DC.date.issued"),
                    ("itemprop", "datePosted"), ("itemprop", "datePublished"))


def page_date(page: str | BeautifulSoup) -> str | None:
    """The date a posting page says it was posted (YYYY-MM-DD): JSON-LD datePosted, else a
    published-date meta tag or itemprop. None when the page doesn't say."""
    soup = page if isinstance(page, BeautifulSoup) else BeautifulSoup(page, "html.parser")
    ld = _job_posting_ld(soup)
    if ld and _date(ld.get("datePosted")):
        return _date(ld.get("datePosted"))
    for attr, value in _META_DATE_ATTRS:
        for tag in soup.find_all(attrs={attr: value}):
            found = _date(tag.get("content") or tag.get("datetime") or tag.get_text(strip=True))
            if found:
                return found
    return None


def fetch_posting(url: str, session: Any = None) -> dict[str, Any]:
    """The posting at url: from the ATS's JSON API for a JavaScript-rendered board
    (api_posting), else from the page."""
    return api_posting(url, session) or parse_posting(get(url, session), url)


# --- JavaScript-rendered boards: public JSON instead of the page ----------------------
# Workable and BambooHR pages are empty without JavaScript, and Breezy's description sits
# where no page parser looks, so their postings and board listings come from each ATS's
# public JSON. Greenhouse, Lever and Ashby pages carry JSON-LD (and ats_api reads their
# boards), and Recruitee and JazzHR render on the server, so those are read as HTML.

_WORKABLE_JOB = re.compile(r"^/([^/]+)/j/([0-9A-Za-z]+)")
_BAMBOO_JOB = re.compile(r"^/careers/(\d+)")
_BREEZY_JOB = re.compile(r"^/p/([0-9a-f]+)")


def api_url_for(url: str) -> str | None:
    """The public JSON endpoint for a posting URL on a JavaScript-rendered board, or None."""
    parts = urlsplit(url or "")
    host, path = (parts.hostname or "").lower(), parts.path
    if host == "apply.workable.com" and (m := _WORKABLE_JOB.match(path)):
        return f"https://apply.workable.com/api/v2/accounts/{m.group(1)}/jobs/{m.group(2)}"
    if host.endswith(".bamboohr.com") and (m := _BAMBOO_JOB.match(path)):
        return f"https://{host}/careers/{m.group(1)}/detail"
    if host.endswith(".breezy.hr") and _BREEZY_JOB.match(path):
        return f"https://{host}/json?verbose=true"  # the whole board; the job is picked by id
    return None


def get_json(url: str, session: Any = None) -> Any:
    check_url(url)
    s = session or requests
    resp = s.get(url, timeout=TIMEOUT_S, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
    if resp.status_code != 200:
        raise PostingError(f"{url}: HTTP {resp.status_code}")
    try:
        return resp.json()
    except ValueError as exc:
        raise PostingError(f"{url}: not JSON") from exc


def api_posting(url: str, session: Any = None) -> dict[str, Any] | None:
    """parse_posting()'s fields from the ATS's JSON, or None when url has no JSON endpoint.
    office_days is 0 when the ATS marks the job remote, else None."""
    api = api_url_for(url)
    if not api:
        return None
    data = get_json(api, session)
    host = (urlsplit(url).hostname or "").lower()
    if host == "apply.workable.com":
        posting = _workable_posting(data)
    elif host.endswith(".bamboohr.com"):
        posting = _bamboo_posting(data)
    else:
        job_id = _BREEZY_JOB.match(urlsplit(url).path).group(1)
        item = next((p for p in data if isinstance(p, dict) and p.get("id") == job_id), None)
        if item is None:
            raise PostingError(f"{url}: not on the board's JSON listing (closed?)")
        posting = _breezy_posting(item)
    if len(posting["description"]) < MIN_DESCRIPTION_CHARS:
        raise PostingError(f"{api}: no job description ({len(posting['description'])} characters)")
    return posting


def _place(*parts: Any) -> str:
    return ", ".join(str(p).strip() for p in parts if p and str(p).strip())


def _with_workplace(places: list[str], workplace: str | None) -> tuple[str | None, int | None]:
    """The location text, each place labeled "(Remote)" or "(Hybrid)", and office days
    (0 for remote; hybrid and on-site days are left to pass 2)."""
    label = {"remote": "Remote", "hybrid": "Hybrid"}.get(workplace or "")
    places = [p for p in places if p] or (["United States"] if label else [])
    text = "; ".join(f"{p} ({label})" if label else p for p in places) or None
    return text, 0 if workplace == "remote" else None


def _date(value: Any) -> str | None:
    text = str(value or "")[:10]
    return text if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text) else None


def _workable_posting(data: dict[str, Any]) -> dict[str, Any]:
    locs = [loc for loc in data.get("locations") or [] if not loc.get("hidden")] or [data.get("location") or {}]
    workplace = data.get("workplace") or ("remote" if data.get("remote") else None)
    location, office_days = _with_workplace(
        [_place(loc.get("city"), loc.get("region"), loc.get("country")) for loc in locs], workplace)
    html = data.get("description") or ""
    if data.get("requirements"):
        html += "<h3>Requirements</h3>" + data["requirements"]
    return {"title": str(data.get("title") or "").strip(), "description": html_to_text(html), "location": location,
            "salary": None, "posted_at": _date(data.get("published")), "posted_at_source": "ats",
            "office_days": office_days}


_BAMBOO_WORKPLACE = {"0": "on_site", "1": "remote", "2": "hybrid"}


def _bamboo_posting(data: dict[str, Any]) -> dict[str, Any]:
    job = (data.get("result") or {}).get("jobOpening") or {}
    loc = job.get("atsLocation") or {}
    if not any(loc.get(k) for k in ("city", "state", "country")):
        loc = {**(job.get("location") or {}), "country": (job.get("location") or {}).get("addressCountry")}
    workplace = "remote" if job.get("isRemote") is True else _BAMBOO_WORKPLACE.get(str(job.get("locationType")))
    location, office_days = _with_workplace([_place(loc.get("city"), loc.get("state"), loc.get("country"))], workplace)
    return {"title": str(job.get("jobOpeningName") or "").strip(), "description": html_to_text(job.get("description") or ""),
            "location": location, "salary": None, "posted_at": _date(job.get("datePosted")), "posted_at_source": "ats",
            "office_days": office_days}


def _breezy_posting(item: dict[str, Any]) -> dict[str, Any]:
    locs = item.get("locations") or [item.get("location") or {}]
    first = item.get("location") or locs[0]
    remote = (first.get("remote_details") or {}).get("value")
    workplace = remote if remote in ("remote", "hybrid") else ("remote" if first.get("is_remote") else None)
    location, office_days = _with_workplace([str(loc.get("name") or "") for loc in locs], workplace)
    return {"title": str(item.get("name") or "").strip(), "description": html_to_text(item.get("description") or ""),
            "location": location, "salary": None, "posted_at": _date(item.get("published_date")),
            "posted_at_source": "ats", "office_days": office_days}


def board_listing(board_url: str, session: Any = None) -> list[tuple[str, str, str | None]] | None:
    """[(title, posting URL, location)] from the board's JSON listing, or None for a board
    that's read as HTML."""
    parts = urlsplit(board_url)
    host = (parts.hostname or "").lower()
    path = [p for p in parts.path.split("/") if p]
    if host == "apply.workable.com" and path:
        data = get_json(f"https://apply.workable.com/api/v1/widget/accounts/{path[0]}", session)
        return [(j.get("title") or "", f"https://apply.workable.com/{path[0]}/j/{j.get('shortcode')}/",
                 _place(j.get("city"), j.get("state"), j.get("country"))) for j in data.get("jobs") or []]
    if host.endswith(".bamboohr.com"):
        data = get_json(f"https://{host}/careers/list", session)
        return [(j.get("jobOpeningName") or "", f"https://{host}/careers/{j.get('id')}",
                 _place(*((j.get("atsLocation") or {}).get(k) for k in ("city", "state", "country"))))
                for j in data.get("result") or []]
    if host.endswith(".breezy.hr"):
        data = get_json(f"https://{host}/json", session)
        return [(p.get("name") or "", p.get("url") or "", (p.get("location") or {}).get("name"))
                for p in data if isinstance(p, dict) and p.get("url")]
    return None


def _job_posting_ld(soup: BeautifulSoup) -> dict[str, Any] | None:
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(script.string or "")
        except json.JSONDecodeError:
            continue
        for item in data if isinstance(data, list) else data.get("@graph", [data]) if isinstance(data, dict) else []:
            if isinstance(item, dict) and item.get("@type") == "JobPosting":
                return item
    return None


def _ld_location(ld: dict[str, Any]) -> str | None:
    locs = ld.get("jobLocation")
    locs = locs if isinstance(locs, list) else [locs] if locs else []
    out = []
    for loc in locs:
        addr = (loc or {}).get("address") if isinstance(loc, dict) else None
        if isinstance(addr, dict):
            out.append(", ".join(str(addr[k]) for k in ("addressLocality", "addressRegion") if addr.get(k)))
    return "; ".join(o for o in out if o) or None


def _ld_salary(ld: dict[str, Any]) -> tuple[int | None, int | None, str] | None:
    base = ld.get("baseSalary")
    if not isinstance(base, dict):
        return None
    value = base.get("value") if isinstance(base.get("value"), dict) else {}
    unit = str(value.get("unitText") or base.get("unitText") or "YEAR").upper()
    factor = {"YEAR": 1, "HOUR": 2080, "MONTH": 12, "WEEK": 52}.get(unit)
    if factor is None:
        return None

    def amount(v: Any) -> int | None:
        try:
            n = float(v) * factor
        except (TypeError, ValueError):
            return None
        return int(n) if 1000 <= n <= 2_000_000 else None

    lo = amount(value.get("minValue") if value else None)
    hi = amount(value.get("maxValue") if value else None) or amount(value.get("value") if value else None)
    if not lo and not hi:
        return None
    return lo, hi, str(base.get("currency") or "USD")[:3].upper()


# --- job boards --------------------------------------------------------------------

def board_url_for(url: str) -> str | None:
    """The job board (listing page) an ATS posting URL belongs to, or None when it can't
    be told (company career sites, JavaScript-only boards such as Workday)."""
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    path = [p for p in parts.path.split("/") if p]
    root = f"https://{host}/"
    if host.endswith(".applytojob.com") or host.endswith(".breezy.hr") or host.endswith(".recruitee.com"):
        return root
    if host.endswith(".bamboohr.com"):
        return f"https://{host}/careers"
    if host in ("boards.greenhouse.io", "job-boards.greenhouse.io") and path:
        return f"https://job-boards.greenhouse.io/{path[0]}"
    if host == "jobs.lever.co" and path:
        return f"https://jobs.lever.co/{path[0]}"
    if host == "jobs.ashbyhq.com" and path:
        return f"https://jobs.ashbyhq.com/{path[0]}"
    if host == "apply.workable.com" and path:
        return f"https://apply.workable.com/{path[0]}/"
    if host == "jobs.smartrecruiters.com" and path:
        return f"https://jobs.smartrecruiters.com/{path[0]}"
    return None


def normalize_title(title: str) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", (title or "").lower()))


def find_on_board(board_url: str, title: str, location: str | None = None,
                  session: Any = None) -> str | None:
    """The URL of the posting with exactly this title on the board, or None. When the
    title appears more than once, the posting whose location matches wins; still unsure
    means None (the lookup handles it). JavaScript-rendered boards are read from their
    JSON listing (board_listing)."""
    want = normalize_title(title)
    city = normalize_title((location or "").split(",")[0])
    listing = board_listing(board_url, session)
    if listing is not None:
        found = [(url, loc) for t, url, loc in listing if normalize_title(t) == want]
        if len(found) <= 1:
            return found[0][0] if found else None
        return next((url for url, loc in found if city and city in normalize_title(loc or "")), None)
    soup = BeautifulSoup(get(board_url, session), "html.parser")
    host = urlsplit(board_url).hostname
    matches: list[str] = []
    for a in soup.find_all("a", href=True):
        texts = {normalize_title(a.get_text(" ", strip=True))}
        texts |= {normalize_title(t) for t in a.stripped_strings}
        if want not in texts:
            continue
        href = urljoin(board_url, a["href"])
        if urlsplit(href).hostname == host and href not in matches:
            matches.append(href)
    if len(matches) <= 1:
        return matches[0] if matches else None
    if not city:
        return None
    for href in matches[:MAX_SAME_TITLE]:
        try:
            posting = fetch_posting(href, session)
        except (PostingError, requests.RequestException):
            continue
        if city in normalize_title(posting.get("location") or ""):
            return href
    return None


def cache_board(job: dict[str, Any], url: str, source: str, *, db_path: Path | str | None = None,
                conn: Any = None) -> str | None:
    board = board_url_for(url)
    if board:
        db.set_company_board(normalize_company(job["company"]), job["company"], board, source,
                             db_path=db_path, conn=conn)
    return board


# --- set-posting ---------------------------------------------------------------------

def set_posting(job_id: int, url: str, *, profile: dict[str, Any], preferred: list[dict[str, Any]],
                db_path: Path | str | None = None, session: Any = None, pass2_runner: Any = None,
                research_kwargs: dict[str, Any] | None = None) -> list[str]:
    """Store the official posting at url for the job, rescore it (pass 2), check its age
    with the posting's date (age.recheck: a posting too old closes the job) and rewrite its
    brief. Returns report lines."""
    from agents.research import brief
    from agents.scout import pass2

    db.init_db(db_path)
    job = db.get_job(job_id, db_path=db_path)
    if job is None:
        raise LookupError(f"no job with id {job_id}")
    if job["status"] == "closed":
        raise PostingError(f"job {job_id} is closed")
    posting = fetch_posting(url, session)
    api = api_url_for(url)
    lines = [f"Fetched {api or url}: {posting['title']!r}, {len(posting['description']):,} characters"
             + (f", {posting['location']}" if posting.get("location") else "")
             + (f", published {posting['posted_at']}" if posting.get("posted_at") else "")]
    if normalize_title(posting["title"]) != normalize_title(job["title"]):
        lines.append(f"  note: the page's title differs from the job's ({job['title']!r})")

    conn = db.connect(db_path)
    try:
        with conn:
            db.store_details(job_id, description=posting["description"], apply_url=url, source_url=url,
                             years_required=job.get("years_required"), source_trust=source_trust.OFFICIAL,
                             salary=posting["salary"], location=posting.get("location"),
                             posted_at=posting.get("posted_at"), posted_at_source=posting.get("posted_at_source"),
                             office_days=posting.get("office_days"), conn=conn)
            db.log_event(job_id, "posting_set", "user", detail=url, conn=conn)
            board = cache_board(job, url, "user", conn=conn)
            mark_needs_pass2(job_id, conn=conn)
    finally:
        conn.close()
    lines.append(f"  stored: source_trust=official, needs_details=0, apply_url={url}"
                 + (f", salary {posting['salary']}" if posting["salary"] else ", no salary stated"))
    lines.append(f"  board cached for {normalize_company(job['company'])!r}: {board}" if board
                 else "  board: can't be derived from this URL; nothing cached")

    kwargs = {"runner": pass2_runner} if pass2_runner else {}
    p2 = pass2.rescore(profile, preferred, db_path=db_path, job_ids=[job_id], **kwargs)
    after = db.get_job(job_id, db_path=db_path)
    detail = json.loads(after["score_detail"] or "{}")
    lines.append(f"  pass 2: {job['score']} -> {after['score']}; possibly_closed={detail.get('possibly_closed')}; "
                 f"red flags: {'; '.join(detail.get('red_flags') or []) or 'none'}")
    lines += ["  " + line for line in p2.lines()[1:]]
    if p2.errors or p2.stopped:
        lines.append("  pass 2 failed; run .\\run.ps1 pass2 later")

    from agents.scout import age
    after = db.get_job(job_id, db_path=db_path)
    lines.append(f"  date: {age.label(after)}")
    closed = age.recheck(job_id, profile, db_path=db_path)
    if closed:
        lines.append(f"  closed: {closed} (limit {age.limit_for(after, profile)} days); "
                     f".\\run.ps1 reopen {job_id} \"reason\" takes it back")
        return lines

    if after.get("brief_path"):
        r = brief.research(job_id, profile=profile, db_path=db_path, any_status=True, **(research_kwargs or {}))
        lines += ["  " + line for line in r.lines()]
    return lines


def mark_needs_pass2(job_id: int, *, conn: Any) -> None:
    """A new description makes the old pass 2 stale: flag it so pass 2 takes the job again."""
    job = db.get_job(job_id, conn=conn)
    if job and job.get("score_detail"):
        detail = json.loads(job["score_detail"])
        detail["needs_pass2"] = True
        db.set_score(job_id, job["score"], detail, conn=conn)


def main(argv: list[str] | None = None) -> int:
    from agents.scout.companies import DEFAULT_PREFERRED_PATH, load_preferred
    from agents.scout.filters import DEFAULT_PROFILE_PATH, load_profile

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="replace")
    parser = argparse.ArgumentParser(prog="python -m agents.scout.posting", description="Set a job's official posting URL")
    parser.add_argument("job_id", type=int)
    parser.add_argument("url")
    parser.add_argument("--db", type=Path, default=None)
    parser.add_argument("--profile", type=Path, default=DEFAULT_PROFILE_PATH)
    parser.add_argument("--preferred", type=Path, default=DEFAULT_PREFERRED_PATH)
    parser.add_argument("--log-dir", type=Path, default=runlog.DEFAULT_LOG_DIR)
    args = parser.parse_args(argv)
    with runlog.log_to(runlog.log_path("score", args.log_dir)) as log_file:
        try:
            lines = set_posting(args.job_id, args.url, profile=load_profile(args.profile),
                                preferred=load_preferred(args.preferred), db_path=args.db)
        except (PostingError, LookupError, requests.RequestException) as exc:
            print(f"Error: {exc}")
            return 2
    print("\n".join(lines))
    print(f"Log: {log_file}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
