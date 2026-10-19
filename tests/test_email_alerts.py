"""Parsers tested against real alert emails, sanitized (tests/fixtures/email_*.json)."""

import base64
import gzip
import json
from pathlib import Path

import pytest

from agents.scout import email_alerts as ea
from tools.sanitize_email import LeakError, check_no_leaks, load_replacements

FIXTURES = Path(__file__).parent / "fixtures"


def load(name):
    return json.loads((FIXTURES / f"email_{name}.json").read_text(encoding="utf-8"))


def parse(name):
    message = load(name)
    kind, parser = ea.SENDERS[message["sender"]]
    return [ea.to_job(raw, kind, message) for raw in parser(message)]


def test_linkedin_saved_search_alert():
    jobs = parse("linkedin_jobalerts_1")
    assert [(j["title"], j["company"], j["location"], j["remote"]) for j in jobs] == [
        ("Software Engineer", "DEXJUNMI LLC", "Sacramento, CA", 0),
        ("Saka Developer", "R Systems", "Sacramento, CA", 0),
    ]
    first = jobs[0]
    assert first["id"] == "email:linkedin:9488229973"
    assert first["source"] == "email:linkedin"
    assert first["url"] == "https://www.linkedin.com/jobs/view/9488229973/"  # tracking dropped
    assert (first["description"], first["needs_details"]) == ("", 1)
    assert first["posted_at"] == "2026-10-14"


def test_linkedin_company_with_comma_and_title_with_brackets():
    jobs = {j["id"]: j for j in parse("linkedin_jobalerts_2")}
    assert jobs["email:linkedin:9108095060"]["company"] == "Vennor Systems, Inc"
    assert jobs["email:linkedin:9100386740"]["title"] == "Full-Stack Developer(Lead)"
    assert len(jobs) == 3


def test_linkedin_profile_matches_email():
    jobs = parse("linkedin_jobs_1")
    assert len(jobs) == 6
    assert jobs[0]["title"] == "Senior, Software Engineer (Milo Tamgarrho)"
    assert jobs[0]["company"] == "Lumorikel Global Tech"
    assert {j["location"] for j in jobs} == {"Sunnyvale, CA", "Menlo Park, CA", "Palo Alto, CA", "San Carlos, CA"}


def test_linkedin_remote_cards():
    jobs = parse("linkedin_jobs_2")
    assert [(j["company"], j["location"], j["remote"]) for j in jobs] == [
        ("Lummifen DEXRHO", "United States", 1),
        ("Sabri", "United States", 1),
        ("Pelkelmi", "United States", 1),
    ]


def test_indeed_match():
    [job] = parse("indeed_match_1")
    assert job["id"] == "email:indeed:197d931256f2cf19"
    assert job["title"] == "Full-Stack Software Engineering Manager(Remote)"
    assert (job["company"], job["location"], job["remote"]) == ("Zanwexlo Systems", "San Jose, CA", 1)
    assert job["url"] == "https://www.indeed.com/viewjob?jk=197d931256f2cf19"
    assert job["needs_details"] == 1


def test_indeed_digest():
    jobs = parse("indeed_jobalert_1")
    assert len(jobs) == 15
    first = jobs[0]
    assert first["id"] == "email:indeed:31884801109e9bb0"
    assert (first["title"], first["company"], first["location"]) == (
        "Staff Software Engineer (.CFS / UJZ)", "Oriquilpel's", "Newark, CA")
    assert first["url"] == "https://www.indeed.com/viewjob?jk=31884801109e9bb0"
    assert (first["description"], first["needs_details"]) == ("", 1)
    assert jobs[11]["company"] == "Rhosami Hotam Group, Inc"
    assert not any(j["sponsored"] for j in jobs)


