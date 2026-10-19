import json

import pytest

from agents import db
from agents.scout import details as dt
from agents.scout import posting as ps
from agents.scout import shortlist as sl

BOARD = "https://acme.applytojob.com/"
URL = "https://acme.applytojob.com/apply/AAA/Full-Stack-Web-Developer"
URL_WI = "https://acme.applytojob.com/apply/BBB/Full-Stack-Web-Developer-WI"
BODY = "<p><b>Job Summary:</b></p><p>Build internal tools.</p><ul><li>PHP &amp; Python</li><li>SQL</li></ul>" + (
    "<p>Work with manufacturing teams on ERP integrations and reporting.</p>" * 6)


def page(title="Full Stack Web Developer", city="Houston", salary=None, body=BODY):
    ld = {"@context": "http://schema.org/", "@type": "JobPosting", "title": title, "description": body,
          "jobLocation": {"@type": "Place", "address": {"addressLocality": city, "addressRegion": "TX"}}}
    if salary:
        ld["baseSalary"] = salary
    return (f'<html><head><title>{title}</title><script type="application/ld+json">{{"@type": "Organization"}}</script>'
            f'<script type="application/ld+json">{json.dumps(ld)}</script></head><body><nav>menu</nav></body></html>')


BOARD_HTML = ('<html><body><a href="/apply/">View All Jobs</a>'
              '<a href="https://acme.applytojob.com/apply/AAA/Full-Stack-Web-Developer">Full Stack Web Developer</a>'
              '<a href="/apply/CCC/Data-Analyst"><span>Data Analyst</span></a>'
              '<a href="https://elsewhere.com/x">Full Stack Web Developer</a></body></html>')


class Resp:
    def __init__(self, text, status=200):
        self.text, self.status_code = text, status


class FakeSession:
    def __init__(self, pages):
        self.pages = pages
        self.urls = []

    def get(self, url, timeout=None, headers=None):
        self.urls.append(url)
        return Resp(self.pages[url]) if url in self.pages else Resp("", 404)


@pytest.fixture
def db_path(tmp_path):
    path = tmp_path / "jobs.db"
    db.init_db(path)
    return path


def add(db_path, ext_id="1", score=73.0, detail=None, title="Full Stack Web Developer", **extra):
    job = {"source": "email:linkedin", "external_id": ext_id, "company": "Acme Industrial", "title": title,
           "location": "Houston, TX", "url": f"https://www.linkedin.com/jobs/view/{ext_id}/", "needs_details": 1, **extra}
    job_id, _ = db.upsert_job(job, db_path=db_path)
    db.set_status(job_id, "scored", "scout", db_path=db_path)
    db.set_score(job_id, score, detail or {"final": score, "pass": 1}, db_path=db_path)
    return job_id


# --- parsing ---------------------------------------------------------------------------

def test_parse_posting_uses_json_ld_and_keeps_paragraphs_and_bullets():
    p = ps.parse_posting(page(), URL)
    assert p["title"] == "Full Stack Web Developer" and p["location"] == "Houston, TX"
    assert p["description"].startswith("Job Summary:\n\nBuild internal tools.") or \
        p["description"].startswith("Job Summary:\nBuild internal tools.")
    assert "- PHP & Python" in p["description"] and "menu" not in p["description"]


@pytest.mark.parametrize("salary, expected", [
    ({"currency": "USD", "value": {"minValue": 90000, "maxValue": 120000, "unitText": "YEAR"}}, (90000, 120000, "USD")),
    ({"currency": "USD", "value": {"minValue": 40, "maxValue": 50, "unitText": "HOUR"}}, (83200, 104000, "USD")),
    ({"currency": "USD", "value": {"value": 0.4, "unitText": "HOUR"}}, None),  # under 1,000 a year: nonsense
])
def test_salary_from_json_ld(salary, expected):
    assert ps.parse_posting(page(salary=salary), URL)["salary"] == expected


def test_page_without_json_ld_uses_the_description_element():
    html = '<html><body><div id="job-description">' + BODY + "</div><footer>x</footer></body></html>"
    assert "Build internal tools." in ps.parse_posting(html, URL)["description"]


def test_page_without_a_description_is_an_error():
    with pytest.raises(ps.PostingError, match="no job description"):
        ps.parse_posting("<html><body>Sign in</body></html>", URL)


def test_blocked_and_non_http_urls_are_refused():
    with pytest.raises(ps.PostingError, match="never fetched"):
        ps.check_url("https://www.linkedin.com/jobs/view/1/")
    with pytest.raises(ps.PostingError, match="http"):
        ps.check_url("file:///etc/passwd")


