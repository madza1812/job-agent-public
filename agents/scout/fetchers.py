"""Fetch postings from the public Greenhouse, Lever and Ashby board APIs (no auth).

Each fetch_* function returns a list of dicts in one normalized shape, shared
with agents.scout.email_alerts:

    id           "{source}:{slug}:{native_id}"
    source       "greenhouse" | "lever" | "ashby" (email alerts: "email:linkedin" etc.)
    company      company name (Greenhouse provides one; otherwise the caller's or the slug)
    title
    location     primary location, plus any extra locations joined with "; "
    remote       1, 0, or None when the posting doesn't say
    salary_min   annual base salary, or None
    salary_max   annual base salary, or None
    salary_currency  ISO code for salary_min/max, or None
    salary_is_predicted  1 if the salary is a job site's estimate rather than the employer's
    url          public posting page
    description  plain text, HTML stripped
    posted_at    ISO date (YYYY-MM-DD, UTC)

Field names below come from real responses inspected on 2026-10-14 (samples in
tests/fixtures/). Only annual salaries are kept; hourly ranges are left as None
rather than converted. No auth, no logged-in pages.
"""

from __future__ import annotations

import html
import logging
import re
import time
from datetime import datetime, timezone
from typing import Any, Callable

import requests
from bs4 import BeautifulSoup

log = logging.getLogger(__name__)

USER_AGENT = "job-agent/0.1 (personal job-search script)"
TIMEOUT_SECONDS = 20
RETRIES = 2  # retries after the first attempt, so up to 3 requests in total
BACKOFF_SECONDS = 2.0
_RETRY_STATUSES = {429, 500, 502, 503, 504}

GREENHOUSE_URL = "https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true"
LEVER_URL = "https://api.lever.co/v0/postings/{slug}?mode=json"
ASHBY_URL = "https://api.ashbyhq.com/posting-api/job-board/{slug}?includeCompensation=true"

Job = dict[str, Any]


class FetchError(RuntimeError):
    """A job board request failed after all retries, or returned something unusable."""


# --- HTTP ---------------------------------------------------------------------


def _get_json(
    url: str,
    session: requests.Session | None = None,
    sleep: Callable[[float], None] | None = None,
) -> Any:
    """GET url and decode JSON, retrying timeouts, connection errors, 429 and 5xx."""
    http = session or requests.Session()
    sleep = sleep or time.sleep

    last_error: str | None = None
    retry_after: float | None = None
    for attempt in range(RETRIES + 1):
        if attempt:
            delay = BACKOFF_SECONDS * 2 ** (attempt - 1)
            if retry_after is not None:
                delay = max(delay, retry_after)
            log.info("Retrying %s in %.1fs (attempt %d of %d)", url, delay, attempt + 1, RETRIES + 1)
            sleep(delay)
        retry_after = None
        try:
            resp = http.get(
                url,
                headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
                timeout=TIMEOUT_SECONDS,
            )
        except (requests.Timeout, requests.ConnectionError) as exc:
            last_error = f"{type(exc).__name__}: {exc}"
            log.warning("Request to %s failed: %s", url, last_error)
            continue
        if resp.status_code in _RETRY_STATUSES:
            last_error = f"HTTP {resp.status_code}"
            retry_after = _retry_after(resp)
            log.warning("Request to %s returned HTTP %d", url, resp.status_code)
            continue
        if resp.status_code != 200:
            raise FetchError(f"GET {url} returned HTTP {resp.status_code}")
        try:
            return resp.json()
        except ValueError:
            raise FetchError(f"GET {url} did not return JSON") from None
    raise FetchError(f"GET {url} failed after {RETRIES + 1} attempts: {last_error}")


def _retry_after(resp: Any) -> float | None:
    try:
        return float(resp.headers.get("Retry-After", ""))
    except ValueError:
        return None


# --- Public fetchers ----------------------------------------------------------


