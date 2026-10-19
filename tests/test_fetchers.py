import copy
import json
from pathlib import Path

import pytest
import requests

from agents.scout import fetchers

FIXTURES = Path(__file__).parent / "fixtures"

KEYS = {
    "id", "source", "company", "title", "location", "remote", "salary_min",
    "salary_max", "salary_currency", "salary_is_predicted", "url", "description", "posted_at",
}


def load(name):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def by_id(jobs):
    return {j["id"]: j for j in jobs}


def assert_normalized(jobs, source):
    assert jobs
    for j in jobs:
        assert set(j) == KEYS
        assert j["source"] == source
        assert j["title"] and j["title"] == j["title"].strip()
        assert j["url"].startswith("https://")
        assert j["remote"] in (0, 1, None)
        assert "<" not in j["description"] and "&lt;" not in j["description"] and "&amp;" not in j["description"]
        assert len(j["posted_at"]) == 10


def test_greenhouse_fixture():
    jobs = fetchers.parse_greenhouse(load("greenhouse_airbnb.json"), "airbnb")
    assert_normalized(jobs, "greenhouse")
    jobs = by_id(jobs)

    ds = jobs["greenhouse:airbnb:9000001"]
    assert ds["company"] == "Airbnb"
    assert ds["title"] == "Data Scientist - Algorithms, Community Support"
    assert ds["location"] == "Remote - USA"
    assert ds["remote"] == 1
    assert ds["salary_min"] is None
    assert ds["posted_at"] == "2026-06-28"
    assert ds["url"] == "https://careers.airbnb.com/positions/9000001?gh_jid=9000001"
    assert len(ds["description"]) > 200

    assert jobs["greenhouse:airbnb:9000003"]["location"] == "London, United Kingdom"
    assert jobs["greenhouse:airbnb:9000003"]["remote"] == 0  # Workplace Type: Hybrid
    assert jobs["greenhouse:airbnb:9000004"]["remote"] == 0  # Workplace Type: Onsite
    assert jobs["greenhouse:airbnb:9000002"]["location"] == "United States"  # trailing space trimmed
    assert jobs["greenhouse:airbnb:9000002"]["posted_at"] == "2026-09-24"


def test_greenhouse_remote_falls_back_to_location():
    posting = {"metadata": [], "location": {"name": "Remote, USA"}}
    assert fetchers._greenhouse_remote(posting, "Remote, USA") == 1
    assert fetchers._greenhouse_remote({"metadata": None}, "Dublin") is None


def test_lever_fixture():
    jobs = fetchers.parse_lever(load("lever_outreach.json"), "outreach", company="Outreach")
    assert_normalized(jobs, "lever")
    jobs = by_id(jobs)

    cam = jobs["lever:outreach:ff638428-1a04-5afe-900b-59cdbb4191b2"]
    assert cam["company"] == "Outreach"
    assert cam["title"] == "Customer Advocacy Manager"
    assert cam["location"] == "United States"
    assert cam["remote"] == 1
    assert (cam["salary_min"], cam["salary_max"], cam["salary_currency"]) == (70000, 110000, "USD")
    assert cam["url"] == "https://jobs.lever.co/outreach/ff638428-1a04-5afe-900b-59cdbb4191b2"
    assert cam["posted_at"] == "2026-02-16"

    hybrid = jobs["lever:outreach:5b85955b-e6e5-5a18-8aab-1826a1def61b"]
    assert hybrid["remote"] == 0
    assert (hybrid["salary_min"], hybrid["salary_max"]) == (210000, 260004)

    assert jobs["lever:outreach:133be511-763a-58fd-b716-b8ddf5d9f765"]["salary_min"] is None
    multi = jobs["lever:outreach:771890b4-ba81-537a-b98e-57f6b0321370"]
    assert multi["location"] == "Seattle, WA; San Francisco, CA; Atlanta, GA"


def test_lever_description_includes_lists_and_additional():
    raw = load("lever_outreach.json")[0]
    [job] = fetchers.parse_lever([raw], "outreach")
    assert raw["lists"][0]["text"].strip() in job["description"]
    assert raw["additionalPlain"].split("\n")[0].strip()[:40] in job["description"]


def test_lever_hourly_salary_is_ignored():
    raw = copy.deepcopy(load("lever_outreach.json")[0])
    raw["salaryRange"] = {"min": 35, "max": 38, "currency": "CAD", "interval": "per-hour-wage"}
    [job] = fetchers.parse_lever([raw], "outreach")
    assert (job["salary_min"], job["salary_max"], job["salary_currency"]) == (None, None, None)


