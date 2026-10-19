import json
from datetime import datetime, timedelta, timezone

import pytest

from agents import db
from agents.scout import ats_api as aa
from agents.scout import details as dt

GH = "https://boards-api.greenhouse.io/v1/boards"
CONTENT = "&lt;p&gt;Build search for shoppers.&lt;/p&gt;&lt;ul&gt;&lt;li&gt;React&lt;/li&gt;&lt;/ul&gt;" + \
    "&lt;p&gt;We move money responsibly and ship weekly.&lt;/p&gt;" * 10


class Resp:
    def __init__(self, data, status=200):
        self.data, self.status_code = data, status
        self.text = json.dumps(data)

    def json(self):
        return self.data


class FakeSession:
    def __init__(self, routes):
        self.routes = routes
        self.urls = []

    def get(self, url, timeout=None, headers=None):
        self.urls.append(url)
        return Resp(self.routes[url]) if url in self.routes else Resp({"error": "not found"}, 404)


def gh_routes(slug="paxmora", name="Paxmora", jobs=None):
    jobs = jobs if jobs is not None else [
        {"id": 1, "title": "Software Engineer I, Fullstack (Billing)", "location": {"name": "San Francisco, CA"},
         "absolute_url": "https://job-boards.greenhouse.io/paxmora/jobs/1", "company_name": name},
        {"id": 2, "title": "Staff Engineer", "location": {"name": "Remote US"}, "absolute_url": "x", "company_name": name},
    ]
    return {f"{GH}/{slug}/jobs": {"jobs": jobs}, f"{GH}/{slug}": {"name": name},
            f"{GH}/{slug}/jobs/1": {"content": CONTENT}}


@pytest.fixture
def db_path(tmp_path):
    path = tmp_path / "jobs.db"
    db.init_db(path)
    return path


def add(db_path, company="Paxmora", title="Software Engineer I, Fullstack (Billing)", location="San Francisco, CA",
        ext_id="1", trust=None):
    job_id, _ = db.upsert_job({"source": "email:indeed", "external_id": ext_id, "company": company, "title": title,
                               "location": location, "url": f"https://www.indeed.com/viewjob?jk={ext_id}",
                               "needs_details": 1}, db_path=db_path)
    db.set_status(job_id, "scored", "scout", db_path=db_path)
    db.set_score(job_id, 75.0, {"final": 75.0, "pass": 1}, db_path=db_path)
    if trust:
        conn = db.connect(db_path)
        with conn:
            conn.execute("UPDATE jobs SET source_trust = ?, details_status = 'not_found', details_attempts = 2"
                         " WHERE id = ?", (trust, job_id))
        conn.close()
    return db.get_job(job_id, db_path=db_path)


def test_slug_guesses():
    assert aa.slug_guesses("Sitewire by Hexacorp") == ["sitewirebyhexacorp", "sitewire", "sitewire-by-hexacorp"]
    assert aa.slug_guesses("Paxmora, Inc.") == ["paxmora", "paxmorainc"]
    assert aa.slug_guesses("odoo") == ["odoo"]


def test_greenhouse_hit_matches_title_and_location_and_caches(db_path):
    job = add(db_path)
    r = aa.probe_job(job, session=FakeSession(gh_routes()), db_path=db_path)
    assert r.status == "hit" and (r.board.ats, r.board.slug) == ("greenhouse", "paxmora")
    assert r.match["ratio"] == 1.0 and r.match["url"] == "https://job-boards.greenhouse.io/paxmora/jobs/1"
    assert r.match["description"].startswith("Build search for shoppers.") and "- React" in r.match["description"]
    assert db.get_ats_probe("paxmora", db_path=db_path)["result"] == "hit"
    assert db.get_company_board("paxmora", db_path=db_path)["board_url"] == "https://job-boards.greenhouse.io/paxmora"
    # Next time the cached board is used: no guessing.
    s = FakeSession(gh_routes())
    assert aa.probe_job(job, session=s, db_path=db_path).status == "cached-hit"
    assert s.urls[0] == f"{GH}/paxmora/jobs"


def test_a_board_of_another_company_does_not_count(db_path):
    job = add(db_path)
    r = aa.probe_job(job, session=FakeSession(gh_routes(name="Paxmorative Labs")), db_path=db_path)
    assert r.status == "miss" and r.board is None
    assert any("not this company" in t for t in r.tried)


def test_empty_board_does_not_count(db_path):
    r = aa.probe_job(add(db_path), session=FakeSession(gh_routes(jobs=[])), db_path=db_path)
    assert r.status == "miss"


