import pytest

from agents import db
from agents.scout import retrust as rt

ORACLE = "https://example.fa.us2.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_1/job/1000"


@pytest.fixture
def db_path(tmp_path):
    path = tmp_path / "jobs.db"
    db.init_db(path)
    return path


def add(db_path, ext_id, company, trust, source_url, apply_url=None, status="scored"):
    job_id, _ = db.upsert_job({"source": "email:glassdoor", "external_id": ext_id, "company": company,
                               "title": "Engineer", "url": f"https://www.glassdoor.com/job-listing/j?jl={ext_id}",
                               "needs_details": 1}, db_path=db_path)
    db.set_status(job_id, "scored", "scout", db_path=db_path)
    db.set_score(job_id, 70, {"final": 70, "pass": 2}, db_path=db_path)
    db.store_details(job_id, description="text", apply_url=apply_url, source_url=source_url,
                     years_required=None, source_trust=trust, db_path=db_path)
    if status == "closed":
        db.set_status(job_id, "closed", "user", "skipped: test", db_path=db_path)
    return job_id


def test_official_apply_url_upgrades_an_aggregator_job(db_path):
    # A job before set-posting: text from anitab.org, apply link on hooli.com.
    a = add(db_path, "1", "Hooli", "aggregator", "https://jobs.anitab.org/companies/hooli/jobs/1",
            "https://www.hooli.com/about/careers/applications/jobs/results/1")
    result = rt.retrust(db_path=db_path)
    job = db.get_job(a, db_path=db_path)
    assert [ch.job_id for ch in result.changes] == [a]
    assert job["source_trust"] == "official"
    assert job["apply_url"] == "https://www.hooli.com/about/careers/applications/jobs/results/1"
    conn = db.connect(db_path)
    ev = conn.execute("SELECT actor, detail FROM events WHERE job_id = ? AND event = 'trust_changed'", (a,)).fetchone()
    assert ev[0] == "scout" and ev[1].startswith("aggregator -> official")


def test_oracle_source_upgrades_and_becomes_the_apply_link(db_path):
    a = add(db_path, "1", "Initech", "aggregator", ORACLE)
    rt.retrust(db_path=db_path)
    job = db.get_job(a, db_path=db_path)
    assert (job["source_trust"], job["apply_url"]) == ("official", ORACLE)


def test_aggregator_apply_link_is_replaced_by_the_official_source(db_path):
    a = add(db_path, "1", "Acme", "official", "https://boards.greenhouse.io/acme/jobs/1",
            "https://www.indeed.com/viewjob?jk=abc")
    result = rt.retrust(db_path=db_path)
    assert result.changes[0].old_trust == result.changes[0].new_trust == "official"
    assert db.get_job(a, db_path=db_path)["apply_url"] == "https://boards.greenhouse.io/acme/jobs/1"


def test_unchanged_closed_and_user_set_jobs_are_left_alone(db_path):
    same = add(db_path, "1", "Acme", "official", "https://boards.greenhouse.io/acme/jobs/1",
               "https://boards.greenhouse.io/acme/jobs/1#app")
    closed = add(db_path, "2", "Initech", "aggregator", ORACLE, status="closed")
    user = add(db_path, "3", "Acme", "official", "https://jobright.ai/jobs/info/1")
    db.log_event(user, "posting_set", "user", detail="https://jobright.ai/jobs/info/1", db_path=db_path)
    result = rt.retrust(db_path=db_path)
    assert result.changes == []
    assert db.get_job(closed, db_path=db_path)["source_trust"] == "aggregator"
    assert db.get_job(user, db_path=db_path)["source_trust"] == "official"
    assert db.get_job(same, db_path=db_path)["apply_url"].endswith("#app")


def test_official_job_with_no_official_link_drops_to_aggregator(db_path):
    a = add(db_path, "1", "Acme", "official", "https://jobright.ai/jobs/info/1")
    rt.retrust(db_path=db_path)
    assert db.get_job(a, db_path=db_path)["source_trust"] == "aggregator"


def test_dry_run_writes_nothing(db_path):
    a = add(db_path, "1", "Initech", "aggregator", ORACLE)
    result = rt.retrust(db_path=db_path, dry_run=True)
    assert len(result.changes) == 1 and "would change" in result.lines()[0]
    assert db.get_job(a, db_path=db_path)["source_trust"] == "aggregator"
