"""Hard filters and dedupe keys for normalized postings (see fetchers.py for the shape).

apply_hard_filters() drops postings that clearly don't fit profile.yaml's
hard_filters. When the data is missing or ambiguous it lets the posting through:
the scorer can rank a doubtful job low, but a dropped job is never seen.
"""

from __future__ import annotations

import re
import unicodedata
from datetime import date
from pathlib import Path
from typing import Any

import yaml

from agents.scout.companies import normalize_company

DEFAULT_PROFILE_PATH = Path(__file__).resolve().parents[2] / "profile" / "profile.yaml"

Job = dict[str, Any]

# Places that are clearly somewhere else. Cities in target states belong in
# profile.yaml's location_aliases, not here.
_US_STATES = {
    "Alabama": "AL", "Alaska": "AK", "Arizona": "AZ", "Arkansas": "AR", "California": "CA",
    "Colorado": "CO", "Connecticut": "CT", "Delaware": "DE", "Florida": "FL", "Georgia": "GA",
    "Hawaii": "HI", "Idaho": "ID", "Illinois": "IL", "Indiana": "IN", "Iowa": "IA",
    "Kansas": "KS", "Kentucky": "KY", "Louisiana": "LA", "Maine": "ME", "Maryland": "MD",
    "Massachusetts": "MA", "Michigan": "MI", "Minnesota": "MN", "Mississippi": "MS",
    "Missouri": "MO", "Montana": "MT", "Nebraska": "NE", "Nevada": "NV", "New Hampshire": "NH",
    "New Jersey": "NJ", "New Mexico": "NM", "New York": "NY", "North Carolina": "NC",
    "North Dakota": "ND", "Ohio": "OH", "Oklahoma": "OK", "Oregon": "OR", "Pennsylvania": "PA",
    "Rhode Island": "RI", "South Carolina": "SC", "South Dakota": "SD", "Tennessee": "TN",
    "Texas": "TX", "Utah": "UT", "Vermont": "VT", "Virginia": "VA", "Washington": "WA",
    "West Virginia": "WV", "Wisconsin": "WI", "Wyoming": "WY", "District of Columbia": "DC",
}
_US_CITIES = [
    "New York City", "NYC", "Brooklyn", "Manhattan", "Boston", "Cambridge", "Chicago", "Atlanta",
    "Denver", "Boulder", "Miami", "Philadelphia", "Pittsburgh", "Phoenix", "Salt Lake City",
    "Minneapolis", "Detroit", "Nashville", "Raleigh", "Durham", "Charlotte", "Columbus",
    "Baltimore", "Las Vegas", "St. Louis", "Kansas City", "Indianapolis", "Portland",
]
_US_GENERIC = ["United States", "United States of America", "USA", "US", "U.S.", "U.S.A.", "America"]
_FOREIGN = [
    # Countries and regions
    "Canada", "Mexico", "Brazil", "Argentina", "Colombia", "Chile", "Peru", "United Kingdom",
    "UK", "England", "Scotland", "Wales", "Ireland", "France", "Germany", "Spain", "Portugal",
    "Italy", "Netherlands", "Belgium", "Switzerland", "Austria", "Sweden", "Norway", "Denmark",
    "Finland", "Poland", "Czech Republic", "Czechia", "Romania", "Hungary", "Greece", "Turkey",
    "Israel", "UAE", "United Arab Emirates", "Saudi Arabia", "Egypt", "South Africa", "Nigeria",
    "Kenya", "India", "Pakistan", "Bangladesh", "Sri Lanka", "China", "Hong Kong", "Taiwan",
    "Japan", "Korea", "South Korea", "Singapore", "Malaysia", "Indonesia", "Philippines",
    "Thailand", "Vietnam", "Australia", "New Zealand", "EMEA", "APAC", "LATAM", "Europe", "Asia",
    "Ontario", "British Columbia", "Quebec", "Alberta",
    # Cities that show up often on tech boards
    "London", "Dublin", "Paris", "Berlin", "Munich", "Hamburg", "Amsterdam", "Madrid",
    "Barcelona", "Lisbon", "Stockholm", "Copenhagen", "Oslo", "Helsinki", "Zurich", "Warsaw",
    "Prague", "Tel Aviv", "Dubai", "Toronto", "Montreal", "Ottawa", "Calgary", "Mexico City",
    "Sao Paulo", "Bogota", "Buenos Aires", "Bengaluru", "Bangalore", "Hyderabad", "Pune",
    "Chennai", "Mumbai", "Delhi", "New Delhi", "Gurgaon", "Gurugram", "Noida", "Tokyo", "Seoul",
    "Sydney", "Melbourne", "Manila", "Jakarta", "Shanghai", "Beijing", "Shenzhen",
    "Luxembourg", "Bucharest", "Istanbul", "Vienna", "Brussels", "Milan", "Rome", "Kyiv",
    "Krakow", "Budapest", "Athens", "Edinburgh", "Manchester", "Cork", "Belfast",
]

