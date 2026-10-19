"""Which Gmail messages may be about which job: queries, and scoring from headers only.

Candidates are jobs with status applied or interviewing. Each gets one Gmail query:
mail from the company's own domains (from apply_url, source_url and the cached board,
when they're the company's and not an ATS's) OR from an ATS sender domain
(config/source_trust.yaml ats_sender_domains) mentioning the company name. Two more
queries find mail about jobs not marked applied: everything from ATS senders (for "Did
you apply?"), and recruiter-sounding mail in the primary inbox (recruiter outreach).
Every query skips label:JobAlerts and mail from the user.

score_header() rates a message's From, Subject and sender name against a job; only a
likely match (is_likely: LIKELY_SCORE or more, from the company's domain or an ATS) has
its body fetched.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from urllib.parse import urlsplit

from agents.scout import source_trust
from agents.scout.companies import normalize_company

LIKELY_SCORE = 50
SENDER_COMPANY_DOMAIN = 50
SENDER_ATS = 20
COMPANY_IN_SUBJECT = 30
COMPANY_IN_SENDER_NAME = 20
TITLE_IN_SUBJECT = 20
QUERY_NAMED_COMPANY = 10  # Gmail found the company's name in this message (subject or body)

BASE_QUERY = "-label:JobAlerts -from:me"
RECRUITER_TERMS = '(recruiter OR recruiting OR "your background" OR "your profile" OR "your experience")'
RECRUITER_ROLE_TERMS = "(role OR position OR opportunity OR opening)"
# Automated senders that are never a person reaching out (LinkedIn's notifications etc.).
_AUTOMATED_LOCAL = re.compile(r"^(no-?reply|do-?not-?reply|notifications?|news(letter)?|mailer|updates?|info|"
                              r"marketing|hello|support|team|jobs?|alerts?)\b", re.I)
_BULK_DOMAINS = ("linkedin.com", "indeed.com", "glassdoor.com", "ziprecruiter.com", "postjobfree.com",
                 "theladders.com", "dice.com", "monster.com")
_RECRUITER_SUBJECT = re.compile(r"\b(recruit\w*|opportunit\w*|role|position|opening|your (background|profile|experience))\b",
                                re.I)
# One-time codes and similar: recorded, never classified.
IGNORED_SUBJECT = re.compile(r"\b(security|verification|one[- ]time|confirmation) code\b|\bpasscode\b|"
                             r"\bverify your email\b", re.I)
_TITLE_STOPWORDS = {"and", "the", "for", "with", "of", "to", "in", "ii", "iii", "iv", "sr", "jr", "team"}
_SECOND_LEVEL = {"co", "com", "org", "net", "ac", "gov", "edu"}


@dataclass
class Candidate:
    job: dict[str, Any]
    domains: list[str] = field(default_factory=list)  # the company's own mail domains

    @property
    def key(self) -> str:
        return normalize_company(self.job["company"])


def registrable(host: str) -> str:
    """mail.acme.com -> acme.com; jobs.acme.co.uk -> acme.co.uk."""
    labels = [lab for lab in host.lower().strip(".").split(".") if lab]
    if len(labels) >= 3 and labels[-2] in _SECOND_LEVEL and len(labels[-1]) == 2:
        return ".".join(labels[-3:])
    return ".".join(labels[-2:])


def sender_domain(address: str) -> str:
    return address.rsplit("@", 1)[-1].lower().strip() if "@" in address else ""


def domain_matches(domain: str, domains: list[str]) -> bool:
    return any(domain == d or domain.endswith("." + d) for d in domains)


def company_domains(job: dict[str, Any], board_url: str | None, config: dict[str, Any] | None = None) -> list[str]:
    """The company's own domains: hosts of apply_url, source_url and the cached board
    that source_trust counts as the company's (not an ATS, public-sector board or
    aggregator), plus company_domains from the config."""
    config = config or source_trust.load_config()
    not_company = config["public_sector_domains"] + config["blocked_fetch_domains"]
    out: list[str] = []
    for url in (job.get("apply_url"), job.get("source_url"), board_url):
        host = _host(url)
        if not host or source_trust.is_ats(url, config) or domain_matches(host, not_company):
            continue
        if source_trust.classify(url, job["company"], config) != source_trust.OFFICIAL:
            continue
        out.append(registrable(host))
    out += config["company_domains"].get(normalize_company(job["company"]), [])
    return sorted(set(out))


def _host(url: str | None) -> str:
    try:
        return (urlsplit((url or "").strip()).hostname or "").lower()
    except ValueError:
        return ""


def after(since: datetime) -> str:
    """Gmail's after: takes seconds since the epoch."""
    return f"after:{int(since.timestamp())}"