@pytest.mark.parametrize("url, board", [
    (URL, BOARD),
    ("https://job-boards.greenhouse.io/ascend/jobs/1000000001", "https://job-boards.greenhouse.io/ascend"),
    ("https://boards.greenhouse.io/acme/jobs/1", "https://job-boards.greenhouse.io/acme"),
    ("https://jobs.lever.co/acme/abc-123", "https://jobs.lever.co/acme"),
    ("https://apply.workable.com/acme/j/ABC/", "https://apply.workable.com/acme/"),
    ("https://acme.bamboohr.com/careers/12", "https://acme.bamboohr.com/careers"),
    ("https://acme.wd5.myworkdayjobs.com/en-US/External/job/X", None),  # JavaScript-only board
    ("https://careers.acme.com/jobs/1", None),
])
def test_board_url_for(url, board):
    assert ps.board_url_for(url) == board


# --- the board ----------------------------------------------------------------------

def test_find_on_board_matches_the_exact_title_on_the_same_host():
    s = FakeSession({BOARD: BOARD_HTML})
    assert ps.find_on_board(BOARD, "Full Stack Web Developer", "Houston, TX", s) == URL
    assert ps.find_on_board(BOARD, "Data Analyst", None, s) == "https://acme.applytojob.com/apply/CCC/Data-Analyst"
    assert ps.find_on_board(BOARD, "Full Stack Developer", None, s) is None


def test_same_title_twice_is_settled_by_location():
    html = BOARD_HTML.replace('<a href="/apply/CCC', f'<a href="{URL_WI}">Full Stack Web Developer</a><a href="/apply/CCC')
    s = FakeSession({BOARD: html, URL: page(city="Houston"), URL_WI: page(city="Mount Pleasant")})
    assert ps.find_on_board(BOARD, "Full Stack Web Developer", "Mount Pleasant, WI", s) == URL_WI
    assert ps.find_on_board(BOARD, "Full Stack Web Developer", None, s) is None  # can't tell: leave it to the lookup


# --- set-posting ----------------------------------------------------------------------

PASS2 = {"skills_match": 75, "seniority_fit": 85, "role_interest": 75, "company_similarity": 50,
         "employer_quality": 70, "staffing_agency": False, "dealbreaker": None, "years_required": 2,
         "stack_match": "PHP, Python, SQL match.", "red_flags": ["onsite only"], "possibly_closed": False,
         "why": "Internal tools at a manufacturer.", "gaps": ["IIS"], "reason": "good fit"}


def pass2_runner(job_id):
    def run(prompt_file, user_prompt):
        assert "Build internal tools." in user_prompt
        return {"result": json.dumps([{**PASS2, "id": job_id}]), "is_error": False,
                "modelUsage": {"claude-sonnet-5-5": {"inputTokens": 3, "outputTokens": 300, "costUSD": 0.02}}}
    return run


PROFILE = {"scoring_weights": {"skills_match": 30, "seniority_fit": 20, "role_interest": 15, "employer_quality": 10,
                               "company_pref": 10, "location": 10, "compensation": 5}}


def test_set_posting_stores_official_posting_reruns_pass2_and_caches_board(db_path):
    job_id = add(db_path, detail={"final": 73.0, "pass": 2, "possibly_closed": True,
                                  "red_flags": ["posting shown as closed on aggregators"]})
    salary = {"currency": "USD", "value": {"minValue": 80000, "maxValue": 100000, "unitText": "YEAR"}}
    s = FakeSession({URL: page(salary=salary)})
    lines = ps.set_posting(job_id, URL, profile=PROFILE, preferred=[], db_path=db_path, session=s,
                           pass2_runner=pass2_runner(job_id))
    job = db.get_job(job_id, db_path=db_path)
    assert (job["source_trust"], job["needs_details"], job["apply_url"], job["source_url"]) == ("official", 0, URL, URL)
    assert job["description"].startswith("Job Summary:") and (job["salary_min"], job["salary_max"]) == (80000, 100000)
    detail = json.loads(job["score_detail"])
    assert detail["pass"] == 2 and detail["possibly_closed"] is False and "needs_pass2" not in detail
    assert not sl.possibly_closed(job)
    assert db.get_company_board("acme industrial", db_path=db_path)["board_url"] == BOARD
    assert any("board cached" in line for line in lines)
    event = db.connect(db_path).execute("SELECT actor, detail FROM events WHERE event = 'posting_set'").fetchone()
    assert tuple(event) == ("user", URL)


def test_set_posting_rewrites_an_existing_brief(db_path, monkeypatch):
    job_id = add(db_path)
    db.set_brief_path(job_id, "briefs/old.md", db_path=db_path)
    calls = []
    from agents.research import brief
    monkeypatch.setattr(brief, "research", lambda jid, **kw: calls.append((jid, kw["any_status"])) or brief.Result(False))
    ps.set_posting(job_id, URL, profile=PROFILE, preferred=[], db_path=db_path, session=FakeSession({URL: page()}),
                   pass2_runner=pass2_runner(job_id))
    assert calls == [(job_id, True)]


