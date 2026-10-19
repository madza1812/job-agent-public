import json
from datetime import date, timedelta

import pytest

from agents import db, digest
from agents.common.claude import Usage, UsageTotals

TODAY = date.today()


@pytest.fixture
def db_path(tmp_path):
    path = tmp_path / "jobs.db"
    db.init_db(path)
    return path


def add(db_path, ext_id, score, status="scored", company="Acme", **extra):
    job_id, _ = db.upsert_job({"source": "greenhouse", "external_id": ext_id, "company": company,
                               "title": f"Engineer {ext_id}", "location": "San Jose, CA", **extra}, db_path=db_path)
    db.set_status(job_id, "scored", "scout", db_path=db_path)
    detail = {"pass": 2, "why": "Your ERP work fits.", "gaps": ["Go"], "red_flags": [], "reason": "fit",
              "components": {"skills_match": {"value": 90}, "location": {"value": 100}},
              "adjustments": [{"sponsored": -5}], "dealbreaker": None}
    db.set_score(job_id, score, detail, db_path=db_path)
    if status == "shortlisted":
        db.set_status(job_id, "shortlisted", "scout", db_path=db_path)
    return job_id


def backdate_shortlist(db_path, job_id, days):
    when = (TODAY - timedelta(days=days)).isoformat() + "T12:00:00+00:00"
    conn = db.connect(db_path)
    with conn:
        conn.execute("UPDATE events SET at = ? WHERE job_id = ? AND to_status = 'shortlisted'", (when, job_id))
    conn.close()


def sections(text):
    return [line for line in text.splitlines() if line.startswith("## ")]


def test_sections_in_order(db_path):
    text = digest.build(TODAY, db_path=db_path)
    assert sections(text) == ["## Warnings", "## Application updates", "## Today's top 3", "## Worth checking yourself", "## Still waiting",
                              "## Cleaned up today", "## Runners-up 4-10", "## Run stats", "## Commands"]


def test_top_entry_has_everything(db_path):
    a = add(db_path, "1", 80, status="shortlisted", salary_min=90000, salary_max=120000,
            url="https://www.linkedin.com/jobs/view/1/")
    conn = db.connect(db_path)
    with conn:
        conn.execute("UPDATE jobs SET apply_url = 'https://jobs.acme.com/1/apply' WHERE id = ?", (a,))
    conn.close()
    text = digest.build(TODAY, db_path=db_path)
    top = text.split("## Today's top 3")[1].split("## Still waiting")[0]
    assert "Acme: Engineer 1 (80.0)" in top and "San Jose, CA" in top and "$90,000–$120,000" in top
    assert "Your ERP work fits." in top and "**Gaps:** Go" in top
    assert "https://jobs.acme.com/1/apply" in top and "skills 90" in top and "sponsored -5" in top


def test_missing_brief_shows_the_research_command(db_path):
    a = add(db_path, "1", 80, status="shortlisted")
    assert f"(`.\\run.ps1 research {a}`)" in digest.build(TODAY, db_path=db_path)


def test_alert_link_fallback_is_labeled(db_path):
    add(db_path, "1", 80, status="shortlisted", url="https://www.linkedin.com/jobs/view/1/")
    assert "(alert link; official posting not found)" in digest.build(TODAY, db_path=db_path)


def test_still_waiting_shows_days(db_path):
    a = add(db_path, "1", 80, status="shortlisted")
    backdate_shortlist(db_path, a, 3)
    waiting = digest.build(TODAY, db_path=db_path).split("## Still waiting")[1].split("## Runners-up")[0]
    assert f"#{a}" in waiting and "waiting 3 days" in waiting


def test_applied_or_closed_jobs_are_not_waiting(db_path):
    a = add(db_path, "1", 80, status="shortlisted")
    backdate_shortlist(db_path, a, 2)
    db.set_status(a, "closed", "user", "skipped: no", db_path=db_path)
    waiting = digest.build(TODAY, db_path=db_path).split("## Still waiting")[1].split("## Runners-up")[0]
    assert f"#{a}" not in waiting


