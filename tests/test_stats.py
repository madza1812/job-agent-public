import json
from datetime import datetime, timedelta, timezone

import pytest

from agents import db, feedback, stats

NOW = datetime(2026, 10, 17, 18, 0, tzinfo=timezone.utc)
OLD = (NOW - timedelta(days=10)).isoformat(timespec="seconds")


@pytest.fixture
def db_path(tmp_path, monkeypatch):
    path = tmp_path / "jobs.db"
    db.init_db(path)
    monkeypatch.setattr(db, "_now", lambda: (NOW - timedelta(hours=2)).isoformat(timespec="seconds"))
    return path


def add(db_path, ext, score, skills, role, *, source="email:linkedin", company="Acme"):
    job_id, _ = db.upsert_job({"source": source, "external_id": ext, "company": company, "title": f"Engineer {ext}"},
                              db_path=db_path)
    db.set_status(job_id, "scored", "scout", db_path=db_path)
    db.set_score(job_id, score, {"final": score, "pass": 1, "components": {"skills_match": {"value": skills},
                                                                          "role_interest": {"value": role}}},
                 db_path=db_path)
    return job_id


def backdate(db_path, sql, *params):
    conn = db.connect(db_path)
    with conn:
        conn.execute(sql, params)
    conn.close()


def test_emails_per_sender_with_unparsed(db_path):
    db.record_processed_email("m1", "email:linkedin", 5, sender="jobalerts-noreply@linkedin.com", db_path=db_path)
    db.record_processed_email("m2", "email:linkedin", 3, sender="jobalerts-noreply@linkedin.com", db_path=db_path)
    db.record_processed_email("m3", "email:indeed", 0, sender="donotreply@jobalert.indeed.com", db_path=db_path)
    db.record_processed_email("old", "email:indeed", 9, sender="donotreply@jobalert.indeed.com", db_path=db_path)
    backdate(db_path, "UPDATE processed_emails SET processed_at = ? WHERE message_id = 'old'", OLD)
    db.record_unparsed_email("u1", "noreply@glassdoor.com", db_path=db_path)
    db.record_unparsed_email("m2", "jobalerts-noreply@linkedin.com", db_path=db_path)  # failed once, parsed later
    rows = {e["sender"]: e for e in stats.collect(7, db_path=db_path, now=NOW)["emails"]}
    assert rows["jobalerts-noreply@linkedin.com"] == {"sender": "jobalerts-noreply@linkedin.com", "read": 2,
                                                      "jobs_found": 8, "skipped": 0, "unparsed": 1,
                                                      "still_unparsed": 0}
    assert rows["donotreply@jobalert.indeed.com"]["read"] == 1  # the 10-day-old one is outside the window
    assert rows["noreply@glassdoor.com"] == {"sender": "noreply@glassdoor.com", "read": 1, "jobs_found": 0,
                                             "skipped": 0, "unparsed": 1, "still_unparsed": 1}


def test_jobs_found_filtered_by_reason_and_scored(db_path):
    add(db_path, "1", 70, 80, 60)
    add(db_path, "2", 50, 40, 30, source="greenhouse")
    db.record_filtered_posting("email:linkedin:9", "title contains excluded word: 'senior'", "X", "Senior", db_path=db_path)
    db.record_filtered_posting("email:linkedin:8", "title contains excluded word: 'senior'", "Y", "Sr", db_path=db_path)
    db.record_filtered_posting("email:indeed:7", "location outside target states", "Z", "Dev", db_path=db_path)
    db.record_filtered_posting("email:indeed:7", "location outside target states", "Z", "Dev", db_path=db_path)  # once
    j = stats.collect(7, db_path=db_path, now=NOW)["jobs"]
    assert j["found"] == 2 and j["found_by_source"] == {"email:linkedin": 1, "greenhouse": 1}
    assert j["filtered_at_fetch"] == 3 and j["scored"] == 2
    assert j["filtered_by_reason"]["title contains excluded word"] == {
        "count": 2, "reasons": {"title contains excluded word: 'senior'": 2}}
    assert j["filtered_by_reason"]["location outside target states"]["count"] == 1


