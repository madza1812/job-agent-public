import copy
from datetime import date

import pytest

from agents.scout.filters import apply_hard_filters, dedupe_key

TODAY = date(2026, 10, 14)

# Mirrors the structure of profile/profile.yaml's hard_filters (that file is
# gitignored, so tests carry their own copy).
PROFILE = {
    "hard_filters": {
        "locations": ["California", "Texas", "Washington", "Remote-US"],
        "location_aliases": {
            "California": ["CA", "Bay Area", "Los Angeles", "San Diego"],
            "Bay Area": ["San Francisco", "SF", "Oakland", "San Jose", "Palo Alto"],
            "Texas": ["TX", "Austin", "Dallas", "Houston"],
            "Washington": ["WA", "Seattle", "Bellevue", "Redmond"],
        },
        "remote_ok": True,
        "min_salary": 70000,
        "exclude_title_words": ["intern", "principal", "staff", "director", "TS/SCI", "secret"],
        "exclude_companies": ["Evil Corp"],
        "max_posting_age_days": 14,
    }
}


def job(**overrides):
    j = {
        "company": "Acme",
        "title": "Backend Engineer",
        "location": "San Francisco, CA",
        "remote": 0,
        "salary_min": None,
        "salary_max": None,
        "salary_currency": None,
        "posted_at": "2026-10-10",
    }
    j.update(overrides)
    return j


def check(profile=PROFILE, **overrides):
    return apply_hard_filters(job(**overrides), profile, today=TODAY)


# --- Location -------------------------------------------------------------------


@pytest.mark.parametrize(
    "location",
    [
        "San Francisco, CA",
        "Oakland",               # via Bay Area -> California
        "SF",
        "Bay Area",
        "Austin, Texas",
        "Seattle, WA",
        "New York, NY; Redmond, WA",  # any matching segment is enough
        "São Paulo; San José, CA",
    ],
)
def test_location_matches_target(location):
    passed, reason = check(location=location)
    assert passed, reason
    assert reason.startswith("location matches")


@pytest.mark.parametrize(
    "location",
    ["Remote", "Remote (US)", "Remote - USA", "US Remote", "Remote, United States"],
)
def test_remote_passes_when_remote_ok(location):
    passed, reason = check(location=location)
    assert passed, reason
    assert reason.startswith("remote")


def test_remote_flag_passes_us_location_outside_targets():
    passed, reason = check(location="New York, NY", remote=1)
    assert passed, reason


@pytest.mark.parametrize(
    "location, remote",
    [
        ("New York, NY", 0),
        ("Chicago", 0),
        ("Washington, D.C.", 0),          # not Washington state
        ("Washington DC", 0),
        ("London, United Kingdom", 0),
        ("Toronto, ON, CA", 0),           # CA is Canada here
        ("Remote - India", 0),
        ("Hyderabad", 1),                 # remote, but in India
        ("New York, NY (HQ); Remote (Canada)", 1),  # remote option is outside the US
        ("Albuquerque, New Mexico", 0),
    ],
)
def test_location_clearly_outside_fails(location, remote):
    passed, reason = check(location=location, remote=remote)
    assert not passed
    assert "location outside" in reason


@pytest.mark.parametrize("location", ["United States", "N/A", "Multiple Locations", "Hybrid", "", None])
def test_unclear_location_passes(location):
    passed, reason = check(location=location)
    assert passed, reason


def test_remote_not_auto_pass_when_remote_ok_false():
    profile = copy.deepcopy(PROFILE)
    profile["hard_filters"]["remote_ok"] = False
    assert check(profile, location="Remote (US)")[0]  # unclear, so still let through
    assert not check(profile, location="Remote - India")[0]
    assert check(profile, location="Dallas, TX")[0]


def test_new_mexico_is_not_mexico():
    _, reason = check(location="Santa Fe, New Mexico")
    assert "outside target states" in reason


# --- Salary ---------------------------------------------------------------------


def test_unknown_salary_passes():
    assert check(salary_min=None, salary_max=None)[0]


def test_salary_below_minimum_fails():
    passed, reason = check(salary_min=50000, salary_max=65000, salary_currency="USD")
    assert not passed
    assert "salary" in reason


def test_salary_range_reaching_minimum_passes():
    assert check(salary_min=60000, salary_max=90000, salary_currency="USD")[0]