def test_runners_up_are_ranked_from_4(db_path):
    for i in range(9):
        add(db_path, str(i), 50 + i)
    runners = digest.build(TODAY, db_path=db_path).split("## Runners-up 4-10")[1].split("## Run stats")[0]
    lines = [line for line in runners.splitlines() if line[:1].isdigit()]
    assert len(lines) == 7 and lines[0].startswith("4. ") and "58.0" in lines[0]


def test_warnings_include_notices_and_run_warnings(db_path):
    db.raise_notice("gmail_auth_expired", "Gmail login expired.", db_path=db_path)
    text = digest.build(TODAY, warnings=["details failed: boom"], db_path=db_path)
    warn = text.split("## Warnings")[1].split("## Today's")[0]
    assert "Gmail login expired." in warn and "details failed: boom" in warn


def test_run_stats(db_path):
    usage = UsageTotals()
    usage.add([Usage(model="claude-sonnet-5-5", input_tokens=10, output_tokens=200, cost_usd=0.05),
               Usage(model="claude-haiku-5-5", input_tokens=900, web_searches=4, cost_usd=0.02)])
    stats = digest.RunStats(emails_read=5, jobs_found=40, filtered=12, scored=20, usage=usage,
                            steps=[("fetch", "ok"), ("details", "failed")])
    text = digest.build(TODAY, stats=stats, db_path=db_path)
    assert "Emails read: 5" in text and "Jobs found: 40" in text and "Filtered out: 12" in text
    assert "Scored (pass 1): 20" in text and "4 web searches" in text and "details failed" in text


def test_write_uses_date_name(tmp_path):
    path = digest.write("# hi\n", date(2026, 10, 16), tmp_path)
    assert path.name == "2026-10-16.md" and path.read_text(encoding="utf-8") == "# hi\n"


def test_pass1_only_job_says_gaps_not_checked(db_path):
    a = add(db_path, "1", 80, status="shortlisted")
    conn = db.connect(db_path)
    with conn:
        conn.execute("UPDATE jobs SET score_detail = ? WHERE id = ?", (json.dumps({"pass": 1, "reason": "ok"}), a))
    conn.close()
    assert "not checked: no full posting was found" in digest.build(TODAY, db_path=db_path)


def set_trust(db_path, job_id, trust, **cols):
    conn = db.connect(db_path)
    with conn:
        conn.execute("UPDATE jobs SET source_trust = ? WHERE id = ?", (trust, job_id))
        for k, v in cols.items():
            conn.execute(f"UPDATE jobs SET {k} = ? WHERE id = ?", (v, job_id))  # test-only column names
    conn.close()


def test_aggregator_link_is_labeled(db_path):
    a = add(db_path, "1", 80, status="shortlisted")
    set_trust(db_path, a, "aggregator", source_url="https://jobright.ai/jobs/info/x")
    assert "https://jobright.ai/jobs/info/x (aggregator, verify before applying)" in digest.build(TODAY, db_path=db_path)


def test_official_link_has_no_label(db_path):
    a = add(db_path, "1", 80, status="shortlisted")
    set_trust(db_path, a, "official", apply_url="https://boards.greenhouse.io/acme/jobs/1")
    text = digest.build(TODAY, db_path=db_path)
    assert "- **Apply:** https://boards.greenhouse.io/acme/jobs/1" in text.splitlines()


def test_worth_checking_shows_unverified_jobs_with_alert_link_and_not_in_runners_up(db_path):
    picked = add(db_path, "1", 70, status="shortlisted")
    unverified = add(db_path, "2", 85, url="https://www.indeed.com/viewjob?jk=abc")
    set_trust(db_path, unverified, "not_found")
    text = digest.build(TODAY, db_path=db_path)
    worth = text.split("## Worth checking yourself")[1].split("## Still waiting")[0]
    assert f"#{unverified}" in worth and "https://www.indeed.com/viewjob?jk=abc" in worth
    assert "posting not found" in worth and f"#{picked}" not in worth
    assert f"#{unverified} " not in text.split("## Runners-up")[1]