def test_miss_is_cached_and_retried_after_30_days(db_path):
    job = add(db_path, company="odoo")
    s = FakeSession({})
    r = aa.probe_job(job, session=s, db_path=db_path)
    assert r.status == "miss" and r.tried == ["greenhouse/odoo: none", "lever/odoo: none", "ashby/odoo: none"]
    s2 = FakeSession({})
    assert aa.probe_job(job, session=s2, db_path=db_path).status == "cached-miss" and s2.urls == []
    later = datetime.now(timezone.utc) + timedelta(days=31)
    assert aa.probe_job(job, session=s2, db_path=db_path, now=later).status == "miss" and s2.urls
    assert aa.probe_job(job, session=FakeSession({}), db_path=db_path, refresh=True).status == "miss"


def test_similar_title_needs_085_and_a_compatible_location(db_path):
    routes = gh_routes(jobs=[
        {"id": 1, "title": "Software Engineer I, Fullstack (Billing)", "location": {"name": "New York, NY"}},
        {"id": 3, "title": "Software Engineer II, Backend", "location": {"name": "San Francisco, CA"}},
    ])
    r = aa.probe_job(add(db_path), session=FakeSession(routes), db_path=db_path)
    assert r.match is None and r.best["ratio"] == 1.0  # right title, wrong city
    assert aa.location_ok("United States", "New York, NY", False)
    assert aa.location_ok("San Francisco, CA", "Remote US", False)


def test_lever_and_ashby_need_the_company_named_in_postings(db_path):
    other = {"jobs": [{"title": "Software Engineer I, Fullstack (Billing)", "location": "San Francisco",
                       "jobUrl": "https://jobs.ashbyhq.com/paxmora/9", "descriptionPlain": "We extend our platform.",
                       "descriptionHtml": "<p>We extend our platform.</p>", "isListed": True}]}
    r = aa.probe_job(add(db_path), session=FakeSession({"https://api.ashbyhq.com/posting-api/job-board/paxmora": other}),
                     db_path=db_path)
    assert r.status == "miss" and "ashby/paxmora: 1 jobs, company not named in its postings: not this company" in r.tried
    mine = {"jobs": [{**other["jobs"][0], "descriptionHtml": "<p>At Paxmora we build billing.</p>" * 20}]}
    r = aa.probe_job(add(db_path, ext_id="2"), session=FakeSession({"https://api.ashbyhq.com/posting-api/job-board/paxmora": mine}),
                     db_path=db_path, refresh=True)
    assert r.status == "hit" and r.match["url"] == "https://jobs.ashbyhq.com/paxmora/9"


def test_store_match_makes_the_job_official(db_path):
    job = add(db_path)
    r = aa.probe_job(job, session=FakeSession(gh_routes()), db_path=db_path)
    conn = db.connect(db_path)
    with conn:
        aa.store_match(job, r, conn=conn)
    conn.close()
    after = db.get_job(job["id"], db_path=db_path)
    assert (after["source_trust"], after["needs_details"]) == ("official", 0)
    assert after["source_url"] == "https://job-boards.greenhouse.io/paxmora/jobs/1"


def test_details_upgrades_a_not_found_job_for_free(db_path):
    job = add(db_path, trust="not_found")

    def never(prompt_file, user_prompt):
        raise AssertionError("no Claude call")

    result = dt.fetch_details(db_path=db_path, runner=never, sleep=lambda s: None, session=FakeSession(gh_routes()))
    after = db.get_job(job["id"], db_path=db_path)
    assert (result.rechecked, result.free_found) == (1, 1)
    assert (after["source_trust"], after["details_status"]) == ("official", "found")


@pytest.mark.parametrize("job_loc, post_loc, remote, score", [
    ("San Francisco, CA", "Remote Canada", False, 0),
    ("San Francisco, CA", "Remote US", False, 1),
    ("San Francisco, CA", "San Francisco, California, United States", False, 2),
    ("San Francisco, CA", "New York, NY", False, 0),
    ("United States", "United States (Remote)", False, 1),
    ("United States", "Remote UK", False, 0),
    ("Houston, TX", "Houston, TX; Austin, TX", False, 2),
])
def test_location_score(job_loc, post_loc, remote, score):
    assert aa.location_score(job_loc, post_loc, remote) == score


def test_same_title_in_two_countries_picks_the_us_one(db_path):
    routes = gh_routes(jobs=[
        {"id": 1, "title": "Software Engineer I, Fullstack (Billing)", "location": {"name": "Remote Canada"}},
        {"id": 2, "title": "Software Engineer I, Fullstack (Billing)", "location": {"name": "Remote US"}},
    ])
    routes[f"{GH}/paxmora/jobs/2"] = {"content": CONTENT}
    r = aa.probe_job(add(db_path), session=FakeSession(routes), db_path=db_path)
    assert r.match["id"] == 2
