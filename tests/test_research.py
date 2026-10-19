import json
from datetime import date, datetime, timedelta, timezone

import pytest

from agents import db
from agents.common.claude import MaxTurnsReached, UsageLimitReached
from agents.research import brief as rb

TODAY = date(2026, 10, 16)
APPLY = "https://job-boards.greenhouse.io/acme/jobs/1"
PROFILE = {"identity": {"name": "Candidate", "years_experience": 4}, "skills": {"core": ["PHP"]},
           "experience": [], "targets": {"roles": ["Backend Engineer"]}, "hard_filters": {"secret": "x"}}


@pytest.fixture
def db_path(tmp_path):
    path = tmp_path / "jobs.db"
    db.init_db(path)
    return path


def add(db_path, ext_id="1", company="Acme Inc", status="shortlisted"):
    job_id, _ = db.upsert_job({"source": "email:linkedin", "external_id": ext_id, "company": company,
                               "title": "Software Engineer II", "location": "Remote",
                               "url": f"https://www.linkedin.com/jobs/view/{ext_id}/"}, db_path=db_path)
    db.set_status(job_id, "scored", "scout", db_path=db_path)
    db.set_score(job_id, 70.0, {"final": 70.0, "pass": 2, "why": "Good fit.", "gaps": ["AWS"],
                                "red_flags": ["on-call"], "components": {"skills_match": {"value": 60}}},
                 db_path=db_path)
    db.store_details(job_id, description="Build APIs in PHP. " * 50, apply_url=APPLY, source_url=APPLY,
                     years_required=3, source_trust="official", db_path=db_path)
    if status == "shortlisted":
        db.set_status(job_id, "shortlisted", "scout", db_path=db_path)
    return job_id


def reply(text, searches=0):
    usage = {"claude-sonnet-5-5": {"inputTokens": 100, "outputTokens": 900, "costUSD": 0.02}}
    if searches:
        usage["claude-haiku-5-5"] = {"inputTokens": 5000, "outputTokens": 300, "webSearchRequests": searches,
                                     "costUSD": 0.01}
    return {"result": text, "is_error": False, "num_turns": 3, "modelUsage": usage}


COMPANY = {"markdown": "### What they do\nAcme sells widgets to factories [1]. " + "More facts [2]. " * 10,
           "sources": [{"n": 1, "title": "About Acme", "url": "https://acme.com/about", "date": None, "kind": "official"},
                       {"n": 2, "title": "Acme raises", "url": "https://news.example.com/acme", "date": "2026-05-01",
                        "kind": "news"}]}


def good_brief(headings=None, apply=APPLY, sources=3, table=True):
    headings = headings or rb.template_headings()
    parts = []
    for h in headings:
        body = "Text (stated)."
        if "skill" in h.lower() and table:
            body = "| Requirement | Stated or inferred | Your evidence | Fit |\n|---|---|---|---|\n| PHP | stated | 4 yrs | strong |"
        if h == headings[0]:
            body = f"- **Apply:** {apply}"
        if "source" in h.lower():
            body = "\n".join(f"{i}. Page {i}, https://example.com/{i} (2026-01-01)" for i in range(1, sources + 1))
        parts.append(f"## {h}\n{body}\n")
    return "\n".join(parts)


class Calls:
    def __init__(self, company=None, role=None):
        self.company = list(company or [])
        self.role = list(role or [])
        self.company_prompts, self.role_prompts, self.role_system = [], [], []

    def company_call(self, prompt_file, user_prompt):
        self.company_prompts.append(user_prompt)
        r = self.company.pop(0)
        if isinstance(r, Exception):
            raise r
        return r

    def role_call(self, prompt_file, user_prompt):
        self.role_prompts.append(user_prompt)
        self.role_system.append(prompt_file.read_text(encoding="utf-8"))
        r = self.role.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


def run(db_path, tmp_path, calls, job_id=None, **kw):
    return rb.research(job_id, profile=PROFILE, db_path=db_path, briefs_dir=tmp_path / "briefs",
                       company_call=calls.company_call, role_call=calls.role_call, sleep=lambda s: None,
                       today=TODAY, **kw)


def test_template_has_nine_sections():
    headings = rb.template_headings()
    assert len(headings) == 9
    assert any("skill" in h.lower() for h in headings) and "source" in headings[-1].lower()


def test_validate_brief_accepts_a_good_brief():
    assert rb.validate_brief(good_brief(), rb.template_headings(), APPLY) == []


