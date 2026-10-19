from datetime import date, datetime, timedelta, timezone

import pytest

from agents import db, digest, feedback
from agents.scout import waiting

PROFILE = {"shortlist_min_score": 65, "waiting_expiry_days": 10}


@pytest.fixture
def db_path(tmp_path):
    path = tmp_path / "jobs.db"
    db.init_db(path)
    return path


def add(db_path, ext, status="shortlisted", days_ago=11, company="Acme"):
    job_id, _ = db.upsert_job({"source": "email:linkedin", "external_id": ext, "company": company,
                               "title": "Engineer"}, db_path=db_path)
    db.set_status(job_id, "scored", "scout", db_path=db_path)
    db.set_score(job_id, 70, {"pass": 2}, db_path=db_path)
    db.set_status(job_id, "shortlisted", "scout", db_path=db_path)
    if status in ("researched", "applied"):
        db.set_status(job_id, "researched", "research", db_path=db_path)
    if status == "applied":
        db.mark_applied(job_id, "ats", db_path=db_path)
    at = (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat(timespec="seconds")
    conn = db.connect(db_path)
    with conn:  # everything so far happened days_ago
        conn.execute("UPDATE events SET at = ? WHERE job_id = ?", (at, job_id))
    conn.close()
    return job_id


def status(db_path, job_id):
    return db.get_job(job_id, db_path=db_path)["status"]


def test_closes_waiting_jobs_with_no_action_for_10_days(db_path):
    stale_s = add(db_path, "1")
    stale_r = add(db_path, "2", "researched")
    fresh = add(db_path, "3", days_ago=9)
    closed = waiting.expire(PROFILE, db_path=db_path)
    assert sorted(j["id"] for j in closed) == sorted([stale_s, stale_r])
    assert status(db_path, stale_s) == status(db_path, stale_r) == "closed" and status(db_path, fresh) == "shortlisted"
    event = db.get_job_events(stale_s, db_path=db_path)[-1]
    assert (event["actor"], event["from_status"], event["detail"]) == ("scout", "shortlisted", "stale: no action 10 days")


@pytest.mark.parametrize("act", [
    lambda p, j: db.set_user_rating(j, 1, db_path=p),
    lambda p, j: (db.set_hold(j, "later", "2000-01-02", db_path=p)),  # a hold that has ended still counts
    lambda p, j: db.log_event(j, "posting_set", "user", detail="https://x.example/1", db_path=p),
])
def test_any_action_since_shortlisting_keeps_it(db_path, act):
    job_id = add(db_path, "1")
    act(db_path, job_id)
    assert waiting.expire(PROFILE, db_path=db_path) == []
    assert status(db_path, job_id) == "shortlisted"


def test_held_applied_and_interviewing_jobs_are_never_touched(db_path):
    held = add(db_path, "1")
    conn = db.connect(db_path)
    with conn:  # held, with no user event (e.g. set long ago): still never touched
        conn.execute("UPDATE jobs SET hold_reason = 'later', held_until = NULL WHERE id = ?", (held,))
    conn.close()
    applied = add(db_path, "2", "applied")
    interviewing = add(db_path, "3", "applied")
    db.set_status(interviewing, "interviewing", "tracker", db_path=db_path)
    conn = db.connect(db_path)
    with conn:
        conn.execute("UPDATE events SET at = '2000-01-01T00:00:00+00:00' WHERE job_id IN (?, ?)", (applied, interviewing))
    conn.close()
    assert waiting.expire(PROFILE, db_path=db_path) == []
    assert [status(db_path, j) for j in (held, applied, interviewing)] == ["shortlisted", "applied", "interviewing"]


def test_expiry_days_come_from_the_profile(db_path):
    job_id = add(db_path, "1", days_ago=6)
    assert waiting.expire(PROFILE, db_path=db_path) == []
    assert [j["id"] for j in waiting.expire({"waiting_expiry_days": 5}, db_path=db_path)] == [job_id]
    assert db.get_job_events(job_id, db_path=db_path)[-1]["detail"] == "stale: no action 5 days"
    assert waiting.expiry_days({}) == waiting.DEFAULT_EXPIRY_DAYS == 10


def test_dry_run_closes_nothing(db_path):
    job_id = add(db_path, "1")
    assert [j["id"] for j in waiting.expire(PROFILE, db_path=db_path, dry_run=True)] == [job_id]
    assert status(db_path, job_id) == "shortlisted"


def test_digest_lists_cleaned_up_today(db_path):
    job_id = add(db_path, "1", company="Stale Co")
    text = digest.build(db_path=db_path)
    assert "Nothing went stale today, and no posting was too old." in text.split("## Cleaned up today")[1].split("##")[0]
    waiting.expire(PROFILE, db_path=db_path)
    section = digest.build(db_path=db_path).split("## Cleaned up today")[1].split("##")[0]
    assert f"#{job_id} **Stale Co**: Engineer (70.0), stale: no action 10 days" in section
    assert 'reopen <id> "reason"' in section
    tomorrow = digest.build(date.today() + timedelta(days=1), db_path=db_path)
    assert "Stale Co" not in tomorrow.split("## Cleaned up today")[1].split("##")[0]


def test_reopen_takes_a_stale_close_back(db_path, capsys):
    job_id = add(db_path, "1")
    waiting.expire(PROFILE, db_path=db_path)
    assert feedback.main(["--db", str(db_path), "reopen", str(job_id), "still interested"]) == 0
    assert status(db_path, job_id) == "scored" and db.get_job(job_id, db_path=db_path)["score"] == 70
    event = db.get_job_events(job_id, db_path=db_path)[-1]
    assert (event["actor"], event["from_status"], event["detail"]) == ("user", "closed", "reopened: still interested")
    assert waiting.cleaned_up(db_path=db_path) == []  # no longer closed