def _quoted(text: str) -> str:
    return '"' + re.sub(r'["()]', " ", text).strip() + '"'


def job_query(cand: Candidate, ats_senders: list[str], since: datetime) -> str:
    parts = []
    if cand.domains:
        parts.append("from:(" + " OR ".join(cand.domains) + ")")
    parts.append(f"(from:({' OR '.join(ats_senders)}) {_quoted(cand.job['company'])})")
    return f"{BASE_QUERY} {after(since)} ({' OR '.join(parts)})"


def ats_query(ats_senders: list[str], since: datetime) -> str:
    return f"{BASE_QUERY} {after(since)} from:({' OR '.join(ats_senders)})"


def recruiter_query(since: datetime) -> str:
    return f"{BASE_QUERY} {after(since)} category:primary {RECRUITER_TERMS} {RECRUITER_ROLE_TERMS}"


def _words(text: str) -> str:
    return " " + normalize_company(text) + " "


def company_named(company: str, text: str) -> bool:
    """The company's normalized name appears as whole words in text."""
    key = normalize_company(company)
    return bool(key) and f" {key} " in _words(text)


def title_in_subject(title: str, subject: str) -> bool:
    """At least half of the title's significant words are in the subject."""
    words = {w for w in normalize_company(title).split() if len(w) > 2 and w not in _TITLE_STOPWORDS}
    if not words:
        return False
    found = set(normalize_company(subject).split())
    return len(words & found) * 2 >= len(words)


def score_header(header: dict[str, Any], job: dict[str, Any], *, domains: list[str], ats_senders: list[str],
                 query_named_company: bool = False) -> tuple[int, list[str]]:
    """(score, reasons) for a message's headers against one job."""
    domain = sender_domain(header.get("sender") or "")
    score, why = 0, []
    if domains and domain_matches(domain, domains):
        score += SENDER_COMPANY_DOMAIN
        why.append("company domain")
    elif domain_matches(domain, ats_senders):
        score += SENDER_ATS
        why.append("ATS sender")
    if company_named(job["company"], header.get("subject") or ""):
        score += COMPANY_IN_SUBJECT
        why.append("company in subject")
    elif company_named(job["company"], header.get("sender_name") or ""):
        score += COMPANY_IN_SENDER_NAME
        why.append("company in sender name")
    if title_in_subject(job["title"], header.get("subject") or ""):
        score += TITLE_IN_SUBJECT
        why.append("title in subject")
    if query_named_company:
        score += QUERY_NAMED_COMPANY
        why.append("company named in message")
    return score, why


def is_likely(score: int, why: list[str]) -> bool:
    """Enough to fetch the body: LIKELY_SCORE, with the sender being the company or an
    ATS. A subject naming the company and title alone is what job alerts look like."""
    return score >= LIKELY_SCORE and ("company domain" in why or "ATS sender" in why)


def best_job(header: dict[str, Any], jobs: list[Candidate], ats_senders: list[str],
             named_by_query: set[int] = frozenset()) -> tuple[Candidate | None, int, list[str]]:
    """The job these headers fit best: highest score; on a tie, an applied or
    interviewing job, then the newest."""
    best: tuple[Candidate | None, int, list[str]] = (None, 0, [])
    rank = None
    for cand in jobs:
        score, why = score_header(header, cand.job, domains=cand.domains, ats_senders=ats_senders,
                                  query_named_company=cand.job["id"] in named_by_query)
        key = (score, cand.job["status"] in ("applied", "interviewing"), cand.job["id"])
        if score > 0 and (rank is None or key > rank):
            best, rank = (cand, score, why), key
    return best


def looks_like_recruiter(header: dict[str, Any], own_addresses: set[str] = frozenset()) -> bool:
    """A person (not a notification or newsletter) writing about a role."""
    sender = (header.get("sender") or "").lower()
    if not sender or sender in own_addresses:
        return False
    local, domain = sender.split("@", 1) if "@" in sender else (sender, "")
    if _AUTOMATED_LOCAL.match(local) or domain_matches(domain, list(_BULK_DOMAINS)):
        return False
    return bool(_RECRUITER_SUBJECT.search(header.get("subject") or ""))


def body_names_company(message: dict[str, Any], company: str, plain: str) -> bool:
    """After the body is fetched: the company is named in subject, sender name or text."""
    return any(company_named(company, t) for t in (message.get("subject") or "", message.get("sender_name") or "", plain))