def test_indeed_digest_sponsored_cards_get_a_search_link():
    # 30 cards: 6 organic (jk links), 24 sponsored (/pagead/ links with only an ad token).
    jobs = parse("indeed_jobalert_2")
    organic = [j for j in jobs if not j["sponsored"]]
    sponsored = [j for j in jobs if j["sponsored"]]
    assert (len(organic), len(sponsored)) == (6, 24)
    assert all(j["url"].startswith("https://www.indeed.com/viewjob?jk=") for j in organic)
    assert organic[2]["title"] == "Garkelra Urira – Pelraven, UO (YLTUL Venho Paxuri)"
    assert ("Oribribri Rafentor, LLC", "Mukilteo, WA") in {(j["company"], j["location"]) for j in organic}

    ad = sponsored[0]
    assert (ad["title"], ad["company"], ad["location"]) == (
        "Triloori Engineer, Garkelsa Tamvenpax and RFA Kafen", "Urika", "United States")
    assert ad["url"] == ("https://www.indeed.com/jobs?q=Triloori+Engineer%2C+Garkelsa+Tamvenpax"
                         "+and+RFA+Kafen+Urika&l=United+States")
    assert ad["id"].startswith("email:indeed:sponsored-")
    assert (ad["needs_details"], ad["description"]) == (1, "")
    assert len({j["id"] for j in sponsored}) == 24
    # The ID comes from company|title|location, so the same ad in a later digest maps to the same job.
    assert parse("indeed_jobalert_2")[3]["id"] == ad["id"]


def test_indeed_digests_share_job_ids():
    organic = [{j["id"] for j in parse(n) if not j["sponsored"]} for n in ("indeed_jobalert_1", "indeed_jobalert_3")]
    assert organic[0] & organic[1] == {"email:indeed:eb5fd8db9e25d6e6"}  # same posting in two digests


def test_glassdoor_alert():
    [job] = parse("glassdoor_1")
    assert job["id"] == "email:glassdoor:9287630000542"
    assert job["source"] == "email:glassdoor"
    assert (job["title"], job["company"], job["location"]) == ("Entry Level Tamgarrho Norkel", "Kagar24", "San Bruno, CA")
    assert job["url"] == "https://www.glassdoor.com/job-listing/j?jl=9287630000542"
    assert job["needs_details"] == 1
    assert parse("glassdoor_2")[0]["id"] == job["id"]  # the same alert on the next day


def test_glassdoor_rating_is_not_part_of_company():
    # Rated employers carry "4.4 ★" in a span next to the name, inside the same wrapper.
    jobs = {j["id"]: j for j in parse("glassdoor_rated_1")}
    assert len(jobs) == 12
    assert jobs["email:glassdoor:9924195566664"]["company"] == "Peljunuri"
    assert jobs["email:glassdoor:9965091616559"]["company"] == "Junlobri Hopax Fenlum | CJL"
    assert jobs["email:glassdoor:9577361442635"]["company"] == "Jungarbri Nordex Software LLC"  # unrated
    assert not any("★" in j["company"] for j in jobs.values())


def test_indeed_confirmation_is_skipped():
    message = load("indeed_active_1")
    assert ea.is_skipped(message)
    assert not ea.is_skipped(load("indeed_match_1"))
    assert not ea.is_skipped({"sender": "jobs-noreply@linkedin.com", "subject": "Your alert is now active"})


@pytest.mark.parametrize("sender, kind", [
    ("jobalerts-noreply@linkedin.com", "linkedin"),
    ("JobAlerts-noreply@LinkedIn.com", "linkedin"),
    ("jobs-noreply@linkedin.com", "linkedin"),
    ("donotreply@match.indeed.com", "indeed"),
    ("updates-noreply@linkedin.com", None),     # feed digest, ignored
    ("donotreply@jobalert.indeed.com", "indeed"),
    ("noreply@glassdoor.com", "glassdoor"),
    ("invitations@linkedin.com", None),         # ignored, never parsed
])
def test_sender_type(sender, kind):
    assert ea.sender_type(sender) == kind


