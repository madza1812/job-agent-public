import json
import logging

import pytest

from agents import db
from agents.common.claude import UsageLimitReached
from agents.scout import score as sc
from agents.scout.filters import location_matches

WEIGHTS = {"skills_match": 30, "seniority_fit": 20, "role_interest": 15, "employer_quality": 10,
           "company_pref": 10, "location": 10, "compensation": 5}
PROFILE = {
    "identity": {"name": "Test User", "summary": "Engineer"},
    "skills": {"core": ["Python"]},
    "targets": {"roles": ["Backend Engineer"]},
    "hard_filters": {"locations": ["California"], "remote_ok": True, "min_salary": 70000,
                     "location_aliases": {"California": ["California", "CA", "San Jose"]}},
    "scoring_weights": WEIGHTS,
    "sanitize": {"replace": {"secret": "x"}},
}
PREFERRED = [{"name": "Abbott", "tier": "strong", "aliases": []}]


@pytest.fixture
def db_path(tmp_path):
    path = tmp_path / "jobs.db"
    db.init_db(path)
    return path


def add(db_path, ext_id, **extra):
    job = {"source": "greenhouse", "external_id": ext_id, "company": "Acme", "title": "Backend Engineer", **extra}
    job_id, _ = db.upsert_job(job, db_path=db_path)
    return job_id


def job(db_path, job_id):
    return next(j for j in db.get_jobs(db_path=db_path) if j["id"] == job_id)


def rating(**over):
    r = {"skills_match": 80, "seniority_fit": 80, "role_interest": 80, "company_similarity": 40,
         "employer_quality": 80, "staffing_agency": False, "dealbreaker": None, "reason": "ok"}
    return {**r, **over}


def reply(items, model="claude-sonnet-5-5"):
    return {
        "result": json.dumps(items),
        "is_error": False,
        "modelUsage": {model: {"inputTokens": 2, "outputTokens": 40, "cacheReadInputTokens": 500,
                               "cacheCreationInputTokens": 100, "costUSD": 0.01}},
    }


class FakeRunner:
    def __init__(self, *replies):
        self.replies = list(replies)
        self.calls = []

    def __call__(self, prompt_file, user_prompt):
        self.calls.append((prompt_file.read_text(encoding="utf-8"), user_prompt))
        r = self.replies.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


def run(db_path, runner, **kw):
    return sc.score_jobs(PROFILE, PREFERRED, db_path=db_path, runner=runner, sleep=lambda s: None, **kw)


# --- the final-score formula ------------------------------------------------------

BASE_JOB = {"id": 1, "title": "Backend Engineer", "company": "Acme", "location": "San Jose, CA"}


def final(job=None, **over):
    return sc.compute_final({**BASE_JOB, **(job or {})}, rating(**over), PROFILE)


def test_weighted_average_of_components():
    # skills/seniority/role/employer 80, company_similarity 40, location match 100, compensation unknown 50
    score, detail = final()
    expected = (30 * 80 + 20 * 80 + 15 * 80 + 10 * 80 + 10 * 40 + 10 * 100 + 5 * 50) / 100
    assert score == round(expected, 1) == 76.5
    assert detail["components"]["employer_quality"] == {"value": 80, "source": "claude"}
    assert detail["weighted"] == expected and detail["final"] == score


@pytest.mark.parametrize("tier,value", [("dream", 100), ("strong", 80), ("interested", 65)])
def test_company_pref_is_tier_score_when_preferred(tier, value):
    _, detail = final({"preferred_tier": tier, "preferred_match": "X"}, company_similarity=5)
    assert detail["components"]["company_pref"]["value"] == value
    assert detail["components"]["company_pref"]["source"] == "code"


def test_company_pref_is_company_similarity_when_not_preferred():
    _, detail = final(company_similarity=33)
    assert detail["components"]["company_pref"] == {
        "value": 33, "source": "claude", "note": "not preferred; company_similarity"}


@pytest.mark.parametrize("job,value", [
    ({"location": "San Jose, CA"}, 100),
    ({"location": "Remote"}, 100),
    ({"location": None, "remote": 1}, 100),
    ({"location": "Austin, TX"}, 50),
    ({"location": None}, 50),          # unknown
    ({"location": "United States"}, 50),  # unclear
])
def test_location_is_100_for_match_or_remote_else_50(job, value):
    _, detail = final(job)
    assert detail["components"]["location"]["value"] == value