def test_set_posting_refuses_linkedin(db_path):
    job_id = add(db_path)
    with pytest.raises(ps.PostingError):
        ps.set_posting(job_id, "https://www.linkedin.com/jobs/view/1/", profile=PROFILE, preferred=[],
                       db_path=db_path, session=FakeSession({}))


# --- details: the free board check ------------------------------------------------------

def never_called(prompt_file, user_prompt):
    raise AssertionError("Claude should not be called")


def test_details_uses_the_cached_board_before_claude(db_path):
    db.set_company_board("acme industrial", "Acme Industrial", BOARD, "user", db_path=db_path)
    job_id = add(db_path)
    result = dt.fetch_details(db_path=db_path, runner=never_called, sleep=lambda s: None,
                              session=FakeSession({BOARD: BOARD_HTML, URL: page()}))
    job = db.get_job(job_id, db_path=db_path)
    assert (result.found, result.free_found, result.usage.calls) == (1, 1, 0)
    assert (job["source_trust"], job["apply_url"], job["details_status"]) == ("official", URL, "found")


def test_details_falls_back_to_claude_with_the_board_in_the_prompt(db_path):
    db.set_company_board("acme industrial", "Acme Industrial", BOARD, "user", db_path=db_path)
    add(db_path, title="Software Developer")
    prompts = []

    def runner(prompt_file, user_prompt):
        prompts.append((prompt_file.read_text(encoding="utf-8"), user_prompt))
        return {"result": '{"found": false}', "is_error": False, "modelUsage": {}}

    dt.fetch_details(db_path=db_path, runner=runner, sleep=lambda s: None, session=FakeSession({BOARD: BOARD_HTML}))
    system, user = prompts[0]
    assert f"Company's known job board: {BOARD}" in user
    assert "applytojob.com" in system and "{{ATS_DOMAINS}}" not in system and "calcareers.ca.gov" in system


def test_possibly_closed_job_is_rechecked_free_and_flagged_for_pass2(db_path):
    db.set_company_board("acme industrial", "Acme Industrial", BOARD, "user", db_path=db_path)
    job_id = add(db_path, needs_details=0, detail={"final": 73.0, "pass": 2, "possibly_closed": True})
    conn = db.connect(db_path)
    with conn:
        conn.execute("UPDATE jobs SET details_status = 'found', source_trust = 'aggregator' WHERE id = ?", (job_id,))
    conn.close()
    result = dt.fetch_details(db_path=db_path, runner=never_called, sleep=lambda s: None,
                              session=FakeSession({BOARD: BOARD_HTML, URL: page()}))
    job = db.get_job(job_id, db_path=db_path)
    assert (result.rechecked, result.free_found, job["source_trust"]) == (1, 1, "official")
    assert json.loads(job["score_detail"])["needs_pass2"] is True


def test_official_ats_find_by_claude_caches_the_board(db_path):
    job_id = add(db_path)
    found = {"found": True, "source_url": "https://jobs.lever.co/acmeind/123", "apply_url": None,
             "description": "Build things. " * 100, "salary": None, "years_required": 2, "still_open": True}
    runner = lambda f, u: {"result": json.dumps(found), "is_error": False, "modelUsage": {}}  # noqa: E731
    dt.fetch_details(db_path=db_path, runner=runner, sleep=lambda s: None)
    assert db.get_company_board("acme industrial", db_path=db_path)["board_url"] == "https://jobs.lever.co/acmeind"
    assert db.get_job(job_id, db_path=db_path)["source_trust"] == "official"


def test_a_users_board_is_not_replaced_by_a_lookup(db_path):
    db.set_company_board("acme", "Acme", BOARD, "user", db_path=db_path)
    db.set_company_board("acme", "Acme", "https://jobs.lever.co/other", "lookup", db_path=db_path)
    assert db.get_company_board("acme", db_path=db_path)["board_url"] == BOARD


# --- shortlist: the re-check rule -------------------------------------------------------

def test_job_moved_back_then_confirmed_open_can_be_shortlisted_again(db_path):
    job_id = add(db_path, detail={"final": 73.0, "pass": 2})
    conn = db.connect(db_path)
    with conn:
        conn.execute("UPDATE jobs SET source_trust = 'official' WHERE id = ?", (job_id,))
    conn.close()
    assert [j["id"] for j in sl.shortlist({}, db_path=db_path).picked] == [job_id]
    db.set_score(job_id, 73.0, {"final": 73.0, "pass": 2, "possibly_closed": True}, db_path=db_path)
    assert sl.shortlist({}, db_path=db_path).demoted[0]["id"] == job_id
    assert sl.shortlist({}, db_path=db_path).picked == []  # still possibly closed
    db.set_score(job_id, 74.0, {"final": 74.0, "pass": 2, "possibly_closed": False}, db_path=db_path)
    assert [j["id"] for j in sl.shortlist({}, db_path=db_path).picked] == [job_id]
    assert db.get_job(job_id, db_path=db_path)["status"] == "shortlisted"
