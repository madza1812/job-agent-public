"""Job alert emails: find the sender's parser, extract postings, normalize them.

Alerts are read from Gmail (read-only) with alert_query(). Parsers are matched by
exact sender address and were written against real alert emails (sanitized
samples in tests/fixtures/email_*.json):

    jobalerts-noreply@linkedin.com   saved-search alerts          -> parse_linkedin
    jobs-noreply@linkedin.com        "jobs that match your profile" -> parse_linkedin
    donotreply@match.indeed.com      single-job matches           -> parse_indeed_match
    donotreply@jobalert.indeed.com   saved-search digests         -> parse_indeed_digest
    noreply@glassdoor.com            saved-search alerts          -> parse_glassdoor

A parser takes a message from agents.common.gmail.get_message and returns a list
of {title, company, location, url, native_id, remote[, sponsored, age]} dicts. to_job() turns those
into the shape fetchers.py uses: source "email:<type>", an empty description and
needs_details = 1, because an alert only carries a summary of the posting.

posted_at: Indeed digests and Glassdoor alerts show an age badge on each card ("Just
posted", "Today", "3 days ago", "30+ days ago"; Glassdoor "10h", "3d", "30d+"), which
becomes an estimated date counted back from the email's date (posted_at_source
alert_badge; "30+" counts as 30). Cards without one (LinkedIn alerts and Indeed's
single-job matches show none) get the email's date (email_date): the posting is at least
that old.

Emails matching SKIP_RULES (Indeed's "...is now active" confirmations) are
recognized and skipped. IGNORED_SENDERS (e.g. LinkedIn invitations) land in the
label but never carry jobs; they're marked processed without being parsed.
Other senders with no parser are logged by address, never treated as a failure.
"""

from __future__ import annotations

import base64
import binascii
import gzip
import hashlib
import json
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Iterator, Protocol
from urllib.parse import parse_qs, parse_qsl, urlencode, urlsplit, urlunsplit

from bs4 import BeautifulSoup, Comment

from agents.db import EMAIL_SOURCE_PREFIX

log = logging.getLogger(__name__)

LABEL = "JobAlerts"
DEFAULT_SINCE_DAYS = 3

Message = dict[str, Any]
RawJob = dict[str, Any]
Parser = Callable[[Message], list[RawJob]]

_REMOTE_RE = re.compile(r"\bremote\b", re.I)
_WORKPLACE_RE = re.compile(r"\s*\((remote|hybrid|on-site|onsite)\)\s*$", re.I)
_INDEED_JK_RE = re.compile(r"[0-9a-f]{16}")
_META_SEP = " · "  # "Company · City, ST (Hybrid)" on LinkedIn and Indeed cards
# A card's age badge: "Just posted", "Today", "3 days ago", "30+ days ago" (Indeed);
# "Just posted", "10h", "3d", "30d+" (Glassdoor).
_AGE_BADGE = re.compile(
    r"^(?:(?P<now>just posted|today)|(?P<days>\d+)\+?\s*(?:days?\s+ago|d)\+?|(?P<hours>\d+)\s*(?:h|hours?\s+ago))$",
    re.I)

# Query parameters that only track the click. Anything not listed is kept.
_TRACKING_PARAMS = {
    "trk", "trkemail", "trackingid", "refid", "lipi", "midtoken", "midsig", "eid", "otptoken",
    "ssid", "fccid", "ref", "src", "cmp", "campaign", "mc_cid", "mc_eid", "gclid", "fbclid",
    "_hsenc", "_hsmi", "alid", "tk", "from", "bb", "xkcb", "rjptk",
}


def alert_query(since_days: int = DEFAULT_SINCE_DAYS) -> str:
    if since_days < 1:
        raise ValueError("since_days must be at least 1")
    return f"label:{LABEL} newer_than:{since_days}d"


class Mailbox(Protocol):
    def list_messages(self, query: str) -> list[str]: ...
    def get_message(self, message_id: str) -> Message: ...


@dataclass
class AlertEmail:
    message: Message
    sender_type: str | None  # None: no parser for this sender
    jobs: list[dict[str, Any]]
    error: str | None = None
    skipped: bool = False  # a known non-job email, e.g. an alert confirmation
    ignored: bool = False  # from a sender in IGNORED_SENDERS

    @property
    def source(self) -> str | None:
        return f"{EMAIL_SOURCE_PREFIX}{self.sender_type}" if self.sender_type else None

    @property
    def parsed(self) -> bool:
        return self.sender_type is not None and self.error is None and bool(self.jobs)


# --- Parsers ------------------------------------------------------------------------