@pytest.mark.parametrize("text, problem", [
    (lambda h: good_brief(h[:-2] + h[-1:]), "missing sections"),
    (lambda h: good_brief(table=False), "no markdown table"),
    (lambda h: good_brief(sources=2), "only 2 source URLs"),
    (lambda h: good_brief(apply="https://elsewhere.example.com/x"), "apply link"),
])
def test_validate_brief_catches_each_problem(text, problem):
    h = rb.template_headings()
    problems = rb.validate_brief(text(h), h, APPLY)
    assert any(problem in p for p in problems), problems


def test_writes_brief_caches_company_and_sets_researched(db_path, tmp_path):
    job_id = add(db_path)
    calls = Calls([reply(json.dumps(COMPANY), searches=4)], [reply(good_brief())])
    result = run(db_path, tmp_path, calls)

    assert len(result.written) == 1 and not result.errors
    job = db.get_job(job_id, db_path=db_path)
    assert job["status"] == "researched"
    path = tmp_path / "briefs" / "2026-10-16-acme-inc-software-engineer-ii.md"
    assert job["brief_path"] == str(path)
    text = path.read_text(encoding="utf-8")
    assert text.startswith("# Acme Inc: Software Engineer II\n") and APPLY in text
    cached = db.get_company_research("acme", db_path=db_path)
    assert cached["sources"][1]["date"] == "2026-05-01"
    # Usage is per brief: 2 calls, web searches counted.
    b = result.written[0]
    assert b.usage.calls == 2 and b.usage.total().web_searches == 4
    events = db.connect(db_path).execute("SELECT actor FROM events WHERE to_status = 'researched'").fetchall()
    assert [e[0] for e in events] == ["research"]


def test_role_prompt_carries_research_posting_scoring_and_profile(db_path, tmp_path):
    add(db_path)
    calls = Calls([reply(json.dumps(COMPANY))], [reply(good_brief())])
    run(db_path, tmp_path, calls)
    prompt, system = calls.role_prompts[0], calls.role_system[0]
    assert "Acme sells widgets" in prompt and "https://acme.com/about" in prompt
    assert "Build APIs in PHP." in prompt and '"gaps"' in prompt and "on-call" in prompt
    assert f"Apply link (copy exactly): {APPLY}" in prompt
    assert "Backend Engineer" in system and "hard_filters" not in system
    assert "<!-- template -->" not in system and "## Skills match" in system
    assert "2026-10-16" in calls.company_prompts[0] and "2025-10-16" in calls.company_prompts[0]


def test_cached_company_research_is_reused_within_30_days(db_path, tmp_path):
    add(db_path, "1", "Acme Inc")
    add(db_path, "2", "Acme")  # same normalized company
    calls = Calls([reply(json.dumps(COMPANY))], [reply(good_brief()), reply(good_brief())])
    result = run(db_path, tmp_path, calls)
    assert len(result.written) == 2 and len(calls.company_prompts) == 1
    assert [b.company_cached for b in result.written] == [False, True]


def test_stale_company_research_is_refreshed(db_path, tmp_path):
    add(db_path)
    db.add_company_research("acme", "Acme", "old", [], db_path=db_path)
    later = datetime.now(timezone.utc) + timedelta(days=31)
    calls = Calls([reply(json.dumps(COMPANY))], [reply(good_brief())])
    run(db_path, tmp_path, calls, now=later)
    assert len(calls.company_prompts) == 1


def test_failed_checks_retry_once_with_the_problems(db_path, tmp_path):
    job_id = add(db_path)
    calls = Calls([reply(json.dumps(COMPANY))], [reply(good_brief(sources=1)), reply(good_brief())])
    result = run(db_path, tmp_path, calls)
    assert len(result.written) == 1
    assert "failed these checks" in calls.role_prompts[1] and "only 1 source URLs" in calls.role_prompts[1]
    assert db.get_job(job_id, db_path=db_path)["status"] == "researched"


def test_two_failed_attempts_leave_the_job_shortlisted(db_path, tmp_path):
    job_id = add(db_path)
    calls = Calls([reply(json.dumps(COMPANY))], [reply(good_brief(sources=1)), reply(good_brief(table=False))])
    result = run(db_path, tmp_path, calls)
    assert result.written == [] and "failed checks after 2 attempts" in result.errors[0]
    job = db.get_job(job_id, db_path=db_path)
    assert job["status"] == "shortlisted" and job["brief_path"] is None
    assert result.usage.calls == 3  # still counted
    assert not (tmp_path / "briefs").exists() or not any((tmp_path / "briefs").iterdir())


