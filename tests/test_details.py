import json

import pytest

from agents import db
from agents.common.claude import MaxTurnsReached, UsageLimitReached
from agents.scout import details as dt

LONG = "Build and maintain our ERP. " * 60  # well over LOW_CONFIDENCE_CHARS


@pytest.fixture
def db_path(tmp_path):
    path = tmp_path / "jobs.db"
    db.init_db(path)
    return path


def add(db_path, ext_id, score, status="scored", **extra):
    job = {"source": "email:linkedin", "external_id": ext_id, "company": "Acme", "title": "Engineer",
           "url": f"https://www.linkedin.com/jobs/view/{ext_id}/", "needs_details": 1, **extra}
    job_id, _ = db.upsert_job(job, db_path=db_path)
    if status != "new":
        db.set_status(job_id, "scored", "scout", db_path=db_path)
        db.set_score(job_id, score, {"final": score, "pass": 1}, db_path=db_path)
    if status == "shortlisted":
        db.set_status(job_id, "shortlisted", "scout", db_path=db_path)
    return job_id


def reply(obj, searches=1):
    return {"result": "```json\n" + json.dumps(obj) + "\n```", "is_error": False, "num_turns": 4,
            "modelUsage": {"claude-sonnet-5-5": {"inputTokens": 8, "outputTokens": 500, "costUSD": 0.03},
                           "claude-haiku-5-5": {"inputTokens": 9000, "outputTokens": 900,
                                                "webSearchRequests": searches, "costUSD": 0.02}}}


FOUND = {"found": True, "source_url": "https://boards.greenhouse.io/acme/jobs/1",
         "apply_url": "https://boards.greenhouse.io/acme/jobs/1#app", "description": LONG,
         "salary": {"min": 90000, "max": 120000, "currency": "USD"}, "years_required": 2, "still_open": True}


class FakeRunner:
    def __init__(self, *replies):
        self.replies = list(replies)
        self.prompts = []

    def __call__(self, prompt_file, user_prompt):
        self.prompts.append(user_prompt)
        r = self.replies.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


def run(db_path, runner, **kw):
    return dt.fetch_details(db_path=db_path, runner=runner, sleep=lambda s: None, **kw)


def test_found_posting_is_stored_and_needs_details_cleared(db_path):
    a = add(db_path, "1", 70)
    result = run(db_path, FakeRunner(reply(FOUND)))
    job = db.get_job(a, db_path=db_path)
    assert (result.found, job["needs_details"], job["details_status"]) == (1, 0, "found")
    assert job["description"].startswith("Build and maintain our ERP.")
    assert job["apply_url"].endswith("#app") and job["source_url"].startswith("https://boards.greenhouse.io")
    assert (job["salary_min"], job["salary_max"], job["salary_is_predicted"], job["years_required"]) == (90000, 120000, 0, 2)
    assert job["status"] == "scored"


def test_prompt_carries_company_title_location_and_alert_link(db_path):
    add(db_path, "77", 70, location="San Jose, CA")
    runner = FakeRunner(reply(FOUND))
    run(db_path, runner)
    assert "Company: Acme" in runner.prompts[0] and "Location: San Jose, CA" in runner.prompts[0]
    assert "Alert link (to identify the job only): https://www.linkedin.com/jobs/view/77/" in runner.prompts[0]


def test_not_found_keeps_job_and_is_not_searched_again(db_path):
    a = add(db_path, "1", 70)
    result = run(db_path, FakeRunner(reply({"found": False})))
    job = db.get_job(a, db_path=db_path)
    assert (result.not_found, job["details_status"], job["status"], job["needs_details"]) == (1, "not_found", "scored", 1)
    assert dt.candidates(db_path) == []


def test_out_of_turns_counts_as_not_found_and_logs_usage(db_path):
    a = add(db_path, "1", 70)
    result = run(db_path, FakeRunner(MaxTurnsReached("out", reply({}, searches=3))))
    assert db.get_job(a, db_path=db_path)["details_status"] == "not_found"
    assert result.usage.total().web_searches == 3


def test_closed_posting_closes_the_job(db_path):
    a = add(db_path, "1", 70)
    result = run(db_path, FakeRunner(reply({**FOUND, "still_open": False})))
    assert result.closed == 1
    assert db.get_job(a, db_path=db_path)["status"] == "closed"
    conn = db.connect(db_path)
    detail = conn.execute("SELECT detail FROM events WHERE job_id = ? ORDER BY id DESC LIMIT 1", (a,)).fetchone()[0]
    conn.close()
    assert detail == "posting closed"


def test_pool_is_top_15_scored_jobs_needing_details(db_path):
    ids = [add(db_path, str(i), 50 + i) for i in range(20)]
    full = add(db_path, "full", 99, needs_details=0, description=LONG)
    short = add(db_path, "short", 98, needs_details=0, description="Short blurb.")
    picked = [j["id"] for j in dt.candidates(db_path)]
    assert full not in picked and short in picked  # low confidence: thin description
    assert len(picked) == 14 and set(picked) - {short} == set(ids[-13:])


def test_shortlisted_jobs_are_not_in_the_pool(db_path):
    add(db_path, "1", 90, status="shortlisted")
    assert dt.candidates(db_path) == []


def test_usage_limit_stops(db_path):
    add(db_path, "1", 70)
    add(db_path, "2", 60)
    runner = FakeRunner(UsageLimitReached("usage limit reached"))
    result = run(db_path, runner)
    assert result.stopped and len(runner.prompts) == 1


