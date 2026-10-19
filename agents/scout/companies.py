"""Preferred companies (profile/preferred_companies.yaml): a scoring reference.

match_preferred() tells whether a posting's company is one the user prefers, and
at what tier. Entries with poll_feed: true are also fetched from their ATS board.
"""

from __future__ import annotations

import re
import unicodedata
from pathlib import Path
from typing import Any

import yaml

DEFAULT_PREFERRED_PATH = Path(__file__).resolve().parents[2] / "profile" / "preferred_companies.yaml"

TIERS = ("dream", "strong", "interested")
FEED_ATSES = ("greenhouse", "lever", "ashby")

# Removed only from the end of a name, so "Co-op Financial" keeps its "co". Several can
# stack ("Globex Global Tech, Inc." -> "globex"); the first word always stays.
_SUFFIXES: tuple[tuple[str, ...], ...] = tuple(sorted(
    (tuple(s.split()) for s in (
        "inc", "incorporated", "llc", "ltd", "limited", "corp", "corporation", "co", "company",
        "plc", "gmbh", "pbc", "lp", "llp", "sa", "ag", "nv", "bv",
        "global tech", "technologies",
    )),
    key=len, reverse=True,
))

Company = dict[str, Any]


class PreferredCompaniesError(ValueError):
    """preferred_companies.yaml is malformed."""


def normalize_company(name: str | None) -> str:
    """Comparison key for a company name: lowercase, fold accents, drop punctuation, a
    leading "the" and trailing Inc/LLC/Corp/Global Tech/Technologies etc. Only for
    comparing; the original name is what gets stored and shown."""
    if not name:
        return ""
    text = "".join(c for c in unicodedata.normalize("NFKD", name) if not unicodedata.combining(c))
    text = text.lower().replace("&", " and ")
    # "L.L.C." -> "llc" before the remaining punctuation becomes spaces.
    text = re.sub(r"\b([a-z])\.(?=[a-z]\.)", r"\1", text)
    words = re.sub(r"[^\w\s]|_", " ", text).split()
    stripped = True
    while stripped:
        stripped = False
        for suffix in _SUFFIXES:
            if len(words) > len(suffix) and tuple(words[-len(suffix):]) == suffix:
                del words[-len(suffix):]
                stripped = True
                break
    if len(words) > 1 and words[0] == "the":
        words.pop(0)
    return " ".join(words)


def load_preferred(path: Path | str | None = None) -> list[Company]:
    """Load and validate the preferred list. A missing file or empty list is fine."""
    path = Path(path) if path else DEFAULT_PREFERRED_PATH
    if not path.exists():
        return []
    with open(path, encoding="utf-8") as f:
        data = yaml.safe_load(f)
    if data is None:
        return []
    if not isinstance(data, dict):
        raise PreferredCompaniesError(f"{path}: expected a mapping with a 'companies' key")
    companies = data.get("companies") or []
    if not isinstance(companies, list):
        raise PreferredCompaniesError(f"{path}: 'companies' must be a list")

    problems = []
    seen: dict[str, str] = {}
    for i, c in enumerate(companies):
        where = f"companies[{i}]"
        if not isinstance(c, dict) or not isinstance(c.get("name"), str) or not c["name"].strip():
            problems.append(f"{where}: needs a name")
            continue
        where = f"{where} ({c['name']})"
        if c.get("tier") not in TIERS:
            problems.append(f"{where}: tier must be one of {', '.join(TIERS)}, got {c.get('tier')!r}")
        aliases = c.get("aliases") or []
        if not isinstance(aliases, list) or not all(isinstance(a, str) for a in aliases):
            problems.append(f"{where}: aliases must be a list of names")
            aliases = []
        if c.get("poll_feed"):
            if c.get("ats") not in FEED_ATSES:
                problems.append(f"{where}: poll_feed needs ats: {' | '.join(FEED_ATSES)}")
            if not c.get("slug"):
                problems.append(f"{where}: poll_feed needs a slug")
        for n in [c["name"], *aliases]:
            key = normalize_company(n)
            if key in seen and seen[key] != c["name"]:
                problems.append(f"{where}: {n!r} also matches {seen[key]!r}")
            seen[key] = c["name"]
    if problems:
        raise PreferredCompaniesError(f"{path}:\n  " + "\n  ".join(problems))
    return companies


def feed_companies(companies: list[Company]) -> list[Company]:
    """Entries whose ATS board Scout should also poll."""
    return [c for c in companies if c.get("poll_feed")]


def match_preferred(company_name: str | None, companies: list[Company] | None = None) -> tuple[str, str] | None:
    """(preferred company name, tier) if company_name is that company or an alias, else None."""
    key = normalize_company(company_name)
    if not key:
        return None
    for c in load_preferred() if companies is None else companies:
        if any(normalize_company(n) == key for n in [c["name"], *(c.get("aliases") or [])]):
            return c["name"], c["tier"]
    return None