def test_unusable_company_reply_retries_then_fails(db_path, tmp_path):
    job_id = add(db_path)
    calls = Calls([reply("no json here"), reply('{"markdown": "short", "sources": []}')], [])
    result = run(db_path, tmp_path, calls)
    assert "company research unusable" in result.errors[0]
    assert db.get_job(job_id, db_path=db_path)["status"] == "shortlisted"
    assert db.get_company_research("acme", db_path=db_path) is None


def test_company_out_of_turns_is_a_failure_not_a_crash(db_path, tmp_path):
    add(db_path)
    calls = Calls([MaxTurnsReached("out", reply("", searches=8))], [])
    result = run(db_path, tmp_path, calls)
    assert "ran out of turns" in result.errors[0] and result.usage.calls == 1


def test_usage_limit_stops_the_run(db_path, tmp_path):
    add(db_path, "1")
    add(db_path, "2", "Other Co")
    calls = Calls([UsageLimitReached("limit")], [])
    result = run(db_path, tmp_path, calls)
    assert result.stopped and len(calls.company_prompts) == 1


def test_only_shortlisted_jobs_by_default(db_path):
    a = add(db_path, "1")
    add(db_path, "2", status="scored")
    assert [j["id"] for j in rb.candidates(db_path=db_path)] == [a]


def test_shortlisted_job_with_a_brief_already_is_researched_without_calls(db_path, tmp_path):
    job_id = add(db_path)
    brief = tmp_path / "old.md"
    brief.write_text("# old", encoding="utf-8")
    db.set_brief_path(job_id, str(brief), db_path=db_path)
    result = run(db_path, tmp_path, Calls())
    assert result.candidates == 0 and result.usage.calls == 0
    assert db.get_job(job_id, db_path=db_path)["status"] == "researched"


def test_any_status_rewrites_a_scored_jobs_existing_brief(db_path, tmp_path):
    job_id = add(db_path, status="scored")
    db.set_brief_path(job_id, "briefs/x.md", db_path=db_path)
    calls = Calls([reply(json.dumps(COMPANY))], [reply(good_brief())])
    result = run(db_path, tmp_path, calls, job_id=job_id, any_status=True)
    job = db.get_job(job_id, db_path=db_path)
    assert len(result.written) == 1 and job["status"] == "scored" and job["brief_path"] != "briefs/x.md"


def test_a_given_id_rewrites_a_researched_jobs_brief(db_path, tmp_path):
    job_id = add(db_path)
    calls = Calls([reply(json.dumps(COMPANY))], [reply(good_brief()), reply(good_brief())])
    run(db_path, tmp_path, calls)
    result = run(db_path, tmp_path, calls, job_id=job_id)
    assert len(result.written) == 1 and db.get_job(job_id, db_path=db_path)["status"] == "researched"


def test_a_given_new_or_closed_job_is_refused(db_path):
    job_id = add(db_path, status="scored")
    db.set_status(job_id, "closed", "user", "skipped: no", db_path=db_path)
    with pytest.raises(ValueError, match="closed"):
        rb.candidates(job_id, db_path=db_path)
    new_id, _ = db.upsert_job({"source": "email:linkedin", "external_id": "9", "company": "New Co",
                               "title": "Engineer"}, db_path=db_path)
    with pytest.raises(ValueError, match="new"):
        rb.candidates(new_id, db_path=db_path)


def test_a_given_scored_job_gets_a_brief_and_stays_scored(db_path, tmp_path):
    job_id = add(db_path, status="scored")
    calls = Calls([reply(json.dumps(COMPANY))], [reply(good_brief())])
    result = run(db_path, tmp_path, calls, job_id=job_id)
    job = db.get_job(job_id, db_path=db_path)
    assert len(result.written) == 1 and job["status"] == "scored" and job["brief_path"]


def test_dry_run_calls_nothing(db_path, tmp_path):
    add(db_path)
    result = run(db_path, tmp_path, Calls(), dry_run=True)
    assert result.candidates == 1 and result.written == []


def test_slug():
    assert rb.slug("Sitewire by Hexacorp", 30) == "sitewire-by-hexacorp"
    assert rb.slug("Café / Ops — Sr.", 30) == "cafe-ops-sr"


def test_brief_filename_keeps_50_characters_of_company():
    job = {"company": "Northgate Industrial Systems - NIS", "title": "Full Stack Web Developer"}
    assert rb.brief_filename(job, TODAY) == "2026-10-16-northgate-industrial-systems-nis-full-stack-web-developer.md"