def parse_linkedin(message: Message) -> list[RawJob]:
    """Cards are <a href=".../comm/jobs/view/<id>/?..."> holding the title, then
    "Company · City, ST (Hybrid)", then pay and badges. The same job has several
    links (logo, card, title); the one with the most text is the card."""
    soup = BeautifulSoup(message.get("html") or "", "html.parser")
    cards: dict[str, tuple[str, list[str]]] = {}
    for a in soup.find_all("a", href=re.compile(r"linkedin\.com/(?:comm/)?jobs/view/\d+")):
        job_id = re.search(r"/jobs/view/(\d+)", a["href"]).group(1)
        strings = _strings(a)
        if job_id not in cards or len(strings) > len(cards[job_id][1]):
            cards[job_id] = (a["href"], strings)

    jobs = []
    for job_id, (href, strings) in cards.items():
        if len(strings) < 2 or _META_SEP not in strings[1]:
            log.warning("LinkedIn card %s has an unexpected layout: %r", job_id, strings[:3])
            continue
        company, location, remote = _split_meta(strings[1])
        jobs.append({
            "native_id": job_id,
            "title": strings[0],
            "company": company,
            "location": location,
            "remote": remote,
            "url": f"https://www.linkedin.com/jobs/view/{job_id}/",
        })
    return jobs


def parse_indeed_match(message: Message) -> list[RawJob]:
    """A title link (a cts.indeed.com redirect) followed by "Company · City, ST".

    The redirect token is base64 gzip JSON whose "u" is the real link
    (indeed.com/rc/clk?jk=<job key>&...). It's decoded locally; nothing is fetched.
    """
    soup = BeautifulSoup(message.get("html") or "", "html.parser")
    jobs: dict[str, RawJob] = {}
    for a in soup.find_all("a", href=re.compile(r"cts\.indeed\.com/")):
        jk = _indeed_job_key(a["href"])
        title = " ".join(a.get_text(" ", strip=True).split())
        if not jk or not title or jk in jobs:
            continue
        meta = next((s for s in _strings_after(a, 3) if _META_SEP in s), None)
        if meta is None:
            continue  # "View job" / "Learn more" links, not the card title
        company, location, remote = _split_meta(meta)
        jobs[jk] = {
            "native_id": jk,
            "title": title,
            "company": company,
            "location": location,
            "remote": remote if remote is not None else _indeed_work_setting(message.get("text") or ""),
            "url": f"https://www.indeed.com/viewjob?jk={jk}",
        }
    return list(jobs.values())


def parse_indeed_digest(message: Message) -> list[RawJob]:
    """Each card's title is an <h2> link to indeed.com/rc/clk/dl?jk=<job key>, followed
    by a <p> with the company and a <p> with the location, then pay, badges and age.

    Sponsored cards link to /pagead/clk/dl with only an opaque ad token and no job
    key anywhere in the card. Following that tracking redirect is off limits, so
    they're stored with sponsored = 1 and an Indeed search link (title and company,
    in the card's location) in place of the job page.
    """
    soup = BeautifulSoup(message.get("html") or "", "html.parser")
    jobs: dict[str, RawJob] = {}
    for h2 in soup.find_all("h2"):
        a = h2.find("a", href=True)
        if a is None:
            continue
        parts = urlsplit(a["href"])
        if not (parts.netloc == "indeed.com" or parts.netloc.endswith(".indeed.com")):
            continue
        sponsored = parts.path.startswith("/pagead/")
        jk = None if sponsored else parse_qs(parts.query).get("jk", [None])[0]
        if not sponsored and (not jk or not _INDEED_JK_RE.fullmatch(jk)):
            continue
        title = _clean(a.get_text(" ", strip=True))
        meta = [_clean(p.get_text(" ", strip=True)) for p in h2.find_all_next("p", limit=2)]
        card = []
        for el in h2.find_all_next(["p", "h2"], limit=12):
            if el.name == "h2":
                break
            card.append(_clean(el.get_text(" ", strip=True)) or "")
        if not title or len(meta) < 2:
            log.warning("Indeed digest card %r has an unexpected layout", jk or title)
            continue
        company, location = meta
        if sponsored:
            card_key = f"{company}|{title}|{location}".lower()
            native_id = "sponsored-" + hashlib.sha1(card_key.encode("utf-8")).hexdigest()[:16]
            query = {"q": f"{title} {company or ''}".strip(), **({"l": location} if location else {})}
            url = f"https://www.indeed.com/jobs?{urlencode(query)}"
        else:
            native_id, url = jk, f"https://www.indeed.com/viewjob?jk={jk}"
        if native_id in jobs:
            continue
        jobs[native_id] = {
            "native_id": native_id,
            "title": title,
            "company": company,
            "location": location,
            "remote": None,
            "url": url,
            "sponsored": int(sponsored),
            "age": age_badge(card),
        }
    return list(jobs.values())