COMMUTE_PROFILE = {
    **PROFILE,
    "hard_filters": {"locations": ["Bay Area", "Central Valley", "Remote-US"], "remote_ok": True,
                     "location_aliases": {"Bay Area": ["San Jose", "San Francisco"], "Central Valley": ["Stockton"]}},
    "commute": {"remote": 100, "places": {"Central Valley": 100,
                                          "Bay Area": {"1-2": 80, 3: 50, "4-5": 25, "unknown": 60}}},
}


@pytest.mark.parametrize("location,office_days,value", [
    ("Remote", None, 100),
    ("Stockton, CA", 5, 100),            # flat score, office days don't matter
    ("San Jose, CA", None, 60),          # pass 1, or the posting doesn't say
    ("San Jose, CA", 1, 80),
    ("San Jose, CA", 2, 80),
    ("San Jose, CA", 3, 50),
    ("San Jose, CA", 5, 25),
    ("San Jose, CA", 0, 100),            # pass 2 found it fully remote
    ("San Francisco or Remote", 4, 100),  # best of the matched places
    ("Austin, TX", None, 50),            # no match: unchanged
])
def test_commute_scores_location_by_place_and_office_days(location, office_days, value):
    _, detail = sc.compute_final({"location": location}, rating(office_days=office_days), COMMUTE_PROFILE)
    assert detail["components"]["location"]["value"] == value


def test_commute_place_must_be_a_location_and_have_unknown():
    bad = {**COMMUTE_PROFILE, "commute": {"places": {"Seattle": 100}}}
    with pytest.raises(ValueError, match="not in hard_filters.locations"):
        sc.compute_final({"location": "Remote"}, rating(), bad)
    bad = {**COMMUTE_PROFILE, "commute": {"places": {"Bay Area": {"1-2": 80}}}}
    with pytest.raises(ValueError, match="unknown"):
        sc.compute_final({"location": "Remote"}, rating(), bad)


@pytest.mark.parametrize("job,value", [
    ({}, 50),
    ({"salary_max": 90000}, 100),
    ({"salary_min": 60000, "salary_max": 65000}, 20),
    ({"salary_max": 90000, "salary_currency": "EUR"}, 50),
])
def test_compensation_50_when_unknown(job, value):
    _, detail = final(job)
    assert detail["components"]["compensation"]["value"] == value


def test_staffing_agency_minus_10_and_sponsored_minus_5():
    base, _ = final()
    assert final(staffing_agency=True)[0] == base - 10
    assert final({"sponsored": 1})[0] == base - 5
    score, detail = final({"sponsored": 1}, staffing_agency=True)
    assert score == base - 15
    assert detail["adjustments"] == [{"staffing_agency": -10}, {"sponsored": -5}]


def test_dealbreaker_caps_at_20():
    score, detail = final(dealbreaker="requires clearance")
    assert score == 20 and detail["capped_at"] == 20
    low, detail = final(dealbreaker="x", skills_match=0, seniority_fit=0, role_interest=0, company_similarity=0,
                        employer_quality=0)
    assert low == (10 * 100 + 5 * 50) / 100 == 12.5 and detail["capped_at"] is None


def test_score_is_clamped_at_zero():
    score, _ = final({"sponsored": 1, "location": "Austin, TX"}, staffing_agency=True, skills_match=0,
                     seniority_fit=0, role_interest=0, company_similarity=0, employer_quality=0)
    assert score == 0


def test_unknown_weight_is_rejected():
    with pytest.raises(ValueError):
        sc.compute_final(BASE_JOB, rating(), {**PROFILE, "scoring_weights": {"vibes": 100}})


# --- the run ----------------------------------------------------------------------

def test_flags_strip_claude_code_context_and_use_sonnet():
    flags = sc.CLAUDE_FLAGS
    assert flags[flags.index("--model") + 1] == "sonnet"
    assert flags[flags.index("--tools") + 1] == ""
    assert "--strict-mcp-config" in flags and "--disable-slash-commands" in flags
    assert "--json-schema" not in flags


def test_system_prompt_has_profile_and_preferred_but_not_private_extras():
    prompt = sc.build_system_prompt(PROFILE, PREFERRED)
    assert "Backend Engineer" in prompt and "Abbott" in prompt
    assert "sanitize" not in prompt and "location_aliases" not in prompt