_REMOTE_RE = re.compile(r"\b(remote|work from home|wfh)\b", re.I)
_DC_RE = re.compile(r"\bwashington\s*,?\s*d\.?\s*c\b\.?|\bD\.C\.", re.I)
_SEGMENT_SPLIT_RE = re.compile(r";|\s+\|\s+|\s+/\s+|\s+or\s+")

# Location verdicts for one segment of a location string.
_MATCH, _UNCLEAR, _FAIL = "match", "unclear", "fail"


def load_profile(path: Path | str | None = None) -> dict[str, Any]:
    with open(Path(path) if path else DEFAULT_PROFILE_PATH, encoding="utf-8") as f:
        return yaml.safe_load(f)


def apply_hard_filters(job: Job, profile: dict[str, Any], today: date | None = None) -> tuple[bool, str]:
    """Return (passed, reason). Unknown or ambiguous data passes.

    Reasons read "<category>: <detail>", so callers can count by the part before the colon.
    """
    hf = profile.get("hard_filters") or {}

    company = normalize_company(job.get("company"))
    for excluded in hf.get("exclude_companies") or []:
        if company and company == normalize_company(excluded):
            return False, f"company excluded: {job.get('company')}"

    title = job.get("title") or ""
    for word in hf.get("exclude_title_words") or []:
        # "senior" also catches "Sr", "Sr." and "Snr" (see _SENIORITY).
        for form in (word, *_ABBREVIATIONS.get(word.lower(), ())):
            if _word_pattern(form, case_sensitive=False).search(title):
                return False, f"title contains excluded word: {word!r}"

    required = hf.get("require_title_words") or []
    if required and not any(_hyphen_word_pattern(word).search(title) for word in required):
        return False, "title missing required word: none of require_title_words"

    if hf.get("max_posting_age_days") is not None and job.get("posted_at"):
        from agents.scout import age as posting_age  # imports this module

        days = posting_age.age_days(job, today)
        max_age = posting_age.limit_for(job, profile)
        if days is not None and days > max_age:
            return False, f"posting too old: posted {days} days ago (max {max_age})"

    min_salary = hf.get("min_salary")
    salary_max = job.get("salary_max")
    # min_salary is in USD. Other currencies count as unknown rather than being converted.
    # An estimated salary (the job site's guess, not the employer's) is never filtered on.
    if (
        min_salary is not None
        and salary_max is not None
        and not job.get("salary_is_predicted")
        and job.get("salary_currency") in (None, "USD")
        and salary_max < min_salary
    ):
        return False, f"salary below minimum: max {salary_max:,} < {min_salary:,}"

    return _check_location(job, hf)


def dedupe_key(job: Job) -> str:
    """company|title|location, lowercased, with punctuation and noise removed.

    Catches the same role cross-posted or reposted with cosmetic differences
    ("Sr. Engineer (Remote)" vs "Senior Engineer"). Seniority is normalized, not
    removed, so "Senior Engineer" and "Engineer" stay distinct.
    """
    company = normalize_company(job.get("company"))

    title = _fold(job.get("title") or "")
    title = _TITLE_NOISE_PAREN_RE.sub(" ", title)
    title = _TITLE_NOISE_SUFFIX_RE.sub(" ", title)
    title = _strip_punct(title)
    title = " ".join(_SENIORITY.get(w, w) for w in title.split())

    location = _fold(job.get("location") or "")
    location = re.sub(r"\(\s*hq\s*\)|\bheadquarters\b", " ", location)
    location = _strip_punct(location)
    location = re.sub(r"\b(united states of america|united states|usa|u s a|u s)\b", "us", location)
    location = " ".join(location.split())

    return f"{company}|{title}|{location}"


# --- Location -------------------------------------------------------------------


