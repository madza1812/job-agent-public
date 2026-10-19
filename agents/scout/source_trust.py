"""Classify where a found posting came from: official or aggregator.

The domain lists live in config/source_trust.yaml, not in code. A URL is official when
its host is (a subdomain of) a known ATS, a public-sector job board, one of the
company's domains listed under company_domains, or a domain whose name matches the
company's name (jobs.supermicro.com for "Supermicro"). Anything else is an aggregator.
An ATS entry may carry a path ("oraclecloud.com/hcmUI/CandidateExperience"): then only
URLs under that path count, since the same host serves other things.

A found posting has two links, the page the lookup read (source_url) and where to apply
(apply_url); classify_posting() calls it official when either one is.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import yaml

from agents.scout.companies import normalize_company

CONFIG_PATH = Path(__file__).resolve().parents[2] / "config" / "source_trust.yaml"

OFFICIAL = "official"
AGGREGATOR = "aggregator"
NOT_FOUND = "not_found"
TRUST_LEVELS = (OFFICIAL, AGGREGATOR, NOT_FOUND)

# Host labels that say nothing about who owns the site.
_GENERIC_LABELS = {"www", "jobs", "job", "careers", "career", "apply", "hire", "work", "recruiting", "en", "us"}
_MIN_NAME_LABEL = 4  # a single short word ("ark") is too likely to match by accident


@lru_cache(maxsize=4)
def load_config(path: Path = CONFIG_PATH) -> dict[str, Any]:
    with open(path, encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    return {
        "ats_domains": [d.lower() for d in data.get("ats_domains") or []],
        "public_sector_domains": [d.lower() for d in data.get("public_sector_domains") or []],
        "company_domains": {normalize_company(k): [d.lower() for d in v or []]
                            for k, v in (data.get("company_domains") or {}).items()},
        "blocked_fetch_domains": [d.lower() for d in data.get("blocked_fetch_domains") or []],
        "ats_sender_domains": [d.lower() for d in data.get("ats_sender_domains") or []],
    }


def classify(url: str | None, company: str | None, config: dict[str, Any] | None = None) -> str:
    """official or aggregator for one URL of a found posting."""
    config = config or load_config()
    host = _host(url)
    if not host:
        return AGGREGATOR
    if _matches_any(host, config["blocked_fetch_domains"]):
        return AGGREGATOR
    if is_ats(url, config) or _matches_any(host, config["public_sector_domains"]):
        return OFFICIAL
    key = normalize_company(company)
    if _matches_any(host, config["company_domains"].get(key, [])):
        return OFFICIAL
    if _name_matches_host(key, host):
        return OFFICIAL
    return AGGREGATOR


def classify_posting(source_url: str | None, apply_url: str | None, company: str | None,
                     config: dict[str, Any] | None = None) -> tuple[str, str | None]:
    """(trust, official link) for a found posting: official when source_url or apply_url is
    on the company's own domain or an approved job site, source_url checked first."""
    config = config or load_config()
    for url in (source_url, apply_url):
        if url and classify(url, company, config) == OFFICIAL:
            return OFFICIAL, url
    return AGGREGATOR, None


def is_ats(url: str | None, config: dict[str, Any] | None = None) -> bool:
    """The URL is on a known ATS (and under its path, for an entry that has one)."""
    config = config or load_config()
    host = _host(url)
    if not host:
        return False
    path = (urlsplit(url.strip()).path or "/").lower()
    for entry in config["ats_domains"]:
        domain, _, prefix = entry.partition("/")
        if _matches_any(host, [domain]) and (not prefix or (path + "/").startswith(f"/{prefix.rstrip('/')}/")):
            return True
    return False


def _host(url: str | None) -> str:
    if not url:
        return ""
    try:
        return (urlsplit(url.strip()).hostname or "").lower().rstrip(".")
    except ValueError:
        return ""


def _matches_any(host: str, domains: list[str]) -> bool:
    return any(host == d or host.endswith("." + d) for d in domains)


def _name_matches_host(normalized_company: str, host: str) -> bool:
    """The company's name, joined up, is one of the host's labels: "supermicro" in
    jobs.supermicro.com, "propetroservices" in careers.propetroservices.com."""
    words = normalized_company.split()
    if not words:
        return False
    names = {"".join(words), "".join(words[:2])}
    if len(words[0]) >= _MIN_NAME_LABEL:
        names.add(words[0])
    labels = [lab.replace("-", "") for lab in host.split(".")[:-1] if lab not in _GENERIC_LABELS]
    return any(lab in names and len(lab) >= _MIN_NAME_LABEL for lab in labels)