def test_pass_1_scores_but_never_shortlists(db_path):
    a, b = add(db_path, "1", location="San Jose, CA"), add(db_path, "2")
    top = dict(skills_match=100, seniority_fit=100, role_interest=100, company_similarity=100, employer_quality=100)
    runner = FakeRunner(reply([{"id": a, **rating(**top)}, {"id": b, **rating()}]))
    result = run(db_path, runner)
    assert (result.scored, result.unanswered) == (2, 0)
    assert job(db_path, a)["status"] == job(db_path, b)["status"] == "scored"
    stored = job(db_path, a)
    assert stored["score"] == 97.5  # 95 from the rest at 100, compensation unknown 50 * 5
    assert json.loads(stored["score_detail"])["model"] == "claude-sonnet-5-5"


def test_scored_jobs_without_detail_are_rescored_in_place(db_path):
    a = add(db_path, "1")
    db.set_status(a, "scored", "scout", db_path=db_path)
    db.set_score(a, 40, db_path=db_path)
    done = add(db_path, "2")
    db.set_status(done, "scored", "scout", db_path=db_path)
    db.set_score(done, 50, {"final": 50}, db_path=db_path)
    runner = FakeRunner(reply([{"id": a, **rating()}]))
    result = run(db_path, runner)
    assert result.rescored == 1 and job(db_path, a)["status"] == "scored"
    assert job(db_path, a)["score_detail"] is not None
    sent_ids = [json.loads(line)["id"] for line in runner.calls[0][1].splitlines()[1:]]
    assert sent_ids == [a]
    assert job(db_path, done)["score"] == 50


def test_jobs_are_batched(db_path):
    ids = [add(db_path, str(i)) for i in range(5)]
    runner = FakeRunner(*(reply([{"id": i, **rating()} for i in chunk]) for chunk in (ids[:2], ids[2:4], ids[4:])))
    result = run(db_path, runner, batch_size=2)
    assert len(runner.calls) == 3 and result.scored == 5


def test_missing_invalid_and_foreign_ids_stay_new(db_path):
    a, b, c = add(db_path, "1"), add(db_path, "2"), add(db_path, "3")
    items = [{"id": a, **rating(skills_match=101)}, {"id": 999, **rating()}, {"id": c, **rating()}]
    result = run(db_path, FakeRunner(reply(items)))
    assert result.unanswered == 2
    assert job(db_path, a)["status"] == "new" and job(db_path, b)["status"] == "new"
    assert job(db_path, c)["status"] == "scored"


def test_reply_with_prose_around_array_is_parsed():
    text = 'Here you go:\n```json\n[' + json.dumps({"id": 1, **rating()}) + ']\n```'
    assert sc.parse_ratings(text, {1})[1]["skills_match"] == 80


def test_usage_is_logged_per_batch(db_path, caplog):
    a = add(db_path, "1")
    with caplog.at_level(logging.INFO):
        result = run(db_path, FakeRunner(reply([{"id": a, **rating()}])))
    line = next(r.message for r in caplog.records if r.message.startswith("usage batch=1/1"))
    assert "model=claude-sonnet-5-5" in line
    assert "input=2 cache_creation=100 cache_read=500 output=40" in line
    assert result.usage.total().output_tokens == 40


def test_usage_limit_stops_run_and_leaves_jobs_new(db_path):
    ids = [add(db_path, str(i)) for i in range(2)]
    runner = FakeRunner(UsageLimitReached("Claude AI usage limit reached"))
    result = run(db_path, runner, batch_size=1)
    assert result.stopped and len(runner.calls) == 1
    assert all(job(db_path, i)["status"] == "new" for i in ids)


def test_failed_call_is_retried(db_path):
    a = add(db_path, "1")
    runner = FakeRunner(sc.ClaudeCallFailed("boom"), reply([{"id": a, **rating()}]))
    result = run(db_path, runner)
    assert len(runner.calls) == 2 and result.scored == 1 and not result.errors


def test_dry_run_calls_nothing(db_path):
    add(db_path, "1")
    runner = FakeRunner()
    result = run(db_path, runner, dry_run=True)
    assert result.batches == 1 and runner.calls == []


def test_payload_strips_html_truncates_and_drops_empty_fields():
    payload = sc.job_payload({"id": 1, "title": "T", "company": "C", "location": None,
                              "description": "&lt;p&gt;Hello&lt;/p&gt;" + "x" * 5000, "needs_details": 0})
    assert payload["description"].startswith("Hello ")
    assert len(payload["description"]) <= sc.DESCRIPTION_CHARS + 2
    assert "location" not in payload and "needs_details" not in payload


