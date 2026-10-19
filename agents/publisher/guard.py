"""Checks every LinkedIn draft in code before it can reach Typefully.

check() returns the problems with a post (empty means it may be saved):

  * length 120-220 words, at most 2 hashtags, at most 2 emoji;
  * no hype words (config/publisher.yaml hype_words);
  * no company the owner is applying to or prefers: names and aliases in
    preferred_companies.yaml, and the companies of jobs shortlisted, researched, applied
    or interviewing, with a brief, or with an application. Matched as whole words,
    ignoring case, except names that are also common words (common_word_companies:
    Beacon, Summit, ...), which match only as capitalized, so "beacon" is fine;
  * nothing about specific applications or recruiters (application_patterns);
  * no security setup: security_terms, and always IP addresses, "port <n>", internal
    hostnames (.local, .lan, .internal, ...), Cloudflare tunnel hostnames and IDs (UUIDs);
  * none of the owner's private terms (profile.yaml publisher.blocked_terms: supplier or
    customer names, ...);
  * phrases on the allowlist (Northwind, Acme Tunnel) are exempt from the company and
    security_terms checks only; the patterns above still apply around them;
  * no email address or phone number, and no link that isn't in the public sources;
  * every number must appear in the public sources, so no internal figure can slip in.

The database is opened read-only and only for the company names to refuse; nothing from
it goes into a prompt.
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from agents.scout.companies import DEFAULT_PREFERRED_PATH, normalize_company
from agents.scout.filters import DEFAULT_PROFILE_PATH

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG_PATH = ROOT / "config" / "publisher.yaml"
DEFAULT_DB_PATH = ROOT / "data" / "jobs.db"

MIN_WORDS = 120
MAX_WORDS = 220
MAX_HASHTAGS = 2
MAX_EMOJI = 2
IN_PLAY = ("shortlisted", "researched", "applied", "interviewing")

_HASHTAG = re.compile(r"(?<![\w#])#[A-Za-z]\w*")
_EMOJI = re.compile("[\U0001F000-\U0001FAFF☀-➿⬀-⯿]")
_URL = re.compile(r"https?://\S+|\b(?:www\.)?[\w-]+\.(?:com|io|dev|org|net|ai)(?:/\S*)?", re.IGNORECASE)
_NUMBER = re.compile(r"\d+(?:[.,]\d+)*")
_EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
_PHONE = re.compile(r"\(?\b\d{3}\)?[ .-]?\d{3}[ .-]\d{4}\b")
_IP = re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}\b")
_PORT = re.compile(r"\bports? \d+", re.IGNORECASE)
_INTERNAL_HOST = re.compile(
    r"\blocalhost\b|\b(?:[a-z0-9-]+\.)+(?:local|lan|internal|intranet|corp|home|localdomain)\b", re.IGNORECASE)
_TUNNEL = re.compile(r"\b[\w.-]*cfargotunnel\.com\b|\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b",
                     re.IGNORECASE)


@dataclass
class Guard:
    hype_words: list[str] = field(default_factory=list)
    security_terms: list[str] = field(default_factory=list)
    application_patterns: list[str] = field(default_factory=list)
    companies: list[str] = field(default_factory=list)
    blocked_terms: list[str] = field(default_factory=list)
    common_word_companies: list[str] = field(default_factory=list)
    allowlist: list[str] = field(default_factory=list)

    def check(self, text: str, sources_text: str) -> list[str]:
        """Every problem with a full post: its shape, then check_content()."""
        problems = []
        words = len(text.split())
        if not MIN_WORDS <= words <= MAX_WORDS:
            problems.append(f"{words} words; it must be {MIN_WORDS}-{MAX_WORDS}")
        hashtags = _HASHTAG.findall(text)
        if len(hashtags) > MAX_HASHTAGS:
            problems.append(f"{len(hashtags)} hashtags ({' '.join(hashtags)}); at most {MAX_HASHTAGS}")
        emoji = _EMOJI.findall(text)
        if len(emoji) > MAX_EMOJI:
            problems.append(f"{len(emoji)} emoji; at most {MAX_EMOJI}")
        return problems + self.check_content(text, sources_text)

    def check_content(self, text: str, sources_text: str) -> list[str]:
        """What a post or an idea may never say, whatever its length."""
        problems = []
        allowed = _spans(self.allowlist, text, ignore_case=True)
        for label, terms, exempt in (("hype word", self.hype_words, []),
                                     ("security detail", self.security_terms, allowed),
                                     ("private term", self.blocked_terms, [])):
            for term in terms:
                if _hit(term, text, True, exempt):
                    problems.append(f"{label}: {term!r}")
        common = {w.lower() for w in self.common_word_companies}
        for term in self.companies:
            if _hit(term, text, term.lower() not in common, allowed):
                problems.append(f"names a company you may be applying to: {term!r}")
        for pattern in self.application_patterns:
            m = re.search(pattern, text, re.IGNORECASE)
            if m:
                problems.append(f"mentions a specific application or recruiter: {m.group(0)!r}")
        if _EMAIL.search(text) or _PHONE.search(text):
            problems.append("contains an email address or phone number")
        if _IP.search(text) or _PORT.search(text):
            problems.append("security detail: an IP address or port")
        for pattern, what in ((_INTERNAL_HOST, "an internal hostname"), (_TUNNEL, "a tunnel hostname or ID")):
            m = pattern.search(text)
            if m:
                problems.append(f"security detail: {what} ({m.group(0)!r})")
        urls = _URL.findall(text)
        for url in urls:
            if url.rstrip(".,;:!?)") not in sources_text:
                problems.append(f"link not in your public sources: {url!r}")
        allowed = _numbers(_URL.sub(" ", sources_text))
        stripped = _URL.sub(" ", _HASHTAG.sub(" ", text))
        for number in sorted(_numbers(stripped) - allowed):
            problems.append(f"number {number!r} isn't in your public sources")
        return problems


def _spans(terms: list[str], text: str, ignore_case: bool) -> list[tuple[int, int]]:
    """(start, end) of every whole-word occurrence of the terms."""
    flags = re.IGNORECASE if ignore_case else 0
    return [m.span() for t in terms if t
            for m in re.finditer(rf"(?<!\w){re.escape(t)}(?!\w)", text, flags)]


def _hit(term: str, text: str, ignore_case: bool, exempt: list[tuple[int, int]]) -> bool:
    """term occurs as a whole word somewhere not entirely inside an exempt (allowlisted)
    span: "Acme Tunnel" is exempt, the "Tunnel ID" in "Acme Tunnel ID" isn't."""
    return any(not any(a <= s and e <= b for a, b in exempt) for s, e in _spans([term], text, ignore_case))