def test_scores_pipeline_and_skip_reasons(db_path):
    a = add(db_path, "1", 80, 90, 70, company="Contoso")
    b = add(db_path, "2", 60, 50, 40)
    add(db_path, "3", 40, 30, 20)
    db.set_status(a, "shortlisted", "scout", db_path=db_path)
    db.set_status(a, "researched", "research", db_path=db_path)
    db.mark_applied(a, "ats", db_path=db_path)
    feedback.skip(b, "recruiter talent network", db_path=db_path)
    data = stats.collect(7, db_path=db_path, now=NOW)
    assert data["scores"]["count"] == 3 and data["scores"]["average"] == 60.0
    assert [t["id"] for t in data["scores"]["top"]] == [a, b, 3]
    p = data["pipeline"]
    assert [r["id"] for r in p["shortlisted"]] == [a] and [r["id"] for r in p["applied"]] == [a]
    assert [(r["id"], r["reason"]) for r in p["skipped"]] == [(b, "recruiter talent network")]


def test_ratings_counts_and_subscore_averages(db_path):
    a = add(db_path, "1", 80, 90, 80)
    b = add(db_path, "2", 70, 70, 60)
    c = add(db_path, "3", 50, 60, 10)
    for job_id, rating in ((a, 1), (b, 1), (c, -1)):
        db.set_user_rating(job_id, rating, db_path=db_path)
    r = stats.collect(7, db_path=db_path, now=NOW)["ratings"]
    assert (r["up"], r["down"]) == (2, 1)
    assert r["all_rated"]["liked"]["skills_match"] == 80.0 and r["all_rated"]["liked"]["role_interest"] == 70.0
    assert r["all_rated"]["disliked"]["role_interest"] == 10.0 and r["all_rated"]["liked"]["final"] == 75.0
    assert r["all_rated"]["liked"]["location"] is None  # not in these score details
    text = stats.render(stats.collect(7, db_path=db_path, now=NOW))
    assert "| role_interest | 70.0 | 10.0 | +60.0 |" in text


def test_usage_per_day_from_logs_counts_each_line_once(tmp_path):
    logs = tmp_path / "logs"
    logs.mkdir()
    line1 = ("2026-10-16 07:00:01,100 INFO agents.common.claude: usage score batch=1/2 jobs=10 "
             "model=claude-sonnet-5-5 input=3 cache_creation=100 cache_read=200 output=50 cost_usd=0.0200 "
             "turns=1 duration_s=9.0")
    line2 = ("2026-10-16 07:05:00,000 INFO agents.common.claude: usage details job=4 model=claude-haiku-5-5 "
             "input=900 cache_creation=0 cache_read=0 output=40 web_searches=3 cost_usd=0.0100 turns=5 duration_s=20")
    old = line1.replace("2026-10-16", "2026-10-01")
    other = "2026-10-16 07:00:02,000 INFO agents.scout.score:   usage total: input=3 cost_usd=0.0200"
    (logs / "score-2026-10-16.log").write_text("\n".join([line1, other, line2, old]), encoding="utf-8")
    (logs / "daily-2026-10-16.log").write_text(line1 + "\n", encoding="utf-8")  # the same line, nested logs
    rows = stats.usage_by_day(logs, NOW - timedelta(days=7))
    assert [(r["day"], r["model"], r["calls"], r["cost_usd"]) for r in rows] == [
        ("2026-10-16", "claude-haiku-5-5", 1, 0.01), ("2026-10-16", "claude-sonnet-5-5", 1, 0.02)]
    assert rows[0]["web_searches"] == 3 and rows[1]["cache_read"] == 200


def test_render_and_json_cli(db_path, tmp_path, capsys):
    add(db_path, "1", 70, 80, 60)
    text = stats.render(stats.collect(7, db_path=db_path, log_dir=tmp_path, now=NOW))
    for heading in ("## Alert emails", "## Jobs", "## Scores", "## Pipeline", "## Your ratings",
                    "## Claude usage per day"):
        assert heading in text
    assert stats.main(["--days", "7", "--json", "--db", str(db_path), "--log-dir", str(tmp_path)]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["days"] == 7 and set(data) >= {"emails", "jobs", "scores", "pipeline", "ratings", "usage"}