def test_web_searches_are_logged(db_path, caplog):
    import logging
    add(db_path, "1", 70)
    with caplog.at_level(logging.INFO):
        run(db_path, FakeRunner(reply(FOUND, searches=2)))
    lines = [r.message for r in caplog.records if r.message.startswith("usage details")]
    assert any("model=claude-haiku-5-5" in line and "web_searches=2" in line for line in lines)


def test_unusable_reply_leaves_job_for_next_run(db_path):
    a = add(db_path, "1", 70)
    result = run(db_path, FakeRunner({"result": "I could not do it.", "is_error": False, "modelUsage": {}}))
    assert result.errors and db.get_job(a, db_path=db_path)["details_status"] is None


def test_found_without_description_or_link_counts_as_not_found():
    assert dt.parse_details(json.dumps({"found": True, "source_url": "https://x.com/j"}))["found"] is False
    assert dt.parse_details(json.dumps({"found": True, "description": LONG}))["found"] is False


def test_found_without_description_is_retried_next_day_not_an_error(db_path):
    from datetime import date, timedelta
    a = add(db_path, "1", 70)
    result = run(db_path, FakeRunner(reply({"found": True, "source_url": "https://x.com/j", "description": ""})))
    job = db.get_job(a, db_path=db_path)
    assert result.errors == [] and result.not_found == 1
    assert (job["details_status"], job["source_trust"], job["details_attempts"]) == ("not_found", "not_found", 1)
    assert dt.candidates(db_path) == []
    assert [j["id"] for j in dt.candidates(db_path, today=date.today() + timedelta(days=1))] == [a]


def test_bad_urls_and_salaries_are_dropped():
    d = dt.parse_details(json.dumps({**FOUND, "apply_url": "javascript:alert(1)",
                                     "salary": {"min": 5, "max": None}, "years_required": "two"}))
    assert d["apply_url"] is None and d["salary"] is None and d["years_required"] is None


AGGREGATOR = {**FOUND, "source_url": "https://jobright.ai/jobs/info/1", "apply_url": None}


def test_aggregator_description_scores_but_salary_and_still_open_are_ignored(db_path):
    a = add(db_path, "1", 70)
    result = run(db_path, FakeRunner(reply({**AGGREGATOR, "still_open": False})))
    job = db.get_job(a, db_path=db_path)
    assert (result.aggregator, result.closed) == (1, 0)
    assert job["source_trust"] == "aggregator" and job["status"] == "scored"
    assert job["salary_min"] is None and job["salary_max"] is None
    assert job["description"].startswith("Build and maintain") and job["needs_details"] == 0


def test_official_apply_url_makes_an_aggregator_find_official(db_path):
    # Job 57: the text came from jobs.anitab.org, but the apply link was on the company's own site.
    a = add(db_path, "1", 70)
    found = {**AGGREGATOR, "apply_url": "https://careers.acme.com/jobs/1", "still_open": False}
    result = run(db_path, FakeRunner(reply(found)))
    job = db.get_job(a, db_path=db_path)
    assert (result.official, result.closed) == (1, 0)
    assert (job["source_trust"], job["apply_url"]) == ("official", "https://careers.acme.com/jobs/1")
    assert job["source_url"] == "https://jobright.ai/jobs/info/1"  # where the text came from
    # Salary and still_open describe the aggregator's page, so they're still ignored.
    assert job["salary_min"] is None and job["status"] == "scored"


def test_official_source_with_aggregator_apply_link_stores_the_official_one(db_path):
    a = add(db_path, "1", 70)
    run(db_path, FakeRunner(reply({**FOUND, "apply_url": "https://www.indeed.com/viewjob?jk=abc"})))
    job = db.get_job(a, db_path=db_path)
    assert (job["source_trust"], job["apply_url"]) == ("official", FOUND["source_url"])


def test_official_source_is_stored_as_official(db_path):
    a = add(db_path, "1", 70)
    result = run(db_path, FakeRunner(reply(FOUND)))
    assert result.official == 1 and db.get_job(a, db_path=db_path)["source_trust"] == "official"


def test_not_found_is_retried_once_on_a_later_day_then_never(db_path):
    from datetime import date, timedelta
    a = add(db_path, "1", 70)
    run(db_path, FakeRunner(reply({"found": False})))
    job = db.get_job(a, db_path=db_path)
    assert (job["source_trust"], job["details_attempts"]) == ("not_found", 1)
    assert dt.candidates(db_path) == []  # not again the same day
    tomorrow = date.today() + timedelta(days=1)
    assert [j["id"] for j in dt.candidates(db_path, today=tomorrow)] == [a]
    run(db_path, FakeRunner(reply({"found": False})), today=tomorrow)
    assert db.get_job(a, db_path=db_path)["details_attempts"] == 2
    assert dt.candidates(db_path, today=tomorrow + timedelta(days=5)) == []


def test_retry_that_finds_the_posting_stores_it(db_path):
    from datetime import date, timedelta
    a = add(db_path, "1", 70)
    run(db_path, FakeRunner(reply({"found": False})))
    run(db_path, FakeRunner(reply(FOUND)), today=date.today() + timedelta(days=1))
    job = db.get_job(a, db_path=db_path)
    assert (job["details_status"], job["source_trust"], job["details_attempts"]) == ("found", "official", 2)


def test_default_runner_denies_linkedin_fetches(monkeypatch):
    seen = {}

    def fake_run_claude(prompt_file, user_prompt, **kw):
        seen.update(kw)
        return {}

    monkeypatch.setattr(dt, "run_claude", fake_run_claude)
    dt.default_runner(None, "x")
    assert "WebFetch(domain:linkedin.com)" in seen["deny"] and "WebFetch(domain:www.linkedin.com)" in seen["deny"]
    assert seen["tools"] == ("WebSearch", "WebFetch") and seen["max_turns"] == dt.MAX_TURNS
