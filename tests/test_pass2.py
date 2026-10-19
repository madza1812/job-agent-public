import json

import pytest

from agents import db
from agents.scout import pass2, score

LONG = "We need PHP and Python for our ERP. " * 40
PROFILE = {
    "targets": {"roles": ["Backend Engineer"]},
    "hard_filters": {"locations": ["California"], "remote_ok": True, "location_aliases": {"California": ["CA"]}},
    "scoring_weights": {"skills_match": 30, "seniority_fit": 20, "role_interest": 15, "employer_quality": 10,
                        "company_pref": 10, "location": 10, "compensation": 5},
}


@pytest.fixture
def db_path(tmp_path):
    path = tmp_path / "jobs.db"
    db.init_db(path)
    return path


def add(db_path, ext_id, score_value, description=LONG, needs_details=0, detail_pass=1):
    job = {"source": "greenhouse", "external_id": ext_id, "company": "Acme", "title": "Engineer",
           "location": "San Jose, CA", "description": description, "needs_details": needs_details}
    job_id, _ = db.upsert_job(job, db_path=db_path)
    db.set_status(job_id, "scored", "scout", db_path=db_path)
    db.set_score(job_id, score_value, {"final": score_value, "pass": detail_pass}, db_path=db_path)
    return job_id


def rating(job_id, **over):
    r = {"id": job_id, "skills_match": 90, "seniority_fit": 80, "role_interest": 70, "company_similarity": 40,
         "employer_quality": 85, "staffing_agency": False, "dealbreaker": None, "years_required": 3,
         "stack_match": "PHP and Python match; no Go.", "red_flags": ["on-call rotation"],
         "why": "Your ERP work fits their internal tools.", "gaps": ["Go"], "reason": "good fit"}
    return {**r, **over}


def reply(items):
    return {"result": json.dumps(items), "is_error": False,
            "modelUsage": {"claude-sonnet-5-5": {"inputTokens": 3, "outputTokens": 300, "costUSD": 0.02}}}


class FakeRunner:
    def __init__(self, *replies):
        self.replies = list(replies)
        self.calls = []

    def __call__(self, prompt_file, user_prompt):
        self.calls.append((prompt_file.read_text(encoding="utf-8"), user_prompt))
        return self.replies.pop(0)


def run(db_path, runner, **kw):
    return pass2.rescore(PROFILE, [], db_path=db_path, runner=runner, sleep=lambda s: None, **kw)


def test_rescores_with_full_description_and_same_formula(db_path):
    a = add(db_path, "1", 60)
    runner = FakeRunner(reply([rating(a)]))
    result = run(db_path, runner)
    job = db.get_job(a, db_path=db_path)
    detail = json.loads(job["score_detail"])
    expected, _ = score.compute_final(job, score.valid_rating(rating(a)), PROFILE)
    assert result.rescored == 1 and job["score"] == expected and job["status"] == "scored"
    assert detail["pass"] == 2 and detail["pass1_final"] == 60
    assert detail["years_required"] == 3 and detail["red_flags"] == ["on-call rotation"]
    assert detail["stack_match"] and detail["why"] and detail["gaps"] == ["Go"]
    assert "We need PHP and Python" in runner.calls[0][1]  # the full description is sent
    assert "years_required" in runner.calls[0][0]  # pass 2 prompt


def test_jobs_without_full_posting_or_already_pass2_are_skipped(db_path):
    add(db_path, "1", 70, description="", needs_details=1)
    add(db_path, "2", 69, detail_pass=2)
    add(db_path, "3", 68, description="tiny")
    assert pass2.candidates(db_path) == []


def test_pass2_rating_needs_lists():
    assert pass2.valid_pass2_rating(rating(1, gaps="Go")) is None
    assert pass2.valid_pass2_rating(rating(1))["gaps"] == ["Go"]


def test_prompt_file_has_pass1_schema_plus_extras():
    text = pass2.PROMPT_PATH.read_text(encoding="utf-8")
    for key in ("skills_match", "employer_quality", "staffing_agency", "dealbreaker",
                "years_required", "office_days", "stack_match", "red_flags", "why", "gaps"):
        assert key in text


def test_office_days_is_0_to_5_or_none(db_path):
    assert pass2.valid_pass2_rating({**rating(1), "office_days": 3})["office_days"] == 3
    for bad in (6, -1, "3", True, None):
        assert pass2.valid_pass2_rating({**rating(1), "office_days": bad})["office_days"] is None
    a = add(db_path, "1", 60)
    run(db_path, FakeRunner(reply([rating(a, office_days=2)])))
    assert json.loads(db.get_job(a, db_path=db_path)["score_detail"])["office_days"] == 2


def test_possibly_closed_is_a_strict_bool():
    assert pass2.valid_pass2_rating({**rating(1), "possibly_closed": True})["possibly_closed"] is True
    assert pass2.valid_pass2_rating({**rating(1), "possibly_closed": "yes"})["possibly_closed"] is False
    assert pass2.valid_pass2_rating(rating(1))["possibly_closed"] is False


def test_stale_pass2_is_rescored_and_pass1_final_kept(db_path):
    a = add(db_path, "1", 70, detail_pass=2)
    db.set_score(a, 70, {"final": 70, "pass": 2, "pass1_final": 61, "needs_pass2": True}, db_path=db_path)
    run(db_path, FakeRunner(reply([rating(a)])))
    detail = json.loads(db.get_job(a, db_path=db_path)["score_detail"])
    assert detail["pass1_final"] == 61 and "needs_pass2" not in detail


def test_job_ids_rescore_exactly_those_jobs_whatever_their_status(db_path):
    a = add(db_path, "1", 70, detail_pass=2)
    add(db_path, "2", 60)
    db.set_status(a, "shortlisted", "scout", db_path=db_path)
    runner = FakeRunner(reply([rating(a)]))
    result = run(db_path, runner, job_ids=[a])
    assert result.rescored == 1 and len(runner.calls) == 1 and f'"id":{a},' in runner.calls[0][1]


def test_posting_type_defaults_to_job_and_is_stored(db_path):
    assert pass2.valid_pass2_rating(rating(1))["posting_type"] == "job"
    assert pass2.valid_pass2_rating({**rating(1), "posting_type": "pool?"})["posting_type"] == "job"
    a = add(db_path, "1", 60)
    run(db_path, FakeRunner(reply([rating(a, posting_type="talent_pool")])))
    assert json.loads(db.get_job(a, db_path=db_path)["score_detail"])["posting_type"] == "talent_pool"
    assert '"posting_type"' in pass2.PROMPT_PATH.read_text(encoding="utf-8")