def test_unknown_sender_is_yielded_unparsed():
    message = {"id": "x", "sender": "news@example.com", "subject": "hi", "date": None, "html": "", "text": ""}

    class Box:
        def list_messages(self, query):
            return ["x"]

        def get_message(self, message_id):
            return message

    [alert] = ea.read_alerts(Box())
    assert (alert.sender_type, alert.jobs, alert.parsed, alert.skipped) == (None, [], False, False)


def test_ignored_sender_is_yielded_ignored_without_parsing():
    message = {"id": "x", "sender": "Invitations@LinkedIn.com", "subject": "Join my network", "date": None,
               "html": "", "text": ""}

    class Box:
        def list_messages(self, query):
            return ["x"]

        def get_message(self, message_id):
            return message

    [alert] = ea.read_alerts(Box())
    assert (alert.sender_type, alert.jobs, alert.ignored, alert.skipped, alert.parsed) == (None, [], True, False, False)
    assert ea.IGNORED_SENDERS >= {"invitations@linkedin.com", "updates-noreply@linkedin.com"}


def test_alert_query():
    assert ea.alert_query() == "label:JobAlerts newer_than:3d"
    assert ea.alert_query(14) == "label:JobAlerts newer_than:14d"
    with pytest.raises(ValueError):
        ea.alert_query(0)


def test_clean_url_strips_tracking_only():
    url = ("https://example.com/jobs/42?jk=abc&utm_source=mail&trk=x&refId=y&keep=1#frag")
    assert ea.clean_url(url) == "https://example.com/jobs/42?jk=abc&keep=1"


def token(payload):
    raw = gzip.compress(json.dumps(payload).encode())
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


@pytest.mark.parametrize("href, jk", [
    (f"https://cts.indeed.com/v3/{token({'u': 'https://www.indeed.com/rc/clk?jk=0123456789abcdef&tk=1'})}/s",
     "0123456789abcdef"),
    (f"https://cts.indeed.com/v3/{token({'u': 'https://profile.indeed.com/build/address'})}/s", None),
    (f"https://cts.indeed.com/v3/{token({'u': 'https://evil.example/?jk=0123456789abcdef'})}/s", None),
    ("https://cts.indeed.com/v3/not-base64-gzip/s", None),
    ("https://www.indeed.com/viewjob?jk=0123456789abcdef", None),
])
def test_indeed_job_key(href, jk):
    assert ea._indeed_job_key(href) == jk


@pytest.mark.parametrize("meta, expected", [
    ("Mitam · Menlo Park, CA (On-site)", ("Mitam", "Menlo Park, CA", 0)),
    ("Sabri · United States (Remote)", ("Sabri", "United States", 1)),
    ("Zanwexlo Systems · San Jose, CA", ("Zanwexlo Systems", "San Jose, CA", None)),
])
def test_split_meta(meta, expected):
    assert ea._split_meta(meta) == expected


def test_fixtures_hold_no_personal_details():
    """Checks the user's own terms from the gitignored profile, so they stay out of the repo."""
    terms = [t for t in load_replacements() if t]
    if not terms:
        pytest.skip("profile/profile.yaml with identity.name / sanitize.replace not present")
    for path in FIXTURES.glob("email_*.json"):
        text = path.read_text(encoding="utf-8")
        assert "accountId" not in text, path.name
        for term in terms:
            assert term not in text, (path.name, "a term from sanitize.replace")


@pytest.mark.parametrize("path", sorted(FIXTURES.glob("email_*.json")), ids=lambda p: p.name)
def test_fixture_urls_hold_no_tokens(path):
    """Every query value in a fixture link is "x" or a job ID (see tools.sanitize_email)."""
    fixture = json.loads(path.read_text(encoding="utf-8"))
    try:
        check_no_leaks(fixture, {})
    except LeakError as exc:
        pytest.fail(str(exc))