def parse_glassdoor(message: Message) -> list[RawJob]:
    """Each card is one <a> to glassdoor.com/partner/jobListing.htm?jobListingId=<id>
    holding the company (a <span>), the title (first <p>) and the location (second
    <p>), then pay, badges and age. The link becomes the plain listing page."""
    soup = BeautifulSoup(message.get("html") or "", "html.parser")
    jobs: dict[str, RawJob] = {}
    for a in soup.find_all("a", href=re.compile(r"glassdoor\.com/partner/jobListing\.htm")):
        listing_id = parse_qs(urlsplit(a["href"]).query).get("jobListingId", [None])[0]
        if not listing_id or not listing_id.isdigit() or listing_id in jobs:
            continue
        company = _glassdoor_company(a)
        lines = [t for t in (_clean(p.get_text(" ", strip=True)) for p in a.find_all("p")) if t]
        if company is None or len(lines) < 2:
            log.warning("Glassdoor card %s has an unexpected layout: %r", listing_id, lines[:3])
            continue
        jobs[listing_id] = {
            "native_id": listing_id,
            "title": lines[0],
            "company": company,
            "location": lines[1],
            "remote": None,
            "url": f"https://www.glassdoor.com/job-listing/j?jl={listing_id}",
            "age": age_badge(_strings(a)),
        }
    return list(jobs.values())


def age_badge(strings: list[str]) -> timedelta | None:
    """How old a card says its posting is, from the first age badge among its strings."""
    for s in strings:
        m = _AGE_BADGE.match((s or "").strip())
        if m:
            if m["now"]:
                return timedelta(0)
            return timedelta(days=int(m["days"])) if m["days"] else timedelta(hours=int(m["hours"]))
    return None


_GLASSDOOR_RATING = re.compile(r"\s*\d(?:\.\d)?\s*★$")


def _glassdoor_company(card) -> str | None:
    """The company name from a card's innermost <span>s. Rated employers put the rating
    ("4.4 ★") in a sibling span inside the same wrapper, so the wrapper's text isn't the
    name; any rating left on the end is stripped as well."""
    for span in card.find_all("span"):
        if span.find("span"):
            continue
        text = _clean(_GLASSDOOR_RATING.sub("", span.get_text(" ", strip=True)))
        if text:
            return text
    return None


# Exact sender address -> (type, parser).
SENDERS: dict[str, tuple[str, Parser]] = {
    "jobalerts-noreply@linkedin.com": ("linkedin", parse_linkedin),
    "jobs-noreply@linkedin.com": ("linkedin", parse_linkedin),
    "donotreply@match.indeed.com": ("indeed", parse_indeed_match),
    "donotreply@jobalert.indeed.com": ("indeed", parse_indeed_digest),
    "noreply@glassdoor.com": ("glassdoor", parse_glassdoor),
}

# Exact sender addresses that reach the label but never carry jobs.
IGNORED_SENDERS: frozenset[str] = frozenset({
    "invitations@linkedin.com",
    "updates-noreply@linkedin.com",  # LinkedIn news-feed digest
})

# (sender domain, subject pattern): known emails that carry no jobs to parse.
SKIP_RULES: list[tuple[str, re.Pattern[str]]] = [
    ("indeed.com", re.compile(r"\bis now active\b", re.I)),
]


def sender_type(sender: str) -> str | None:
    """The parser type for a sender address, or None when there is no parser."""
    entry = SENDERS.get(sender.lower().strip())
    return entry[0] if entry else None


def is_skipped(message: Message) -> bool:
    domain = message["sender"].rsplit("@", 1)[-1].lower()
    return any(
        (domain == d or domain.endswith("." + d)) and pattern.search(message.get("subject") or "")
        for d, pattern in SKIP_RULES
    )


# --- Reading ------------------------------------------------------------------------


def read_alerts(
    mailbox: Mailbox,
    *,
    skip: Callable[[str], bool] = lambda message_id: False,
    since_days: int = DEFAULT_SINCE_DAYS,
) -> Iterator[AlertEmail]:
    """Yield each alert email in the window that `skip` doesn't reject, parsed if possible."""
    for message_id in mailbox.list_messages(alert_query(since_days)):
        if skip(message_id):
            continue
        message = mailbox.get_message(message_id)
        sender = message["sender"]
        if sender.lower().strip() in IGNORED_SENDERS:
            log.info("Ignoring %s from %s: sender on the ignore list", message["subject"], sender)
            yield AlertEmail(message, None, [], ignored=True)
            continue
        if is_skipped(message):
            log.info("Skipping %s from %s: not a job list", message["subject"], sender)
            yield AlertEmail(message, sender_type(sender), [], skipped=True)
            continue
        entry = SENDERS.get(sender.lower().strip())
        if entry is None:
            log.warning("No parser for job alert sender %s (subject %r)", sender, message["subject"])
            yield AlertEmail(message, None, [])
            continue
        kind, parser = entry
        try:
            raw_jobs = parser(message)
        except Exception as exc:  # a changed email layout must not stop the run
            log.exception("The %s parser failed on message %s", kind, message_id)
            yield AlertEmail(message, kind, [], error=f"{type(exc).__name__}: {exc}")
            continue
        jobs = [j for j in (to_job(raw, kind, message) for raw in raw_jobs) if j is not None]
        if not jobs:
            log.warning("The %s parser found no jobs in message %s (subject %r)", kind, message_id, message["subject"])
        yield AlertEmail(message, kind, jobs)