def fetch_greenhouse(slug: str, company: str | None = None, session: requests.Session | None = None) -> list[Job]:
    return parse_greenhouse(_get_json(GREENHOUSE_URL.format(slug=slug), session), slug, company)


def fetch_lever(slug: str, company: str | None = None, session: requests.Session | None = None) -> list[Job]:
    return parse_lever(_get_json(LEVER_URL.format(slug=slug), session), slug, company)


def fetch_ashby(slug: str, company: str | None = None, session: requests.Session | None = None) -> list[Job]:
    return parse_ashby(_get_json(ASHBY_URL.format(slug=slug), session), slug, company)


# --- Parsers (pure, used by the offline tests) --------------------------------


def parse_greenhouse(data: dict[str, Any], slug: str, company: str | None = None) -> list[Job]:
    jobs = []
    for p in data.get("jobs", []):
        location = _clean((p.get("location") or {}).get("name"))
        jobs.append(
            _job(
                ats="greenhouse",
                slug=slug,
                native_id=p["id"],
                company=company or _clean(p.get("company_name")) or slug,
                title=p.get("title"),
                location=location,
                remote=_greenhouse_remote(p, location),
                # The board API has no structured pay field; ranges only appear in the HTML body.
                salary=(None, None, None),
                url=p.get("absolute_url"),
                # content is HTML that has been entity-escaped a second time.
                description=html_to_text(html.unescape(p.get("content") or "")),
                posted_at=_iso_date(p.get("first_published") or p.get("updated_at")),
            )
        )
    return jobs


def parse_lever(data: list[dict[str, Any]], slug: str, company: str | None = None) -> list[Job]:
    jobs = []
    for p in data:
        cats = p.get("categories") or {}
        jobs.append(
            _job(
                ats="lever",
                slug=slug,
                native_id=p["id"],
                company=company or slug,
                title=p.get("text"),
                location=_join_locations(cats.get("location"), cats.get("allLocations") or []),
                remote=_workplace_remote(p.get("workplaceType")),
                salary=_lever_salary(p.get("salaryRange")),
                url=p.get("hostedUrl"),
                description=_lever_description(p),
                posted_at=_iso_date_from_ms(p.get("createdAt")),
            )
        )
    return jobs


def parse_ashby(data: dict[str, Any], slug: str, company: str | None = None) -> list[Job]:
    jobs = []
    for p in data.get("jobs", []):
        if not p.get("isListed"):
            continue
        secondary = [s.get("location") for s in p.get("secondaryLocations") or []]
        jobs.append(
            _job(
                ats="ashby",
                slug=slug,
                native_id=p["id"],
                company=company or slug,
                title=p.get("title"),
                location=_join_locations(p.get("location"), secondary),
                remote=_ashby_remote(p),
                salary=_ashby_salary(p.get("compensation")),
                url=p.get("jobUrl"),
                description=_normalize_text(p.get("descriptionPlain") or "")
                or html_to_text(p.get("descriptionHtml") or ""),
                posted_at=_iso_date(p.get("publishedAt")),
            )
        )
    return jobs


# --- Helpers ------------------------------------------------------------------


def _job(
    *,
    ats: str,
    slug: str,
    native_id: Any,
    company: str,
    title: str | None,
    location: str | None,
    remote: int | None,
    salary: tuple[int | None, int | None, str | None],
    url: str | None,
    description: str,
    posted_at: str | None,
    salary_is_predicted: bool = False,
) -> Job:
    salary_min, salary_max, currency = salary
    return {
        "id": f"{ats}:{slug}:{native_id}",
        "source": ats,
        "company": company,
        "title": _clean(title),
        "location": location,
        "remote": remote,
        "salary_min": salary_min,
        "salary_max": salary_max,
        "salary_currency": currency,
        "salary_is_predicted": int(bool(salary_is_predicted)),
        "url": url,
        "description": description,
        "posted_at": posted_at,
    }


def html_to_text(markup: str) -> str:
    if not markup:
        return ""
    return _normalize_text(BeautifulSoup(markup, "html.parser").get_text("\n"))