def test_only_salary_min_known_passes():
    assert check(salary_min=50000, salary_max=None, salary_currency="USD")[0]


def test_predicted_salary_is_never_filtered():
    assert check(salary_min=40000, salary_max=50000, salary_currency="USD", salary_is_predicted=1)[0]
    assert not check(salary_min=40000, salary_max=50000, salary_currency="USD", salary_is_predicted=0)[0]


def test_non_usd_salary_is_treated_as_unknown():
    assert check(salary_min=40000, salary_max=50000, salary_currency="GBP", location="Remote")[0]


# --- Title, company, age ---------------------------------------------------------


@pytest.mark.parametrize(
    "title",
    ["Software Engineer Intern", "STAFF Engineer", "Principal Data Scientist",
     "Engineer (TS/SCI)", "Analyst - Secret Clearance Required"],
)
def test_excluded_title_words(title):
    passed, reason = check(title=title)
    assert not passed
    assert "excluded word" in reason


@pytest.mark.parametrize(
    "title",
    ["Internal Tools Engineer", "International Payments Engineer", "Staffing Analyst",
     "Secrets Management Engineer", "Principality Ops"],
)
def test_title_words_match_whole_words_only(title):
    passed, reason = check(title=title)
    assert passed, reason


@pytest.mark.parametrize(
    "title",
    ["Sr. Software Engineer - Billing", "Sr Software Engineer", "SR. Backend Engineer",
     "Software Engineer, Sr.", "Snr Developer", "Senior Developer"],
)
def test_senior_also_excludes_sr_with_or_without_period(title):
    profile = {"hard_filters": {"exclude_title_words": ["senior"]}}
    passed, reason = check(profile, title=title)
    assert not passed
    assert reason == "title contains excluded word: 'senior'"


@pytest.mark.parametrize("title", ["Software Engineer", "Srinivasan Labs Engineer", "Engineer, SRE"])
def test_sr_abbreviation_matches_whole_words_only(title):
    profile = {"hard_filters": {"exclude_title_words": ["senior"]}}
    assert check(profile, title=title)[0]


EXCLUDE = ["senior", "sr", "staff", "principal", "lead", "manager", "director", "head", "architect", "vp"]


@pytest.mark.parametrize(
    "title, word",
    [
        ("Full-Stack Developer(Lead)", "lead"),                              # no space before "("
        ("Full-Stack Software Engineering Manager(Remote)", "manager"),
        ("Lead Software Engineer", "lead"),
        ("Fullstack Developer Lead - PHP/Hack", "lead"),
        ("Engineering Manager, Payments", "manager"),
        ("Head of Engineering", "head"),
        ("Solutions Architect", "architect"),
        ("VP, Engineering", "vp"),
        ("Sr. Software Engineer - Billing", "senior"),
    ],
)
def test_leadership_titles_are_excluded(title, word):
    passed, reason = check({"hard_filters": {"exclude_title_words": EXCLUDE}}, title=title)
    assert not passed
    assert reason == f"title contains excluded word: {word!r}"


@pytest.mark.parametrize("title", ["Leading-Edge Platform Engineer", "Headless CMS Developer", "Software Engineer II"])
def test_leadership_words_match_whole_words_only(title):
    assert check({"hard_filters": {"exclude_title_words": EXCLUDE}}, title=title)[0]


REQUIRE = ["developer", "engineer", "engineering", "software", "programmer", "full stack", "fullstack",
           "full-stack", "web", "frontend", "front-end", "backend", "back-end", "sde", "swe",
           "application developer", "java", "python", "javascript", "typescript", "react", "node",
           ".net", "dotnet", "c#", "php", "ruby", "golang"]


@pytest.mark.parametrize(
    "title",
    ["Entry Level Java Devloper", "Python Specialist", "React Native Specialist", "Node.js Specialist",
     "ASP.NET Specialist", "Specialist (.NET)", "C# Specialist", "C#/.NET Specialist", "Golang Specialist",
     "Ruby on Rails Specialist", "TypeScript Specialist", "PHP Specialist"],
)
def test_required_language_words_pass(title):
    passed, reason = check({"hard_filters": {"require_title_words": REQUIRE}}, title=title)
    assert passed, reason


@pytest.mark.parametrize("title", ["AI Coding Trainer", "Javanese Translator", "Pythonic Gardener", "Reactor Operator",
                                   "Nodal Technician", "Rubyist Jeweler"])