def test_set_score_rejects_out_of_range(db_path):
    a = add(db_path, "1")
    with pytest.raises(ValueError):
        db.set_score(a, 150, db_path=db_path)


def test_top_lists_scored_open_jobs_by_score(db_path):
    a, b, c = add(db_path, "1"), add(db_path, "2"), add(db_path, "3")
    for job_id, s in ((a, 50), (b, 80), (c, 90)):
        db.set_status(job_id, "scored", "scout", db_path=db_path)
        db.set_score(job_id, s, db_path=db_path)
    db.set_status(c, "closed", "user", db_path=db_path)
    assert [j["id"] for j in db.get_top_jobs(15, db_path=db_path)] == [b, a]


def test_location_matches_distinguishes_unclear_from_match():
    assert location_matches({"location": "San Jose, CA"}, PROFILE)[0] is True
    assert location_matches({"location": "United States"}, PROFILE)[0] is False


def test_employer_quality_is_required_in_a_rating():
    item = {"id": 1, **rating()}
    del item["employer_quality"]
    assert sc.parse_ratings(json.dumps([item]), {1}) == {}


def test_prompt_file_defines_employer_quality_scale():
    prompt = sc.build_system_prompt(PROFILE, PREFERRED)
    assert "employer_quality" in prompt and "80-100" in prompt and "0-19" in prompt


def test_prompt_rates_role_interest_by_ai_and_counts_relevant_experience():
    profile = {**PROFILE, "targets": {"roles": ["Backend Engineer"], "industries_like": ["Finance operations"]}}
    prompt = sc.build_system_prompt(profile, PREFERRED)
    assert "Finance operations" in prompt  # the profile's industries_like reaches Claude
    assert "how central AI, agentic or LLM work is to the role" in prompt
    assert "Ignition SCADA" in prompt and "NetSuite" in prompt
    pass2_prompt = (sc.ROOT / "prompts" / "score_pass2.md").read_text(encoding="utf-8")
    assert "how central AI, agentic or LLM work is to the role" in pass2_prompt  # kept in step


def test_rescore_resets_scored_jobs_and_scores_them_again(db_path):
    a, kept = add(db_path, "1"), add(db_path, "2")
    db.set_status(a, "scored", "scout", db_path=db_path)
    db.set_score(a, 40, {"final": 40}, db_path=db_path)
    for step in ("scored", "shortlisted"):
        db.set_status(kept, step, "scout", db_path=db_path)
    db.set_score(kept, 90, {"final": 90}, db_path=db_path)
    runner = FakeRunner(reply([{"id": a, **rating()}]))
    result = run(db_path, runner, rescore=True)
    assert result.reset == 1 and result.scored == 1
    assert [json.loads(line)["id"] for line in runner.calls[0][1].splitlines()[1:]] == [a]
    # 76.5 as in test_weighted_average_of_components, less 5: no location, so it scores 50
    assert job(db_path, a)["status"] == "scored" and job(db_path, a)["score"] == 71.5
    assert job(db_path, kept)["status"] == "shortlisted" and job(db_path, kept)["score"] == 90


def test_rescore_dry_run_changes_nothing(db_path):
    a = add(db_path, "1")
    db.set_status(a, "scored", "scout", db_path=db_path)
    db.set_score(a, 40, {"final": 40}, db_path=db_path)
    result = run(db_path, FakeRunner(), rescore=True, dry_run=True)
    assert result.reset == 1 and result.batches == 1
    assert job(db_path, a)["status"] == "scored" and job(db_path, a)["score"] == 40


def test_rescore_stopped_by_usage_limit_leaves_jobs_new_and_unscored(db_path):
    a = add(db_path, "1")
    db.set_status(a, "scored", "scout", db_path=db_path)
    db.set_score(a, 40, {"final": 40}, db_path=db_path)
    result = run(db_path, FakeRunner(UsageLimitReached("limit reached")), rescore=True)
    assert result.stopped
    assert job(db_path, a)["status"] == "new" and job(db_path, a)["score"] is None


def test_only_scout_may_reset_scored_to_new(db_path):
    a = add(db_path, "1")
    db.set_status(a, "scored", "scout", db_path=db_path)
    with pytest.raises(db.ActorNotAllowed):
        db.set_status(a, "new", "user", db_path=db_path)
    db.set_status(a, "shortlisted", "scout", db_path=db_path)
    with pytest.raises(db.TransitionError):
        db.set_status(a, "new", "scout", db_path=db_path)