def _numbers(text: str) -> set[str]:
    return {n.replace(",", "") for n in _NUMBER.findall(text)}


def company_terms(name: str) -> list[str]:
    """The name as written, plus its core words in their original casing:
    "Globex Global Tech" -> ["Globex Global Tech", "Globex"]."""
    name = name.strip()
    if not name:
        return []
    out = [name]
    core_len = len(normalize_company(name).split())
    words = [w for w in re.sub(r"[^\w\s&]", " ", name).split()]
    if len(words) > 1 and words[0].lower() == "the":
        words = words[1:]
    core = " ".join(words[:core_len])
    if core and core != name:
        out.append(core)
    return out


def db_companies(db_path: Path) -> list[str]:
    """Companies of jobs in play or applied to. Read-only; a missing database is fine."""
    if not db_path.is_file():
        return []
    conn = sqlite3.connect(f"{db_path.resolve().as_uri()}?mode=ro", uri=True)
    try:
        rows = conn.execute(
            "SELECT DISTINCT company FROM jobs WHERE status IN (?, ?, ?, ?) OR brief_path IS NOT NULL"
            " OR id IN (SELECT job_id FROM applications)",
            IN_PLAY,
        ).fetchall()
    except sqlite3.Error:
        return []
    finally:
        conn.close()
    return [r[0] for r in rows if r[0]]


def preferred_companies(path: Path) -> list[str]:
    if not path.is_file():
        return []
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    out = []
    for c in data.get("companies") or []:
        if isinstance(c, dict) and c.get("name"):
            out += [c["name"], *(c.get("aliases") or [])]
    return out


def load(config_path: Path = DEFAULT_CONFIG_PATH, profile_path: Path = DEFAULT_PROFILE_PATH,
         preferred_path: Path = DEFAULT_PREFERRED_PATH, db_path: Path = DEFAULT_DB_PATH) -> Guard:
    config = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    profile = yaml.safe_load(profile_path.read_text(encoding="utf-8")) if profile_path.is_file() else {}
    publisher = (profile or {}).get("publisher") or {}
    companies: list[str] = []
    for name in [*preferred_companies(preferred_path), *db_companies(db_path)]:
        for term in company_terms(name):
            if term not in companies:
                companies.append(term)
    return Guard(
        hype_words=list(config.get("hype_words") or []),
        security_terms=list(config.get("security_terms") or []),
        application_patterns=list(config.get("application_patterns") or []),
        companies=companies,
        blocked_terms=[str(t) for t in publisher.get("blocked_terms") or []],
        common_word_companies=[str(t) for t in config.get("common_word_companies") or []],
        allowlist=[str(t) for t in config.get("allowlist") or []],
    )