def _normalize_text(text: str) -> str:
    lines = [" ".join(line.replace("\xa0", " ").split()) for line in text.splitlines()]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


def _clean(value: str | None) -> str | None:
    if value is None:
        return None
    value = " ".join(value.split())
    return value or None


def _join_locations(primary: str | None, others: list[str | None]) -> str | None:
    seen: list[str] = []
    for loc in [primary, *others]:
        loc = _clean(loc)
        if loc and loc not in seen:
            seen.append(loc)
    return "; ".join(seen) or None


def _iso_date(value: str | None) -> str | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        log.warning("Unparseable date %r", value)
        return None
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc)
    return dt.date().isoformat()


def _iso_date_from_ms(value: int | None) -> str | None:
    if value is None:
        return None
    return datetime.fromtimestamp(value / 1000, tz=timezone.utc).date().isoformat()


def _workplace_remote(value: str | None) -> int | None:
    """Map Lever/Ashby/Greenhouse-metadata workplace types to 1/0/None."""
    if not value:
        return None
    value = value.strip().lower().replace("-", "").replace(" ", "")
    if value == "remote":
        return 1
    if value in ("onsite", "hybrid"):
        return 0
    return None


def _greenhouse_remote(posting: dict[str, Any], location: str | None) -> int | None:
    # Greenhouse has no standard field. Some boards (e.g. Airbnb) add a custom
    # "Workplace Type" metadata field; otherwise fall back to the location text.
    for meta in posting.get("metadata") or []:
        if (meta.get("name") or "").strip().lower() == "workplace type" and isinstance(meta.get("value"), str):
            mapped = _workplace_remote(meta["value"])
            if mapped is not None:
                return mapped
    if location and "remote" in location.lower():
        return 1
    return None


def _ashby_remote(posting: dict[str, Any]) -> int | None:
    # isRemote is true when any location is remote (e.g. a Hybrid NYC job with
    # a "Remote (US)" secondary location). It is sometimes null.
    if isinstance(posting.get("isRemote"), bool):
        return int(posting["isRemote"])
    return _workplace_remote(posting.get("workplaceType"))


def _lever_salary(rng: dict[str, Any] | None) -> tuple[int | None, int | None, str | None]:
    # Observed intervals: "per-year-salary", "per-hour-wage".
    if not rng or rng.get("interval") != "per-year-salary":
        return None, None, None
    return rng.get("min"), rng.get("max"), rng.get("currency")


def _ashby_salary(comp: dict[str, Any] | None) -> tuple[int | None, int | None, str | None]:
    # summaryComponents lists Salary, EquityCashValue, EquityPercentage, Commission...
    # Some components omit currencyCode entirely. Observed intervals: "1 YEAR", "1 HOUR", "NONE".
    salaries = [
        c
        for c in (comp or {}).get("summaryComponents") or []
        if c.get("compensationType") == "Salary" and c.get("interval") == "1 YEAR"
    ]
    currencies = {c.get("currencyCode") for c in salaries}
    if not salaries or len(currencies) != 1:
        return None, None, None
    mins = [c["minValue"] for c in salaries if c.get("minValue") is not None]
    maxs = [c["maxValue"] for c in salaries if c.get("maxValue") is not None]
    return (min(mins) if mins else None, max(maxs) if maxs else None, currencies.pop())


def _lever_description(posting: dict[str, Any]) -> str:
    # descriptionPlain covers the opening and body; the bullet sections live in
    # `lists` (HTML content) and the closing text in additionalPlain.
    parts = [_normalize_text(posting.get("descriptionPlain") or "")]
    for section in posting.get("lists") or []:
        heading = _clean(section.get("text"))
        body = html_to_text(section.get("content") or "")
        parts.append("\n".join(x for x in (heading, body) if x))
    parts.append(_normalize_text(posting.get("additionalPlain") or ""))
    return "\n\n".join(p for p in parts if p)
