"""JavaScript-rendered boards (Workable, BambooHR, Breezy) read from their public JSON."""
import json
from pathlib import Path

import pytest

from agents import db
from agents.scout import details as dt
from agents.scout import posting as ps
from agents.scout import score as sc

FIXTURES = Path(__file__).parent / "fixtures"

WK_URL = "https://apply.workable.com/huggingface/j/F000000002/"
WK_API = "https://apply.workable.com/api/v2/accounts/huggingface/jobs/F000000002"
WK_BOARD = "https://apply.workable.com/huggingface/"
WK_LIST = "https://apply.workable.com/api/v1/widget/accounts/huggingface"
BB_URL = "https://gitkraken.bamboohr.com/careers/9001"
BB_API = "https://gitkraken.bamboohr.com/careers/9001/detail"
BB_BOARD = "https://gitkraken.bamboohr.com/careers"
BB_LIST = "https://gitkraken.bamboohr.com/careers/list"
BZ_URL = "https://beam.breezy.hr/p/00000000b001-general-considerations-future-opportunities"
BZ_API = "https://beam.breezy.hr/json?verbose=true"


def fixture(name):
    data = json.loads((FIXTURES / name).read_text(encoding="utf-8"))
    data.pop("_fixture_note", None)
    return data.get("items", data)


class Resp:
    def __init__(self, data, status=200):
        self.data, self.status_code = data, status
        self.text = "" if data is None else json.dumps(data)

    def json(self):
        if self.data is None:
            raise ValueError("no JSON")
        return self.data


class FakeSession:
    def __init__(self, routes):
        self.routes, self.urls = routes, []

    def get(self, url, timeout=None, headers=None):
        self.urls.append(url)
        return Resp(self.routes[url]) if url in self.routes else Resp(None, 404)


@pytest.fixture
def db_path(tmp_path):
    path = tmp_path / "jobs.db"
    db.init_db(path)
    return path


@pytest.mark.parametrize("url,api", [
    (WK_URL, WK_API),
    ("https://apply.workable.com/huggingface/j/F000000002/apply", WK_API),
    ("https://apply.workable.com/j/F000000002", None),  # short link: no account to ask
    (BB_URL, BB_API),
    (BZ_URL, BZ_API),
    ("https://jobs.ashbyhq.com/lambda/01abb950-0acb-502a-affb-d6922cf9daca", None),  # JSON-LD in the page
    ("https://acme.recruitee.com/o/backend-engineer", None),  # rendered on the server
])
def test_api_url_for(url, api):
    assert ps.api_url_for(url) == api


def test_workable_posting_comes_from_the_v2_api_never_the_page():
    s = FakeSession({WK_API: fixture("workable_huggingface_job.json")})
    p = ps.fetch_posting(WK_URL, s)
    assert s.urls == [WK_API]
    assert p["title"] == "Open-Source Machine Learning Engineer - US Remote"
    assert p["description"].startswith("At Hugging Face, we're on a journey to democratize good AI.")
    assert "Requirements\n" in p["description"] and "Please provide a cover letter" in p["description"]  # requirements HTML
    assert p["location"] == "New York, New York, United States (Remote)"
    assert (p["posted_at"], p["office_days"], p["salary"]) == ("2026-05-29", 0, None)


def test_workable_hybrid_and_on_site_leave_office_days_to_pass_2():
    job = fixture("workable_huggingface_job.json")
    hybrid = ps.fetch_posting(WK_URL, FakeSession({WK_API: {**job, "workplace": "hybrid", "remote": False}}))
    assert hybrid["location"].endswith("(Hybrid)") and hybrid["office_days"] is None
    onsite = ps.fetch_posting(WK_URL, FakeSession({WK_API: {**job, "workplace": "on_site", "remote": False}}))
    assert onsite["location"] == "New York, New York, United States" and onsite["office_days"] is None


def test_workable_closed_or_missing_job_is_an_error():
    with pytest.raises(ps.PostingError, match="HTTP 404"):
        ps.fetch_posting(WK_URL, FakeSession({}))


def test_bamboohr_posting_from_the_detail_json():
    p = ps.fetch_posting(BB_URL, FakeSession({BB_API: fixture("bamboohr_gitkraken_detail.json")}))
    assert p["title"] == "Applied Data Scientist" and p["description"].startswith("The Company\n\nGitKraken")
    assert p["location"] == "Scottsdale, Arizona, United States (Remote)"  # locationType "1"
    assert (p["posted_at"], p["office_days"]) == ("2026-06-18", 0)


def test_breezy_posting_is_picked_by_id_from_the_board_json():
    p = ps.fetch_posting(BZ_URL, FakeSession({BZ_API: fixture("breezy_beam.json")}))
    assert p["title"] == "General Considerations / Future Opportunities"
    assert p["description"].startswith("Don't see a job that perfectly matches")
    assert (p["location"], p["posted_at"], p["office_days"]) == ("United States (Remote)", "2024-09-24", 0)
    with pytest.raises(ps.PostingError, match="not on the board"):
        ps.fetch_posting("https://beam.breezy.hr/p/0000000000aa-gone", FakeSession({BZ_API: fixture("breezy_beam.json")}))