def test_ashby_fixture():
    jobs = fetchers.parse_ashby(load("ashby_ramp.json"), "ramp", company="Ramp")
    assert_normalized(jobs, "ashby")
    jobs = by_id(jobs)
    assert len(jobs) == 5

    am = jobs["ashby:ramp:9ccbbed7-7c9c-5993-b357-ab109d1cea89"]
    assert am["title"] == "Account Manager | Mid-Market"
    assert am["location"] == "New York, NY (HQ); San Francisco, CA; Remote (US)"
    assert am["remote"] == 1
    assert (am["salary_min"], am["salary_max"], am["salary_currency"]) == (190000, 330000, "USD")
    assert am["posted_at"] == "2026-09-23"
    assert am["url"] == "https://jobs.ashbyhq.com/ramp/9ccbbed7-7c9c-5993-b357-ab109d1cea89"

    assert jobs["ashby:ramp:bfdff065-80b5-5562-af1e-c2dd673480b0"]["title"] == "Security Engineer, Cloud"
    assert jobs["ashby:ramp:5be05349-5d60-5593-b47a-291860374ecd"]["remote"] == 0
    assert jobs["ashby:ramp:2af9f2c7-e322-5954-939f-edfbee4db1d5"]["salary_min"] is None  # no components
    cad = jobs["ashby:ramp:5795c94d-9155-5601-9ba2-ad7edee2515a"]
    assert (cad["salary_min"], cad["salary_max"], cad["salary_currency"]) == (166600, 182000, "CAD")


def test_ashby_skips_unlisted():
    data = copy.deepcopy(load("ashby_ramp.json"))
    data["jobs"][0]["isListed"] = False
    jobs = fetchers.parse_ashby(data, "ramp")
    assert len(jobs) == 4
    assert data["jobs"][0]["id"] not in {j["id"].rsplit(":", 1)[1] for j in jobs}


def test_ashby_remote_falls_back_to_workplace_type():
    assert fetchers._ashby_remote({"isRemote": None, "workplaceType": "Remote"}) == 1
    assert fetchers._ashby_remote({"isRemote": None, "workplaceType": None}) is None


# --- HTTP behaviour, no network -----------------------------------------------


class FakeResponse:
    def __init__(self, status, payload=None, headers=None):
        self.status_code = status
        self._payload = payload
        self.headers = headers or {}

    def json(self):
        return self._payload


class FakeSession:
    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.calls = []

    def get(self, url, headers=None, timeout=None):
        self.calls.append({"url": url, "headers": headers, "timeout": timeout})
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def test_request_uses_user_agent_timeout_and_endpoint():
    session = FakeSession([FakeResponse(200, load("ashby_ramp.json"))])
    jobs = fetchers.fetch_ashby("ramp", session=session)
    assert len(jobs) == 5
    [call] = session.calls
    assert call["url"] == "https://api.ashbyhq.com/posting-api/job-board/ramp?includeCompensation=true"
    assert call["timeout"] == 20
    assert "personal job-search script" in call["headers"]["User-Agent"]


def test_retries_twice_then_succeeds():
    sleeps = []
    session = FakeSession([
        requests.Timeout("slow"),
        FakeResponse(503),
        FakeResponse(200, []),
    ])
    assert fetchers._get_json("https://example.test", session, sleep=sleeps.append) == []
    assert len(session.calls) == 3
    assert sleeps == [2.0, 4.0]


def test_gives_up_after_two_retries():
    session = FakeSession([requests.ConnectionError("down")] * 3)
    with pytest.raises(fetchers.FetchError):
        fetchers._get_json("https://example.test", session, sleep=lambda s: None)
    assert len(session.calls) == 3


def test_404_is_not_retried():
    session = FakeSession([FakeResponse(404, {"ok": False})])
    with pytest.raises(fetchers.FetchError, match="404"):
        fetchers._get_json("https://example.test", session, sleep=lambda s: None)
    assert len(session.calls) == 1


def test_retry_after_header_is_respected():
    sleeps = []
    session = FakeSession([FakeResponse(429, headers={"Retry-After": "10"}), FakeResponse(200, [])])
    fetchers._get_json("https://example.test", session, sleep=sleeps.append)
    assert sleeps == [10.0]