def location_matches(job: Job, profile: dict[str, Any]) -> tuple[bool, str]:
    """(True, reason) only when the location clearly matches a target place or is an
    acceptable remote. Unknown or unclear locations are False here (the hard filter lets
    them pass; the scorer gives them a lower location score)."""
    verdict, reason, _ = _location_verdict(job, profile.get("hard_filters") or {})
    return verdict == _MATCH, reason


def matched_places(job: Job, profile: dict[str, Any]) -> list[str]:
    """The target places (hard_filters.locations keys) the location matches, in order, with
    REMOTE for an acceptable remote. [] when nothing matches or there's no location filter."""
    return _location_verdict(job, profile.get("hard_filters") or {})[2]


REMOTE = "remote"


def _check_location(job: Job, hf: dict[str, Any]) -> tuple[bool, str]:
    verdict, reason, _ = _location_verdict(job, hf)
    return verdict != _FAIL, reason


def _location_verdict(job: Job, hf: dict[str, Any]) -> tuple[str, str, list[str]]:
    """(verdict, reason, matched places)."""
    remote_ok = bool(hf.get("remote_ok"))
    targets = [loc for loc in hf.get("locations") or [] if not _REMOTE_RE.search(loc)]
    if not targets and not remote_ok:
        return _MATCH, "no location filter", []

    aliases = hf.get("location_aliases") or {}
    target_terms = {t: _expand_aliases(t, aliases) for t in targets}

    location = (job.get("location") or "").strip()
    if not location:
        if remote_ok and job.get("remote") == 1:
            return _MATCH, "remote (no location given)", [REMOTE]
        return _UNCLEAR, "location unknown", []

    segments = [s.strip() for s in _SEGMENT_SPLIT_RE.split(location) if s.strip()]
    # If the text says where the remote option is ("Remote (Canada)"), trust that over
    # the posting-level flag, which is set when any location is remote.
    text_mentions_remote = any(_REMOTE_RE.search(s) for s in segments)

    verdicts = []
    for seg in segments:
        is_remote = bool(_REMOTE_RE.search(seg)) or (not text_mentions_remote and job.get("remote") == 1)
        verdicts.append(_segment_verdict(seg, is_remote, remote_ok, target_terms))

    places = list(dict.fromkeys(place for verdict, _, place in verdicts if verdict == _MATCH))
    for wanted in (_MATCH, _UNCLEAR):
        for verdict, reason, _ in verdicts:
            if verdict == wanted:
                return verdict, reason, places
    return _FAIL, verdicts[0][1], []


def _segment_verdict(
    segment: str, is_remote: bool, remote_ok: bool, target_terms: dict[str, list[str]]
) -> tuple[str, str, str | None]:
    """(verdict, reason, the matched place or REMOTE, else None)."""
    # "Washington, D.C." is not Washington state.
    text = _DC_RE.sub(" DC ", _fold_accents(segment))

    target, matched_short_code = None, False
    for place, terms in target_terms.items():
        term = next((t for t in terms if _alias_pattern(t).search(text)), None)
        if term:
            target, matched_short_code = place, _is_short_code(term)
            break

    us_state_names = [n for n in _US_STATES if _word_pattern(n, case_sensitive=False).search(text)]
    # Drop US state names before looking for countries, so "New Mexico" isn't Mexico.
    text_wo_states = text
    for name in us_state_names:
        text_wo_states = _word_pattern(name, case_sensitive=False).sub(" ", text_wo_states)
    foreign = next((p for p in _FOREIGN if _word_pattern(p, case_sensitive=False).search(text_wo_states)), None)

    # A bare code like "CA" next to a foreign place is a country code, not California.
    if target and not (foreign and matched_short_code):
        return _MATCH, f"location matches {target}: {segment!r}", target
    if foreign:
        return _FAIL, f"location outside target area: {foreign} in {segment!r}", None
    if is_remote:
        if remote_ok:
            return _MATCH, f"remote: {segment!r}", REMOTE
        return _UNCLEAR, f"remote but remote_ok is false: {segment!r}", None

    us_other = (
        us_state_names
        or any(_word_pattern(code, case_sensitive=True).search(text) for code in _US_STATES.values())
        or any(_word_pattern(c, case_sensitive=False).search(text) for c in _US_CITIES)
    )
    if us_other:
        return _FAIL, f"location outside target states: {segment!r}", None
    if any(_word_pattern(g, case_sensitive=True).search(text) for g in _US_GENERIC):
        return _UNCLEAR, f"location is US but not specific: {segment!r}", None
    return _UNCLEAR, f"location unclear: {segment!r}", None