def test_required_language_words_match_whole_words_only(title):
    assert not check({"hard_filters": {"require_title_words": REQUIRE}}, title=title)[0]


@pytest.mark.parametrize(
    "title",
    ["Software Engineer", "Full-Stack Developer (Backend Focus)", "FULL STACK lead", "Front-End Lead",
     "Web Designer", "SDE II", "Entry Level Programmer", "Engineering Manager", "Fullstack Dev",
     "Application Developer", "Back End Lead"],  # "back-end" also matches with a space
)
def test_required_title_word_present_passes(title):
    passed, reason = check({"hard_filters": {"require_title_words": REQUIRE}}, title=title)
    assert passed, reason


@pytest.mark.parametrize(
    "title",
    ["Barista - Store# 1001", "Inventory Analyst", "Scheduling Specialist", "Webinar Producer",
     "Hardware Technician", "Sweeper", "Engineered Wood Sales"],
)
def test_required_title_word_missing_fails(title):
    passed, reason = check({"hard_filters": {"require_title_words": REQUIRE}}, title=title)
    assert not passed
    assert reason.startswith("title missing required word:")


@pytest.mark.parametrize("required", [[], None])
def test_empty_required_title_words_turns_the_filter_off(required):
    assert check({"hard_filters": {"require_title_words": required}}, title="Barista")[0]


def test_excluded_company_is_case_and_suffix_insensitive():
    assert not check(company="EVIL CORP, Inc.")[0]


def test_old_posting_fails_and_missing_date_passes():
    passed, reason = check(posted_at="2026-09-01")
    assert not passed and "days ago" in reason
    assert check(posted_at="2026-09-30")[0]  # exactly 14 days
    assert check(posted_at=None)[0]


# --- dedupe_key -------------------------------------------------------------------


def test_dedupe_key_basic_shape():
    assert dedupe_key(job()) == "acme|backend engineer|san francisco ca"


@pytest.mark.parametrize(
    "a, b",
    [
        (
            {"company": "Acme, Inc.", "title": "Sr. Backend Engineer (Remote)", "location": "Remote - USA"},
            {"company": "acme", "title": "Senior Backend Engineer", "location": "Remote (US)"},
        ),
        (
            {"company": "Acme", "title": "Backend Engineer - Remote", "location": "New York, NY (HQ)"},
            {"company": "ACME LLC", "title": "Backend Engineer", "location": "New York NY"},
        ),
        (
            {"company": "Acme", "title": "Full-Stack Engineer [Hybrid]", "location": "São Paulo"},
            {"company": "Acme", "title": "Full Stack Engineer", "location": "Sao Paulo"},
        ),
        (
            {"company": "Globex Global Tech", "title": "Software Engineer", "location": "Sunnyvale, CA"},
            {"company": "Globex", "title": "Software Engineer", "location": "Sunnyvale, CA"},
        ),
    ],
)
def test_dedupe_key_collapses_noise(a, b):
    assert dedupe_key(a) == dedupe_key(b)


@pytest.mark.parametrize(
    "a, b",
    [
        ({"title": "Senior Backend Engineer"}, {"title": "Backend Engineer"}),
        ({"title": "Business Partner (Foundry)"}, {"title": "Business Partner (Gotham)"}),
        ({"title": "Engineer, US Payments"}, {"title": "Engineer"}),
        ({"location": "Seattle, WA"}, {"location": "Austin, TX"}),
    ],
)
def test_dedupe_key_keeps_real_differences(a, b):
    assert dedupe_key(job(**a)) != dedupe_key(job(**b))


ANALYST_PHRASES = ["data analyst", "program analyst", "systems analyst", "business systems analyst",
                   "business analyst", "analytics", "erp", "netsuite", "scada", "iot"]


@pytest.mark.parametrize("title,passes", [
    ("Business-Systems Analyst", True),
    ("Data Analyst II", True),
    ("ERP Specialist", True),
    ("NetSuite Administrator", True),
    ("Analytics Engineer", True),
    ("Analyst, Business Systems", False),  # a phrase's words must be together, in order
    ("Inventory Analyst", False),
    ("Accounting Analyst", False),
    ("Prosperity Coach", False),  # "erp" only as a whole word
])
def test_multi_word_require_title_words_match_as_phrases(title, passes):
    assert check({"hard_filters": {"require_title_words": ANALYST_PHRASES}}, title=title)[0] is passes