def test_top_entry_links_its_brief(db_path):
    a = add(db_path, "1", 80, status="shortlisted")
    b = add(db_path, "2", 75, status="shortlisted")
    db.set_brief_path(a, "briefs/2026-10-16-acme-engineer-1.md", db_path=db_path)
    top = digest.build(TODAY, db_path=db_path).split("## Today's top 3")[1].split("## Worth checking")[0]
    assert "**Brief:** [Read brief](../briefs/2026-10-16-acme-engineer-1.md)" in top
    assert f"not written yet; research retries it next run (`.\\run.ps1 research {b}`)" in top


def test_possibly_closed_job_leaves_top_3_and_is_worth_checking(db_path):
    a = add(db_path, "1", 80, status="shortlisted", url="https://www.linkedin.com/jobs/view/1/")
    b = add(db_path, "2", 70, status="shortlisted")
    conn = db.connect(db_path)
    with conn:
        conn.execute("UPDATE jobs SET source_trust = 'aggregator', source_url = 'https://jobright.ai/x' WHERE id = ?", (a,))
    conn.close()
    detail = json.loads(db.get_job(a, db_path=db_path)["score_detail"])
    db.set_score(a, 80, {**detail, "possibly_closed": True}, db_path=db_path)
    db.set_status(a, "scored", "scout", "possibly closed", db_path=db_path)
    text = digest.build(TODAY, db_path=db_path)
    top = text.split("## Today's top 3")[1].split("## Worth checking")[0]
    worth = text.split("## Worth checking yourself")[1].split("## Still waiting")[0]
    assert "Engineer 1" not in top and "Engineer 2" in top and "**Source:** official" in top
    assert (f"#{a} 80.0 **Acme**: Engineer 1 (San Jose, CA), possibly closed (moved back from the shortlist); "
            "source aggregator, posting: https://jobright.ai/x (aggregator, verify before applying). "
            "Date unknown. Alert link: https://www.linkedin.com/jobs/view/1/") in worth


def test_aggregator_only_job_is_labeled_in_worth_checking(db_path):
    a = add(db_path, "1", 90, url="https://www.linkedin.com/jobs/view/1/")
    conn = db.connect(db_path)
    with conn:
        conn.execute("UPDATE jobs SET source_trust = 'aggregator', source_url = 'https://builtin.com/x' WHERE id = ?", (a,))
    conn.close()
    worth = digest.build(TODAY, db_path=db_path).split("## Worth checking yourself")[1].split("## Still waiting")[0]
    assert ("found only on aggregator — run set-posting if you find the official page; "
            "aggregator posting: https://builtin.com/x. Date unknown. Alert link: https://www.linkedin.com/jobs/view/1/") in worth


def test_recruiter_pools_get_their_own_section_one_line_each(db_path):
    pool = add(db_path, "1", 95)
    db.set_score(pool, 95, {"pass": 2, "posting_type": "talent_pool", "components": {}}, db_path=db_path)
    conn = db.connect(db_path)
    with conn:
        conn.execute("UPDATE jobs SET source_url = 'https://acme.breezy.hr/p/1-future' WHERE id = ?", (pool,))
    conn.close()
    other = add(db_path, "2", 70)
    text = digest.build(TODAY, db_path=db_path)
    section = text.split("## Recruiter pools (optional, low effort)\n\n")[1].split("\n\n")[0]
    assert section == f"- #{pool} **Acme**: Engineer 1, https://acme.breezy.hr/p/1-future"
    runners = text.split("## Runners-up")[1]
    assert f"#{pool} " not in runners and f"#{other} " in runners


def test_no_recruiter_pool_section_without_pools(db_path):
    add(db_path, "1", 70)
    assert "Recruiter pools" not in digest.build(TODAY, db_path=db_path)