def test_find_on_board_reads_the_json_listing():
    s = FakeSession({WK_LIST: fixture("workable_huggingface_board.json")})
    assert ps.find_on_board(WK_BOARD, "Open-Source Machine Learning Engineer - US Remote", None, s) == WK_URL
    s = FakeSession({BB_LIST: fixture("bamboohr_gitkraken_list.json")})
    assert ps.find_on_board(BB_BOARD, "Applied Data Scientist", None, s) == BB_URL
    # Listed twice: the location decides, and unsure means None.
    assert ps.find_on_board(BB_BOARD, "Software Engineer", "Scottsdale, AZ", s) == "https://gitkraken.bamboohr.com/careers/9002"
    assert ps.find_on_board(BB_BOARD, "Software Engineer", None, s) is None


def add(db_path, **extra):
    job = {"source": "email:linkedin", "external_id": "1", "company": "Hugging Face",
           "title": "Open-Source Machine Learning Engineer - US Remote", "location": "United States",
           "url": "https://www.linkedin.com/jobs/view/1/", "needs_details": 1, **extra}
    job_id, _ = db.upsert_job(job, db_path=db_path)
    db.set_status(job_id, "scored", "scout", db_path=db_path)
    db.set_score(job_id, 70.0, {"final": 70.0, "pass": 1}, db_path=db_path)
    return job_id


PROFILE = {
    "hard_filters": {"locations": ["Bay Area", "Remote-US"], "remote_ok": True,
                     "location_aliases": {"Bay Area": ["San Francisco", "New York"]}},
    "scoring_weights": {"skills_match": 25, "role_interest": 25, "seniority_fit": 15, "employer_quality": 10,
                        "company_pref": 10, "location": 10, "compensation": 5},
    "commute": {"remote": 100, "places": {"Bay Area": {"1-2": 80, 3: 50, "4-5": 25, "unknown": 60}}},
}
RATING = {"skills_match": 80, "seniority_fit": 80, "role_interest": 90, "company_similarity": 50,
          "employer_quality": 85, "staffing_agency": False, "dealbreaker": None, "years_required": None,
          "stack_match": "", "red_flags": [], "why": "", "gaps": [], "reason": "ok"}


def pass2_runner(job_id, office_days=None):
    def run(prompt_file, user_prompt):
        assert "democratize good AI" in user_prompt  # pass 2 reads the API's description
        return {"result": json.dumps([{**RATING, "id": job_id, "office_days": office_days}]), "is_error": False,
                "modelUsage": {"claude-sonnet-5-5": {"inputTokens": 3, "outputTokens": 300, "costUSD": 0.02}}}
    return run


def test_set_posting_on_workable_stores_the_api_fields_and_scores_remote(db_path):
    job_id = add(db_path)
    s = FakeSession({WK_API: fixture("workable_huggingface_job.json")})
    # Pass 2 guesses 4 office days from the text; the ATS's remote flag wins.
    lines = ps.set_posting(job_id, WK_URL, profile=PROFILE, preferred=[], db_path=db_path, session=s,
                           pass2_runner=pass2_runner(job_id, office_days=4))
    job = db.get_job(job_id, db_path=db_path)
    assert WK_URL not in s.urls
    assert (job["source_trust"], job["apply_url"], job["needs_details"]) == ("official", WK_URL, 0)
    assert (job["location"], job["posted_at"], job["office_days"]) == (
        "New York, New York, United States (Remote)", "2026-05-29", 0)
    detail = json.loads(job["score_detail"])
    assert detail["components"]["location"] == {"value": 100, "source": "code", "note": "remote"}
    assert "published 2026-05-29" in lines[0]


def test_details_lookup_reads_a_workable_find_from_the_api(db_path):
    job_id = add(db_path)
    found = {"found": True, "source_url": WK_URL, "apply_url": WK_URL + "apply", "description": "",
             "salary": None, "years_required": 3, "still_open": True}
    runner = lambda f, u: {"result": json.dumps(found), "is_error": False, "modelUsage": {}}  # noqa: E731
    s = FakeSession({WK_API: fixture("workable_huggingface_job.json")})
    result = dt.fetch_details(db_path=db_path, runner=runner, sleep=lambda s: None, session=s)
    job = db.get_job(job_id, db_path=db_path)
    assert result.official == 1 and job["description"].startswith("At Hugging Face")
    assert (job["office_days"], job["posted_at"], job["years_required"]) == (0, "2026-05-29", 3)
    assert db.get_company_board("hugging face", db_path=db_path)["board_url"] == WK_BOARD


def test_details_lookup_with_no_text_and_no_json_is_not_found(db_path):
    job_id = add(db_path)
    found = {"found": True, "source_url": WK_URL, "description": "", "still_open": True}
    runner = lambda f, u: {"result": json.dumps(found), "is_error": False, "modelUsage": {}}  # noqa: E731
    result = dt.fetch_details(db_path=db_path, runner=runner, sleep=lambda s: None, session=FakeSession({}))
    assert result.not_found == 1 and db.get_job(job_id, db_path=db_path)["details_status"] == "not_found"


def test_stored_office_days_beat_pass_2():
    rating = sc.valid_rating(RATING) | {"office_days": 4}
    _, detail = sc.compute_final({"location": "San Francisco, CA", "office_days": 0}, rating, PROFILE)
    assert detail["components"]["location"]["value"] == 100
    _, detail = sc.compute_final({"location": "San Francisco, CA", "office_days": None}, rating, PROFILE)
    assert detail["components"]["location"]["value"] == 25