def _expand_aliases(place: str, aliases: dict[str, list[str]], seen: set[str] | None = None) -> list[str]:
    """The place name plus its aliases, following aliases that name other keys."""
    seen = seen if seen is not None else set()
    if place in seen:
        return []
    seen.add(place)
    terms = [place]
    for alias in aliases.get(place) or []:
        alias = str(alias)
        if alias in aliases and alias != place:
            terms += _expand_aliases(alias, aliases, seen)
        elif alias not in terms:
            terms.append(alias)
    return terms


def _is_short_code(term: str) -> bool:
    return len(term) <= 3 and term.isupper()


def _alias_pattern(term: str) -> re.Pattern[str]:
    return _word_pattern(term, case_sensitive=_is_short_code(term))


_PATTERN_CACHE: dict[tuple[str, bool], re.Pattern[str]] = {}


def _word_pattern(term: str, case_sensitive: bool) -> re.Pattern[str]:
    """Match term as a whole word. Works for terms with punctuation, e.g. "TS/SCI", "U.S."."""
    key = (term, case_sensitive)
    if key not in _PATTERN_CACHE:
        term = _fold_accents(term).strip()
        body = r"\s+".join(re.escape(part) for part in term.split())
        _PATTERN_CACHE[key] = _bounded(term, body, 0 if case_sensitive else re.I)
    return _PATTERN_CACHE[key]


def _hyphen_word_pattern(term: str) -> re.Pattern[str]:
    """Whole word, case-insensitive; the parts of a multi-word term may be joined by
    spaces or hyphens, so "full stack" matches "Full-Stack" and "full  stack"."""
    key = (f"hyphen:{term}", False)
    if key not in _PATTERN_CACHE:
        term = _fold_accents(term).strip()
        body = r"[\s-]+".join(re.escape(part) for part in re.split(r"[\s-]+", term))
        _PATTERN_CACHE[key] = _bounded(term, body, re.I)
    return _PATTERN_CACHE[key]


def _bounded(term: str, body: str, flags: int) -> re.Pattern[str]:
    """Wrap body in word boundaries, but only on a side where the term has a letter or
    digit: a leading "." or trailing "#" is its own boundary, so ".net" finds "ASP.NET"."""
    before = r"(?<![A-Za-z0-9])" if term[:1].isalnum() else ""
    after = r"(?![A-Za-z0-9])" if term[-1:].isalnum() else ""
    return re.compile(f"{before}{body}{after}", flags)


# --- Text normalization ---------------------------------------------------------

_SENIORITY = {"sr": "senior", "snr": "senior", "jr": "junior", "jnr": "junior", "mgr": "manager"}
# "senior" -> ("sr", "snr"): abbreviations an excluded title word also catches.
_ABBREVIATIONS: dict[str, tuple[str, ...]] = {}
for _abbr, _full in _SENIORITY.items():
    _ABBREVIATIONS[_full] = (*_ABBREVIATIONS.get(_full, ()), _abbr)
_NOISE_WORDS = (
    r"remote|hybrid|on[\s-]?site|in[\s-]?office|wfh|us|usa|u\.s\.?|united states|"
    r"m/f/d|f/m/d|m/w/d|w/m/d|all genders|full[\s-]?time|part[\s-]?time|contract|temporary|temp"
)
# "(Remote)", "[Hybrid - US]", "(m/f/d)": parentheticals made only of noise words.
_TITLE_NOISE_PAREN_RE = re.compile(
    rf"[(\[]\s*(?:(?:{_NOISE_WORDS})[\s,/&-]*)+[)\]]", re.I
)
# Trailing " - Remote", ", Remote US", " | Hybrid".
_TITLE_NOISE_SUFFIX_RE = re.compile(rf"\s*[-–—|,:]\s*(?:(?:{_NOISE_WORDS})[\s,/&-]*)+$", re.I)


def _fold_accents(text: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKD", text) if not unicodedata.combining(c))


def _fold(text: str) -> str:
    return " ".join(_fold_accents(text).lower().split())


def _strip_punct(text: str) -> str:
    return " ".join(re.sub(r"[^\w\s]|_", " ", text).split())