# --- Normalizing --------------------------------------------------------------------


def to_job(raw: RawJob, kind: str, message: Message) -> dict[str, Any] | None:
    """A parser's raw job in the shape fetchers.py returns."""
    title, company, url = _clean(raw.get("title")), _clean(raw.get("company")), raw.get("url")
    if not title or not url:
        log.warning("Skipping a %s alert entry without title or link: %r", kind, raw)
        return None
    url = clean_url(url)
    native_id = raw.get("native_id") or hashlib.sha1(url.encode("utf-8")).hexdigest()[:16]
    location = _clean(raw.get("location"))
    remote = raw.get("remote")
    if remote is None and _REMOTE_RE.search(f"{title} {location or ''}"):
        remote = 1
    posted_at, posted_at_source = _email_date(message.get("date")), "email_date"
    sent = _email_datetime(message.get("date"))
    if sent and raw.get("age") is not None:
        posted_at, posted_at_source = (sent - raw["age"]).date().isoformat(), "alert_badge"
    return {
        "id": f"{EMAIL_SOURCE_PREFIX}{kind}:{native_id}",
        "source": f"{EMAIL_SOURCE_PREFIX}{kind}",
        "company": company or "Unknown company",
        "title": title,
        "location": location,
        "remote": remote,
        "salary_min": None,
        "salary_max": None,
        "salary_currency": None,
        "salary_is_predicted": 0,
        "url": url,
        "description": "",
        "posted_at": posted_at,
        "posted_at_source": posted_at_source if posted_at else None,
        "needs_details": 1,
        "sponsored": int(bool(raw.get("sponsored"))),
    }


def clean_url(url: str) -> str:
    """Drop tracking query parameters and the fragment; keep everything that selects the job."""
    parts = urlsplit(url.strip())
    query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if not _is_tracking_param(k)]
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), ""))


def _is_tracking_param(name: str) -> bool:
    name = name.lower()
    return name.startswith("utm_") or name in _TRACKING_PARAMS


def _split_meta(meta: str) -> tuple[str | None, str | None, int | None]:
    """"Company · City, ST (Hybrid)" -> (company, "City, ST", remote 1/0/None)."""
    company, _, location = meta.rpartition(_META_SEP)
    remote = None
    m = _WORKPLACE_RE.search(location)
    if m:
        remote = 1 if m.group(1).lower() == "remote" else 0
        location = location[: m.start()]
    return _clean(company), _clean(location), remote


def _indeed_job_key(href: str) -> str | None:
    parts = urlsplit(href)
    segments = parts.path.split("/")
    if parts.netloc != "cts.indeed.com" or len(segments) < 3 or segments[1] != "v3":
        return None
    token = segments[2]
    try:
        payload = json.loads(gzip.decompress(base64.urlsafe_b64decode(token + "=" * (-len(token) % 4))))
    except (binascii.Error, OSError, ValueError):
        return None
    target = urlsplit(payload.get("u") or "")
    if not target.netloc.endswith("indeed.com"):
        return None
    jk = parse_qs(target.query).get("jk", [None])[0]
    return jk if jk and re.fullmatch(r"[0-9a-f]{16}", jk) else None


def _indeed_work_setting(text: str) -> int | None:
    m = re.search(r"^Work setting:\s*(.+)$", text, re.M)
    if not m:
        return None
    setting = m.group(1).strip().lower()
    if setting == "remote":
        return 1
    if setting in ("hybrid", "in-person", "in person", "on-site", "onsite"):
        return 0
    return None


def _strings(tag: Any) -> list[str]:
    return [s for s in (_clean(t) for t in tag.find_all(string=True) if not isinstance(t, Comment)) if s]


def _strings_after(tag: Any, n: int) -> list[str]:
    out: list[str] = []
    for t in tag.find_all_next(string=True):
        if isinstance(t, Comment) or t.parent is tag or tag in t.parents:
            continue
        s = _clean(t)
        if s:
            out.append(s)
            if len(out) == n:
                break
    return out


def _email_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value).astimezone(timezone.utc)
    except ValueError:
        return None


def _email_date(value: str | None) -> str | None:
    sent = _email_datetime(value)
    return sent.date().isoformat() if sent else None


def _clean(value: str | None) -> str | None:
    if value is None:
        return None
    value = " ".join(value.replace("\xa0", " ").split())
    return value or None
